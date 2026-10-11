import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("scorecard", Path(__file__).resolve().parents[1] / "scripts" / "scorecard.py")
scorecard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scorecard)

CLAIM = {"field": "public_activity.procurement", "value": {
    "role": "supplier_awarded", "title": "Purchase of vehicles for Ullensvang municipality",
    "url": "https://ted.europa.eu/en/notice/-/detail/478787-2025"}}
PROFILE = {"organisation_number": "979459114", "evidence": {}}


def notice(winners, title="Norway – Motor vehicles – Purchase of vehicles for Ullensvang municipality"):
    return {"publication-number": "478787-2025", "notice-title": {"eng": title}, "winner-identifier": winners}


def test_procurement_claim_verified_from_ted_api(monkeypatch):
    monkeypatch.setattr(scorecard, "_ted_notice", lambda number: notice(["979459114"]))
    assert scorecard.audit_procurement(PROFILE, CLAIM, {})["result"] == "verified"


def test_procurement_claim_fails_when_notice_names_another_winner(monkeypatch):
    monkeypatch.setattr(scorecard, "_ted_notice", lambda number: notice(["999999999"]))
    assert scorecard.audit_procurement(PROFILE, CLAIM, {})["result"] == "not_linked"


def test_procurement_claim_flags_a_different_title(monkeypatch):
    monkeypatch.setattr(scorecard, "_ted_notice", lambda number: notice(["979459114"], "Snow clearing"))
    assert scorecard.audit_procurement(PROFILE, CLAIM, {})["result"] == "changed"
