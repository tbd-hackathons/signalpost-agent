from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.brief import build_brief  # noqa: E402


def test_brief_uses_only_evidence_and_lists_unknowns():
    fin = {"source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/1", "value": {"records": [
        {"period": {"tilDato": "2024-12-31"}, "revenue": 1_000_000.0, "annual_result": 50_000.0, "equity": 200_000.0, "assets": 800_000.0},
        {"period": {"tilDato": "2025-12-31"}, "revenue": 1_500_000.0, "annual_result": 90_000.0, "equity": 300_000.0, "assets": 900_000.0}]}}
    profile = {"organisation_number": "912345678", "name": "VINSNESBAKK ELEKTRO AS", "legal_form": "AS",
               "industry_label": "Elektrisk installasjonsarbeid", "municipality": "SANDNES",
               "evidence": {"financials": fin, "jobs": {"status": "source_error"}}}
    brief = build_brief(profile)
    text = " ".join(s["text"] for s in brief["sentences"])
    assert "grew 50%" in text and "NOK 1.5 m" in text
    assert "revenue growing fast" in brief["signals"] and "profitable last year" in brief["signals"]
    assert "website" in brief["unknown"] and any("job ads" in u for u in brief["unknown"])
    assert all(s["sources"] for s in brief["sentences"][1:])
