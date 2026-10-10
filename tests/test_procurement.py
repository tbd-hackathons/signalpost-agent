from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.brief import build_brief  # noqa: E402
from norway_company_agent.claims_view import contract_view  # noqa: E402
from norway_company_agent.procurement import ProcurementIndex, procurement_record  # noqa: E402

SHA = "a" * 64


def notice(number, published, *, winners=(), buyers=("916879067",), kind="can-standard"):
    return {"publication_number": number, "published": published, "notice_type": kind, "title": "Vintervedlikehold",
            "url": f"https://ted.europa.eu/en/notice/-/detail/{number}", "buyer_ids": list(buyers),
            "buyer_names": ["Sykehusinnkjøp HF"], "winner_ids": list(winners), "winner_names": ["Vinsnesbakk Elektro AS"],
            "content_sha256": SHA}


def index(*rows):
    by = {}
    for row in rows:
        for n in set(row["buyer_ids"]) | set(row["winner_ids"]):
            by.setdefault(n, []).append(row)
    return ProcurementIndex(by, {"retrieved_at": "2026-10-10T00:00:00+00:00", "query": "buyer-country=NOR AND publication-date>=20240101"})


def profile():
    return {"organisation_number": "912345678", "name": "VINSNESBAKK ELEKTRO AS", "evidence": {
        "locations": {"value": {"locations": [{"organisation_number": "998877665"}]}}}}


def test_award_to_company_or_subunit_is_attached_newest_first():
    idx = index(notice("1-2025", "2025-01-02", winners=("912345678",)), notice("2-2026", "2026-09-01", winners=("998877665",)),
                notice("3-2026", "2026-09-02", winners=("111111111",)))
    record = procurement_record(profile(), idx)
    assert record["status"] == "available"
    assert [n["publication_number"] for n in record["value"]["notices"]] == ["2-2026", "1-2025"]
    assert record["value"]["awards_won"] == 2 and "998877665" in record["value"]["notices"][0]["identity_proof"]


def test_buyer_role_and_no_match_and_missing_snapshot():
    p = profile()
    p["organisation_number"] = "916879067"
    record = procurement_record(p, index(notice("4-2026", "2026-09-03", kind="cn-standard")))
    assert record["value"]["notices"][0]["role"] == "buyer_tender"
    assert procurement_record(profile(), index())["status"] == "not_found"
    assert procurement_record(profile(), None)["status"] == "source_error"


def test_claims_and_brief_cite_the_notice():
    p = profile()
    p["evidence"]["procurement"] = procurement_record(p, index(notice("2-2026", "2026-09-01", winners=("912345678",))))
    view = contract_view(p)
    claim = [c for c in view["claims"] if c["field"] == "public_activity.procurement"][0]
    assert claim["availability"] == "available" and claim["value"]["role"] == "supplier_awarded"
    assert view["evidence"][-1]["source_url"].endswith("2-2026")
    brief = build_brief(p)
    assert any("won 1 public contract" in s["text"] for s in brief["sentences"])
