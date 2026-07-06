"""
Deterministic stress scoring per (issuer, period).

How the score is built:
  The score is an additive sum of rule-based penalties over nine deterministic
  core rules (max 100 pts combined), plus capped LLM nudges:
    - Each quantitative ratio rule contributes points on a linear ramp: 0 while
      the ratio is at or healthier than its threshold, then rising continuously
      to the rule's maximum as the ratio worsens toward a severe extreme (and
      clamped at the maximum beyond it). Thresholds are calibrated to rating-agency
      grids and distress research, so a speculative-grade reading already carries
      roughly half a rule's points.
    - The nine core rules and their maxima, grouped by what they measure. Debt
      serviceability dominates, led by the two strongest empirical distress
      predictors (leverage and cash-flow-to-debt):
        Debt serviceability (46):
          leverage (net debt / EBITDA)      17
          interest coverage                 14
          cash flow to debt (FFO/Debt)      15
        Earnings / cash generation (24):
          profitability (EBITDA margin)     14
          free cash flow (FCF margin)       10
        Liquidity (15):
          liquidity (cash / short-term debt) 9
          current ratio                      6
        Solvency (9):
          debt to assets (gearing)           9
        Refinancing (6):
          maturity wall                      6
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
    - DISTRESS ESCALATION floor: if >= 4 core rules are "severe" (>= 80% of their
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


# ── Tunable parameters ─────────────────────────────────────────────────────────

# Every scoring knob lives in one config dict so the backtest UI can experiment
# with different weights/thresholds and (separately) apply them to the live
# portfolio. DEFAULT_CONFIG reproduces the historically hard-coded constants
# EXACTLY — with no saved config and an unedited draft, scores are unchanged.
#
# Per-rule entries are {weight, healthy, severe}: `weight` is the rule's max
# points; the ramp awards 0 pts at `healthy`, rising linearly to `weight` at
# `severe` (see _ramp). The 9 rule keys (and their order) match the breakdown
# keys used throughout the system.
DEFAULT_CONFIG: dict = {
    "rules": {
        "profitability":         {"weight": 14.0, "healthy": 0.10, "severe": -0.05},
        "leverage>5x":           {"weight": 17.0, "healthy": 3.0,  "severe": 6.0},
        "coverage<2x":           {"weight": 14.0, "healthy": 4.0,  "severe": 1.0},
        "cash_flow_to_debt<30%": {"weight": 15.0, "healthy": 0.30, "severe": 0.10},
        "fcf_negative":          {"weight": 10.0, "healthy": 0.0,  "severe": -0.10},
        "liquidity<1x":          {"weight": 9.0,  "healthy": 1.0,  "severe": 0.25},
        "current_ratio<1.5x":    {"weight": 6.0,  "healthy": 1.5,  "severe": 0.75},
        "debt_to_assets>40%":    {"weight": 9.0,  "healthy": 0.40, "severe": 0.65},
        "maturity_wall":         {"weight": 6.0,  "healthy": 0.30, "severe": 0.80},
        # ── 10 additional rules (analytical depth) ───────────────────────────
        # Scored and added to the total, but NOT part of _CORE_RULE_KEYS, so the
        # distress-escalation severe-count still references only the original 9
        # validated core rules. The original 9 still sum to 100, so score_cap
        # stays 100 and the 50-pt threshold / 60-pt floor are unchanged.
        "debt_to_equity>2x":              {"weight": 8.0,  "healthy": 1.0,  "severe": 3.0},
        "revenue_yoy_growth<-5%":         {"weight": 8.0,  "healthy": 0.02, "severe": -0.10},
        "asset_coverage<1.5x":            {"weight": 6.0,  "healthy": 2.0,  "severe": 1.0},
        "tangible_asset_coverage<1x":     {"weight": 8.0,  "healthy": 1.5,  "severe": 0.4},
        "liquidation_asset_coverage<0.7x":{"weight": 8.0,  "healthy": 1.2,  "severe": 0.3},
        "quick_ratio<1x":                 {"weight": 5.0,  "healthy": 1.2,  "severe": 0.5},
        "ocf_ebitda_conversion<0.7x":     {"weight": 6.0,  "healthy": 0.85, "severe": 0.5},
        "moody_adjusted_fcf_negative":    {"weight": 8.0,  "healthy": 0.0,  "severe": -0.10},
        "rcf_net_debt<15%":               {"weight": 10.0, "healthy": 0.30, "severe": 0.05},
        "maturity_coverage_near_term<1x": {"weight": 7.0,  "healthy": 1.5,  "severe": 0.5},
        # Moody's-adjusted leverage (Formula 2, lease-capitalized). SUPPLEMENTS the
        # core leverage>5x rule — same 3×/6× ramp thresholds, additional-bucket
        # weight (not core, so the escalation severe-count is unchanged). Only fires
        # for issuers whose ROU liability is XBRL-tagged (leverage_adjusted present);
        # absent → _ramp(None)=0, so all existing scorecards are unchanged. Its
        # scoring is GATED on adjusted EBITDA > 0 (see compute_score) — the ratio is
        # not meaningful when EBITDA ≤ 0; lease_debt_burden carries severity there.
        "leverage_adjusted>5x":           {"weight": 10.0, "healthy": 3.0,  "severe": 6.0},
        # Lease-inflation multiple (adjusted_net_debt / raw_net_debt), Moody's-style
        # lease capitalization. Layer A (always-on flag) fires at healthy(1.5×)/
        # severe(2.0×) regardless of weight; layer C (this weight) scores only when
        # burden ≥ severe(2×) AND coverage/FCF is weak (see compute_score). Additional
        # bucket, not core → escalation severe-count unchanged.
        #
        # Weight 6 enabled as FP-safe insurance. A/B (2026-07, 5 healthy lease-heavy
        # controls incl. 3 clean across 40 periods) showed C adds ZERO false positives
        # and the gate correctly suppresses healthy names — FP-safety is demonstrated.
        # C's catch/lead-time BENEFIT is UNTESTED: the A/B set had no borderline
        # (just-below-threshold) cases, the only region C could change an outcome, so
        # it neither helped nor hurt there. Enabled on the basis of proven safety +
        # potential value on future borderline lease-heavy names, not demonstrated
        # benefit. Revisit if borderline cases become available to power a real value test.
        "lease_debt_burden":              {"weight": 6.0,  "healthy": 1.5,  "severe": 2.0},
        # Pension-inflation multiple (raw net_debt + unfunded pension) / raw net_debt,
        # Moody's-style pension capitalization — PARALLEL to lease_debt_burden.
        # FLAG-ONLY this pass (weight 0.0): emits a Moody's-provenance pension flag
        # (layer A) where the deterministic XBRL adjustment is available, scores 0.
        # No gated-C weight yet: deterministic coverage is only ~26% of filers and
        # RAD isn't even covered, so there is no representative set to test a weight
        # on — raise only once the LLM footnote layer lifts pension coverage.
        "pension_debt_burden":            {"weight": 0.0,  "healthy": 1.05, "severe": 1.20},
    },
    # Points forced on these rules when EBITDA <= 0 (the ramp would flip sign).
    "ebitda_override": {"leverage>5x": 17.0, "coverage<2x": 14.0},
    # LLM qualitative signals: per-finding points + per-rule caps + combined cap.
    "llm": {
        "high_severity_per": 2.0, "high_severity_cap": 10.0,
        "covenant_per": 3.0,      "covenant_cap": 6.0,
        "provision_per": 2.0,     "provision_cap": 6.0,
        # Going-concern (Stage 2b): Tier-1 is the strongest qualitative signal but
        # still LLM-derived, so it stays inside the combined_cap (15) and cannot
        # cross the 50 threshold alone. Tier-2 is moderate/low-confidence and
        # strictly less than Tier-1 (3 < 8), per LLM_GOING_CONCERN §12.
        "going_concern_tier1_per": 8.0, "going_concern_tier1_cap": 8.0,
        "going_concern_tier2_per": 3.0, "going_concern_tier2_cap": 3.0,
        "combined_cap": 15.0,
    },
    "score_cap": 100.0,
    # Stressed when score >= threshold.
    "threshold": 50,
    # Distress escalation floor: if >= min_severe core rules are "severe"
    # (points >= severe_frac × weight), the final score is floored at `floor`.
    "escalation": {"min_severe": 4, "severe_frac": 0.8, "floor": 60.0},
}

# Canonical order of the 9 core (quantitative) rule keys. Fixes the order in
# which severe signals are listed in the escalation alert, independent of how a
# stored/merged config dict was constructed.
_CORE_RULE_KEYS = (
    "profitability", "leverage>5x", "coverage<2x", "cash_flow_to_debt<30%",
    "fcf_negative", "liquidity<1x", "current_ratio<1.5x", "debt_to_assets>40%",
    "maturity_wall",
)

# The 10 additional rules: rule key → the ratio name (in extract_all's output)
# whose value drives the ramp. These are scored and added to the additive total,
# but deliberately NOT included in _CORE_RULE_KEYS — the distress-escalation
# severe-count must keep counting only the original 9 validated core rules.
# Direction (higher- vs lower-worse) is inferred by _ramp from each rule's
# healthy/severe pair, so no per-rule direction flag is needed.
_ADDITIONAL_RULE_RATIOS = {
    "debt_to_equity>2x":              "debt_to_equity",
    "revenue_yoy_growth<-5%":         "revenue_yoy_growth",
    "asset_coverage<1.5x":            "asset_coverage",
    "tangible_asset_coverage<1x":     "tangible_asset_coverage",
    "liquidation_asset_coverage<0.7x":"liquidation_asset_coverage",
    "quick_ratio<1x":                 "quick_ratio",
    "ocf_ebitda_conversion<0.7x":     "ocf_ebitda_conversion",
    "moody_adjusted_fcf_negative":    "moody_adjusted_fcf",
    "rcf_net_debt<15%":               "rcf_net_debt",
    "maturity_coverage_near_term<1x": "maturity_coverage_near_term",
    # Both handled specially in compute_score (not by the generic ramp loop):
    # leverage_adjusted>5x is gated on adjusted EBITDA > 0; lease_debt_burden is
    # flag-only. Listed here so additional_total sums them and from_dict carries them.
    "leverage_adjusted>5x":           "leverage_adjusted",
    "lease_debt_burden":              "lease_debt_burden",
    # Flag-only (weight 0) this pass — bespoke block in compute_score.
    "pension_debt_burden":            "pension_debt_burden",
}

# Additional rules with bespoke scoring logic in compute_score — the generic ramp
# loop SKIPS these and sets their breakdown entries explicitly.
_SPECIAL_ADDITIONAL_RULES = frozenset({"leverage_adjusted>5x", "lease_debt_burden", "pension_debt_burden"})

# Co-condition threshold for lease_debt_burden Option-C scoring: a severe lease
# burden only scores when interest coverage is below this (or FCF is negative).
LEASE_BURDEN_WEAK_COVERAGE = 2.0


@dataclass(frozen=True)
class ScoreConfig:
    """
    Typed, validated view over a config dict (see DEFAULT_CONFIG).

    from_dict deep-merges a (possibly partial) dict over DEFAULT_CONFIG, so a
    config saved before a future knob is added still loads — missing keys fall
    back to defaults. This is the forward/back-compat mechanism.
    """
    rules: dict
    ebitda_override: dict
    llm: dict
    score_cap: float
    escalation: dict
    threshold: int

    @classmethod
    def from_dict(cls, d: dict | None) -> "ScoreConfig":
        d = d or {}
        in_rules = d.get("rules") or {}
        merged_rules: dict[str, dict[str, float]] = {}
        for key, defaults in DEFAULT_CONFIG["rules"].items():
            r = {**defaults, **(in_rules.get(key) or {})}
            merged_rules[key] = {
                "weight": float(r["weight"]),
                "healthy": float(r["healthy"]),
                "severe": float(r["severe"]),
            }
        ebitda_override = {
            k: float(v)
            for k, v in {**DEFAULT_CONFIG["ebitda_override"], **(d.get("ebitda_override") or {})}.items()
        }
        llm = {
            k: float(v)
            for k, v in {**DEFAULT_CONFIG["llm"], **(d.get("llm") or {})}.items()
        }
        esc = {**DEFAULT_CONFIG["escalation"], **(d.get("escalation") or {})}
        escalation = {
            "min_severe": int(esc["min_severe"]),
            "severe_frac": float(esc["severe_frac"]),
            "floor": float(esc["floor"]),
        }
        return cls(
            rules=merged_rules,
            ebitda_override=ebitda_override,
            llm=llm,
            score_cap=float(d.get("score_cap", DEFAULT_CONFIG["score_cap"])),
            escalation=escalation,
            threshold=int(d.get("threshold", DEFAULT_CONFIG["threshold"])),
        )

    def to_dict(self) -> dict:
        return {
            "rules": {k: dict(v) for k, v in self.rules.items()},
            "ebitda_override": dict(self.ebitda_override),
            "llm": dict(self.llm),
            "score_cap": self.score_cap,
            "escalation": dict(self.escalation),
            "threshold": self.threshold,
        }


# The default config, fully materialised — used as the keyword default below.
DEFAULT = ScoreConfig.from_dict(DEFAULT_CONFIG)

# The cut-off score above which an issuer is flagged as "stressed".
# Imported by: src/backtest.py, api/main.py, src/track.py, lib/api.ts (frontend).
# Defined here from DEFAULT_CONFIG so the literal lives in exactly one place.
STRESS_THRESHOLD = DEFAULT_CONFIG["threshold"]


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
    going_concern: list[Any] | None = None,
    *,
    config: ScoreConfig = DEFAULT,
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
        config:          Scoring parameters (weights, ramp thresholds, caps,
                         escalation). Defaults to DEFAULT, which reproduces the
                         original hard-coded behavior exactly.

    Returns:
        ScoreResult with score, per-rule breakdown, and alert strings.
    """
    if findings is None:
        findings = []
    if covenants is None:
        covenants = []
    if loss_provisions is None:
        loss_provisions = []
    if going_concern is None:
        going_concern = []

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

    rules = config.rules  # {rule_key: {weight, healthy, severe}}

    # ── Rule 0: Operating profitability (EBITDA margin) ──────────────────────
    # EBITDA margin = EBITDA / revenue. Unlike leverage/coverage this never flips
    # sign, so it reliably penalises operating losses — the most fundamental
    # distress signal. Ramp: 0 pts at/above the healthy margin, rising to the full
    # weight at the severe margin and below (operating losses).
    prof_w = rules["profitability"]["weight"]
    prof = _val("ebitda_margin")
    prof_pts = _ramp(prof, rules["profitability"]["healthy"], rules["profitability"]["severe"], prof_w)
    breakdown["profitability"] = prof_pts
    if prof_pts > 0:
        alerts.append(f"EBITDA margin {prof * 100:.0f}% weak ({prof_pts:.0f}/{prof_w:.0f} pts)")

    # ── Rule 1: Leverage > 5× ────────────────────────────────────────────────
    # Net debt / EBITDA above 5× is a widely-used speculative-grade boundary.
    # Investment-grade issuers typically run below 3×. SIGN-AWARE: negative EBITDA
    # flips the ratio sign, so when EBITDA <= 0 we force the full penalty rather
    # than trusting the ramp (a money-losing issuer cannot be low-leverage). A
    # negative ratio from a net-cash position with positive EBITDA → ramp → 0 pts.
    lev_w = rules["leverage>5x"]["weight"]
    lev = _val("leverage")
    if ebitda_negative:
        lev_pts = config.ebitda_override["leverage>5x"]
        alerts.append(f"Leverage rule maxed: negative EBITDA — debt unservable from earnings ({lev_pts:.0f}/{lev_w:.0f} pts)")
    else:
        lev_pts = _ramp(lev, rules["leverage>5x"]["healthy"], rules["leverage>5x"]["severe"], lev_w)
        if lev_pts > 0:
            alerts.append(f"Leverage {lev:.1f}× elevated ({lev_pts:.0f}/{lev_w:.0f} pts)")
    breakdown["leverage>5x"] = lev_pts

    # ── Rule 2: Interest Coverage < 2× ──────────────────────────────────────
    # EBITDA / interest expense below 2× means earnings barely cover interest.
    # SIGN-AWARE: when EBITDA <= 0 coverage is negative/meaningless and operations
    # cannot cover interest at all, so we force the full penalty.
    cov_w = rules["coverage<2x"]["weight"]
    cov = _val("interest_coverage")
    if ebitda_negative:
        cov_pts = config.ebitda_override["coverage<2x"]
        alerts.append(f"Coverage rule maxed: negative EBITDA — interest uncovered by earnings ({cov_pts:.0f}/{cov_w:.0f} pts)")
    else:
        cov_pts = _ramp(cov, rules["coverage<2x"]["healthy"], rules["coverage<2x"]["severe"], cov_w)
        if cov_pts > 0:
            alerts.append(f"Interest coverage {cov:.1f}× thin ({cov_pts:.0f}/{cov_w:.0f} pts)")
    breakdown["coverage<2x"] = cov_pts

    # ── Rule 3: Free Cash Flow negative ─────────────────────────────────────
    # Negative FCF (OCF minus capex) means the company consumed more cash than it
    # generated. Scored on FCF margin (FCF / revenue) so the penalty has a
    # magnitude: 0 pts at/above the healthy margin, rising to the full weight at
    # the severe margin and below. The raw FCF value is used only for the alert.
    fcf_w = rules["fcf_negative"]["weight"]
    fcf_margin = _val("fcf_margin")
    fcf = _val("free_cash_flow")
    fcf_pts = _ramp(fcf_margin, rules["fcf_negative"]["healthy"], rules["fcf_negative"]["severe"], fcf_w)
    breakdown["fcf_negative"] = fcf_pts
    if fcf_pts > 0:
        fcf_str = f"{fcf:,.0f}" if fcf is not None else "n/a"
        alerts.append(
            f"Free cash flow negative ({fcf_str}, {fcf_margin * 100:.0f}% margin, {fcf_pts:.0f}/{fcf_w:.0f} pts)"
        )

    # ── Rule 4: Liquidity < 1× ──────────────────────────────────────────────
    # Cash / short-term debt below 1× means the company can't cover its maturing
    # near-term obligations with cash on hand alone. Ramp: 0 pts at/above the
    # healthy level, rising to the full weight at the severe level and below.
    liq_w = rules["liquidity<1x"]["weight"]
    liq = _val("liquidity")
    liq_pts = _ramp(liq, rules["liquidity<1x"]["healthy"], rules["liquidity<1x"]["severe"], liq_w)
    breakdown["liquidity<1x"] = liq_pts
    if liq_pts > 0:
        alerts.append(f"Liquidity {liq:.2f}× thin ({liq_pts:.0f}/{liq_w:.0f} pts)")

    # ── Rule 4b: Cash flow to debt < 30% ─────────────────────────────────────
    # operating_cashflow / gross_debt — a proxy for the rating agencies' FFO/Debt,
    # the single most predictive distress ratio in the literature (Beaver, Jooste).
    cfd_w = rules["cash_flow_to_debt<30%"]["weight"]
    cfd = _val("cash_flow_to_debt")
    cfd_pts = _ramp(cfd, rules["cash_flow_to_debt<30%"]["healthy"], rules["cash_flow_to_debt<30%"]["severe"], cfd_w)
    breakdown["cash_flow_to_debt<30%"] = cfd_pts
    if cfd_pts > 0:
        alerts.append(f"Cash flow to debt {cfd * 100:.0f}% weak ({cfd_pts:.0f}/{cfd_w:.0f} pts)")

    # ── Rule 4c: Current ratio < 1.5× ────────────────────────────────────────
    # current_assets / current_liabilities — working-capital liquidity.
    cur_w = rules["current_ratio<1.5x"]["weight"]
    cur = _val("current_ratio")
    cur_pts = _ramp(cur, rules["current_ratio<1.5x"]["healthy"], rules["current_ratio<1.5x"]["severe"], cur_w)
    breakdown["current_ratio<1.5x"] = cur_pts
    if cur_pts > 0:
        alerts.append(f"Current ratio {cur:.2f}× thin ({cur_pts:.0f}/{cur_w:.0f} pts)")

    # ── Rule 4d: Debt to assets > 40% ────────────────────────────────────────
    # gross_debt / total_assets — capital-structure gearing, the sole balance-sheet
    # solvency rule. Ramp: 0 pts at/below the healthy level, rising to the full
    # weight at the severe level and above.
    dta_w = rules["debt_to_assets>40%"]["weight"]
    dta = _val("debt_to_assets")
    dta_pts = _ramp(dta, rules["debt_to_assets>40%"]["healthy"], rules["debt_to_assets>40%"]["severe"], dta_w)
    breakdown["debt_to_assets>40%"] = dta_pts
    if dta_pts > 0:
        alerts.append(f"Debt to assets {dta * 100:.0f}% elevated ({dta_pts:.0f}/{dta_w:.0f} pts)")

    # ── Additional rules (10): scored like the core ratio rules ──────────────
    # Each maps to one ratio value and ramps on its own healthy/severe pair.
    # They add to the breakdown and the additive total, but are excluded from
    # the escalation severe-count (which counts only _CORE_RULE_KEYS). A rule is
    # silently skipped if it isn't present in the active config (back-compat with
    # configs saved before these rules existed).
    for rule_key, ratio_name in _ADDITIONAL_RULE_RATIOS.items():
        if rule_key in _SPECIAL_ADDITIONAL_RULES:
            continue  # scored below with bespoke logic
        rule = rules.get(rule_key)
        if rule is None:
            breakdown[rule_key] = 0.0
            continue
        add_w = rule["weight"]
        add_val = _val(ratio_name)
        add_pts = _ramp(add_val, rule["healthy"], rule["severe"], add_w)
        breakdown[rule_key] = add_pts
        if add_pts > 0:
            alerts.append(f"{ratio_name} {add_val:.2f} stressed ({add_pts:.0f}/{add_w:.0f} pts)")

    # ── Adjusted leverage (lease-capitalized) — GATED on adjusted EBITDA > 0 ──
    # The leverage_adjusted RATIO is stored faithfully even when adjusted EBITDA ≤ 0
    # (a degenerate negative value, kept for trajectory tracking), but it is only
    # MEANINGFUL as a stress ramp when EBITDA > 0 (e.g. profitable lease-heavy names
    # like Stein Mart 4.8×→19×). When adjusted EBITDA ≤ 0 the rule scores 0 and we
    # flag it — the debt-burden signal below carries severity instead.
    la_rule = rules.get("leverage_adjusted>5x")
    la = ratios.get("leverage_adjusted")
    la_pts = 0.0
    if la_rule is not None and isinstance(la, RatioResult):
        adj_ebitda = la.inputs.get("adjusted_ebitda")
        if adj_ebitda is not None and adj_ebitda > 0:
            la_pts = _ramp(la.value, la_rule["healthy"], la_rule["severe"], la_rule["weight"])
            if la_pts > 0:
                alerts.append(
                    f"Adjusted leverage {la.value:.1f}× (lease-capitalized) elevated "
                    f"({la_pts:.0f}/{la_rule['weight']:.0f} pts)"
                )
        else:
            alerts.append(
                "Adjusted leverage ratio not meaningful (EBITDA≤0) — see lease_debt_burden"
            )
    breakdown["leverage_adjusted>5x"] = la_pts

    # ── Lease-debt burden — Moody's lease capitalization (A: always-flag + C: selective score) ──
    # adjusted_net_debt / raw_net_debt = how much operating leases inflate the debt
    # obligation under Moody's adjustment criteria. Always defined (no EBITDA in the
    # denominator), so it works at any profitability. Two layers:
    #   A — ALWAYS-ON flag (0 pts): emit a watch (≥ healthy=1.5×) / flag (≥ severe=2×)
    #       alert for any computable burden. Pure visibility → zero FP risk.
    #   C — SELECTIVE scoring (weight-gated): award points ONLY when the burden is
    #       severe (≥2×) AND the issuer is already weak on cash servicing
    #       (interest_coverage < LEASE_BURDEN_WEAK_COVERAGE OR moody_adjusted_fcf < 0).
    #       weight 0.0 ⇒ C-off (baseline); a positive weight ⇒ C-on (A/B is a pure
    #       config diff). Additional bucket, so the escalation severe-count is untouched.
    ldb_rule = rules.get("lease_debt_burden")
    ldb = ratios.get("lease_debt_burden")
    ldb_pts = 0.0
    if ldb_rule is not None and isinstance(ldb, RatioResult):
        burden = ldb.value
        adj_b = ldb.inputs.get("adjusted_net_debt", 0.0) / 1e9
        raw_b = ldb.inputs.get("raw_net_debt", 0.0) / 1e9
        # A — always-on flag, Moody's provenance wording (both watch and flag levels).
        if burden >= ldb_rule["severe"]:
            alerts.append(
                f"Operating leases capitalized under Moody's adjustment criteria: "
                f"adjusted debt {burden:.2f}× raw (${adj_b:.1f}B vs ${raw_b:.1f}B) (flag)"
            )
        elif burden >= ldb_rule["healthy"]:
            alerts.append(
                f"Operating leases capitalized under Moody's adjustment criteria: "
                f"adjusted debt {burden:.2f}× raw (${adj_b:.1f}B vs ${raw_b:.1f}B) (watch)"
            )
        # C — selective scoring: severe burden AND weak coverage/FCF co-condition.
        cov = _val("interest_coverage")
        mfcf = _val("moody_adjusted_fcf")
        weak = (cov is not None and cov < LEASE_BURDEN_WEAK_COVERAGE) or (mfcf is not None and mfcf < 0.0)
        if burden >= ldb_rule["severe"] and weak:
            ldb_pts = ldb_rule["weight"]  # gate is at `severe`, so the ramp would saturate → full weight
            if ldb_pts > 0:
                alerts.append(
                    f"Lease-debt burden scored: {burden:.2f}× raw debt with weak coverage/FCF "
                    f"({ldb_pts:.0f}/{ldb_rule['weight']:.0f} pts)"
                )
    breakdown["lease_debt_burden"] = ldb_pts

    # ── Pension-debt burden — Moody's pension capitalization (FLAG-ONLY this pass) ──
    # (raw net_debt + unfunded pension) / raw net_debt. Layer A only: emit a
    # Moody's-provenance flag where the deterministic XBRL adjustment is available
    # (funded status tagged / derivable, ~26% of filers), and score 0. No gated-C
    # weight yet — coverage is too thin to calibrate/test, and RAD isn't covered.
    # Parallel to lease_debt_burden; independent of it (own ratio, own provenance).
    pdb_rule = rules.get("pension_debt_burden")
    pdb = ratios.get("pension_debt_burden")
    pdb_pts = 0.0
    if pdb_rule is not None and isinstance(pdb, RatioResult):
        pburden = pdb.value
        adj_b = pdb.inputs.get("adjusted_net_debt", 0.0) / 1e9
        raw_b = pdb.inputs.get("raw_net_debt", 0.0) / 1e9
        deficit_b = pdb.inputs.get("unfunded_pension_added", 0.0) / 1e9
        if pburden >= pdb_rule["severe"]:
            alerts.append(
                f"Unfunded pension capitalized under Moody's adjustment criteria: "
                f"deficit ${deficit_b:.1f}B, adjusted debt {pburden:.2f}× raw "
                f"(${adj_b:.1f}B vs ${raw_b:.1f}B) (flag)"
            )
        elif pburden >= pdb_rule["healthy"]:
            alerts.append(
                f"Unfunded pension capitalized under Moody's adjustment criteria: "
                f"deficit ${deficit_b:.1f}B, adjusted debt {pburden:.2f}× raw "
                f"(${adj_b:.1f}B vs ${raw_b:.1f}B) (watch)"
            )
        # No scoring this pass: pdb_rule["weight"] is 0.0 (flag-only).
        pdb_pts = _ramp(pburden, pdb_rule["healthy"], pdb_rule["severe"], pdb_rule["weight"])
    breakdown["pension_debt_burden"] = pdb_pts

    # ── LLM qualitative adjustment ───────────────────────────────────────────
    # High-severity findings each add `high_severity_per` pts, capped at
    # `high_severity_cap`. Findings may be Finding dataclasses or dicts loaded
    # from Supabase — _attr reads severity from either. The COMBINED LLM
    # contribution is clamped (combined_cap) at the end so qualitative-only
    # signals can't cross the threshold alone.
    high_sev = [f for f in findings if _attr(f, "severity", "") == "high"]
    llm_pts = min(len(high_sev) * config.llm["high_severity_per"], config.llm["high_severity_cap"])
    breakdown["llm_high_severity"] = llm_pts
    if high_sev:
        alerts.append(f"{len(high_sev)} high-severity qualitative concern(s) flagged")

    # ── Rule 5: Maturity wall (DETERMINISTIC, full weight) ───────────────────
    # near_term_pct = (y1 + y2 + y3) / total scheduled principal, from XBRL maturity
    # tags. XBRL-derived (not LLM), so it carries full weight like the ratio rules.
    # None (no/zero schedule) → skip.
    wall_w = rules["maturity_wall"]["weight"]
    near_term_pct = _attr(maturity, "near_term_pct") if maturity is not None else None
    # Reconciliation guard: when the tagged buckets don't reconcile with XBRL
    # total debt (schedule_confidence == "degraded", e.g. RAD dropping the
    # y5/thereafter buckets), near_term_pct is computed off a truncated total and
    # would score artificially high. Suppress the rule (treat as None → 0 pts, so
    # it stays out of the severe-count / escalation floor) and route to review
    # rather than scoring a wrong number at full deterministic weight.
    # "high", "unknown", and absent (legacy dicts) all fall through unchanged.
    schedule_confidence = _attr(maturity, "schedule_confidence", None) if maturity is not None else None
    if schedule_confidence == "degraded":
        breakdown["maturity_wall"] = 0.0
        alerts.append(
            "Maturity schedule under-reconciled with total debt "
            "— maturity_wall suppressed, routed to review"
        )
    else:
        wall_pts = _ramp(near_term_pct, rules["maturity_wall"]["healthy"], rules["maturity_wall"]["severe"], wall_w)
        breakdown["maturity_wall"] = wall_pts
        if wall_pts > 0:
            alerts.append(
                f"Maturity wall: {near_term_pct * 100:.0f}% of debt due within 3 years "
                f"({wall_pts:.0f}/{wall_w:.0f} pts)"
            )

    # ── Rule 6: Covenant proximity (LLM-DERIVED, capped) ─────────────────────
    # Each covenant the footnote describes the company as close to breaching adds
    # `covenant_per` pts, capped at `covenant_cap`. LLM-derived, so kept marginal.
    near_cov = [c for c in covenants if _attr(c, "near_limit", False)]
    cov_prox_pts = min(len(near_cov) * config.llm["covenant_per"], config.llm["covenant_cap"])
    breakdown["covenant_proximity"] = cov_prox_pts
    if near_cov:
        alerts.append(f"{len(near_cov)} covenant(s) near their limit")

    # ── Rule 7: Material loss provisions (LLM-DERIVED, capped) ───────────────
    # Each material litigation/contingency provision adds `provision_per` pts,
    # capped at `provision_cap`.
    material = [p for p in loss_provisions if _attr(p, "is_material", False)]
    prov_pts = min(len(material) * config.llm["provision_per"], config.llm["provision_cap"])
    breakdown["litigation_provision"] = prov_pts
    if material:
        alerts.append(f"{len(material)} material loss provision(s) disclosed")

    # ── Rule 8: Going-concern (LLM-DERIVED, capped; Stage 2b) ────────────────
    # Tier-1 (formal substantial-doubt) is the strongest qualitative signal;
    # Tier-2 (soft precursor) is moderate and strictly lower (3 < 8), never equal.
    # Both stay inside the combined LLM cap so they can't cross the threshold
    # alone. GoingConcern items may be dataclasses or dicts (Supabase) — _attr.
    gc_t1 = [g for g in going_concern if _attr(g, "tier") == 1]
    gc_t2 = [g for g in going_concern if _attr(g, "tier") == 2]
    gc_pts = (
        min(len(gc_t1) * config.llm["going_concern_tier1_per"], config.llm["going_concern_tier1_cap"])
        + min(len(gc_t2) * config.llm["going_concern_tier2_per"], config.llm["going_concern_tier2_cap"])
    )
    breakdown["going_concern"] = gc_pts
    if gc_t1:
        alerts.append(f"Going-concern: formal substantial-doubt (Tier 1) flagged "
                      f"({gc_pts:.0f} pts)")
    elif gc_t2:
        alerts.append(f"Going-concern: {len(gc_t2)} soft precursor(s) (Tier 2) "
                      f"({gc_pts:.0f} pts)")

    # ── Combine: core + LLM (combined cap), then escalate ────────────────────
    # The deterministic core rules and their maxima (weights). Used both for the
    # severe-signal count (escalation floor) and to separate core from LLM.
    core_maxima = {key: rules[key]["weight"] for key in _CORE_RULE_KEYS}
    core_total = sum(breakdown[k] for k in core_maxima)

    # Additional (deterministic) rules: full-weight, added to the total like the
    # core rules. They are NOT in core_maxima, so they don't feed the escalation
    # severe-count. score_cap (100) still clamps the combined sum.
    additional_total = sum(breakdown.get(k, 0.0) for k in _ADDITIONAL_RULE_RATIOS)

    # LLM signals: each rule keeps its own cap, but the COMBINED contribution is
    # clamped (combined_cap) so qualitative-only signals can never cross the
    # threshold alone.
    llm_keys = ("llm_high_severity", "covenant_proximity", "litigation_provision", "going_concern")
    llm_total = min(sum(breakdown[k] for k in llm_keys), config.llm["combined_cap"])

    score = min(core_total + additional_total + llm_total, config.score_cap)

    # ── Distress escalation floor ────────────────────────────────────────────
    # Count core rules that are "severe" (>= severe_frac of their max). When
    # >= min_severe fire together, the issuer is in compounding distress and is
    # floored at `floor` so the additive sum can't let it slip under the threshold.
    severe_frac = config.escalation["severe_frac"]
    severe_signals = [k for k, mx in core_maxima.items() if breakdown[k] >= severe_frac * mx]
    if len(severe_signals) >= config.escalation["min_severe"]:
        floor = config.escalation["floor"]
        if score < floor:
            score = floor
        alerts.append(
            f"Distress escalation: {len(severe_signals)} core signals severe "
            f"({', '.join(severe_signals)}) — score floored at {floor:.0f}"
        )

    return ScoreResult(score=score, breakdown=breakdown, alerts=alerts)
