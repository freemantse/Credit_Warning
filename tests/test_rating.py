"""Tests for src/rating.py — pure arithmetic, no network or LLM."""

import pytest

from src.extract import RatioResult
from src.rating import (
    RATING_SCALE,
    DEFAULT,
    compute_implied_rating,
    is_investment_grade,
    rating_index,
    _grid_profile,
)


def make_ratio(name, value, inputs=None):
    return RatioResult(
        name=name, value=value, inputs=inputs or {}, source_tags={}, period_end="2023-12-31"
    )


def ebitda_inputs(op_inc, dep=0.0):
    """Inputs carrying an EBITDA recover_ebitda() can read back."""
    return {"operating_income": op_inc, "depreciation": dep}


# ── _grid_profile (the bucketing primitive) ──────────────────────────────────

def test_grid_profile_higher_is_better():
    edges = [0.60, 0.45, 0.30, 0.20, 0.12]  # FFO/Debt
    assert _grid_profile(0.80, edges, True) == 1   # Minimal
    assert _grid_profile(0.50, edges, True) == 2   # Modest
    assert _grid_profile(0.35, edges, True) == 3   # Intermediate
    assert _grid_profile(0.25, edges, True) == 4   # Significant
    assert _grid_profile(0.15, edges, True) == 5   # Aggressive
    assert _grid_profile(0.05, edges, True) == 6   # Highly Leveraged


def test_grid_profile_lower_is_better():
    edges = [1.5, 2.0, 3.0, 4.0, 5.0]  # Debt/EBITDA
    assert _grid_profile(1.0, edges, False) == 1
    assert _grid_profile(1.8, edges, False) == 2
    assert _grid_profile(2.5, edges, False) == 3
    assert _grid_profile(3.5, edges, False) == 4
    assert _grid_profile(4.5, edges, False) == 5
    assert _grid_profile(7.0, edges, False) == 6


def test_grid_profile_boundary_inclusive_on_strong_side():
    # A value exactly at the first edge lands in the strongest band.
    assert _grid_profile(0.60, [0.60, 0.45, 0.30, 0.20, 0.12], True) == 1
    assert _grid_profile(1.5, [1.5, 2.0, 3.0, 4.0, 5.0], False) == 1


# ── compute_implied_rating ───────────────────────────────────────────────────

def test_healthy_issuer_is_investment_grade():
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.70),   # FFO/Debt → Minimal
        "leverage": make_ratio("leverage", 1.0, ebitda_inputs(100)),  # Debt/EBITDA → Minimal
        "interest_coverage": make_ratio("interest_coverage", 20.0, ebitda_inputs(100)),  # → Minimal
    }
    res = compute_implied_rating(ratios)
    assert res is not None
    assert is_investment_grade(res.implied_rating)
    # Minimal financial risk + default (Satisfactory) business risk → strong IG.
    assert res.financial_risk_index == 1
    assert res.financial_risk_profile == "Minimal"


def test_distressed_issuer_is_speculative():
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.05),    # FFO/Debt → Highly Leveraged
        "leverage": make_ratio("leverage", 8.0, ebitda_inputs(100)),   # Debt/EBITDA → Highly Leveraged
        "interest_coverage": make_ratio("interest_coverage", 1.0, ebitda_inputs(100)),  # → Highly Leveraged
    }
    res = compute_implied_rating(ratios)
    assert res is not None
    assert not is_investment_grade(res.implied_rating)
    assert res.financial_risk_index == 6


def test_negative_ebitda_forces_bottom_band_subfactors():
    # FFO/Debt looks healthy, but EBITDA is negative → Debt/EBITDA & coverage forced to band 6.
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.50),
        "leverage": make_ratio("leverage", -2.0, ebitda_inputs(-100)),  # EBITDA = -100
        "interest_coverage": make_ratio("interest_coverage", -1.0, ebitda_inputs(-100)),
    }
    res = compute_implied_rating(ratios)
    assert res is not None
    assert res.subscores["debt_to_ebitda"]["profile"] == 6
    assert res.subscores["debt_to_ebitda"]["overridden"] is True
    assert res.subscores["ebitda_to_interest"]["profile"] == 6
    assert res.subscores["ebitda_to_interest"]["overridden"] is True
    assert any("EBITDA" in n for n in res.notes)


def test_returns_none_when_too_few_subfactors():
    # Only one sub-factor resolves → no guess.
    ratios = {"cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.40)}
    assert compute_implied_rating(ratios) is None


def test_two_of_three_subfactors_renormalises():
    # Coverage missing; FFO/Debt and Debt/EBITDA present → still rated.
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.35),   # Intermediate (3)
        "leverage": make_ratio("leverage", 2.5, ebitda_inputs(100)),  # Intermediate (3)
    }
    res = compute_implied_rating(ratios)
    assert res is not None
    assert res.financial_risk_index == 3
    assert any("renormalised" in n for n in res.notes)


def test_business_risk_input_changes_rating():
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.35),   # Intermediate
        "leverage": make_ratio("leverage", 2.5, ebitda_inputs(100)),  # Intermediate
        "interest_coverage": make_ratio("interest_coverage", 8.0, ebitda_inputs(100)),  # Intermediate
    }
    excellent = compute_implied_rating(ratios, business_risk=1)
    vulnerable = compute_implied_rating(ratios, business_risk=6)
    assert excellent is not None and vulnerable is not None
    # Better business risk → better (lower-index) rating for the same financials.
    assert excellent.rating_index < vulnerable.rating_index


def test_subscores_record_source_ratio():
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.35),
        "leverage": make_ratio("leverage", 2.5, ebitda_inputs(100)),
        "interest_coverage": make_ratio("interest_coverage", 8.0, ebitda_inputs(100)),
    }
    res = compute_implied_rating(ratios)
    assert res.subscores["ffo_to_debt"]["source_ratio"] == "cash_flow_to_debt"
    assert res.subscores["debt_to_ebitda"]["source_ratio"] == "leverage"


# ── Anchor-matrix invariants ─────────────────────────────────────────────────

def test_anchor_matrix_is_valid_and_monotonic():
    m = DEFAULT.anchor_matrix
    assert len(m) == 6 and all(len(row) == 6 for row in m)
    # Every cell is a real rating on the scale.
    for row in m:
        for letter in row:
            assert letter in RATING_SCALE
    # Ratings worsen (index increases) left→right across each row and top→bottom
    # down each column — the matrix must be non-improving in both directions.
    for r in range(6):
        for c in range(6):
            if c + 1 < 6:
                assert rating_index(m[r][c]) <= rating_index(m[r][c + 1])
            if r + 1 < 6:
                assert rating_index(m[r][c]) <= rating_index(m[r + 1][c])


def test_rating_scale_helpers():
    assert rating_index("AAA") == 0
    assert is_investment_grade("BBB-") is True
    assert is_investment_grade("BB+") is False
