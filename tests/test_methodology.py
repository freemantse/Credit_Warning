"""Tests for src/methodology.py — deterministic SIC classification + LLM fallback.

Pure logic; the LLM fallback is exercised with a stub client so no network/API key
is needed.
"""

import json

import pytest

from src.methodology import (
    MOODYS_METHODOLOGIES,
    MOODYS_INFRASTRUCTURE,
    MOODYS_FINANCIAL,
    SP_SECTORS,
    SP_FINANCIAL_EXTERNAL,
    MOODYS_DEFAULT,
    SP_DEFAULT,
    MethodologyClass,
    classify_methodology,
)


# ── Bucket integrity ─────────────────────────────────────────────────────────

def test_bucket_names_are_unique():
    """No accidental duplicate labels within an agency's list."""
    moodys_all = MOODYS_METHODOLOGIES + MOODYS_INFRASTRUCTURE + MOODYS_FINANCIAL
    assert len(moodys_all) == len(set(moodys_all))
    sp_all = SP_SECTORS + SP_FINANCIAL_EXTERNAL
    assert len(sp_all) == len(set(sp_all))


def test_sp_has_forty_sections():
    """S&P's Sector-Specific Corporate Methodology has exactly 40 numbered sections."""
    assert len(SP_SECTORS) == 40


# ── Deterministic SIC mapping ────────────────────────────────────────────────

@pytest.mark.parametrize("sic,moodys,sp", [
    # Utilities asymmetry: Moody's routes to its Infrastructure group, S&P keeps it
    # inside the corporate document ("Regulated Utilities").
    ("4911", "Regulated Electric and Gas Utilities", "Regulated Utilities"),
    # Oil major / refiner.
    ("2911", "Integrated Oil and Gas", "Refining And Marketing"),
    # Upstream E&P.
    ("1311", "Independent Exploration and Production", "Oil And Gas Exploration And Production"),
    # Software (longest-prefix 7372 overrides the 73 business-services major group).
    ("7372", "Software and Diversified Technology", "Technology Software And Services"),
    # Semiconductors.
    ("3674", "Software and Diversified Technology", "Technology Hardware And Semiconductors"),
    # Auto OEM vs auto supplier (4-digit override of the 37 transport-equipment group).
    ("3711", "Automobile Manufacturers", "Auto And Commercial Vehicle Manufacturing"),
    ("3714", "Automotive Suppliers", "Auto Suppliers"),
    # Restaurants (58) vs general retail.
    ("5812", "Restaurant Industry", "Retail And Restaurants"),
    # Pharma (2834) overrides the 28 chemicals group.
    ("2834", "Pharmaceutical Industry", "Pharmaceuticals"),
    # Specialty chemicals via a 3-digit refinement (285 paints/coatings).
    ("2851", "Chemical Industry", "Specialty Chemicals"),
    # REIT.
    ("6798", "REITs and Other Commercial Real Estate Firms", "Homebuilders And Real Estate Developers"),
])
def test_specific_sic_maps_high_confidence(sic, moodys, sp):
    mc = classify_methodology(sic)
    assert mc.moodys_methodology == moodys
    assert mc.sp_sector == sp
    assert mc.source == "sic_table"
    assert mc.confidence == "high"


def test_two_digit_major_group_is_medium_confidence():
    """A bare 2-digit major-group hit (no 3–4 digit refinement) is medium-confidence."""
    mc = classify_methodology("35")  # industrial machinery → Manufacturing / Capital Goods
    assert mc.moodys_methodology == "Manufacturing"
    assert mc.sp_sector == "Capital Goods"
    assert mc.confidence == "medium"
    assert mc.source == "sic_table"


def test_longest_prefix_wins():
    """3571 (computers, under 357) beats the generic 35 machinery mapping."""
    mc = classify_methodology("3571")
    assert mc.moodys_methodology == "Software and Diversified Technology"
    assert mc.sp_sector == "Technology Hardware And Semiconductors"


# ── Financial-sector flagging ────────────────────────────────────────────────

