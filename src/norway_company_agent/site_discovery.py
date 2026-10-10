"""Try website candidates in order and publish the first one that proves it is this exact entity.

Publication rules
- registry website / company-specific registry e-mail domain: the starter's identity gate
  (organisation number or legal name on the homepage) OR registry contact details on the site.
- guessed or shared domains (`requires_strict_proof`): organisation number on the homepage OR
  registry contact details on the site. Name similarity alone never publishes a guessed domain.
Rejected candidates are kept in `value.discovery.attempts` with the reason, never published.
"""
from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from .identity import apply_website_identity_gate
from .site_candidates import contact_proof, place_proof, website_candidates
from .website import fetch_website


def _page_text(value: dict[str, Any]) -> str:
    parts = [value.get("title"), value.get("description"), value.get("identity_text_excerpt"), value.get("main_text_excerpt")]
    for page in value.get("pages") or []:
        parts += [page.get("title"), page.get("identity_text_excerpt"), page.get("main_text_excerpt")]
    return " ".join(str(p or "") for p in parts)


def _publish_socials(value: dict[str, Any]) -> None:
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in value.get("social_link_assessments") or []
        if item.get("publishable")
    ]


def _fold(text: str) -> str:
    text = text.casefold().replace("å", "aa").replace("ø", "o").replace("æ", "ae")
    return re.sub(r"[^a-z0-9]", "", text)


def _domain_is_legal_name(domain_label: str, legal_name: str) -> bool:
    """The site's domain name spells the legal name (allowing å->aa and similar), not a parent or manager."""
    name = _fold(re.sub(r"\b(as|asa|ans|da|sa|ba|ks|nuf|enk)\b", " ", legal_name.casefold()))
    if not name:
        return False
    variants = {name, name.replace("aa", "a")}
    label = {domain_label, domain_label.replace("aa", "a")}
    return any(a == b or (len(b) >= 5 and b in a) or SequenceMatcher(None, a, b).ratio() >= 0.85
               for a in label for b in variants)


def _accept_domain_handles(value: dict[str, Any], legal_name: str = "") -> None:
    """A profile whose handle contains the verified site's domain name is the company's own.

    The legal-name rule misses brands written differently from the legal name, e.g. GLÅME AS ->
    glaame.no -> facebook.com/glaame.no. The domain itself passed the identity gate, so its name is
    as strong as the legal name. Short or generic domain names (under 5 letters) are not used.
    """
    domain = str(value.get("registered_domain") or "")
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].casefold())
    if len(label) < 5 or not _domain_is_legal_name(label, legal_name):
        return
    for item in value.get("social_link_assessments") or []:
        handle = re.sub(r"[^a-z0-9]", "", urllib.parse.unquote(urllib.parse.urlparse(item["url"]).path).casefold())
        if not item.get("publishable") and label in handle:
            item.update(publishable=True, identity_score=0.95, method="verified_domain_name_in_handle_v1",
                        reason=f"handle contains the verified website's domain name '{label}'")
    _publish_socials(value)


def resolve_website(profile: dict[str, Any], domain_counts: Counter | None, *, max_candidates: int = 3,
                    guess: bool = True, extra_websites: list[str] | None = None,
                    search_websites: list[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """`extra_websites`: sites an organisation-number-keyed source (Wikidata, NAV employer) records for this company.

    `search_websites`: domains a search API returned for the company name. They are tried alone (the other
    candidates already failed in the first pass) and need the same strict proof as a guessed domain.
    """
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": []}
    candidates = [] if search_websites else website_candidates(profile, domain_counts, guess=guess)
    for url in search_websites or []:
        host = (urllib.parse.urlparse(url).hostname or "").casefold().removeprefix("www.")
        if host and host not in {c["host"] for c in candidates}:
            candidates.append({"url": f"https://{host}/", "host": host, "source": "search_api_candidate",
                               "reason": "a search API returned this site for the legal name",
                               "requires_strict_proof": True})
    hosts = {c["host"] for c in candidates}
    for url in extra_websites or []:
        host = (urllib.parse.urlparse(url if "://" in url else "https://" + url).hostname or "").casefold().removeprefix("www.")
        if host and host not in hosts:
            hosts.add(host)
            # Ahead of name guesses, behind the registry's own website and e-mail domain.
            position = sum(1 for c in candidates if c["source"] != "legal_name_domain_guess")
            candidates.insert(position, {"url": url if "://" in url else "https://" + url, "host": host,
                                         "source": "organisation_number_keyed_source",
                                         "reason": "Wikidata or NAV records this website for the organisation number",
                                         "requires_strict_proof": False})
    candidates = candidates[:max_candidates]
    attempts: list[dict[str, Any]] = []
    fallback: dict[str, Any] | None = None

    for cand in candidates:
        record, m = fetch_website(cand["url"])
        metrics["requests"] += m.get("requests", 0)
        metrics["bytes"] += m.get("bytes", 0)
        metrics["latencies_ms"] += m.get("latencies_ms", [])
        attempt = {k: cand[k] for k in ("url", "source", "reason", "requires_strict_proof")}
        if record.get("status") != "available":
            attempt.update(outcome="unreachable", status=record.get("status"), note=record.get("note"))
            attempts.append(attempt)
            fallback = fallback or record
            continue

        gated = apply_website_identity_gate(profile, record)
        site, assessment = gated["website"], gated["assessment"] or {}
        value = site.get("value") or {}
        org_on_page = assessment.get("score") == 1.0
        proof = contact_proof(profile, _page_text(value))
        if cand["requires_strict_proof"]:
            if not org_on_page and not proof and assessment.get("score", 0) >= 0.95:
                # Full legal name on the homepage plus the registered town is enough for a guessed domain.
                proof = place_proof(profile, _page_text(value))
            accept = org_on_page or bool(proof)
        else:
            accept = bool(assessment.get("publishable")) or bool(proof)

        if accept and not assessment.get("publishable"):
            # Contact proof upgrades an otherwise uncertain name match.
            assessment = {**assessment, "status": "exact", "score": 0.97, "publishable": True,
                          "reasons": [*assessment.get("reasons", []), proof],
                          "method": "registry_contact_on_site_v1"}
            value["identity_assessment"] = assessment
            _publish_socials(value)
        elif not accept and assessment.get("publishable"):
            # Strict candidate with only a name match: do not publish.
            assessment = {**assessment, "status": "related_or_uncertain", "publishable": False,
                          "reasons": [*assessment.get("reasons", []),
                                      "guessed/shared domain needs organisation number or registry contact on the site"]}
            value["identity_assessment"] = assessment
            value["social_links"] = []

        attempt.update(outcome="accepted" if accept else "rejected", identity=assessment.get("status"),
                       score=assessment.get("score"), proof=proof)
        attempts.append(attempt)
        if accept:
            _accept_domain_handles(value, profile.get("name") or "")
            value["discovery"] = {"selected": attempt, "attempts": attempts, "method": "site_discovery_v1"}
            site["value"] = value
            return site, metrics
        fallback = fallback or site

    if fallback is None:
        fallback, _ = fetch_website(None)  # the starter's standard not_found evidence record
        fallback["note"] = "No website candidate from registry website, registry e-mail domain or legal-name domain"
    elif fallback.get("status") == "available":
        # A candidate was reachable but not proven: keep it as not published.
        value = fallback.get("value") or {}
        value["social_links"] = []
    fallback.setdefault("value", {})
    if isinstance(fallback.get("value"), dict):
        fallback["value"]["discovery"] = {"selected": None, "attempts": attempts, "method": "site_discovery_v1"}
    return fallback, metrics
