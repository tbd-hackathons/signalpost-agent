from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent import site_discovery  # noqa: E402
from norway_company_agent.site_candidates import (  # noqa: E402
    contact_proof, email_domain, guessed_domains, place_proof, website_candidates,
)


def profile(name="VINSNESBAKK ELEKTRO AS", org="912345678", **raw):
    row = {"navn": name, "organisasjonsnummer": org, "hjemmeside": "", "epostadresse": "",
           "telefon": "", "mobil": "", "forretningsadresse.adresse": "Industriveien 4",
           "forretningsadresse.poststed": "SANDNES", "forretningsadresse.kommune": "SANDNES"}
    row.update(raw)
    return {"organisation_number": org, "name": name, "website": row["hjemmeside"],
            "evidence": {"registry": {"value": row}}}


def site(text, title="", url="https://example.no/"):
    return {"status": "available", "source_url": url,
            "value": {"final_url": url, "title": title, "main_text_excerpt": text, "pages": [],
                      "discovered_social_links": [{"platform": "facebook", "url": "https://facebook.com/vinsnesbakkelektro"}]}}


def test_email_domain_and_free_mail():
    assert email_domain("Post@Firma.no") == "firma.no"
    assert email_domain("nonsense") is None
    p = profile(epostadresse="ola@gmail.com")
    assert website_candidates(p, Counter(), guess=False) == []


def test_shared_email_domain_is_strict():
    p = profile(epostadresse="post@regnskapshuset.no")
    cands = website_candidates(p, Counter({"regnskapshuset.no": 40}), guess=False)
    assert cands[0]["source"] == "registry_email_domain_shared" and cands[0]["requires_strict_proof"]


def test_company_email_domain_is_not_strict():
    p = profile(epostadresse="post@vinsnesbakkelektro.no")
    cands = website_candidates(p, Counter({"vinsnesbakkelektro.no": 1}), guess=False)
    assert cands[0]["source"] == "registry_email_domain" and not cands[0]["requires_strict_proof"]


def test_guessed_domains_skip_long_and_generic_names():
    assert "vinsnesbakkelektro.no" in guessed_domains("VINSNESBAKK ELEKTRO AS")
    assert guessed_domains("EIENDOM AS") == []
    assert guessed_domains("A B C D E F AS") == []


def test_contact_and_place_proof():
    p = profile(epostadresse="post@vinsnesbakk.no", telefon="+47 51 23 45 67")
    assert contact_proof(p, "Kontakt oss: post@vinsnesbakk.no")
    assert contact_proof(p, "Ring 51 23 45 67")
    assert contact_proof(p, "Ring 99 88 77 66") is None
    assert place_proof(p, "Vinsnesbakk Elektro, Sandnes")
    assert place_proof(p, "Vinsnesbakk Elektro, Bergen") is None


def test_place_proof_needs_the_whole_legal_name():
    """ARKWRIGHT X AS must not publish on "Arkwright Consulting ... Oslo": one shared word and a town is
    not an identity. The full name, short tokens included, has to appear before a place proves anything."""
    p = profile(name="ARKWRIGHT X AS", org="813302632",
                forretningsadresse={"poststed": "OSLO"},
                **{"forretningsadresse.poststed": "OSLO", "forretningsadresse.kommune": "OSLO",
                   "forretningsadresse.adresse": "Storgata 1", "forretningsadresse.postnummer": "0155"})
    consultancy = ("Arkwright is a leading strategy and management consulting firm offering expertise in "
                   "business transformation, with offices in Hamburg, London, Oslo and Stockholm.")
    assert place_proof(p, consultancy) is None
    # The company's own site, naming the entity as it is registered, still passes.
    assert place_proof(p, "Arkwright X AS, Storgata 1, 0155 Oslo")


