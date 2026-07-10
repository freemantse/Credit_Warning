"""
Tests for the sector-gated intangibles-capex rule in capex_total (src/extract.py) —
the first sector-conditional logic in src/. Covers: in-sector ADDS capitalized
software, out-of-sector does NOT, missing row DEGRADES to ungated, and the
confidence/source policy (untrusted sector does not fire). Pure, no network.
"""
import pytest

import src.extract as extract
from src.extract import capex_total
from src.sector_routing import Sector

PERIOD = "2024-12-31"

# Minimal companyfacts: own-use PP&E $100M + capitalized-software intangibles $40M.
FACTS = {
    "cik": 12345,
    "facts": {"us-gaap": {
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
            {"end": PERIOD, "val": 100_000_000, "filed": "2025-02-01", "form": "10-K"}]}},
        "PaymentsToAcquireIntangibleAssets": {"units": {"USD": [
            {"end": PERIOD, "val": 40_000_000, "filed": "2025-02-01", "form": "10-K"}]}},
    }},
}


def _sec(moodys, confidence="high", source="sic_table"):
    return Sector(cik="0000012345", moodys_methodology=moodys, sp_sector="",
                  is_financial=False, confidence=confidence, source=source,
                  classifier_commit="test")


def _patch_sector(monkeypatch, sector):
    monkeypatch.setattr(extract, "get_sector", lambda cik: sector)


def test_in_sector_adds_intangibles(monkeypatch):
    # Software (trusted) → PaymentsToAcquireIntangibleAssets is capitalized software → summed.
    _patch_sector(monkeypatch, _sec("Software"))
    total, inputs, tags = capex_total(FACTS, PERIOD)
    assert total == 140_000_000
    assert inputs["capex_ppe"] == 100_000_000
    assert inputs["capex_intangibles"] == 40_000_000
    assert tags["sector_gated"] == "Software"           # provenance recorded
    assert "capex_intangibles" in tags


def test_out_of_sector_does_not_add(monkeypatch):
    # Pharmaceuticals → the tag is acquired IP, NOT capex → NOT summed (PP&E only).
    _patch_sector(monkeypatch, _sec("Pharmaceuticals"))
    total, inputs, tags = capex_total(FACTS, PERIOD)
    assert total == 100_000_000
    assert inputs["capex_intangibles"] == 0.0
    assert "sector_gated" not in tags


def test_missing_row_degrades_to_ungated(monkeypatch):
    # No routing row → gate cannot apply → current (ungated) behavior, no error.
    _patch_sector(monkeypatch, None)
    total, inputs, tags = capex_total(FACTS, PERIOD)
    assert total == 100_000_000
    assert inputs["capex_intangibles"] == 0.0
    assert "sector_gated" not in tags


def test_untrusted_sector_does_not_fire(monkeypatch):
    # In-set bucket but low-confidence / llm_fallback → NOT trusted → gate does not fire.
    _patch_sector(monkeypatch, _sec("Software", confidence="low", source="llm_fallback"))
    total, inputs, tags = capex_total(FACTS, PERIOD)
    assert total == 100_000_000
    assert "sector_gated" not in tags
