"""OUTPUT_CONTRACT view of a profile: flat `claims`, the `evidence` they cite, and `external_observations`.

Added to every terminal envelope next to the full profile, so each external fact (website, company
profiles, job ads, dated posts) can be scored one by one with its source, retrieval time and span.
Only exact-entity facts become `available` claims; a source that was checked and found nothing gives a
`not_available` claim, and a source that could not be checked gives `failed`.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .external_footprint import publishable_observation


def _ev_id(*parts: Any) -> str:
    return "ev-" + hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def contract_view(profile: dict[str, Any]) -> dict[str, Any]:
    claims: list[dict[str, Any]] = []
    evidence: dict[str, dict[str, Any]] = {}
    observations: list[dict[str, Any]] = []
    records = profile.get("evidence") or {}

    def cite(source_url: str, source_class: str, retrieved_at: Any, sha: Any, span: str) -> str:
        eid = _ev_id(source_url, sha, span)
        evidence.setdefault(eid, {"id": eid, "source_url": source_url, "source_class": source_class,
                                  "retrieved_at": retrieved_at, "content_sha256": sha, "claim_span": span[:500]})
        return eid

    website = records.get("website") or {}
    web = website.get("value") or {}
    assessment = web.get("identity_assessment") or {}
    if website.get("status") == "available" and assessment.get("publishable"):
        span = "; ".join(str(r) for r in assessment.get("reasons") or [])[:500] or web.get("title") or ""
        eid = cite(website.get("source_url"), "company_owned", website.get("retrieved_at"), website.get("content_sha256"), span)
        claims.append({"field": "official_website", "value": web.get("final_url"), "availability": "available",
                       "confidence": assessment.get("score"), "evidence_ids": [eid],
                       "found_by": ((web.get("discovery") or {}).get("selected") or {}).get("source")})
        for link in web.get("social_links") or []:
            claims.append({"field": f"social_profile.{link['platform']}", "value": link["url"], "availability": "available",
                           "confidence": assessment.get("score"), "evidence_ids": [eid]})
    else:
        claims.append({"field": "official_website", "value": None,
                       "availability": "failed" if website.get("status") in {"source_error"} else
                       "blocked" if website.get("status") == "blocked" else "not_available",
                       "confidence": None, "evidence_ids": []})

    published_profiles = {link["url"].casefold().rstrip("/") for link in web.get("social_links") or []} \
        if website.get("status") == "available" and assessment.get("publishable") else set()
    wikidata = records.get("wikidata") or {}
    for link in (wikidata.get("value") or {}).get("social_profiles") or []:
        if link["url"].casefold().rstrip("/") in published_profiles:
            continue
        span = f"{wikidata['value']['identity_proof']}; {link['platform']} account '{link['handle']}'"
        eid = cite(wikidata["source_url"], "open_data_wikidata", wikidata.get("retrieved_at"), wikidata.get("content_sha256"), span)
        claims.append({"field": f"social_profile.{link['platform']}", "value": link["url"], "availability": "available",
                       "confidence": 0.9, "evidence_ids": [eid], "found_by": "wikidata_organisation_number"})
        observations.append({
            "id": "wd:" + hashlib.sha256(f"{profile['organisation_number']}|{link['url']}".encode()).hexdigest()[:20],
            "organisation_number": str(profile["organisation_number"]), "platform": link["platform"],
            "signal_type": "profile_handle", "source_class": "open_data", "source_url": wikidata["source_url"],
            "item_url": link["url"], "retrieved_at": wikidata.get("retrieved_at"), "content_sha256": wikidata.get("content_sha256"),
            "exact_entity": True, "identity_proof": wikidata["value"]["identity_proof"], "acquisition_mode": "official_api",
            "rights_status": "approved", "evidence_span": span, "value": link})

    activity = web.get("activity") or {}
    for obs in activity.get("observations") or []:
        if obs["signal_type"] == "job_posting":
            continue  # job ads are cited from the jobs record below
        observations.append(obs)
        if obs["signal_type"] == "public_post":
            eid = cite(obs["source_url"], "company_owned", obs["retrieved_at"], obs["content_sha256"], obs["evidence_span"])
            claims.append({"field": "public_post", "value": {"title": obs["value"].get("title"), "url": obs["item_url"],
                                                             "published": obs["value"].get("published")},
                           "availability": "available", "confidence": 0.95, "evidence_ids": [eid]})

    jobs = records.get("jobs") or {}
    job_value = jobs.get("value") or {}
    for posting in job_value.get("postings") or []:
        span = f"{posting.get('title')} · employer {posting.get('employer_name')} ({posting.get('employer_orgnr')})"
        eid = cite(posting["url"], "official_job_board", posting["retrieved_at"], posting["content_sha256"], span)
        claims.append({"field": "job_posting", "value": {k: posting.get(k) for k in (
            "title", "url", "published", "expires", "work_locations", "employer_orgnr")},
            "availability": "available", "confidence": 0.99, "evidence_ids": [eid]})
    for posting in job_value.get("company_site_postings") or []:
        eid = cite(posting.get("listed_on") or posting["url"], "company_owned", posting.get("retrieved_at"),
                   posting.get("content_sha256"), posting["title"])
        claims.append({"field": "job_posting", "value": {k: posting.get(k) for k in ("title", "url", "date_posted", "valid_through")},
                       "availability": "available", "confidence": 0.9, "evidence_ids": [eid]})
    observations += job_value.get("observations") or []
    if jobs and not job_value.get("active_job_count"):
        claims.append({"field": "job_posting", "value": None,
                       "availability": "failed" if jobs.get("status") == "source_error" else "not_available",
                       "confidence": None, "evidence_ids": [], "note": jobs.get("note")})

    observations = [obs for obs in observations if publishable_observation(obs)]
    return {"claims": claims, "evidence": list(evidence.values()), "external_observations": observations}
