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
    ratios = {
        "leverage": make_ratio("leverage", 6.0),       # > 5× → 25 pts
        "interest_coverage": make_ratio("interest_coverage", 1.5),  # < 2× → 25 pts
        "free_cash_flow": make_ratio("free_cash_flow", -500_000),   # negative → 20 pts
        "liquidity": make_ratio("liquidity", 0.5),     # < 1× → 20 pts
    }
    result = compute_score(ratios, [])
    assert result.score == 90.0  # 25+25+20+20 = 90, no LLM findings
    assert len(result.alerts) == 4


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
        "leverage": make_ratio("leverage", 10.0),
        "interest_coverage": make_ratio("interest_coverage", 0.5),
        "free_cash_flow": make_ratio("free_cash_flow", -1_000_000),
        "liquidity": make_ratio("liquidity", 0.1),
    }
    findings = [MockFinding("x", "high") for _ in range(10)]
    result = compute_score(ratios, findings)
    assert result.score == 100.0


def test_breakdown_is_auditable():
    ratios = {"leverage": make_ratio("leverage", 6.0)}
    result = compute_score(ratios)
    assert "leverage>5x" in result.breakdown
    assert result.breakdown["leverage>5x"] == 25.0


def test_missing_ratios_skip_gracefully():
    # No ratios at all — should score 0 with no crash
    result = compute_score({}, [])
    assert result.score == 0.0


def test_stress_threshold_constant():
    # Sanity check that threshold is set
    assert STRESS_THRESHOLD == 50
