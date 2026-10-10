from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.brief import build_brief  # noqa: E402
from norway_company_agent.claims_view import contract_view  # noqa: E402


def profile(name, url, domain, title, score=0.8):
    return {"organisation_number": "912345678", "name": name, "legal_form": "AS", "evidence": {"website": {
        "status": "available", "source_url": url, "retrieved_at": "2026-10-10T00:00:00Z", "content_sha256": "c" * 64,
        "value": {"final_url": url, "registered_domain": domain, "title": title,
                  "social_links": [{"platform": "facebook", "url": "https://facebook.com/thongruppen"}],
                  "identity_assessment": {"publishable": True, "score": score,
                                          "reasons": ["registry-linked URL lacks strong exact-entity identity evidence"]}}}}}


def test_parent_site_is_related_not_official_and_its_socials_are_dropped():
    view = contract_view(profile("SARTOR STORSENTER AS", "https://thon.no/", "thon.no", "Thon Gruppen – Bolig, eiendom"))
    fields = {c["field"]: c for c in view["claims"]}
    assert fields["related_website"]["value"]["url"] == "https://thon.no/"
    assert fields["official_website"]["availability"] == "ambiguous"
    assert not any(c["field"].startswith("social_profile") for c in view["claims"])
    brief = build_brief(profile("SARTOR STORSENTER AS", "https://thon.no/", "thon.no", "Thon Gruppen"))
    assert "own website" in brief["unknown"] and "part of a group or managed" in brief["signals"]


def test_own_site_by_title_word_stays_official():
    view = contract_view(profile("YKO AS", "https://yko.no/", "yko.no", "YKO AS"))
    fields = {c["field"]: c for c in view["claims"]}
    assert fields["official_website"]["availability"] == "available" and "related_website" not in fields
    assert any(c["field"] == "social_profile.facebook" for c in view["claims"])
