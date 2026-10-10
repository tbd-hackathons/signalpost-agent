from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.llm_extract import job_in_context, looks_like_date, page_blocks, verbatim  # noqa: E402


def test_title_must_be_presented_as_a_vacancy():
    text = "I vår egen skole lærer man om selskapet.\nVi søker etter en dyktig og sosial veterinær med oppstart januar 2027."
    assert not job_in_context("Lærer", text, [])
    assert job_in_context("veterinær", text, [])
    assert job_in_context("Prosjektleder elektro", "Om oss\nProsjektleder elektro", ["Prosjektleder elektro"])


def test_page_blocks_keeps_list_items_and_drops_navigation():
    html = "<nav><a>Hjem</a></nav><h2>Ledige stillinger</h2><ul><li>Lagermedarbeider – Bergen</li></ul><p>Tekst.</p>"
    text, standalone = page_blocks(html)
    assert "Lagermedarbeider – Bergen" in standalone and "Hjem" not in text


def test_verbatim_and_dates():
    assert verbatim("Gode kompensasjonsordninger", "GODE kompensasjonsordninger er viktig")
    assert not verbatim("Ny daglig leder", "Gode kompensasjonsordninger")
    assert looks_like_date("17. oktober 2026") and looks_like_date("2026-09-24") and not looks_like_date("i fjor")
