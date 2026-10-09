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