def test_guessed_domain_needs_more_than_a_name(monkeypatch):
    p = profile()
    monkeypatch.setattr(site_discovery, "website_candidates", lambda *a, **k: [
        {"url": "https://vinsnesbakkelektro.no/", "host": "vinsnesbakkelektro.no",
         "source": "legal_name_domain_guess", "reason": "guess", "requires_strict_proof": True}])
    # Name only, different town: must not publish.
    monkeypatch.setattr(site_discovery, "fetch_website",
                        lambda url=None, **k: (site("Vinsnesbakk Elektro i Bergen leverer elektro." * 3, title="Vinsnesbakk Elektro"), {"requests": 1}) if url else ({"status": "not_found"}, {}))
    record, _ = site_discovery.resolve_website(p, Counter())
    assert not record["value"]["identity_assessment"]["publishable"]
    assert record["value"]["social_links"] == []
    # Name plus registered town: publish.
    monkeypatch.setattr(site_discovery, "fetch_website",
                        lambda url=None, **k: (site("Vinsnesbakk Elektro i Sandnes leverer elektro." * 3, title="Vinsnesbakk Elektro"), {"requests": 1}))
    record, _ = site_discovery.resolve_website(p, Counter())
    assert record["value"]["identity_assessment"]["publishable"]
    assert record["value"]["discovery"]["selected"]["source"] == "legal_name_domain_guess"


def test_guessed_domain_with_org_number_publishes(monkeypatch):
    p = profile(name="SPIND EIENDOM AS", org="987654321")
    monkeypatch.setattr(site_discovery, "website_candidates", lambda *a, **k: [
        {"url": "https://spind.no/", "host": "spind.no", "source": "legal_name_domain_guess",
         "reason": "guess", "requires_strict_proof": True}])
    monkeypatch.setattr(site_discovery, "fetch_website",
                        lambda url=None, **k: (site("Spind Eiendom AS, org.nr 987 654 321"), {"requests": 1}))
    record, _ = site_discovery.resolve_website(p, Counter())
    assert record["value"]["identity_assessment"]["publishable"]


def test_handles_named_after_verified_domain_publish():
    value = {"registered_domain": "glaame.no", "social_link_assessments": [
        {"platform": "facebook", "url": "https://facebook.com/glaame.no", "publishable": False},
        {"platform": "facebook", "url": "https://facebook.com/someoneelse", "publishable": False}]}
    site_discovery._accept_domain_handles(value, "GLÅME AS")
    assert value["social_links"] == [{"platform": "facebook", "url": "https://facebook.com/glaame.no"}]


def test_manager_domain_handles_are_not_credited_to_the_client():
    value = {"registered_domain": "bonord.no", "social_link_assessments": [
        {"platform": "linkedin", "url": "https://linkedin.com/company/bonord", "publishable": False}]}
    site_discovery._accept_domain_handles(value, "ALASKASVINGEN BORETTSLAG")
    assert not value.get("social_links")


def test_handle_with_distinctive_legal_name_word_publishes_but_parent_handle_does_not():
    value = {"registered_domain": "gresseth.no", "social_link_assessments": [
        {"platform": "facebook", "url": "https://facebook.com/ByggmesterGresseth", "publishable": False},
        {"platform": "facebook", "url": "https://facebook.com/veidekke", "publishable": False},
        {"platform": "facebook", "url": "https://facebook.com/byggmesterforbundet", "publishable": False}]}
    site_discovery._accept_domain_handles(value, "BYGGMESTER IVER R. GRESSETH AS")
    assert value["social_links"] == [{"platform": "facebook", "url": "https://facebook.com/ByggmesterGresseth"}]


def test_channel_id_link_publishes_only_from_a_site_named_after_the_company():
    link = {"platform": "youtube", "url": "https://youtube.com/channel/UCiZnrzRl72p7wpDma5v6lgQ", "publishable": False}
    own = {"registered_domain": "llentab.no", "social_link_assessments": [dict(link)]}
    site_discovery._accept_domain_handles(own, "LLENTAB AS")
    assert own["social_links"] == [{"platform": "youtube", "url": link["url"]}]
    parent = {"registered_domain": "akvagroup.com", "social_link_assessments": [dict(link)]}
    site_discovery._accept_domain_handles(parent, "SPERRE AS")
    assert not parent.get("social_links")


