"""Active job ads for the exact legal entity, from NAV's public job-ad feed (arbeidsplassen.nav.no).

Identity: every NAV ad names its employer by organisation number. The number is either the legal
entity itself or one of its registered sub-units (underenheter). An ad is attached to a company only
when that number equals the company's own number or one of the sub-unit numbers that the official
registry lists for it. Name similarity never attaches an ad; it only decides which new ads to look up.

Freshness: the local index (built by `scripts/nav_active_index.py`) is a starting list. During a run,
every matched ad is fetched again from NAV, and is published only if NAV still reports it ACTIVE with
the same employer number and an expiry date that has not passed.

Privacy: contact persons, e-mail addresses and phone numbers in the ads are never copied.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import threading
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .evidence import evidence, utc_now
from .nav_feed import NavFeed, compact

FEED_HOME = "https://arbeidsplassen.nav.no/stillinger"
FEED_API = "https://pam-stilling-feed.nav.no"
MAX_ADS_PER_COMPANY = 40
LEGAL_SUFFIX = re.compile(r"\b(asa|as|ans|da|sa|ba|ks|nuf|enk|stiftelse|kf|iks)\b\.?", re.I)


def norm_name(name: str | None) -> str:
    text = LEGAL_SUFFIX.sub(" ", str(name or "").casefold())
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def ad_summary(detail: dict[str, Any], retrieved_at: str | None = None) -> dict[str, Any] | None:
    """Reduce a NAV feed-entry detail to the published, non-personal fields."""
    ad = detail.get("ad_content") or {}
    employer = ad.get("employer") or {}
    if not ad or not employer.get("orgnr"):
        return None
    raw = json.dumps(detail, ensure_ascii=False, sort_keys=True).encode()
    places = []
    for loc in ad.get("workLocations") or []:
        place = {k: loc.get(k) for k in ("city", "municipal", "county", "postalCode", "country") if loc.get(k)}
        if place:
            places.append(place)
    return {
        "uuid": detail.get("uuid") or ad.get("uuid"),
        "status": detail.get("status"),
        "employer_orgnr": str(employer.get("orgnr")),
        "employer_name": employer.get("name"),
        "title": ad.get("title"),
        "job_title": ad.get("jobtitle"),
        "published": ad.get("published"),
        "expires": ad.get("expires"),
        "application_due": ad.get("applicationDue"),
        "updated": detail.get("sistEndret") or ad.get("updated"),
        "engagement_type": ad.get("engagementtype"),
        "extent": ad.get("extent"),
        "position_count": ad.get("positioncount"),
        "sector": ad.get("sector"),
        "occupations": [c.get("level2") or c.get("level1") for c in ad.get("occupationCategories") or [] if c],
        "work_locations": places,
        "url": ad.get("link") or f"{FEED_HOME}/stilling/{detail.get('uuid')}",
        "source_ad_url": ad.get("sourceurl") or None,
        "origin": ad.get("source"),
        "retrieved_at": retrieved_at or utc_now(),
        "content_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _expired(summary: dict[str, Any], today: date) -> bool:
    value = str(summary.get("expires") or "")[:10]
    try:
        return date.fromisoformat(value) < today
    except ValueError:
        return False


class NavJobIndex:
    """Active ads keyed by employer organisation number, plus new feed entries keyed by employer name."""

    def __init__(self, by_orgnr: dict[str, list[dict[str, Any]]], built_at: str | None,
                 new_entries_by_name: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.by_orgnr = by_orgnr
        self.built_at = built_at
        self.new_entries_by_name = new_entries_by_name or {}

    @classmethod
    def load(cls, cache_dir: str | Path) -> "NavJobIndex | None":
        path = Path(cache_dir) / "active-ads.jsonl.gz"
        if not path.exists():
            return None
        meta_path = Path(cache_dir) / "active-ads.meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        by_orgnr: dict[str, list[dict[str, Any]]] = defaultdict(list)
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                by_orgnr[row["employer_orgnr"]].append(row)
        return cls(dict(by_orgnr), meta.get("built_at"))

    def candidates(self, orgnrs: set[str], names: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        known = [ad for org in sorted(orgnrs) for ad in self.by_orgnr.get(org, [])]
        known_ids = {ad["uuid"] for ad in known}
        fresh = [entry for name in sorted(names) for entry in self.new_entries_by_name.get(name, [])
                 if entry["uuid"] not in known_ids]
        return known, fresh


def entity_numbers(profile: dict[str, Any]) -> tuple[set[str], set[str]]:
    """The company's own number plus registered sub-unit numbers; and normalised names to look up."""
    orgnrs = {str(profile["organisation_number"])}
    names = {norm_name(profile.get("name"))}
    locations = ((profile.get("evidence") or {}).get("locations") or {}).get("value") or {}
    for unit in locations.get("locations") or []:
        if unit.get("organisation_number"):
            orgnrs.add(str(unit["organisation_number"]))
        if unit.get("name"):
            names.add(norm_name(unit["name"]))
    names.discard("")
    return orgnrs, names