def test_bank_flagged_financial():
    mc = classify_methodology("6021")  # national commercial banks
    assert mc.is_financial is True
    assert mc.moodys_methodology == "Banks"
    assert mc.sp_sector == "Banks (Financial Institutions criteria)"
    assert "does not apply" in mc.notes


def test_insurer_flagged_financial():
    mc = classify_methodology("6311")  # life insurance
    assert mc.is_financial is True
    assert mc.moodys_methodology == "Insurers"


# ── LLM fallback for ambiguous SICs ──────────────────────────────────────────

class _StubMessage:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]
        self.stop_reason = "end_turn"


class _StubClient:
    """Minimal anthropic-like client returning a canned JSON object."""
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

        class _Messages:
            def __init__(self, outer):
                self._outer = outer

            def create(self, **_):
                self._outer.calls += 1
                return _StubMessage(json.dumps(self._outer._payload))

        self.messages = _Messages(self)


def test_blank_check_sic_uses_llm_fallback():
    stub = _StubClient({"moodys": "Manufacturing", "sp": "Capital Goods"})
    mc = classify_methodology("6770", name="Acme Acquisition Corp", llm_client=stub)
    assert stub.calls == 1
    assert mc.source == "llm_fallback"
    assert mc.confidence == "low"
    assert mc.moodys_methodology == "Manufacturing"
    assert mc.sp_sector == "Capital Goods"


def test_holding_company_67_is_ambiguous():
    """Bare 67 (holding/investment offices) with no recognised 4-digit code → LLM."""
    stub = _StubClient({"moodys": "Business and Consumer Services", "sp": "Business And Consumer Services"})
    mc = classify_methodology("6719", name="Some Holdings Inc", llm_client=stub)
    assert stub.calls == 1
    assert mc.source == "llm_fallback"


def test_recognised_67_code_not_ambiguous():
    """6798 (REIT) and 6726 (asset mgr) are recognised — no LLM call."""
    stub = _StubClient({"moodys": "X", "sp": "Y"})
    mc = classify_methodology("6726", llm_client=stub)
    assert stub.calls == 0
    assert mc.moodys_methodology == "Asset Management"
    assert mc.sp_sector == "Asset Managers"


def test_llm_off_list_label_degrades_to_default():
    stub = _StubClient({"moodys": "Not A Real Bucket", "sp": "Also Fake"})
    mc = classify_methodology("6770", name="Mystery Corp", llm_client=stub)
    assert mc.source == "default"
    assert mc.moodys_methodology == MOODYS_DEFAULT
    assert mc.sp_sector == SP_DEFAULT


def test_missing_sic_without_client_returns_default():
    """No SIC and no LLM client available → flagged low-confidence default, no crash."""
    mc = classify_methodology(None)
    assert mc.source == "default"
    assert mc.confidence == "low"
    assert mc.moodys_methodology == MOODYS_DEFAULT


def test_unmatched_sic_returns_default():
    """A SIC in no table (and not ambiguous) → generic default, low confidence.

    SIC 43 (US Postal Service) is intentionally absent from the mapping.
    """
    mc = classify_methodology("4300")
    assert mc.source == "default"
    assert mc.confidence == "low"


# ── TRBC cross-check ─────────────────────────────────────────────────────────

def test_trbc_agreement_keeps_confidence():
    mc = classify_methodology("4911", trbc="Utilities")
    assert mc.confidence == "high"
    assert "disagree" not in mc.notes


def test_trbc_disagreement_downgrades_confidence():
    """SIC says utility (high), TRBC says Technology → downgrade to medium + note."""
    mc = classify_methodology("4911", trbc="Technology")
    assert mc.confidence == "medium"
    assert "disagree" in mc.notes.lower()


def test_trbc_unmapped_sector_ignored():
    mc = classify_methodology("4911", trbc="Some Unknown Sector")
    assert mc.confidence == "high"


def test_returns_methodology_class():
    assert isinstance(classify_methodology("4911"), MethodologyClass)