def test_wikidata_site_is_tried_before_name_guesses(monkeypatch):
    p = profile()
    monkeypatch.setattr(site_discovery, "website_candidates", lambda *a, **k: [
        {"url": "https://vinsnesbakkelektro.no/", "host": "vinsnesbakkelektro.no",
         "source": "legal_name_domain_guess", "reason": "guess", "requires_strict_proof": True}])
    tried = []
    monkeypatch.setattr(site_discovery, "fetch_website",
                        lambda url=None, **k: (tried.append(url), ({"status": "not_found"}, {}))[1])
    site_discovery.resolve_website(p, Counter(), extra_websites=["https://www.vbe.no"])
    assert tried[0] == "https://www.vbe.no"


def test_town_inside_the_name_is_not_place_proof():
    p = profile(name="OSLO SCIENCE CITY", **{"forretningsadresse.poststed": "OSLO", "forretningsadresse.kommune": "OSLO",
                                             "forretningsadresse.adresse": "Gaustadalléen 21", "forretningsadresse.postnummer": "0349"})
    assert place_proof(p, "Oslo Science City – fremtiden skapes i Oslo") is None
    assert place_proof(p, "Oslo Science City, Gaustadalléen 21, 0349 Oslo")


def test_deeper_proof_reads_contact_page_for_org_number(monkeypatch):
    from norway_company_agent import site_intel
    from norway_company_agent.site_discovery import _deeper_proof

    class FakeFetcher:
        requests, bytes, latencies_ms = 0, 0, []

        def get(self, url):
            self.requests += 1
            body = b"<html><body>Friluftscamp AS - org.nr. 912 345 678</body></html>" if "om-oss" in url else b"<html></html>"
            return body, url, "text/html"

    monkeypatch.setattr(site_intel, "SiteFetcher", FakeFetcher)
    profile = {"organisation_number": "912345678", "name": "FRILUFTSCAMP AS", "evidence": {}}
    value = {"final_url": "https://friluftscamp.no/", "site_links": [{"url": "https://friluftscamp.no/om-oss/", "text": "Om oss"}]}
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    assert "organisation number" in _deeper_proof(profile, value, metrics)
    assert metrics["requests"] == 1
    profile["organisation_number"] = "987654321"
    assert _deeper_proof(profile, value, metrics) is None


def test_declared_site_that_is_not_ours_is_published_as_related(monkeypatch):
    """The registry records a website that turns out to be the housing co-operative's manager. The link is
    real, so it must be reported as a related site rather than dropped."""
    p = profile(name="BORETTSLAGET VIKENSGATE 20")
    monkeypatch.setattr(site_discovery, "website_candidates", lambda *a, **k: [
        {"url": "https://vestbo.no/", "host": "vestbo.no", "source": "registry_website",
         "reason": "registry website field", "requires_strict_proof": False}])
    record = {"status": "available", "source_url": "https://vestbo.no/", "retrieved_at": "2026-10-11T00:00:00Z",
              "content_sha256": "a" * 64,
              "value": {"final_url": "https://vestbo.no/", "registered_domain": "vestbo.no", "title": "Vestbo",
                        "main_text_excerpt": "Vi forvalter borettslag og sameier i Bergen.", "pages": [],
                        "social_links": [{"platform": "facebook", "url": "https://facebook.com/vestbo"}]}}
    monkeypatch.setattr(site_discovery, "fetch_website",
                        lambda url, **k: (record, {"requests": 1, "bytes": 1, "latencies_ms": [1]}))
    monkeypatch.setattr(site_discovery, "apply_website_identity_gate", lambda profile, rec: {
        "website": rec, "assessment": {"status": "related_or_uncertain", "score": 0.3, "publishable": False,
                                       "reasons": ["registry-linked URL lacks strong exact-entity identity evidence"]}})
    result, _ = site_discovery.resolve_website(p, Counter())
    assert result["status"] == "available"
    assert result["value"]["related_only"] is True
    assert result["value"]["social_links"] == []
    assert any("does not present itself as this company's own" in r for r in result["value"]["identity_assessment"]["reasons"])


