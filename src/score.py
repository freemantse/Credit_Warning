"""
Deterministic stress scoring per (issuer, period).

How the score is built:
  The score is an additive sum of rule-based penalties over six deterministic
  core rules (max 100 pts combined), plus capped LLM nudges:
    - Each quantitative ratio rule contributes points on a linear ramp: 0 while
      the ratio is at or healthier than its threshold, then rising continuously
      to the rule's maximum as the ratio worsens toward a severe extreme (and
      clamped at the maximum beyond it).
    - The six core rules and their maxima:
        profitability (EBITDA margin)  20
        leverage (net debt / EBITDA)   20
        interest coverage              20
        free cash flow (FCF margin)    15
        liquidity                      15
        maturity wall                  10
    - LLM signals (high-severity findings, covenant proximity, loss provisions)
      contribute up to 15 additional points combined.
    - The total is capped at 100.

  Two robustness rules guard against the failure mode where a deeply distressed
  issuer reads as healthy:
    - SIGN-AWARE override: leverage = net_debt / EBITDA and interest coverage =
      EBITDA / interest both flip sign when EBITDA is negative, which would let a
      money-losing issuer score 0 on those ramps. When EBITDA <= 0 we force both
      rules to their full penalty. We branch on the EBITDA sign (not the ratio
      sign) so a negative leverage caused by a net-cash position with POSITIVE
      EBITDA still scores 0 — financial strength, not distress.
    - DISTRESS ESCALATION floor: if >= 3 core rules are "severe" (>= 80% of their
      max), the final score is floored at 60 (High Risk) regardless of the sum,
      so compounding distress can't slip under the threshold.

  Crucially, the LLM findings only adjust the score at the margin — they
  cannot independently push an issuer past the stress threshold (50 pts):
  the combined LLM cap is 15 pts.

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


def _ramp(
    value: float | None,
    healthy: float,
    severe: float,
    max_pts: float,
) -> float:
    """
    Linearly map a ratio value to stress points in [0, max_pts].

      - At `healthy` (and anything healthier) → 0 pts.
      - At `severe` (and anything worse) → max_pts.
      - Linear in between.

    Direction is inferred from healthy vs severe via the sign of
    (severe - healthy), so the same formula handles both "higher is worse"
    ratios (leverage, maturity wall: healthy < severe) and "lower is worse"
    ratios (coverage, liquidity, fcf_margin: healthy > severe).

    Returns 0.0 for None — a missing ratio is never penalised.
    """
    if value is None:
        return 0.0
    frac = (value - healthy) / (severe - healthy)
    frac = max(0.0, min(1.0, frac))
    return round(frac * max_pts, 1)


def _ebitda(ratios: dict[str, RatioResult]) -> float | None:
    """
    Recover the period's EBITDA value from whichever ratio carries it in its
    inputs (operating_income + depreciation). ebitda_margin, leverage, and
    interest_coverage all store these inputs; we check them in turn.

    Returns the EBITDA value, or None if no ratio with those inputs was computed.
    Used by the sign-aware leverage/coverage override.
    """
    for name in ("ebitda_margin", "leverage", "interest_coverage"):
        r = ratios.get(name)
        if isinstance(r, RatioResult):
            inp = r.inputs
            if "operating_income" in inp and "depreciation" in inp:
                return inp["operating_income"] + inp["depreciation"]
    return None


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

    # EBITDA value (operating_income + depreciation) recovered from the ratio
    # inputs. Drives the sign-aware override on leverage and coverage below.
    ebitda_value = _ebitda(ratios)
    ebitda_negative = ebitda_value is not None and ebitda_value <= 0

    # ── Rule 0: Operating profitability (EBITDA margin) ──────────────────────
    # EBITDA margin = EBITDA / revenue. Unlike leverage/coverage this never flips
    # sign, so it reliably penalises operating losses — the most fundamental
    # distress signal. Ramp: 0 pts at/above a 10% margin, rising to the full
    # 20 pts at a -5% margin and below (operating losses).
    prof = _val("ebitda_margin")
    prof_pts = _ramp(prof, healthy=0.10, severe=-0.05, max_pts=20.0)
    breakdown["profitability"] = prof_pts
    if prof_pts > 0:
        alerts.append(f"EBITDA margin {prof * 100:.0f}% weak ({prof_pts:.0f}/20 pts)")

    # ── Rule 1: Leverage > 5× ────────────────────────────────────────────────
    # Net debt / EBITDA above 5× is a widely-used speculative-grade boundary.
    # Investment-grade issuers typically run below 3×. Above 5× indicates
    # the company would take over 5 years of full EBITDA to pay off its debt.
    # Ramp: 0 pts at/below 3× (investment-grade norm), rising to the full 20 pts
    # at 6× and beyond. SIGN-AWARE: negative EBITDA flips the ratio sign, so when
    # EBITDA <= 0 we force the full penalty rather than trusting the ramp (a
    # money-losing issuer cannot be low-leverage). A negative ratio from a
    # net-cash position with positive EBITDA is left to the ramp → 0 pts.
    lev = _val("leverage")
    if ebitda_negative:
        lev_pts = 20.0
        alerts.append("Leverage rule maxed: negative EBITDA — debt unservable from earnings (20/20 pts)")
    else:
        lev_pts = _ramp(lev, healthy=3.0, severe=6.0, max_pts=20.0)
        if lev_pts > 0:
            alerts.append(f"Leverage {lev:.1f}× elevated ({lev_pts:.0f}/20 pts)")
    breakdown["leverage>5x"] = lev_pts

    # ── Rule 2: Interest Coverage < 2× ──────────────────────────────────────
    # EBITDA / interest expense below 2× means earnings barely cover interest.
    # At 1× the company's entire EBITDA goes to interest; below 1× it cannot
    # cover interest from operations at all.
    # Ramp: 0 pts at/above 4× (Fed flags <4× as a distress warning), rising to
    # the full 20 pts at 1× and below. SIGN-AWARE: when EBITDA <= 0 coverage is
    # negative/meaningless and operations cannot cover interest at all, so we
    # force the full penalty.
    cov = _val("interest_coverage")
    if ebitda_negative:
        cov_pts = 20.0
        alerts.append("Coverage rule maxed: negative EBITDA — interest uncovered by earnings (20/20 pts)")
    else:
        cov_pts = _ramp(cov, healthy=4.0, severe=1.0, max_pts=20.0)
        if cov_pts > 0:
            alerts.append(f"Interest coverage {cov:.1f}× thin ({cov_pts:.0f}/20 pts)")
    breakdown["coverage<2x"] = cov_pts

    # ── Rule 3: Free Cash Flow negative ─────────────────────────────────────
    # Negative FCF (OCF minus capex) means the company consumed more cash than
    # it generated, even before considering debt repayment or dividends.
    # Sustained negative FCF forces reliance on debt or equity financing.
    #
    # Scored on FCF margin (FCF / revenue) so the penalty has a magnitude:
    # 0 pts at/above break-even (margin >= 0), rising to the full 20 pts at a
    # deeply negative -10% margin and below. The raw FCF value is used only for
    # the alert string.
    # Ramp on FCF margin: 0 pts at/above break-even (margin >= 0), rising to the
    # full 20 pts at a deeply negative -10% margin and below. Mature issuers
    # target 10–15% FCF margin; sustained negative is the credit concern.
    fcf_margin = _val("fcf_margin")
    fcf = _val("free_cash_flow")
    fcf_pts = _ramp(fcf_margin, healthy=0.0, severe=-0.10, max_pts=15.0)
    breakdown["fcf_negative"] = fcf_pts
    if fcf_pts > 0:
        fcf_str = f"{fcf:,.0f}" if fcf is not None else "n/a"
        alerts.append(
            f"Free cash flow negative ({fcf_str}, {fcf_margin * 100:.0f}% margin, {fcf_pts:.0f}/15 pts)"
        )

    # ── Rule 4: Liquidity < 1× ──────────────────────────────────────────────
    # Cash / short-term debt below 1× means the company can't cover its maturing
    # near-term obligations with cash on hand alone — it would need to refinance
    # or draw on revolving credit facilities.
    # Ramp: 0 pts at/above 1× (cash fully covers short-term debt), rising to the
    # full 15 pts at 0.25× and below — covenant minimums commonly sit at
    # 0.25–0.5×, so below 0.25× signals acute refinancing pressure.
    liq = _val("liquidity")
    liq_pts = _ramp(liq, healthy=1.0, severe=0.25, max_pts=15.0)
    breakdown["liquidity<1x"] = liq_pts
    if liq_pts > 0:
        alerts.append(f"Liquidity {liq:.2f}× thin ({liq_pts:.0f}/15 pts)")

    # ── LLM qualitative adjustment ───────────────────────────────────────────
    # High-severity findings from the LLM review each add 2 pts, capped at 10.
    #
    # Why getattr(f, "severity", "")?
    #   Findings could be Finding dataclass instances (from llm_review.py) or
    #   plain dicts loaded from Supabase. getattr() safely handles dataclasses;
    #   for dicts it returns "" (the default), so dict findings contribute 0 pts.
    #   This makes the function tolerant of both input formats.
    #
    # LLM cap rationale: the high-severity (10), covenant-proximity (6), and
    # loss-provision (6) caps are intentionally low — but to keep the original
    # guarantee that qualitative-only signals can't cross the 50-pt threshold,
    # the COMBINED LLM contribution is clamped to 15 pts at the end (below).
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
    # Ramp: 0 pts at/below 30% near-term, rising to the full 10 pts at 80% and
    # above — rating agencies penalise heavy near-term maturity concentration.
    near_term_pct = _attr(maturity, "near_term_pct") if maturity is not None else None
    wall_pts = _ramp(near_term_pct, healthy=0.30, severe=0.80, max_pts=10.0)
    breakdown["maturity_wall"] = wall_pts
    if wall_pts > 0:
        alerts.append(
            f"Maturity wall: {near_term_pct * 100:.0f}% of debt due within 2 years "
            f"({wall_pts:.0f}/10 pts)"
        )

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

    # ── Combine: core (max 100) + LLM (combined cap 15), then escalate ───────
    # The six deterministic core rules and their maxima. Used both for the
    # severe-signal count (escalation floor) and to separate core from LLM.
    core_maxima = {
        "profitability": 20.0,
        "leverage>5x": 20.0,
        "coverage<2x": 20.0,
        "fcf_negative": 15.0,
        "liquidity<1x": 15.0,
        "maturity_wall": 10.0,
    }
    core_total = sum(breakdown[k] for k in core_maxima)

    # LLM signals: each rule keeps its own cap, but the COMBINED contribution is
    # clamped to 15 so qualitative-only signals can never cross the 50 threshold.
    llm_keys = ("llm_high_severity", "covenant_proximity", "litigation_provision")
    llm_total = min(sum(breakdown[k] for k in llm_keys), 15.0)

    score = min(core_total + llm_total, 100.0)

    # ── Distress escalation floor ────────────────────────────────────────────
    # Count core rules that are "severe" (>= 80% of their max). When >= 3 fire
    # together, the issuer is in compounding distress and is floored at 60
    # (High Risk) so the additive sum can't let it slip under the threshold.
    severe_signals = [k for k, mx in core_maxima.items() if breakdown[k] >= 0.8 * mx]
    if len(severe_signals) >= 3:
        if score < 60.0:
            score = 60.0
        alerts.append(
            f"Distress escalation: {len(severe_signals)} core signals severe "
            f"({', '.join(severe_signals)}) — score floored at 60"
        )

    return ScoreResult(score=score, breakdown=breakdown, alerts=alerts)
