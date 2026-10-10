"""What changed for one company since a previous run, on top of the starter's `refresh.diff_profile`.

Adds the fields this agent publishes beyond the starter (verified website, job ads, Wikidata profiles)
and compares them by stable identity (URL, ad id), so a re-fetch with a new timestamp is not a change.
Each change keeps the earlier evidence fingerprint (`old_content_sha256`), so prior evidence is preserved.
"""
from __future__ import annotations

from typing import Any

from .refresh import diff_profile


def _site(profile: dict[str, Any]) -> str | None:
    site = (profile.get("evidence") or {}).get("website") or {}
    value = site.get("value") or {}
    if site.get("status") == "available" and (value.get("identity_assessment") or {}).get("publishable"):
        return value.get("final_url")
    return None


def _job_ids(profile: dict[str, Any]) -> set[str] | None:
    jobs = (profile.get("evidence") or {}).get("jobs") or {}
    if jobs.get("status") not in {"available", "not_found"}:
        return None  # not checked: no claim either way
    value = jobs.get("value") or {}
    return {p["url"] for p in value.get("postings") or []} | {p["url"] for p in value.get("company_site_postings") or []}


def _wikidata_profiles(profile: dict[str, Any]) -> set[str]:
    value = ((profile.get("evidence") or {}).get("wikidata") or {}).get("value") or {}
    return {p["url"] for p in value.get("social_profiles") or []}


def changes_since(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    changes = [c for c in diff_profile(previous, current) if c["field"] != "website.social_links" or _site(current)]
    org = current["organisation_number"]
    ev_now, ev_then = current.get("evidence") or {}, previous.get("evidence") or {}

    def add(field: str, old: Any, new: Any, module: str) -> None:
        record, before = ev_now.get(module) or {}, ev_then.get(module) or {}
        changes.append({"organisation_number": org, "field": field, "old_value": old, "new_value": new,
                        "source_url": record.get("source_url"), "retrieved_at": record.get("retrieved_at"),
                        "old_content_sha256": before.get("content_sha256"),
                        "new_content_sha256": record.get("content_sha256"), "status": record.get("status")})

    if _site(previous) != _site(current) and (_site(current) or (ev_now.get("website") or {}).get("status") == "not_found"):
        add("website.verified_url", _site(previous), _site(current), "website")
    old_jobs, new_jobs = _job_ids(previous), _job_ids(current)
    if old_jobs is not None and new_jobs is not None and old_jobs != new_jobs:
        add("jobs.active_ads", sorted(old_jobs - new_jobs), sorted(new_jobs - old_jobs), "jobs")
    if _wikidata_profiles(previous) != _wikidata_profiles(current):
        add("wikidata.social_profiles", sorted(_wikidata_profiles(previous)), sorted(_wikidata_profiles(current)), "wikidata")
    return changes
