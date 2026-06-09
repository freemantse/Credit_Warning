"""Tests for src/score.py — pure arithmetic, no network or LLM."""

import pytest
from dataclasses import dataclass
from src.extract import RatioResult
from src.score import compute_score, ScoreResult, STRESS_THRESHOLD


def make_ratio(name, value):
    return RatioResult(name=name, value=value, inputs={}, source_tags={}, period_end="2023-12-31")


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
    # Ratios at or past their severe edges → each rule maxes out.
    ratios = {
        "leverage": make_ratio("leverage", 6.0),         # >= 6× severe → 25 pts
        "interest_coverage": make_ratio("interest_coverage", 1.0),  # <= 1× severe → 25 pts
        "free_cash_flow": make_ratio("free_cash_flow", -500_000),
        "fcf_margin": make_ratio("fcf_margin", -0.10),   # <= -10% severe → 20 pts
        "liquidity": make_ratio("liquidity", 0.25),      # <= 0.25× severe → 20 pts
    }
    result = compute_score(ratios, [])
    assert result.score == 90.0  # 25+25+20+20 = 90, no LLM findings
    assert len(result.alerts) == 4


def test_ratios_ramp_partially():
    # Each ratio sits at the midpoint of its ramp → half points.
    ratios = {
        "leverage": make_ratio("leverage", 4.5),         # midpoint 3→6 → 12.5
        "interest_coverage": make_ratio("interest_coverage", 2.5),  # midpoint 4→1 → 12.5
        "fcf_margin": make_ratio("fcf_margin", -0.05),   # midpoint 0→-0.10 → 10.0
        "liquidity": make_ratio("liquidity", 0.625),     # midpoint 1→0.25 → 10.0
    }
    result = compute_score(ratios, [])
    assert result.breakdown["leverage>5x"] == 12.5
    assert result.breakdown["coverage<2x"] == 12.5
    assert result.breakdown["fcf_negative"] == 10.0
    assert result.breakdown["liquidity<1x"] == 10.0
    assert result.score == 45.0


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
        "leverage": make_ratio("leverage", 12.0),
        "interest_coverage": make_ratio("interest_coverage", 0.0),
        "free_cash_flow": make_ratio("free_cash_flow", -1_000_000),
        "fcf_margin": make_ratio("fcf_margin", -0.20),
        "liquidity": make_ratio("liquidity", 0.0),
    }
    findings = [MockFinding("x", "high") for _ in range(10)]
    result = compute_score(ratios, findings)
    assert result.score == 100.0


def test_breakdown_is_auditable():
    # Leverage 4.5× is the midpoint of the 3→6 ramp: → 12.5 pts.
    ratios = {"leverage": make_ratio("leverage", 4.5)}
    result = compute_score(ratios)
    assert "leverage>5x" in result.breakdown
    assert result.breakdown["leverage>5x"] == 12.5


def test_missing_ratios_skip_gracefully():
    # No ratios at all — should score 0 with no crash
    result = compute_score({}, [])
    assert result.score == 0.0


def test_stress_threshold_constant():
    # Sanity check that threshold is set
    assert STRESS_THRESHOLD == 50
