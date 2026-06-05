"""
Deterministic stress scoring per (issuer, period).

Numbers come only from RatioResult objects. LLM findings contribute
a capped adjustment — they never set the base score.

Score range: 0–100. Higher = more stressed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.extract import RatioResult

STRESS_THRESHOLD = 50  # score at or above this → stressed


@dataclass
class ScoreResult:
    score: float                    # 0–100
    breakdown: dict[str, float]     # component → points contributed
    alerts: list[str]               # human-readable triggered thresholds


def compute_score(
    ratios: dict[str, RatioResult],
    findings: list[Any] | None = None,
) -> ScoreResult:
    """
    Combine ratio results and LLM findings into a stress score.

    Args:
        ratios: output of extract_all() — only RatioResult values are used.
        findings: list of Finding objects from llm_review (or empty/None).

    Returns:
        ScoreResult with score, breakdown, and alerts.
    """
    if findings is None:
        findings = []

    breakdown: dict[str, float] = {}
    alerts: list[str] = []

    def _val(name: str) -> float | None:
        r = ratios.get(name)
        if isinstance(r, RatioResult):
            return r.value
        return None

    # Leverage > 5× → 25 pts
    lev = _val("leverage")
    if lev is not None and lev > 5.0:
        breakdown["leverage>5x"] = 25.0
        alerts.append(f"Leverage {lev:.1f}× > 5× threshold")
    else:
        breakdown["leverage>5x"] = 0.0

    # Interest coverage < 2× → 25 pts
    cov = _val("interest_coverage")
    if cov is not None and cov < 2.0:
        breakdown["coverage<2x"] = 25.0
        alerts.append(f"Interest coverage {cov:.1f}× < 2× threshold")
    else:
        breakdown["coverage<2x"] = 0.0

    # FCF negative → 20 pts
    fcf = _val("free_cash_flow")
    if fcf is not None and fcf < 0:
        breakdown["fcf_negative"] = 20.0
        alerts.append(f"Free cash flow negative ({fcf:,.0f})")
    else:
        breakdown["fcf_negative"] = 0.0

    # Liquidity < 1× → 20 pts
    liq = _val("liquidity")
    if liq is not None and liq < 1.0:
        breakdown["liquidity<1x"] = 20.0
        alerts.append(f"Liquidity {liq:.2f}× < 1× threshold")
    else:
        breakdown["liquidity<1x"] = 0.0

    # High-severity LLM findings: +2 pts each, capped at 10 pts
    high_sev = [f for f in findings if getattr(f, "severity", "") == "high"]
    llm_pts = min(len(high_sev) * 2.0, 10.0)
    breakdown["llm_high_severity"] = llm_pts
    if high_sev:
        alerts.append(f"{len(high_sev)} high-severity qualitative concern(s) flagged")

    score = min(sum(breakdown.values()), 100.0)

    return ScoreResult(score=score, breakdown=breakdown, alerts=alerts)
