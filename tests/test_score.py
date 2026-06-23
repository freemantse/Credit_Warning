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
        "leverage": make_ratio("leverage", 6.0, ebitda_inputs(1_000_000)),   # >= 6× → 16
        "interest_coverage": make_ratio("interest_coverage", 1.0, ebitda_inputs(1_000_000)),  # <= 1× → 14
        "free_cash_flow": make_ratio("free_cash_flow", -500_000),
        "fcf_margin": make_ratio("fcf_margin", -0.10),   # <= -10% → 10
        "liquidity": make_ratio("liquidity", 0.25),      # <= 0.25× → 8
    }
    result = compute_score(ratios, [])
    # 17+14+10+9 = 50 core; 4 severe signals trigger the escalation floor → 60.
    assert result.breakdown["leverage>5x"] == 17.0
    assert result.breakdown["coverage<2x"] == 14.0
    assert result.breakdown["fcf_negative"] == 10.0
    assert result.breakdown["liquidity<1x"] == 9.0
    assert result.score == 60.0


def test_ratios_ramp_partially():
    # Each ratio sits at the midpoint of its ramp → half points. Positive EBITDA
    # keeps the sign-aware override off.
    ratios = {
        "leverage": make_ratio("leverage", 4.5, ebitda_inputs(1_000_000)),   # midpoint 3→6 → 8.5
        "interest_coverage": make_ratio("interest_coverage", 2.5, ebitda_inputs(1_000_000)),  # midpoint 4→1 → 7.0
        "fcf_margin": make_ratio("fcf_margin", -0.05),   # midpoint 0→-0.10 → 5.0
        "liquidity": make_ratio("liquidity", 0.625),     # midpoint 1→0.25 → 4.5
    }
    result = compute_score(ratios, [])
    assert result.breakdown["leverage>5x"] == 8.5
    assert result.breakdown["coverage<2x"] == 7.0
    assert result.breakdown["fcf_negative"] == 5.0
    assert result.breakdown["liquidity<1x"] == 4.5
    assert result.score == 25.0


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
    assert result.breakdown["leverage>5x"] == 17.0
    assert result.breakdown["coverage<2x"] == 14.0
    assert result.breakdown["profitability"] == 14.0
    assert result.breakdown["fcf_negative"] == 10.0
    # 17+14+14+10 = 55 core; 4 severe signals → escalation floor lifts to 60.
    assert result.score == 60.0  # High Risk — no longer "healthy"


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


def test_escalation_floor_four_severe_signals():
    # Four severe core signals (sum well under 60) prove the floor lifts the score
    # to 60. With 8 core rules the trigger is >= 4 severe.
    ratios = {
        "liquidity": make_ratio("liquidity", 0.25),                 # severe → 9
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.0),  # severe → 15
        "debt_to_assets": make_ratio("debt_to_assets", 0.65),       # severe → 9
        "fcf_margin": make_ratio("fcf_margin", -0.10),              # severe → 10
    }
    result = compute_score(ratios, [])
    # Raw core sum is 9+15+9+10 = 43, but 4 severe signals floor it at 60.
    assert result.score == 60.0
    # Drop one severe signal → only 3 severe → floor no longer applies → raw sum.
    ratios.pop("fcf_margin")
    assert compute_score(ratios, []).score == 33.0  # 9+15+9


def test_profitability_ramp_endpoints():
    assert compute_score({"ebitda_margin": make_ratio("ebitda_margin", 0.10)}).breakdown["profitability"] == 0.0
    assert compute_score({"ebitda_margin": make_ratio("ebitda_margin", -0.05)}).breakdown["profitability"] == 14.0
    assert compute_score({"ebitda_margin": make_ratio("ebitda_margin", 0.025)}).breakdown["profitability"] == 7.0


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


def all_severe_ratios():
    """Every core ratio pushed at/past its severe edge (positive EBITDA so the
    sign-aware override stays off and the ramps are exercised)."""
    return {
        "leverage": make_ratio("leverage", 12.0, ebitda_inputs(1_000_000)),
        "interest_coverage": make_ratio("interest_coverage", 0.0, ebitda_inputs(1_000_000)),
        "ebitda_margin": make_ratio("ebitda_margin", -0.20, ebitda_inputs(1_000_000)),
        "fcf_margin": make_ratio("fcf_margin", -0.20),
        "liquidity": make_ratio("liquidity", 0.0),
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.0),
        "debt_to_assets": make_ratio("debt_to_assets", 0.95),
    }


def test_core_maxima_sum_to_94():
    # Maturity wall is the 8th core rule; feed it via the maturity arg at severe.
    @dataclass
    class Mat:
        near_term_pct: float = 0.90
    result = compute_score(all_severe_ratios(), [], maturity=Mat())
    # Every core rule maxed → the 8 rules sum to exactly the 94-pt budget
    # (100 minus the retired 6-pt current-ratio rule).
    assert result.score == 94.0


def test_score_capped_at_100():
    # Core maxes to 94 (incl. the maturity wall); 10 high findings would push the
    # total to 104, but it is capped at 100 (and the combined LLM cap is 15).
    findings = [MockFinding("x", "high") for _ in range(10)]
    result = compute_score(all_severe_ratios(), findings, maturity={"near_term_pct": 0.90})
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
    # Leverage 4.5× is the midpoint of the 3→6 ramp → 0.5 × 17 = 8.5 pts.
    ratios = {"leverage": make_ratio("leverage", 4.5, ebitda_inputs(1_000_000))}
    result = compute_score(ratios)
    assert "leverage>5x" in result.breakdown
    assert result.breakdown["leverage>5x"] == 8.5


# ── New ratio rules: ramp endpoints (calibrated, not lenient) ───────────────

def test_cash_flow_to_debt_ramp():
    # healthy 0.30 → 0; severe 0.10 → full 15; 0.20 (≈BB) → half.
    assert compute_score({"cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.30)}).breakdown["cash_flow_to_debt<30%"] == 0.0
    assert compute_score({"cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.10)}).breakdown["cash_flow_to_debt<30%"] == 15.0
    assert compute_score({"cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.20)}).breakdown["cash_flow_to_debt<30%"] == 7.5


def test_debt_to_assets_ramp():
    # healthy 0.40 → 0; severe 0.65 → full 9.
    assert compute_score({"debt_to_assets": make_ratio("debt_to_assets", 0.40)}).breakdown["debt_to_assets>40%"] == 0.0
    assert compute_score({"debt_to_assets": make_ratio("debt_to_assets", 0.65)}).breakdown["debt_to_assets>40%"] == 9.0


def test_missing_ratios_skip_gracefully():
    # No ratios at all — should score 0 with no crash
    result = compute_score({}, [])
    assert result.score == 0.0


def test_stress_threshold_constant():
    # Sanity check that threshold is set
    assert STRESS_THRESHOLD == 50
