"""Tests for src/score.py — pure arithmetic, no network or LLM."""

import pytest
from dataclasses import dataclass
from src.extract import RatioResult
from src.score import compute_score, ScoreResult, STRESS_THRESHOLD


def make_ratio(name, value, inputs=None):
    return RatioResult(
        name=name, value=value, inputs=inputs or {}, source_tags={}, period_end="2023-12-31"
    )


def ebitda_inputs(op_inc, dep=0.0):
    """Inputs dict carrying an EBITDA the scorer can recover via _ebitda()."""
    return {"operating_income": op_inc, "depreciation": dep}


@dataclass
class MockFinding:
    concern: str
    severity: str
    evidence_quote: str = "some quote"
    source: str = "10-K"


def test_healthy_issuer_scores_zero():
    ratios = {
        "leverage": make_ratio("leverage", 2.0),
        "interest_coverage": make_ratio("interest_coverage", 5.0),
        "free_cash_flow": make_ratio("free_cash_flow", 1_000_000),
        "liquidity": make_ratio("liquidity", 2.5),
    }
    result = compute_score(ratios, [])
    assert result.score == 0.0
    assert result.alerts == []


def test_all_thresholds_triggered():
    # Ratios at or past their severe edges → each rule maxes out. Positive EBITDA
    # in the inputs keeps the sign-aware override OFF so the ramps are exercised.
    ratios = {
        "leverage": make_ratio("leverage", 6.0, ebitda_inputs(1_000_000)),   # >= 6× → 20
        "interest_coverage": make_ratio("interest_coverage", 1.0, ebitda_inputs(1_000_000)),  # <= 1× → 20
        "free_cash_flow": make_ratio("free_cash_flow", -500_000),
        "fcf_margin": make_ratio("fcf_margin", -0.10),   # <= -10% → 15
        "liquidity": make_ratio("liquidity", 0.25),      # <= 0.25× → 15
    }
    result = compute_score(ratios, [])
    # 20+20+15+15 = 70 core; 4 severe signals also trigger the floor (60 < 70).
    assert result.score == 70.0
    assert result.breakdown["leverage>5x"] == 20.0
    assert result.breakdown["coverage<2x"] == 20.0


def test_ratios_ramp_partially():
    # Each ratio sits at the midpoint of its ramp → half points. Positive EBITDA
    # keeps the sign-aware override off.
    ratios = {
        "leverage": make_ratio("leverage", 4.5, ebitda_inputs(1_000_000)),   # midpoint 3→6 → 10.0
        "interest_coverage": make_ratio("interest_coverage", 2.5, ebitda_inputs(1_000_000)),  # midpoint 4→1 → 10.0
        "fcf_margin": make_ratio("fcf_margin", -0.05),   # midpoint 0→-0.10 → 7.5
        "liquidity": make_ratio("liquidity", 0.625),     # midpoint 1→0.25 → 7.5
    }
    result = compute_score(ratios, [])
    assert result.breakdown["leverage>5x"] == 10.0
    assert result.breakdown["coverage<2x"] == 10.0
    assert result.breakdown["fcf_negative"] == 7.5
    assert result.breakdown["liquidity<1x"] == 7.5
    assert result.score == 35.0


def test_negative_ebitda_forces_full_leverage_and_coverage():
    # Beyond-Meat-style: negative EBITDA. Leverage = net_debt/EBITDA goes negative
    # (would have scored 0 on the old ramp); the sign-aware override forces full
    # penalty on both leverage and coverage, profitability also fires.
    ratios = {
        "leverage": make_ratio("leverage", -2.0, ebitda_inputs(-1_000_000)),
        "interest_coverage": make_ratio("interest_coverage", -1.0, ebitda_inputs(-1_000_000)),
        "ebitda_margin": make_ratio("ebitda_margin", -0.08, ebitda_inputs(-1_000_000)),
        "fcf_margin": make_ratio("fcf_margin", -0.15),
    }
    result = compute_score(ratios, [])
    assert result.breakdown["leverage>5x"] == 20.0
    assert result.breakdown["coverage<2x"] == 20.0
    assert result.breakdown["profitability"] == 20.0
    assert result.breakdown["fcf_negative"] == 15.0
    assert result.score >= 75.0  # High Risk — no longer "healthy"


def test_net_cash_negative_leverage_not_penalised():
    # Negative leverage from a NET-CASH position with POSITIVE EBITDA is strength,
    # not distress — the sign-aware branch checks the EBITDA sign, so it stays 0.
    ratios = {
        "leverage": make_ratio("leverage", -1.5, ebitda_inputs(5_000_000)),
        "interest_coverage": make_ratio("interest_coverage", 10.0, ebitda_inputs(5_000_000)),
        "ebitda_margin": make_ratio("ebitda_margin", 0.25, ebitda_inputs(5_000_000)),
    }
    result = compute_score(ratios, [])
    assert result.breakdown["leverage>5x"] == 0.0
    assert result.breakdown["coverage<2x"] == 0.0
    assert result.score == 0.0


