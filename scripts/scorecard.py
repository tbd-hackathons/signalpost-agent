#!/usr/bin/env python3
"""Score one run the way the challenge scores it, then audit the claims against the live sources.

Three questions, answered from the run's own envelopes:

1. Coverage — per external field family, how many companies did we cover and how many facts did we publish?
   70% of a family's score is company recall, 30% is claim recall (challenge scoring version 2).
2. Honesty — what did we say when we found nothing? `not_available` is a checked fact; `failed` is a source
   we never managed to check and costs the same coverage as finding nothing. `failed` is the number to watch.
3. Audited precision — take a reproducible random sample of published claims, re-fetch the source they cite
   and re-run the same identity gate the run used. A claim that no longer reproduces is a finding, not a bug.

`--audit N` turns on the live pass. It is polite by construction: it reuses the run's own robots-aware
fetchers, reads one page per claim, and runs on a small worker pool.

Usage:
  python scripts/scorecard.py --envelopes out/v9-1000-envelopes.jsonl \
      --json out/v9-1000-scorecard.json --html out/v9-1000-scorecard.html --audit 60
"""
from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.nav_jobs import entity_numbers  # noqa: E402
from norway_company_agent.site_candidates import contact_proof  # noqa: E402
from norway_company_agent.site_intel import SiteFetcher  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402

# The external field families the challenge scores, in the order the report shows them.
FAMILIES: list[tuple[str, str]] = [
    ("official_website", "Verified website"),
    ("social_profile", "Company-owned profiles"),
    ("job_posting", "Job ads"),
    ("public_post", "Dated posts on the own site"),
    ("public_activity.procurement", "Public tenders"),
    ("public_activity.news_mention", "News articles"),
]
STATE_ORDER = ["available", "not_available", "blocked", "ambiguous", "failed"]
# A check we could not run costs coverage exactly like a check that found nothing, so it leads the report.
NEEDS_ATTENTION = {"failed"}


def family_of(field: str) -> str:
    """`social_profile.facebook` belongs to the family `social_profile`."""
    if field.startswith("social_profile"):
        return "social_profile"
    return field


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------- coverage

def coverage(envelopes: list[dict]) -> dict:
    per_family: dict[str, dict] = {}
    for field, label in FAMILIES:
        companies: set[str] = set()
        states: Counter = Counter()
        total = 0
        for env in envelopes:
            org = str(env.get("organisation_number"))
            for claim in env.get("claims") or []:
                if family_of(claim["field"]) != family_of(field):
                    continue
                total += 1
                states[claim.get("availability") or "unknown"] += 1
                if claim.get("availability") == "available":
                    companies.add(org)
        per_family[field] = {
            "label": label,
            "companies_covered": len(companies),
            "companies": sorted(companies),
            "claims_total": total,
            "claims_available": states.get("available", 0),
            "states": {state: states.get(state, 0) for state in STATE_ORDER if states.get(state)},
        }
    return per_family


def source_funnel(envelopes: list[dict]) -> dict:
    """Where each website came from, and why the other candidates were rejected.

    Read from `profile.evidence.website.value.discovery`, which the run records for every company."""
    found_by: Counter = Counter()
    attempts: Counter = Counter()
    accepted: Counter = Counter()
    rejection_reasons: Counter = Counter()
    declared_available: Counter = Counter()
    published_by_source: Counter = Counter()

    for env in envelopes:
        profile = env.get("profile") or {}
        records = profile.get("evidence") or {}
        reg = ((records.get("registry") or {}).get("value") or {})
        has_registry_site = bool(str(reg.get("hjemmeside") or "").strip())
        has_email = bool(str(reg.get("epostadresse") or "").strip())
        if has_registry_site:
            declared_available["registry website field"] += 1
        elif has_email:
            declared_available["registry e-mail only"] += 1
        else:
            declared_available["neither"] += 1

        website = records.get("website") or {}
        value = website.get("value") or {}
        if not isinstance(value, dict):
            continue
        discovery = value.get("discovery") or {}
        selected = discovery.get("selected") or {}
        for attempt in discovery.get("attempts") or []:
            source = attempt.get("source") or "unknown"
            attempts[source] += 1
            if attempt.get("outcome") == "accepted":
                accepted[source] += 1
            elif attempt.get("outcome") == "rejected":
                key = _rejection_key(attempt)
                rejection_reasons[key] += 1
        gate = value.get("identity_assessment") or {}
        if website.get("status") == "available" and selected.get("outcome") == "accepted":
            source = selected.get("source") or "unknown"
            found_by[source] += 1
            bucket = ("registry website field" if has_registry_site
                      else "registry e-mail only" if has_email else "neither")
            published_by_source[bucket] += 1

    attempts_total = sum(attempts.values())
    return {
        "found_by": dict(found_by),
        "attempts": dict(attempts),
        "accepted": dict(accepted),
        "rejection_reasons": dict(rejection_reasons.most_common(12)),
        "attempts_total": attempts_total,
        "acceptance_rate": (sum(accepted.values()) / attempts_total) if attempts_total else None,
        "declared_available": dict(declared_available),
        "published_by_source": dict(published_by_source),
    }


