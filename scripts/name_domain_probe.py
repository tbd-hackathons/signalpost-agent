#!/usr/bin/env python3
"""Does a name-derived domain actually belong to the company? Measure it before trusting it.

DNS tells us a hostname exists. That is not evidence that it is *this* company's site. This samples
companies whose registry entry gives no website, fetches the name-derived candidates that resolve, and runs
the same identity gate the run uses. The number that matters is the accepted share, and every rejection is
printed with its reason so the rule can be judged rather than assumed.

Polite by construction: two pages per company, a small worker pool, robots.txt respected by the fetchers.

Usage: python scripts/name_domain_probe.py --batch data-cache/test-1000.jsonl --sample 120 --workers 4
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dns_funnel import candidates, resolves  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.site_candidates import contact_proof, place_proof  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402


def page_text(value: dict) -> str:
    parts = [value.get("title"), value.get("description"), value.get("identity_text_excerpt"), value.get("main_text_excerpt")]
    for page in value.get("pages") or []:
        parts += [page.get("title"), page.get("identity_text_excerpt"), page.get("main_text_excerpt")]
    return " ".join(str(p or "") for p in parts)


def probe(profile: dict, hosts: list[str]) -> dict:
    """Try the name-derived hosts in order and report the first that proves identity."""
    tried: list[dict] = []
    for host in hosts:
        url = f"https://{host}/"
        try:
            record, _ = fetch_website(url)
        except Exception as exc:
            tried.append({"host": host, "outcome": "error", "note": f"{type(exc).__name__}"})
            continue
        if record.get("status") != "available":
            tried.append({"host": host, "outcome": "unreachable", "note": str(record.get("status"))})
            continue
        value = record.get("value") or {}
        text = page_text(value)
        gate = apply_website_identity_gate(profile, record) or {}
        assessment = gate.get("assessment") or {}
        proof = contact_proof(profile, text)
        if not proof and assessment.get("score", 0) >= 0.95:
            proof = place_proof(profile, text)
        accepted = bool(assessment.get("publishable")) or bool(proof)
        tried.append({"host": host, "outcome": "accepted" if accepted else "rejected",
                      "identity": assessment.get("status"), "score": assessment.get("score"),
                      "proof": proof, "title": str(value.get("title") or "")[:70]})
        if accepted:
            return {"organisation_number": str(profile["organisation_number"]), "name": profile.get("name"),
                    "accepted": True, "host": host, "proof": proof, "identity": assessment.get("status"),
                    "title": str(value.get("title") or "")[:70], "tried": tried}
    return {"organisation_number": str(profile["organisation_number"]), "name": profile.get("name"),
            "accepted": False, "tried": tried}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", default="out/ws-a-profiles.jsonl")
    parser.add_argument("--sample", type=int, default=120)
    parser.add_argument("--per-company", type=int, default=3, help="Name-derived hosts to fetch after DNS")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", help="Write every attempt here")
    args = parser.parse_args()

    import random

    rows = [json.loads(line) for line in Path(args.profiles).read_text().splitlines() if line.strip()]
    # Only companies the registry gave nothing usable for: website empty, and no live site cached.
    silent = []
    for row in rows:
        records = row.get("evidence") or {}
        website = records.get("website") or {}
        registry = ((records.get("registry") or {}).get("value") or {})
        if website.get("status") == "available":
            continue
        if str(registry.get("hjemmeside") or "").strip():
            continue
        silent.append(row)
    random.Random(args.seed).shuffle(silent)
    sample = silent[: args.sample]

    # DNS first, in parallel, so we only fetch hosts that exist.
    plans: list[tuple[dict, list[str]]] = []
    for row in sample:
        hosts = [h for h in candidates(row.get("name") or "", 10) if resolves(h)][: args.per_company]
        if hosts:
            plans.append((row, hosts))
    print(f"silent companies: {len(silent):,} · sampled {len(sample)} · with a resolving name-domain {len(plans)}", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda item: probe(item[0], item[1]), plans):
            results.append(result)

    accepted = [r for r in results if r["accepted"]]
    reasons: Counter = Counter()
    for result in results:
        if result["accepted"]:
            continue
        last = result["tried"][-1] if result["tried"] else {}
        reasons[f'{last.get("outcome")}: {last.get("identity") or last.get("note") or ""}'] += 1

    print()
    print(f"companies probed            : {len(results)}")
    print(f"name-domain accepted        : {len(accepted)} ({len(accepted) / max(1, len(results)):.0%} of probed, "
          f"{len(accepted) / max(1, len(silent)):.1%} of all silent companies)")
    print()
    print("rejection reasons:")
    for reason, count in reasons.most_common(10):
        print(f"  {count:>4}  {reason}")
    print()
    print("accepted examples:")
    for result in accepted[:12]:
        print(f'  {result["organisation_number"]} {str(result["name"])[:34]:34} -> {result["host"]:32} '
              f'[{result["identity"]}] {str(result["proof"])[:40]}')
    if args.json:
        Path(args.json).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
