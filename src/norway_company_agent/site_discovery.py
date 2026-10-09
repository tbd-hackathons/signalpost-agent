"""Try website candidates in order and publish the first one that proves it is this exact entity.

Publication rules
- registry website / company-specific registry e-mail domain: the starter's identity gate
  (organisation number or legal name on the homepage) OR registry contact details on the site.
- guessed or shared domains (`requires_strict_proof`): organisation number on the homepage OR
  registry contact details on the site. Name similarity alone never publishes a guessed domain.
Rejected candidates are kept in `value.discovery.attempts` with the reason, never published.
"""
from __future__ import annotations

from collections import Counter
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


def resolve_website(profile: dict[str, Any], domain_counts: Counter | None, *, max_candidates: int = 3,
                    guess: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": []}
    candidates = website_candidates(profile, domain_counts, guess=guess)[:max_candidates]
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
