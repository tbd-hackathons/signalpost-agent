from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.external_footprint import publishable_observation  # noqa: E402
from norway_company_agent.search_enrich import (distinctive_name, identity_in_text, news_observation,  # noqa: E402
                                                queries_for, website_hits)


def profile(name="VINSNESBAKK ELEKTRO AS", form="AS"):
    return {"organisation_number": "912345678", "name": name, "legal_form": form, "municipality": "SANDNES",
            "evidence": {"registry": {"value": {"business_address": {"poststed": "HOMMERSÅK", "kommune": "SANDNES"}}}}}


def test_queries_skip_generic_names_for_news():
    assert [q["kind"] for q in queries_for(profile(), want_website=True, want_news=True)] == ["web", "news"]
    assert [q["kind"] for q in queries_for(profile("BYGG AS"), want_website=True, want_news=True)] == ["web"]
    assert queries_for(profile(), want_website=True, want_news=False)[0]["query"] == '"VINSNESBAKK ELEKTRO" Sandnes'
    assert not distinctive_name("Bygg AS") and distinctive_name("Biosort AS") is False and distinctive_name("Vinsnesbakk AS")


def test_directories_are_not_website_candidates():
    hits = [{"url": "https://www.proff.no/selskap/x"}, {"url": "https://vinsnesbakk.no/om-oss"},
            {"url": "https://vinsnesbakk.no/kontakt"}, {"url": "https://m.facebook.com/x"}]
    assert website_hits(hits) == ["https://vinsnesbakk.no/om-oss"]


def test_article_needs_number_or_full_name_with_place():
    p = profile()
    assert identity_in_text(p, "Vinsnesbakk Elektro AS (org.nr. 912 345 678) vant kontrakten.")
    assert identity_in_text(p, "Vinsnesbakk Elektro AS i Hommersåk ansetter fem nye.")
    assert identity_in_text(p, "Vinsnesbakk Elektro AS ansetter fem nye.") is None  # no place
    assert identity_in_text(p, "Vinsnesbakk Elektro i Sandnes ansetter.") is None  # legal form missing
    assert identity_in_text(profile("BYGG AS"), "Bygg AS i Sandnes vokser.") is None  # generic name


def test_news_observation_is_publishable():
    mention = {"url": "https://www.aftenbladet.no/a/1", "title": "Ansetter", "published": "2026-10-01", "retrieved_at": "2026-10-10T00:00:00Z",
               "content_sha256": "b" * 64, "identity_proof": "number", "evidence_span": "Vinsnesbakk Elektro AS ansetter."}
    assert publishable_observation(news_observation(profile(), mention))
