"""
Tests for the sector-gated intangibles-capex rule in capex_total (src/extract.py) —
the first sector-conditional logic in src/. Covers: in-sector ADDS capitalized
software, out-of-sector does NOT, missing row DEGRADES to ungated, and the
confidence/source policy (untrusted sector does not fire). Pure, no network.
"""
import pytest

import src.extract as extract
from src.extract import capex_total, revenue_value, _revenue_tag_order
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


# ── Utility construction-in-progress gate (fix (d)) ─────────────────────────────
# GATE SHAPE DIFFERS from intangibles: PaymentsForConstructionInProcess is a
# sector-conditional PRIMARY-PREFERRED alternative (used INSTEAD OF the normal capex
# list for utilities, never summed on top), and stays entirely out of the chain for
# non-utilities. Companyfacts with own-use PP&E $100M AND a construction line $500M
# (the utility-comprehensive figure that CONTAINS the PP&E subset, e.g. AEP-shaped).
UTIL_FACTS = {
    "cik": 4904,
    "facts": {"us-gaap": {
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
            {"end": PERIOD, "val": 100_000_000, "filed": "2025-02-01", "form": "10-K"}]}},
        "PaymentsForConstructionInProcess": {"units": {"USD": [
            {"end": PERIOD, "val": 500_000_000, "filed": "2025-02-01", "form": "10-K"}]}},
    }},
}


def _usec(moodys, confidence="high", source="sic_table"):
    return Sector(cik="0000004904", moodys_methodology=moodys, sp_sector="",
                  is_financial=False, confidence=confidence, source=source,
                  classifier_commit="test")


def test_utility_gate_prefers_construction_as_primary(monkeypatch):
    # Trusted utility → construction is the comprehensive capex line → used INSTEAD OF
    # the normal PP&E primary (NOT summed: total is the construction figure alone, and
    # PP&E is NOT double-counted on top of it).
    _patch_sector(monkeypatch, _usec("Regulated Electric and Gas Utilities"))
    total, inputs, tags = capex_total(UTIL_FACTS, PERIOD)
    assert total == 500_000_000                       # construction alone, PP&E not added
    assert inputs["capex_ppe"] == 500_000_000         # component P is the construction line
    assert inputs["capex_intangibles"] == 0.0
    assert tags["sector_gated"] == "Regulated Electric and Gas Utilities"
    # tag provenance points at the construction line, not PP&E
    assert tags["capex_ppe"] == "us-gaap/PaymentsForConstructionInProcess"


def test_utility_gate_covers_all_infra_buckets(monkeypatch):
    # All three MOODYS_INFRASTRUCTURE buckets open the gate.
    for bucket in ("Regulated Electric and Gas Utilities",
                   "Unregulated Utilities and Power Companies",
                   "Regulated Electric and Gas Networks"):
        _patch_sector(monkeypatch, _usec(bucket))
        total, inputs, tags = capex_total(UTIL_FACTS, PERIOD)
        assert total == 500_000_000, bucket
        assert tags["sector_gated"] == bucket


def test_utility_gate_falls_back_when_no_construction_tag(monkeypatch):
    # Utility that did NOT tag construction → P falls back to the normal capex list,
    # no gate provenance recorded.
    _patch_sector(monkeypatch, _usec("Regulated Electric and Gas Utilities"))
    total, inputs, tags = capex_total(FACTS, PERIOD)   # FACTS has no construction tag
    assert inputs["capex_ppe"] == 100_000_000          # normal PP&E primary
    assert "sector_gated" not in tags


def test_non_utility_ignores_construction_tag(monkeypatch):
    # Shipping (Matson) → construction is a small ADD-ON there, kept OUT of the chain →
    # byte-identical to the ungated behavior (normal PP&E primary, construction ignored).
    _patch_sector(monkeypatch, _usec("Shipping"))
    total, inputs, tags = capex_total(UTIL_FACTS, PERIOD)
    assert total == 100_000_000                        # PP&E only; construction NOT used
    assert inputs["capex_ppe"] == 100_000_000
    assert "sector_gated" not in tags
    assert tags["capex_ppe"] == "us-gaap/PaymentsToAcquirePropertyPlantAndEquipment"


def test_utility_gate_missing_row_degrades(monkeypatch):
    # No routing row → gate cannot apply → normal primary, construction ignored, no error.
    _patch_sector(monkeypatch, None)
    total, inputs, tags = capex_total(UTIL_FACTS, PERIOD)
    assert total == 100_000_000
    assert "sector_gated" not in tags