def _observation(profile: dict[str, Any], ad: dict[str, Any], proof: str) -> dict[str, Any]:
    span = " · ".join(str(x) for x in (ad.get("title"), ad.get("employer_name"), ad.get("employer_orgnr")) if x)
    return {
        "id": "nav:" + str(ad["uuid"]),
        "organisation_number": str(profile["organisation_number"]),
        "platform": "job_board",
        "signal_type": "job_posting",
        "source_class": "official_job_board",
        "source_url": ad["url"],
        "retrieved_at": ad["retrieved_at"],
        "observed_at": ad.get("published"),
        "content_sha256": ad["content_sha256"],
        "exact_entity": True,
        "identity_proof": proof,
        "acquisition_mode": "official_api",
        "rights_status": "approved",
        "evidence_span": span,
        "value": {k: ad.get(k) for k in ("title", "job_title", "published", "expires", "application_due",
                                         "engagement_type", "extent", "position_count", "occupations",
                                         "work_locations", "employer_orgnr", "employer_name", "sector")},
    }


def jobs_for_profile(profile: dict[str, Any], index: NavJobIndex | None, fetch_detail,
                     *, today: date | None = None, linked_uuids: list[str] | None = None,
                     site_jobs: list[dict[str, Any]] | None = None,
                     site_observations: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (evidence record for field `jobs`, metrics). `fetch_detail(uuid)` returns NAV's JSON.

    `linked_uuids`: NAV ads that the verified company website links to; looked up like index hits.
    `site_jobs`: other ads found on the verified website (see `site_intel`), merged after NAV's.
    """
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": []}
    if index is None and not site_jobs:
        return evidence("jobs", "source_error", "nav_public_job_feed", FEED_HOME,
                        note="NAV job-ad index not available for this run"), metrics
    today = today or datetime.now(timezone.utc).date()
    orgnrs, names = entity_numbers(profile)
    known, fresh = index.candidates(orgnrs, names) if index else ([], [])
    seen_ids = {ad["uuid"] for ad in [*known, *fresh]}
    fresh += [{"uuid": uuid} for uuid in dict.fromkeys(linked_uuids or []) if uuid not in seen_ids]
    postings: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    for ad in [*known, *fresh][:MAX_ADS_PER_COMPANY]:
        uuid = ad["uuid"]
        try:
            detail = fetch_detail(uuid)
            metrics["requests"] += 1
        except Exception as exc:  # keep the cached copy out of the profile when it cannot be re-checked
            metrics["requests"] += 1
            checked.append({"uuid": uuid, "outcome": "recheck_failed", "error": f"{type(exc).__name__}"})
            continue
        current = ad_summary(detail) if detail else None
        if not current or current.get("status") != "ACTIVE" or _expired(current, today):
            checked.append({"uuid": uuid, "outcome": "no_longer_active"})
            continue
        if current["employer_orgnr"] not in orgnrs:
            checked.append({"uuid": uuid, "outcome": "different_employer", "employer_orgnr": current["employer_orgnr"]})
            continue
        own = current["employer_orgnr"] == str(profile["organisation_number"])
        proof = ("NAV ad names this organisation number as employer" if own else
                 f"NAV ad names registered sub-unit {current['employer_orgnr']} of this organisation as employer")
        current["identity_proof"] = proof
        postings.append(current)
        observations.append(_observation(profile, current, proof))
        checked.append({"uuid": uuid, "outcome": "published"})

    postings.sort(key=lambda ad: str(ad.get("published") or ""), reverse=True)
    nav_ids = {p["uuid"] for p in postings}
    extra = [job for job in site_jobs or [] if not any(uuid in job["url"] for uuid in nav_ids)
             and "arbeidsplassen.nav.no" not in job["url"]]
    observations += [obs for obs in (site_observations or []) if obs.get("item_url") in {job["url"] for job in extra}]
    value = {
        "active_job_count": len(postings) + len(extra),
        "company_site_postings": extra,
        "positions_advertised": sum(int(p["position_count"]) for p in postings if str(p.get("position_count") or "").isdigit()),
        "latest_published": postings[0]["published"] if postings else None,
        "postings": postings,
        "observations": observations,
        "matched_numbers": sorted(orgnrs),
        "index_built_at": index.built_at if index else None,
        "checks": checked,
        "method": "nav_feed_orgnr_v1",
    }
    if postings or extra:
        source = postings[0]["url"] if postings else extra[0].get("listed_on") or extra[0]["url"]
        return evidence("jobs", "available", "nav_public_job_feed", source, value=value,
                        note="Active ads on arbeidsplassen.nav.no (NAV), matched by employer organisation number, "
                             "plus ads listed on the verified company website"), metrics
    if any(c["outcome"] == "recheck_failed" for c in checked):
        return evidence("jobs", "source_error", "nav_public_job_feed", FEED_HOME, value=value,
                        note="NAV could not confirm matched ads during this run; none published"), metrics
    return evidence("jobs", "not_found", "nav_public_job_feed", FEED_HOME, value=value,
                    note="No active NAV job ad names this organisation or its registered sub-units"), metrics


class NavDetailClient:
    """Thread-safe NAV feed-entry fetcher sharing one public token."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.feed = NavFeed(timeout=timeout)
        self._lock = threading.Lock()

    def __call__(self, uuid: str) -> dict[str, Any]:
        with self._lock:
            if self.feed.token is None:
                self.feed._refresh_token()
        return self.feed.get(f"/api/v1/feedentry/{uuid}", retries=2)


def catch_up(index: NavJobIndex, feed: NavFeed, next_url: str | None, *, max_pages: int = 10) -> dict[str, Any]:
    """Read feed pages newer than the index, so ads posted since the index was built can be found.

    Newly ACTIVE entries are kept by normalised employer name (the feed index has no organisation
    number); `jobs_for_profile` then confirms the number from each ad itself. Ads that turned
    INACTIVE are dropped from the index.
    """
    pages, latest = 0, {}
    url = next_url
    while url and pages < max_pages:
        page = feed.get(url)
        pages += 1
        items = [compact(item) for item in page.get("items") or []]
        for row in items:
            latest[row["uuid"]] = row
        url = page.get("next_url")
        if len(items) < 1000:
            break
    inactive = {uuid for uuid, row in latest.items() if row.get("status") != "ACTIVE"}
    if inactive:
        index.by_orgnr = {org: [ad for ad in ads if ad["uuid"] not in inactive] for org, ads in index.by_orgnr.items()}
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in latest.values():
        if row.get("status") == "ACTIVE" and row.get("business_name"):
            by_name[norm_name(row["business_name"])].append({"uuid": row["uuid"]})
    index.new_entries_by_name = dict(by_name)
    return {"pages": pages, "new_active": sum(len(v) for v in by_name.values()), "dropped_inactive": len(inactive)}