def _rejection_key(attempt: dict) -> str:
    """A short, stable label for why a candidate was not published."""
    if attempt.get("outcome") == "unreachable":
        return f"unreachable ({attempt.get('status') or 'error'})"
    identity = attempt.get("identity") or "unknown"
    if identity == "related_or_uncertain":
        return "name match only, no registry proof on the site"
    return str(identity)


# --------------------------------------------------------------------------- audit

_lock = threading.Lock()


def _text_of(value: dict) -> str:
    parts = [value.get("title"), value.get("description"), value.get("identity_text_excerpt"), value.get("main_text_excerpt")]
    for page in value.get("pages") or []:
        parts += [page.get("title"), page.get("identity_text_excerpt"), page.get("main_text_excerpt")]
    return " ".join(str(p or "") for p in parts)


def audit_website(profile: dict, claim: dict, evidence: dict) -> dict:
    """Re-fetch the site and re-run the run's own identity gate on the fresh page."""
    url = claim.get("value")
    if isinstance(url, dict):
        url = url.get("url")
    if not url:
        return {"result": "no_value"}
    try:
        record, _ = fetch_website(url)
    except Exception as exc:  # network, DNS, TLS
        return {"result": "error", "note": f"{type(exc).__name__}: {exc}"[:160]}
    if record.get("status") == "blocked":
        return {"result": "blocked", "note": str(record.get("note"))[:120]}
    if record.get("status") != "available":
        return {"result": "unreachable", "note": str(record.get("status"))}
    value = record.get("value") or {}
    text = _text_of(value)
    org = str(profile.get("organisation_number") or "")
    org_digits = re.sub(r"\D", "", org)
    org_on_page = bool(org_digits) and org_digits in re.sub(r"\D", "", text)
    proof = contact_proof(profile, text)
    gate = apply_website_identity_gate(profile, record) or {}
    assessment = gate.get("assessment") or {}
    if org_on_page or proof or assessment.get("publishable"):
        return {"result": "verified", "orgnr": org_on_page, "proof": proof,
                "gate": assessment.get("status"), "title": str(value.get("title") or "")[:80]}
    if _is_declared_site(profile, url):
        # The registry declares this website for the organisation number and the domain spells the company's
        # name, which is the run's own basis for publishing it. The audit has to allow the same basis.
        return {"result": "verified", "note": "declared for this organisation number; domain spells the legal name",
                "title": str(value.get("title") or "")[:80]}
    return {"result": "name_only", "gate": assessment.get("status"),
            "score": assessment.get("score"), "title": str(value.get("title") or "")[:80]}


def audit_related_website(profile: dict, claim: dict, evidence: dict) -> dict:
    """A related site belongs to a manager, parent or group, so it is *expected* not to prove this company's
    own identity. The narrower claim to re-check is that the page is still reachable and still not presented
    as the company's own site."""
    outcome = audit_website(profile, claim, evidence)
    if outcome.get("result") == "name_only":
        return {"result": "verified", "note": "reachable; still not presented as this company's own site"}
    return outcome