def test_escalation_floor_three_severe_signals():
    # Three severe core signals (none individually catastrophic on sum) → floor 60.
    ratios = {
        "leverage": make_ratio("leverage", 6.0, ebitda_inputs(1_000_000)),   # severe → 20
        "interest_coverage": make_ratio("interest_coverage", 1.0, ebitda_inputs(1_000_000)),  # severe → 20
        "ebitda_margin": make_ratio("ebitda_margin", -0.05, ebitda_inputs(1_000_000)),  # severe → 20
    }
    result = compute_score(ratios, [])
    assert result.score == 60.0  # 20+20+20 = 60, floor also satisfied
    # Drop one severe signal → floor no longer applies.
    ratios.pop("ebitda_margin")
    assert compute_score(ratios, []).score == 40.0


def test_profitability_ramp_endpoints():
    assert compute_score({"ebitda_margin": make_ratio("ebitda_margin", 0.10)}).breakdown["profitability"] == 0.0
    assert compute_score({"ebitda_margin": make_ratio("ebitda_margin", -0.05)}).breakdown["profitability"] == 20.0
    assert compute_score({"ebitda_margin": make_ratio("ebitda_margin", 0.025)}).breakdown["profitability"] == 10.0


def test_ratios_clamp_at_healthy_edge():
    # Exactly at the healthy edge → 0 pts (boundary is healthy).
    ratios = {
        "leverage": make_ratio("leverage", 3.0),
        "interest_coverage": make_ratio("interest_coverage", 4.0),
        "fcf_margin": make_ratio("fcf_margin", 0.0),
        "liquidity": make_ratio("liquidity", 1.0),
    }
    result = compute_score(ratios, [])
    assert result.score == 0.0


def test_llm_findings_add_points():
    ratios = {
        "leverage": make_ratio("leverage", 2.0),
        "interest_coverage": make_ratio("interest_coverage", 5.0),
        "free_cash_flow": make_ratio("free_cash_flow", 1_000_000),
        "liquidity": make_ratio("liquidity", 2.5),
    }
    findings = [MockFinding("covenant risk", "high") for _ in range(3)]
    result = compute_score(ratios, findings)
    assert result.breakdown["llm_high_severity"] == 6.0  # 3 × 2 pts
    assert result.score == 6.0


def test_llm_findings_capped_at_10():
    ratios = {}
    findings = [MockFinding("x", "high") for _ in range(10)]
    result = compute_score(ratios, findings)
    assert result.breakdown["llm_high_severity"] == 10.0


def test_low_severity_findings_ignored():
    ratios = {}
    findings = [MockFinding("minor issue", "low"), MockFinding("medium risk", "medium")]
    result = compute_score(ratios, findings)
    assert result.breakdown["llm_high_severity"] == 0.0


def test_score_capped_at_100():
    ratios = {
        "leverage": make_ratio("leverage", 12.0, ebitda_inputs(1_000_000)),
        "interest_coverage": make_ratio("interest_coverage", 0.0, ebitda_inputs(1_000_000)),
        "ebitda_margin": make_ratio("ebitda_margin", -0.20, ebitda_inputs(1_000_000)),
        "free_cash_flow": make_ratio("free_cash_flow", -1_000_000),
        "fcf_margin": make_ratio("fcf_margin", -0.20),
        "liquidity": make_ratio("liquidity", 0.0),
    }
    # Core sums to 90; 10 high findings add 10 → 100 (combined LLM cap is 15).
    findings = [MockFinding("x", "high") for _ in range(10)]
    result = compute_score(ratios, findings)
    assert result.score == 100.0


def test_combined_llm_cap_cannot_cross_threshold():
    # Qualitative-only signals (findings + covenants + provisions) are clamped to
    # a combined 15 pts, so they can never alone reach the 50-pt stress threshold.
    @dataclass
    class Cov:
        near_limit: bool = True

    @dataclass
    class Prov:
        is_material: bool = True

    findings = [MockFinding("x", "high") for _ in range(10)]   # 10
    covenants = [Cov(), Cov()]                                  # 6
    provisions = [Prov(), Prov(), Prov()]                       # 6  → raw 22, capped 15
    result = compute_score({}, findings, covenants=covenants, loss_provisions=provisions)
    assert result.score == 15.0
    assert result.score < STRESS_THRESHOLD


def test_breakdown_is_auditable():
    # Leverage 4.5× is the midpoint of the 3→6 ramp → 0.5 × 20 = 10.0 pts.
    ratios = {"leverage": make_ratio("leverage", 4.5, ebitda_inputs(1_000_000))}
    result = compute_score(ratios)
    assert "leverage>5x" in result.breakdown
    assert result.breakdown["leverage>5x"] == 10.0


def test_missing_ratios_skip_gracefully():
    # No ratios at all — should score 0 with no crash
    result = compute_score({}, [])
    assert result.score == 0.0


def test_stress_threshold_constant():
    # Sanity check that threshold is set
    assert STRESS_THRESHOLD == 50