def test_utility_gate_untrusted_does_not_fire(monkeypatch):
    # In-set bucket but untrusted (low/llm_fallback) → gate does not fire → normal primary.
    _patch_sector(monkeypatch, _usec("Regulated Electric and Gas Utilities",
                                     confidence="low", source="llm_fallback"))
    total, inputs, tags = capex_total(UTIL_FACTS, PERIOD)
    assert total == 100_000_000
    assert "sector_gated" not in tags


# ── Revenue tag-ordering gate (fix (b)) ─────────────────────────────────────────
# A THIRD gate shape: a first-match ORDERING preference, not a component. us-gaap/Revenues
# sits at position #2 of the revenue list. For SPECIFIC_TOPLINE_SECTORS (retail/industrial
# buckets where Revenues is a minor "other revenue" line) it is DEMOTED so the specific
# sales tag wins; everyone else keeps the current order. Facts: a minor Revenues line
# ($100M) alongside the true top-line SalesRevenueNet ($900M), Caleres/Apogee-shaped.
REV_FACTS = {
    "cik": 14707,   # placeholder; the sector is monkeypatched, not looked up by cik
    "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            {"end": PERIOD, "val": 100_000_000, "filed": "2025-02-01", "form": "10-K"}]}},
        "SalesRevenueNet": {"units": {"USD": [
            {"end": PERIOD, "val": 900_000_000, "filed": "2025-02-01", "form": "10-K"}]}},
    }},
}


def _rsec(moodys, cik="0000014707", confidence="medium", source="sic_table"):
    return Sector(cik=cik, moodys_methodology=moodys, sp_sector="",
                  is_financial=False, confidence=confidence, source=source,
                  classifier_commit="test")


def test_revenue_specific_topline_sector_demotes(monkeypatch):
    # Retail and Apparel (trusted) → Revenues demoted → the specific SalesRevenueNet wins.
    _patch_sector(monkeypatch, _rsec("Retail and Apparel"))
    val, tag = revenue_value(REV_FACTS, PERIOD)
    assert val == 900_000_000
    assert tag == "us-gaap/SalesRevenueNet"
    # Revenues is pushed to the very end of the resolution order.
    assert _revenue_tag_order(REV_FACTS)[-1] == "us-gaap/Revenues"


def test_revenue_building_materials_also_demotes(monkeypatch):
    # Building Materials (Apogee's bucket) is the other demote bucket.
    _patch_sector(monkeypatch, _rsec("Building Materials"))
    val, tag = revenue_value(REV_FACTS, PERIOD)
    assert val == 900_000_000


def test_revenue_comprehensive_sector_keeps_revenues(monkeypatch):
    # Insurers → NOT a demote bucket → current order → Revenues (position #2) wins.
    _patch_sector(monkeypatch, _rsec("Insurers"))
    val, tag = revenue_value(REV_FACTS, PERIOD)
    assert val == 100_000_000
    assert tag == "us-gaap/Revenues"


def test_revenue_carveout_wins_over_bucket(monkeypatch):
    # cik in REVENUE_COMPREHENSIVE_OVERRIDE (Dillard's) even though its bucket is a demote
    # bucket → carve-out pins comprehensive → Revenues still wins.
    dillards = dict(REV_FACTS, cik=28917)   # 0000028917 is in the override
    _patch_sector(monkeypatch, _rsec("Retail and Apparel", cik="0000028917"))
    val, tag = revenue_value(dillards, PERIOD)
    assert val == 100_000_000
    assert tag == "us-gaap/Revenues"


def test_revenue_missing_sector_degrades(monkeypatch):
    # No routing row → current order → Revenues wins (byte-identical to ungated).
    _patch_sector(monkeypatch, None)
    val, tag = revenue_value(REV_FACTS, PERIOD)
    assert val == 100_000_000
    assert tag == "us-gaap/Revenues"


def test_revenue_untrusted_sector_does_not_demote(monkeypatch):
    # Demote bucket but untrusted (low/llm_fallback) → gate does not fire → Revenues wins.
    _patch_sector(monkeypatch, _rsec("Retail and Apparel", confidence="low", source="llm_fallback"))
    val, tag = revenue_value(REV_FACTS, PERIOD)
    assert val == 100_000_000
    assert tag == "us-gaap/Revenues"