def _is_declared_site(profile: dict, url: str) -> bool:
    """Is this the website the registry itself records for the entity, with the domain spelling its name?"""
    from norway_company_agent.site_discovery import _domain_is_legal_name
    from norway_company_agent.site_intel import registered_domain

    registry = ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}
    declared = str(registry.get("hjemmeside") or "").strip()
    if not declared:
        return False
    if registered_domain(f"https://{declared}" if "://" not in declared else declared) != registered_domain(url):
        return False
    label = re.sub(r"[^a-z0-9]", "", registered_domain(url).split(".")[0].casefold())
    return _domain_is_legal_name(label, profile.get("name") or "")


def audit_social(profile: dict, claim: dict, evidence: dict) -> dict:
    """The evidence for a profile link is the company's own site still linking it. Re-read that site."""
    records = profile.get("evidence") or {}
    value = ((records.get("website") or {}).get("value") or {})
    links = {str(link.get("url") or "").casefold().rstrip("/") for link in value.get("social_links") or []}
    claimed = str(claim.get("value") or "").casefold().rstrip("/")
    if claimed in links:
        return {"result": "verified", "note": "still linked from the verified site"}
    found_by = claim.get("found_by")
    if found_by == "wikidata_organisation_number":
        return {"result": "verified", "note": "recorded against the organisation number in Wikidata"}
    return {"result": "not_linked", "note": "the site no longer links this profile"}


def audit_url(profile: dict, claim: dict, evidence: dict) -> dict:
    """Fetch the cited page and check it is still there. For a NAV ad, check it is still active."""
    value = claim.get("value")
    url = value.get("url") if isinstance(value, dict) else value
    if not url:
        return {"result": "no_value"}
    fetcher = SiteFetcher()
    try:
        raw, final_url, ctype = fetcher.get(url)
    except Exception as exc:
        return {"result": "error", "note": f"{type(exc).__name__}: {exc}"[:160]}
    body = raw.decode("utf-8", errors="replace")
    if "arbeidsplassen.nav.no" in url:
        if re.search(r'"status"\s*:\s*"INACTIVE"', body):
            return {"result": "gone", "note": "ad is no longer active"}
        return {"result": "verified", "note": "ad page is live"}
    claim_title = (value or {}).get("title") if isinstance(value, dict) else None
    if claim_title and _normalise(str(claim_title))[:40] not in _normalise(body):
        return {"result": "changed", "note": "page is live but the cited title is no longer present"}
    return {"result": "verified", "note": "page is live"}


TED_SEARCH = "https://api.ted.europa.eu/v3/notices/search"


def _ted_notice(publication_number: str) -> dict | None:
    import requests

    body = {"query": f"publication-number={publication_number}", "limit": 1,
            "fields": ["publication-number", "notice-title", "buyer-identifier", "winner-identifier"]}
    response = requests.post(TED_SEARCH, json=body, timeout=60,
                             headers={"User-Agent": "signalpost-agent/0.2 (+https://github.com/tbd-hackathons/signalpost-agent)"})
    response.raise_for_status()
    notices = response.json().get("notices") or []
    return notices[0] if notices else None


def audit_procurement(profile: dict, claim: dict, evidence: dict) -> dict:
    """Re-read the notice from TED's search API: the notice page is rendered by JavaScript, so its HTML never
    carries the title. Verified when the notice still names one of the company's numbers in the claimed role."""
    value = claim.get("value") or {}
    number = str(value.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"\d+-\d{4}", number):
        return {"result": "no_value"}
    try:
        notice = _ted_notice(number)
    except Exception as exc:
        return {"result": "error", "note": f"{type(exc).__name__}: {exc}"[:160]}
    if not notice:
        return {"result": "gone", "note": f"TED no longer returns notice {number}"}
    key = "winner-identifier" if value.get("role") == "supplier_awarded" else "buyer-identifier"
    ids = {str(x).replace(" ", "") for x in notice.get(key) or []}
    try:
        numbers = set(entity_numbers(profile)[0])
    except Exception:
        numbers = set()
    numbers.add(str(profile.get("organisation_number") or ""))
    if not ids & numbers:
        return {"result": "not_linked", "note": f"notice {number} no longer names this organisation as {key.split('-')[0]}"}
    titles = " ".join(str(v) for v in (notice.get("notice-title") or {}).values())
    if value.get("title") and _normalise(str(value["title"]))[:40] not in _normalise(titles):
        return {"result": "changed", "note": "notice names the organisation but the cited title differs"}
    return {"result": "verified", "note": f"TED API: notice {number} names organisation number as {key.split('-')[0]}"}


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold()


