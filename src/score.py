"""
Deterministic stress scoring per (issuer, period).

How the score is built:
  The score is a simple additive sum of rule-based penalties:
    - Each quantitative ratio rule contributes 0 or a fixed number of points.
    - High-severity LLM findings contribute up to 10 additional points.
    - The total is capped at 100.

  Crucially, the LLM findings only adjust the score at the margin — they
  cannot independently push an issuer past the stress threshold (50 pts).
  All four ratio rules together total 90 pts; the LLM cap is 10 pts.

STRESS_THRESHOLD = 50:
  A score at or above 50 is treated as "stressed" throughout the system.
  This constant is imported by the backtest, API, and frontend so the
  threshold is defined in exactly one place.

Score range: 0–100. Higher = more credit stress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.extract import RatioResult


# ── Constants ────────────────────────────────────────────────────────────────

# The cut-off score above which an issuer is flagged as "stressed".
# Imported by: src/backtest.py, api/main.py, src/track.py, lib/api.ts (frontend).
STRESS_THRESHOLD = 50


# ── Result container ─────────────────────────────────────────────────────────

@dataclass
class ScoreResult:
    """
    Carries all outputs from compute_score() for one (issuer, period).

    Attributes:
        score:     Final stress score 0–100. Higher = more risk.
        breakdown: Dict mapping each rule key to the points it contributed.
                   e.g. {"leverage>5x": 25.0, "coverage<2x": 0.0, ...}
                   Stored in Supabase and displayed in the frontend audit panel.
        alerts:    Human-readable strings for each triggered rule.
                   e.g. ["Leverage 6.2× > 5× threshold", "FCF negative (...)"]
                   Displayed in the portfolio dashboard and detail page.
    """
    score: float                    # 0–100, higher = more credit stress
    breakdown: dict[str, float]     # component key → points contributed
    alerts: list[str]               # human-readable strings for triggered rules


# ── Scoring function ─────────────────────────────────────────────────────────

def compute_score(
    ratios: dict[str, RatioResult],
    findings: list[Any] | None = None,
) -> ScoreResult:
    """
    Combine ratio results and LLM qualitative findings into a stress score.

    This function is deliberately kept simple and auditable:
      - Each rule either fires (adds points) or doesn't (adds 0).
      - Missing ratios (None) don't add points — an issuer is not penalised
        for having incomplete XBRL data.
      - All breakdowns are recorded so the score can be fully explained.

    Args:
        ratios:   Output of extract_all(). The dict may contain RatioResult
                  objects (successful computations) or MissingDataError objects
                  (failed computations). Only RatioResult values are scored.
        findings: List of Finding objects from llm_review.review_text().
                  Pass [] or None to skip the qualitative adjustment.

    Returns:
        ScoreResult with score, per-rule breakdown, and alert strings.
    """
    if findings is None:
        findings = []

    # Accumulate rule → points mappings. All rules are added (even with 0 pts)
    # so the breakdown always shows the full picture, not just the triggered rules.
    breakdown: dict[str, float] = {}
    alerts: list[str] = []

    def _val(name: str) -> float | None:
        """
        Safely extract a ratio value from the results dict.
        Returns None if the ratio was missing (MissingDataError) or not computed.
        Using isinstance() guards against MissingDataError objects in the dict.
        """
        r = ratios.get(name)
        if isinstance(r, RatioResult):
            return r.value
        return None  # ratio was missing or failed — don't penalise

    # ── Rule 1: Leverage > 5× ────────────────────────────────────────────────
    # Net debt / EBITDA above 5× is a widely-used speculative-grade boundary.
    # Investment-grade issuers typically run below 3×. Above 5× indicates
    # the company would take over 5 years of full EBITDA to pay off its debt.
    lev = _val("leverage")
    if lev is not None and lev > 5.0:
        breakdown["leverage>5x"] = 25.0
        alerts.append(f"Leverage {lev:.1f}× > 5× threshold")
    else:
        breakdown["leverage>5x"] = 0.0  # record 0 so the breakdown is complete

    # ── Rule 2: Interest Coverage < 2× ──────────────────────────────────────
    # EBITDA / interest expense below 2× means earnings barely cover interest.
    # At 1× the company's entire EBITDA goes to interest; below 1× it cannot
    # cover interest from operations at all.
    cov = _val("interest_coverage")
    if cov is not None and cov < 2.0:
        breakdown["coverage<2x"] = 25.0
        alerts.append(f"Interest coverage {cov:.1f}× < 2× threshold")
    else:
        breakdown["coverage<2x"] = 0.0

    # ── Rule 3: Free Cash Flow negative ─────────────────────────────────────
    # Negative FCF (OCF minus capex) means the company consumed more cash than
    # it generated, even before considering debt repayment or dividends.
    # Sustained negative FCF forces reliance on debt or equity financing.
    fcf = _val("free_cash_flow")
    if fcf is not None and fcf < 0:
        breakdown["fcf_negative"] = 20.0
        alerts.append(f"Free cash flow negative ({fcf:,.0f})")
    else:
        breakdown["fcf_negative"] = 0.0

    # ── Rule 4: Liquidity < 1× ──────────────────────────────────────────────
    # Cash / short-term debt below 1× means the company can't cover its maturing
    # near-term obligations with cash on hand alone — it would need to refinance
    # or draw on revolving credit facilities.
    liq = _val("liquidity")
    if liq is not None and liq < 1.0:
        breakdown["liquidity<1x"] = 20.0
        alerts.append(f"Liquidity {liq:.2f}× < 1× threshold")
    else:
        breakdown["liquidity<1x"] = 0.0

    # ── LLM qualitative adjustment ───────────────────────────────────────────
    # High-severity findings from the LLM review each add 2 pts, capped at 10.
    #
    # Why getattr(f, "severity", "")?
    #   Findings could be Finding dataclass instances (from llm_review.py) or
    #   plain dicts loaded from Supabase. getattr() safely handles dataclasses;
    #   for dicts it returns "" (the default), so dict findings contribute 0 pts.
    #   This makes the function tolerant of both input formats.
    #
    # Why cap at 10?
    #   The four ratio rules sum to 90 pts max. The LLM cap of 10 pts means a
    #   qualitative-only signal cannot push an issuer past the 50-pt stress
    #   threshold by itself (max LLM contribution = 10 < 50).
    high_sev = [f for f in findings if getattr(f, "severity", "") == "high"]
    llm_pts = min(len(high_sev) * 2.0, 10.0)
    breakdown["llm_high_severity"] = llm_pts
    if high_sev:
        alerts.append(f"{len(high_sev)} high-severity qualitative concern(s) flagged")

    # Sum all breakdown values and cap at 100 in case future rules push above it.
    score = min(sum(breakdown.values()), 100.0)

    return ScoreResult(score=score, breakdown=breakdown, alerts=alerts)
