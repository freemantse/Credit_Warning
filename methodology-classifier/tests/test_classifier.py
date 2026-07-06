"""Tests for the deterministic SIC classification + LLM fallback. Stdlib + pytest only."""

import json

import pytest

from methodology_classifier import (
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
    moodys_all = MOODYS_METHODOLOGIES + MOODYS_INFRASTRUCTURE + MOODYS_FINANCIAL
    assert len(moodys_all) == len(set(moodys_all))
    sp_all = SP_SECTORS + SP_FINANCIAL_EXTERNAL
    assert len(sp_all) == len(set(sp_all))


def test_sp_has_forty_sections():
    assert len(SP_SECTORS) == 40


# ── Deterministic SIC mapping ────────────────────────────────────────────────

@pytest.mark.parametrize("sic,moodys,sp", [
    ("4911", "Regulated Electric and Gas Utilities", "Regulated Utilities"),
    ("2911", "Integrated Oil and Gas", "Refining And Marketing"),
    ("1311", "Independent Exploration and Production", "Oil And Gas Exploration And Production"),
    ("7372", "Software and Diversified Technology", "Technology Software And Services"),
    ("3674", "Software and Diversified Technology", "Technology Hardware And Semiconductors"),
    ("3711", "Automobile Manufacturers", "Auto And Commercial Vehicle Manufacturing"),
    ("3714", "Automotive Suppliers", "Auto Suppliers"),
    ("5812", "Restaurant Industry", "Retail And Restaurants"),
    ("2834", "Pharmaceutical Industry", "Pharmaceuticals"),
    ("2851", "Chemical Industry", "Specialty Chemicals"),
    ("6798", "REITs and Other Commercial Real Estate Firms", "Homebuilders And Real Estate Developers"),
])
def test_specific_sic_maps_high_confidence(sic, moodys, sp):
    mc = classify_methodology(sic)
    assert mc.moodys_methodology == moodys
    assert mc.sp_sector == sp
    assert mc.source == "sic_table"
    assert mc.confidence == "high"


def test_two_digit_major_group_is_medium_confidence():
    mc = classify_methodology("35")
    assert mc.moodys_methodology == "Manufacturing"
    assert mc.sp_sector == "Capital Goods"
    assert mc.confidence == "medium"


def test_longest_prefix_wins():
    mc = classify_methodology("3571")
    assert mc.moodys_methodology == "Software and Diversified Technology"
    assert mc.sp_sector == "Technology Hardware And Semiconductors"


# ── Financial-sector flagging ────────────────────────────────────────────────

def test_bank_flagged_financial():
    mc = classify_methodology("6021")
    assert mc.is_financial is True
    assert mc.moodys_methodology == "Banks"
    assert mc.sp_sector == "Banks (Financial Institutions criteria)"
    assert "does not apply" in mc.notes


def test_insurer_flagged_financial():
    mc = classify_methodology("6311")
    assert mc.is_financial is True
    assert mc.moodys_methodology == "Insurers"


# ── LLM fallback for ambiguous SICs ──────────────────────────────────────────

class _StubMessage:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]
        self.stop_reason = "end_turn"


class _StubClient:
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


def test_recognised_67_code_not_ambiguous():
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


def test_missing_sic_without_client_returns_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mc = classify_methodology(None)
    assert mc.source == "default"
    assert mc.confidence == "low"
    assert mc.moodys_methodology == MOODYS_DEFAULT


# ── TRBC cross-check ─────────────────────────────────────────────────────────

def test_trbc_agreement_keeps_confidence():
    mc = classify_methodology("4911", trbc="Utilities")
    assert mc.confidence == "high"
    assert "disagree" not in mc.notes


def test_trbc_disagreement_downgrades_confidence():
    mc = classify_methodology("4911", trbc="Technology")
    assert mc.confidence == "medium"
    assert "disagree" in mc.notes.lower()


def test_returns_methodology_class():
    assert isinstance(classify_methodology("4911"), MethodologyClass)
