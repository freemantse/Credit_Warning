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

def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """
    Read a field from either a dataclass instance or a plain dict.

    Footnote signals reach compute_score as dataclasses (Covenant / LossProvision
    straight from extraction) or as dicts (loaded back from Supabase). This makes
    the scoring rules indifferent to which.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def compute_score(
    ratios: dict[str, RatioResult],
    findings: list[Any] | None = None,
    maturity: Any | None = None,
    covenants: list[Any] | None = None,
    loss_provisions: list[Any] | None = None,
) -> ScoreResult:
    """
    Combine ratio results and footnote signals into a stress score.

    This function is deliberately kept simple and auditable:
      - Each rule either fires (adds points) or doesn't (adds 0).
      - Missing ratios (None) don't add points — an issuer is not penalised
        for having incomplete XBRL data.
      - All breakdowns are recorded so the score can be fully explained.

    Two tiers of signal, by trustworthiness:
      - DETERMINISTIC (XBRL-derived) rules carry full weight, like the ratio
        rules: the maturity-wall concentration is computed from tagged figures.
      - LLM-DERIVED signals (covenants, loss provisions) are capped low, mirroring
        the existing 10-pt cap on qualitative findings, so model output can only
        nudge the score at the margin and never breach the threshold alone.

    Args:
        ratios:          Output of extract_all(); only RatioResult values are scored.
        findings:        Finding objects from llm_review.review_text() (or dicts).
        maturity:        MaturitySchedule (or dict) from extract.debt_maturity_schedule.
        covenants:       Covenant objects (or dicts) from footnote_review.
        loss_provisions: LossProvision objects (or dicts) from footnote_review.

    Returns:
        ScoreResult with score, per-rule breakdown, and alert strings.
    """
    if findings is None:
        findings = []
    if covenants is None:
        covenants = []
    if loss_provisions is None:
        loss_provisions = []

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
    high_sev = [f for f in findings if _attr(f, "severity", "") == "high"]
    llm_pts = min(len(high_sev) * 2.0, 10.0)
    breakdown["llm_high_severity"] = llm_pts
    if high_sev:
        alerts.append(f"{len(high_sev)} high-severity qualitative concern(s) flagged")

    # ── Rule 5: Maturity wall (DETERMINISTIC, full weight) ───────────────────
    # near_term_pct = (y1 + y2) / total scheduled principal, from XBRL maturity
    # tags. A heavy near-term concentration means a large share of debt must be
    # refinanced soon — refinancing risk. Because it is XBRL-derived (not LLM),
    # it carries full weight like the ratio rules. None (no/zero schedule) → skip.
    near_term_pct = _attr(maturity, "near_term_pct") if maturity is not None else None
    if near_term_pct is not None and near_term_pct > 0.40:
        breakdown["maturity_wall"] = 15.0
        alerts.append(
            f"Maturity wall: {near_term_pct * 100:.0f}% of debt due within 2 years"
        )
    else:
        breakdown["maturity_wall"] = 0.0

    # ── Rule 6: Covenant proximity (LLM-DERIVED, capped) ─────────────────────
    # Each covenant the footnote describes the company as close to breaching adds
    # 3 pts, capped at 6. LLM-derived, so kept marginal.
    near_cov = [c for c in covenants if _attr(c, "near_limit", False)]
    cov_pts = min(len(near_cov) * 3.0, 6.0)
    breakdown["covenant_proximity"] = cov_pts
    if near_cov:
        alerts.append(f"{len(near_cov)} covenant(s) near their limit")

    # ── Rule 7: Material loss provisions (LLM-DERIVED, capped) ───────────────
    # Each material litigation/contingency provision adds 2 pts, capped at 6.
    material = [p for p in loss_provisions if _attr(p, "is_material", False)]
    prov_pts = min(len(material) * 2.0, 6.0)
    breakdown["litigation_provision"] = prov_pts
    if material:
        alerts.append(f"{len(material)} material loss provision(s) disclosed")

    # Sum all breakdown values and cap at 100 in case future rules push above it.
    score = min(sum(breakdown.values()), 100.0)

    return ScoreResult(score=score, breakdown=breakdown, alerts=alerts)
