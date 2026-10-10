from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.changes import changes_since  # noqa: E402


def profile(job_urls, retrieved="2026-10-10T00:00:00Z"):
    return {"organisation_number": "912345678", "name": "X AS", "evidence": {
        "jobs": {"status": "available", "retrieved_at": retrieved, "content_sha256": retrieved,
                 "value": {"postings": [{"url": u, "retrieved_at": retrieved} for u in job_urls]}}}}


def test_refetch_with_new_timestamp_is_not_a_change():
    assert changes_since(profile(["a"]), profile(["a"], "2026-10-11T00:00:00Z")) == []


def test_new_and_closed_ads_are_reported_with_prior_fingerprint():
    change = changes_since(profile(["a", "b"]), profile(["b", "c"], "2026-10-11T00:00:00Z"))[0]
    assert change["field"] == "jobs.active_ads"
    assert change["old_value"] == ["a"] and change["new_value"] == ["c"]
    assert change["old_content_sha256"] == "2026-10-10T00:00:00Z"


def test_unchecked_jobs_make_no_claim():
    later = copy.deepcopy(profile(["a"]))
    later["evidence"]["jobs"] = {"status": "source_error"}
    assert changes_since(profile(["a"]), later) == []