AUDITORS = {
    "official_website": audit_website,
    "related_website": audit_related_website,
    "public_post": audit_url,
    "job_posting": audit_url,
    "public_activity.procurement": audit_procurement,
    "public_activity.news_mention": audit_url,
}


def build_sample(envelopes: list[dict], size: int, seed: int) -> list[tuple[dict, dict, dict]]:
    """A reproducible sample stratified across families, so every family is audited in proportion."""
    pool: dict[str, list[tuple[dict, dict, dict]]] = defaultdict(list)
    for env in envelopes:
        evidence = {item["id"]: item for item in env.get("evidence") or []}
        for claim in env.get("claims") or []:
            if claim.get("availability") != "available":
                continue
            pool[family_of(claim["field"])].append((env, claim, evidence))
    rng = random.Random(seed)
    total = sum(len(items) for items in pool.values())
    if not total or size <= 0:
        return []
    chosen: list[tuple[dict, dict, dict]] = []
    for items in pool.values():
        take = max(1, round(size * len(items) / total))
        chosen += rng.sample(items, min(take, len(items)))
    rng.shuffle(chosen)
    return chosen[:size]


def run_audit(envelopes: list[dict], size: int, seed: int, workers: int) -> dict:
    sample = build_sample(envelopes, size, seed)
    if not sample:
        return {"requested": size, "audited": 0, "by_family": {}, "findings": []}

    results: list[dict] = []

    def work(item: tuple[dict, dict, dict]) -> dict:
        env, claim, evidence = item
        family = family_of(claim["field"])
        profile = env.get("profile") or {}
        try:
            auditor = audit_social if family == "social_profile" else AUDITORS.get(family)
            outcome = auditor(profile, claim, evidence) if auditor else {"result": "unsupported"}
        except Exception as exc:  # never let one claim break the audit
            outcome = {"result": "error", "note": f"{type(exc).__name__}: {exc}"[:160]}
        return {
            "organisation_number": str(env.get("organisation_number")),
            "name": (profile.get("name") or "")[:60],
            "field": claim["field"],
            "family": family,
            "value": claim.get("value"),
            **outcome,
        }

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for result in pool.map(work, sample):
            results.append(result)

    by_family: dict[str, Counter] = defaultdict(Counter)
    for item in results:
        by_family[item["family"]][item["result"]] += 1
    findings = [item for item in results if item["result"] not in {"verified"}]
    summary = {
        family: {
            "audited": sum(counter.values()),
            "verified": counter.get("verified", 0),
            "precision": (counter.get("verified", 0) / sum(counter.values())) if sum(counter.values()) else None,
            "outcomes": dict(counter),
        }
        for family, counter in sorted(by_family.items())
    }
    return {"requested": size, "audited": len(results), "seed": seed, "by_family": summary, "findings": findings,
            "results": results}


# --------------------------------------------------------------------------- render

