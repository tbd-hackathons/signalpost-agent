from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.external_footprint import publishable_observation  # noqa: E402
from norway_company_agent.nav_jobs import NavJobIndex, ad_summary, jobs_for_profile, norm_name  # noqa: E402

TODAY = date(2026, 10, 10)


def detail(uuid, orgnr, status="ACTIVE", expires="2026-11-08T00:00:00+01:00", positions="2"):
    return {"uuid": uuid, "status": status, "sistEndret": "2026-10-09T19:44:50+02:00", "ad_content": {
        "uuid": uuid, "title": "Elektriker", "jobtitle": "Elektriker", "published": "2026-10-01T10:00:00+02:00",
        "expires": expires, "positioncount": positions, "link": f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}",
        "employer": {"name": "VINSNESBAKK ELEKTRO AS", "orgnr": orgnr},
        "contactList": [{"name": "Kari Nordmann", "email": "kari@example.no", "phone": "99999999"}],
        "workLocations": [{"city": "SANDNES", "municipal": "SANDNES", "county": "ROGALAND"}]}}


def profile():
    return {"organisation_number": "912345678", "name": "VINSNESBAKK ELEKTRO AS", "evidence": {
        "locations": {"value": {"locations": [{"organisation_number": "998877665", "name": "VINSNESBAKK ELEKTRO AS AVD SANDNES"}]}}}}


def index(*ads):
    by_orgnr = {}
    for ad in ads:
        by_orgnr.setdefault(ad["employer_orgnr"], []).append(ad)
    return NavJobIndex(by_orgnr, "2026-10-09T00:00:00Z")


def test_norm_name_strips_legal_form():
    assert norm_name("Vinsnesbakk Elektro AS") == norm_name("VINSNESBAKK ELEKTRO") == "vinsnesbakk elektro"


def test_summary_drops_personal_contacts():
    summary = ad_summary(detail("u1", "912345678"))
    assert "kari" not in str(summary).casefold() and "99999999" not in str(summary)
    assert summary["employer_orgnr"] == "912345678"


def test_own_and_subunit_ads_publish_after_live_recheck():
    idx = index(ad_summary(detail("u1", "912345678")), ad_summary(detail("u2", "998877665")))
    live = {"u1": detail("u1", "912345678"), "u2": detail("u2", "998877665")}
    record, metrics = jobs_for_profile(profile(), idx, live.__getitem__, today=TODAY)
    value = record["value"]
    assert record["status"] == "available" and value["active_job_count"] == 2
    assert value["positions_advertised"] == 4 and metrics["requests"] == 2
    assert all(publishable_observation(o) for o in value["observations"])
    assert "sub-unit 998877665" in value["postings"][1]["identity_proof"] or "sub-unit 998877665" in value["postings"][0]["identity_proof"]


def test_closed_expired_or_reassigned_ads_are_not_published():
    idx = index(ad_summary(detail("u1", "912345678")), ad_summary(detail("u2", "912345678")),
                ad_summary(detail("u3", "912345678")))
    live = {"u1": detail("u1", "912345678", status="INACTIVE"),
            "u2": detail("u2", "912345678", expires="2026-10-01T00:00:00+02:00"),
            "u3": detail("u3", "111111111")}
    record, _ = jobs_for_profile(profile(), idx, live.__getitem__, today=TODAY)
    assert record["status"] == "not_found" and record["value"]["active_job_count"] == 0
    assert {c["outcome"] for c in record["value"]["checks"]} == {"no_longer_active", "different_employer"}


def test_new_ad_found_by_name_needs_matching_number():
    idx = index()
    idx.new_entries_by_name = {"vinsnesbakk elektro": [{"uuid": "n1"}, {"uuid": "n2"}]}
    live = {"n1": detail("n1", "912345678"), "n2": detail("n2", "555555555")}
    record, _ = jobs_for_profile(profile(), idx, live.__getitem__, today=TODAY)
    assert [p["uuid"] for p in record["value"]["postings"]] == ["n1"]


def test_missing_index_is_reported_not_zero():
    record, _ = jobs_for_profile(profile(), None, lambda uuid: {}, today=TODAY)
    assert record["status"] == "source_error" and record.get("value") is None


def test_failed_recheck_is_a_source_error_not_zero():
    idx = index(ad_summary(detail("u1", "912345678")))

    def down(uuid):
        raise TimeoutError

    record, _ = jobs_for_profile(profile(), idx, down, today=TODAY)
    assert record["status"] == "source_error" and record["value"]["active_job_count"] == 0


def test_incomplete_index_never_asserts_no_jobs():
    idx = index()
    idx.complete = False
    record, _ = jobs_for_profile(profile(), idx, lambda uuid: {}, today=TODAY)
    assert record["status"] == "source_error"