def test_a_guessed_site_that_is_not_ours_is_still_dropped(monkeypatch):
    """Only a site the registry declares earns the related label. A rejected name guess is just wrong."""
    p = profile(name="VINSNESBAKK ELEKTRO AS")
    monkeypatch.setattr(site_discovery, "website_candidates", lambda *a, **k: [
        {"url": "https://vinsnesbakk.no/", "host": "vinsnesbakk.no", "source": "legal_name_domain_guess",
         "reason": "guess", "requires_strict_proof": True}])
    record = {"status": "available", "source_url": "https://vinsnesbakk.no/", "retrieved_at": "2026-10-11T00:00:00Z",
              "content_sha256": "b" * 64,
              "value": {"final_url": "https://vinsnesbakk.no/", "registered_domain": "vinsnesbakk.no",
                        "title": "Vinsnesbakk", "main_text_excerpt": "noe annet", "pages": []}}
    monkeypatch.setattr(site_discovery, "fetch_website",
                        lambda url, **k: (record, {"requests": 1, "bytes": 1, "latencies_ms": [1]}))
    monkeypatch.setattr(site_discovery, "apply_website_identity_gate", lambda profile, rec: {
        "website": rec, "assessment": {"status": "related_or_uncertain", "score": 0.3, "publishable": False,
                                       "reasons": ["guessed domain needs proof"]}})
    result, _ = site_discovery.resolve_website(p, Counter())
    assert not result.get("value", {}).get("related_only")


def test_declared_site_redirecting_to_a_parking_page_is_not_a_related_site(monkeypatch):
    """ALCE SPRINKLER AS: the registry website now redirects to the registrar's parking page. That is no
    relationship at all, so it must not be published as a manager, parent or group site."""
    p = profile(name="ALCE SPRINKLER AS")
    monkeypatch.setattr(site_discovery, "website_candidates", lambda *a, **k: [
        {"url": "https://alcesprinkler.no/", "host": "alcesprinkler.no", "source": "registry_website",
         "reason": "registry website field", "requires_strict_proof": False}])
    record = {"status": "available", "source_url": "https://www.webhuset.no/parkering",
              "retrieved_at": "2026-10-11T00:00:00Z", "content_sha256": "c" * 64,
              "value": {"final_url": "https://www.webhuset.no/parkering", "registered_domain": "webhuset.no",
                        "title": "Domene er parkert | Webhuset", "main_text_excerpt": "Dette domenet er parkert.",
                        "pages": []}}
    monkeypatch.setattr(site_discovery, "fetch_website",
                        lambda url, **k: (record, {"requests": 1, "bytes": 1, "latencies_ms": [1]}))
    monkeypatch.setattr(site_discovery, "apply_website_identity_gate", lambda profile, rec: {
        "website": rec, "assessment": {"status": "related_or_uncertain", "score": 0.3, "publishable": False,
                                       "reasons": ["registry-linked URL lacks strong exact-entity identity evidence"]}})
    result, _ = site_discovery.resolve_website(p, Counter())
    assert not result.get("value", {}).get("related_only")


def test_parked_page_on_the_declared_domain_is_not_a_related_site():
    cand = {"host": "example.no"}
    assert not site_discovery._related_link_holds(cand, {"final_url": "https://example.no/", "title": "Domenet er til salgs"})
    assert site_discovery._related_link_holds(cand, {"final_url": "https://www.example.no/", "title": "Vestbo forvaltning"})
    # Ordinary pages that sell something are not "domain for sale" pages (SVØMMEGLEDE AS, pirayaswim.no).
    assert site_discovery._related_link_holds(cand, {"final_url": "https://example.no/", "title": "Startside",
        "main_text_excerpt": "Vi har alltid med svømmebriller og badehetter til salgs i svømmehallen."})
    assert not site_discovery._related_link_holds(cand, {"final_url": "http://www.example.no/", "title": "www.example.no is parked"})