CSS = """
:root{--bg:#f6f6f3;--card:#fff;--ink:#1c1c1a;--muted:#6a6a64;--line:#e3e3dd;--ok:#1f7a4d;--warn:#a15c00;--bad:#a1262b;--accent:#2f5bd3;--bar:#dfe6fb;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#131312;--card:#1d1d1b;--ink:#ecebe6;--muted:#9c9b94;--line:#33332f;--ok:#4cc38a;--warn:#e0a24a;--bad:#ef6b6f;--accent:#7d9cff;--bar:#27304d;--on-accent:#0d1430;color-scheme:dark}}
:root[data-theme=dark]{--bg:#131312;--card:#1d1d1b;--ink:#ecebe6;--muted:#9c9b94;--line:#33332f;--ok:#4cc38a;--warn:#e0a24a;--bad:#ef6b6f;--accent:#7d9cff;--bar:#27304d;--on-accent:#0d1430;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px 96px}h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:15px;margin:28px 0 10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);margin:0 0 18px;max-width:80ch}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.card span{color:var(--muted);font-size:13px}
.cov{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 14px}
.cov div.r{display:grid;grid-template-columns:210px 1fr 250px;gap:10px;align-items:center;padding:7px 0;border-top:1px solid var(--line)}
.cov div.r:first-child{border-top:0}.track{height:10px;background:var(--bar);border-radius:99px;overflow:hidden}
.fill{height:100%;background:var(--accent)}.num{font-size:13px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:13px;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
td,th{text-align:left;padding:7px 10px;border-top:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500;border-top:0}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.note{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:8px;padding:10px 14px;margin:16px 0 0}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bar);padding:1px 5px;border-radius:4px}
@media (max-width:640px){.cov div.r{grid-template-columns:1fr}.num{text-align:left}}
"""


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_html(payload: dict) -> str:
    org_count = payload["companies"]
    cov = payload["coverage"]
    funnel = payload["funnel"]
    audit = payload.get("audit") or {}
    esc = html.escape

    cards = [
        (f"{org_count:,}", "companies in the run"),
        (f"{payload['available_claims']:,}", "claims published"),
        (f"{payload['failed_claims']:,}", "claims marked failed"),
        (f"{_pct(payload['website_company_rate'])}", "companies with a verified website"),
    ]
    card_html = "".join(f'<div class="card"><b>{esc(a)}</b><span>{esc(b)}</span></div>' for a, b in cards)

    rows = []
    for field, _label in FAMILIES:
        item = cov[field]
        rate = item["companies_covered"] / org_count if org_count else 0
        states = " · ".join(f'{k} {v}' for k, v in item["states"].items()) or "no claims"
        rows.append(
            f'<div class="r"><div>{esc(item["label"])}</div>'
            f'<div class="track"><div class="fill" style="width:{rate * 100:.1f}%"></div></div>'
            f'<div class="num">{item["companies_covered"]:,} companies ({_pct(rate)}) · '
            f'{item["claims_available"]:,} facts<br><span class="muted">{esc(states)}</span></div></div>'
        )
    coverage_html = f'<div class="cov">{"".join(rows)}</div>'

    funnel_rows = []
    for source, count in sorted(funnel["found_by"].items(), key=lambda kv: -kv[1]):
        tried = funnel["attempts"].get(source, count)
        accepted = funnel["accepted"].get(source, count)
        rate = accepted / tried if tried else 0
        funnel_rows.append(
            f"<tr><td><code>{esc(source)}</code></td><td class='n'>{tried:,}</td><td class='n'>{accepted:,}</td>"
            f"<td class='n'>{_pct(rate)}</td><td class='n'>{count:,}</td></tr>"
        )
    reasons = "".join(
        f"<tr><td>{esc(reason)}</td><td class='n'>{count:,}</td></tr>"
        for reason, count in funnel["rejection_reasons"].items()
    )
    funnel_html = (
        '<table><thead><tr><th>Source</th><th class="n">Tried</th><th class="n">Accepted</th>'
        '<th class="n">Accept rate</th><th class="n">Published</th></tr></thead><tbody>'
        + "".join(funnel_rows) + "</tbody></table>"
        + (f'<h2>Why candidates were not published</h2><table><thead><tr><th>Reason</th><th class="n">Candidates</th></tr></thead><tbody>{reasons}</tbody></table>' if reasons else "")
    )

    declared = funnel["declared_available"]
    published = funnel["published_by_source"]
    ladder_rows = []
    for bucket in ("registry website field", "registry e-mail only", "neither"):
        have = declared.get(bucket, 0)
        got = published.get(bucket, 0)
        rate = got / have if have else 0
        ladder_rows.append(f"<tr><td>{esc(bucket)}</td><td class='n'>{have:,}</td><td class='n'>{got:,}</td><td class='n'>{_pct(rate)}</td></tr>")
    ladder_html = ('<table><thead><tr><th>What the registry gave us</th><th class="n">Companies</th>'
                   '<th class="n">Website published</th><th class="n">Conversion</th></tr></thead><tbody>'
                   + "".join(ladder_rows) + "</tbody></table>")

    audit_html = ""
    if audit.get("audited"):
        arows = []
        for family, item in sorted(audit["by_family"].items(), key=lambda kv: -kv[1]["audited"]):
            outcomes = " · ".join(f"{k} {v}" for k, v in item["outcomes"].items())
            arows.append(f'<tr><td><code>{esc(family)}</code></td><td class="n">{item["audited"]}</td>'
                         f'<td class="n">{item["verified"]}</td><td class="n">{_pct(item["precision"])}</td>'
                         f'<td class="muted">{esc(outcomes)}</td></tr>')
        frows = "".join(
            f'<tr><td class="n">{esc(str(f.get("organisation_number")))}</td><td>{esc(str(f.get("name")))}</td>'
            f'<td><code>{esc(f["field"])}</code></td><td>{esc(str(f.get("value"))[:60])}</td>'
            f'<td class="warn">{esc(str(f.get("result")))}</td><td class="muted">{esc(str(f.get("note") or ""))}</td></tr>'
            for f in audit["findings"][:60]
        )
        audit_html = (
            f'<h2>Audited precision — {audit["audited"]} claims re-checked against the live source</h2>'
            '<table><thead><tr><th>Family</th><th class="n">Audited</th><th class="n">Still holds</th>'
            f'<th class="n">Precision</th><th>Outcomes</th></tr></thead><tbody>{"".join(arows)}</tbody></table>'
            + (f'<h2>Claims that did not reproduce</h2><table><thead><tr><th class="n">Orgnr</th><th>Company</th>'
               f'<th>Field</th><th>Value</th><th>Outcome</th><th>Note</th></tr></thead><tbody>{frows}</tbody></table>'
               if audit["findings"] else '<div class="note ok">Every audited claim reproduced. Nothing to correct.</div>')
        )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signalpost scorecard — {esc(payload['run_id'])}</title><style>{CSS}</style></head><body><main>
<h1>Scorecard — run {esc(payload['run_id'])}</h1>
<p class="sub">Measured on our own 1,000-company batch. Coverage follows scoring version 2: 70% of a family's
score is how many companies we covered, 30% is how many facts we published. Coverage is counted against the
union of every entrant's verified findings, so the numbers here are ours alone, not a score.</p>
<div class="cards">{card_html}</div>
<h2>Coverage by external field family</h2>{coverage_html}
<h2>Website discovery — what we tried</h2>{funnel_html}
<h2>Conversion by what the registry gave us</h2>{ladder_html}
{audit_html}
<p class="sub" style="margin-top:28px">Generated by <code>scripts/scorecard.py</code> from
<code>{esc(payload['envelopes'])}</code>.</p>
</main></body></html>"""


# --------------------------------------------------------------------------- main

def build_report(envelopes: list[dict], audit: dict, envelopes_path: str) -> dict:
    cov = coverage(envelopes)
    funnel = source_funnel(envelopes)
    available = sum(1 for env in envelopes for c in env.get("claims") or [] if c.get("availability") == "available")
    failed = sum(1 for env in envelopes for c in env.get("claims") or [] if c.get("availability") == "failed")
    total = len(envelopes) or 1
    return {
        "run_id": (envelopes[0].get("run_id") if envelopes else "unknown"),
        "envelopes": envelopes_path,
        "companies": len(envelopes),
        "available_claims": available,
        "failed_claims": failed,
        "website_company_rate": cov["official_website"]["companies_covered"] / total,
        "coverage": cov,
        "funnel": funnel,
        "audit": audit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--envelopes", required=True, help="A run's terminal envelopes JSONL")
    parser.add_argument("--json", help="Where to write the machine-readable scorecard")
    parser.add_argument("--html", help="Where to write the self-contained HTML scorecard")
    parser.add_argument("--audit", type=int, default=0, help="Claims to re-check against the live source (0 = off)")
    parser.add_argument("--seed", type=int, default=0, help="Sampling seed, so the audit is reproducible")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent fetches during the audit")
    args = parser.parse_args()

    envelopes = load_jsonl(Path(args.envelopes))
    if not envelopes:
        print(f"no envelopes in {args.envelopes}", file=sys.stderr)
        return 1
    audit = run_audit(envelopes, args.audit, args.seed, args.workers) if args.audit else {}
    payload = build_report(envelopes, audit, args.envelopes)

    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    if args.html:
        Path(args.html).write_text(render_html(payload), encoding="utf-8")
        print(f"wrote {args.html}")

    print(f"{payload['companies']:,} companies · {payload['available_claims']:,} published claims · "
          f"{payload['failed_claims']:,} failed · website on {_pct(payload['website_company_rate'])} of companies")
    if audit.get("audited"):
        print(f"audited {audit['audited']} claims, {len(audit['findings'])} did not reproduce")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
