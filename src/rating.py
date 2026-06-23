"""
Map the deterministic credit ratios to an S&P-style implied credit rating.

Where this fits (Phase A of the rating-migration work):
  extract.py → extract_all(facts, period_end) → dict of RatioResult objects
  rating.py  → compute_implied_rating(ratios) → ImpliedRatingResult (AAA…CCC-)
  score.py   → compute_score(...) → ScoreResult (the orthogonal 0–100 stress score)

This module is deliberately the rating analogue of score.py:
  - a tunable config dict (DEFAULT_RATING_CONFIG) so the grids/anchor matrix can be
    recalibrated without touching code,
  - a typed, deep-merging view over it (RatingConfig.from_dict),
  - a deterministic mapping (compute_implied_rating) returning an audit-carrying
    result (ImpliedRatingResult), and
  - a materialised module-level DEFAULT used as the keyword default.

How the mapping works (three deterministic stages):
  1. Three sub-factors are bucketed into one of S&P's six FINANCIAL RISK PROFILES
     (Minimal … Highly-Leveraged) using benchmark grids:
        FFO/Debt        ← cash_flow_to_debt   (a CFO/Debt proxy for FFO/Debt)
        Debt/EBITDA     ← leverage
        EBITDA/Interest ← interest_coverage
     These bands are anchored to S&P's published cash-flow/leverage benchmarks.
  2. The three sub-factor profile indices are blended (weighted toward FFO/Debt,
     the strongest distress predictor) into one financial-risk index 1..6.
  3. An ANCHOR MATRIX combines the financial-risk index with a BUSINESS-RISK index
     (1 Excellent … 6 Vulnerable) to yield the implied rating letter.

Honesty notes (surfaced in the result `notes` / `subscores`):
  - FFO/Debt here is a CFO/Debt PROXY (true FFO excludes working-capital swings).
  - The grid edges and anchor matrix are public-methodology APPROXIMATIONS, not the
    agencies' exact (industry-specific, qualitatively-modified) criteria. They are
    meant to be validated against real agency ratings (Phase B).
  - Business risk defaults to a mid value until a per-issuer input is supplied, so
    the rating reflects the financial profile primarily.

Returns None (never a guess) when fewer than two of the three sub-factors resolve —
mirroring extract.py's "never fabricate a number" contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.extract import RatioResult, recover_ebitda


# ── Rating scale ─────────────────────────────────────────────────────────────

# The ordered rating scale, best (index 0) to worst. The INDEX into this tuple is
# the canonical "rating notch number" used everywhere downstream (agency-rating
# alignment, migration labels, seniority notching) — defined here in exactly one
# place, the same discipline as score.STRESS_THRESHOLD.
#
# The scale runs AAA (0) → D (21), 22 notches, so it spans the FULL S&P/Fitch space
# including the distressed/default tail (CC, C, D). It is APPEND-ONLY below CCC-:
# the implied-rating anchor matrix only ever emits AAA…CCC+, so adding CC/C/D leaves
# every previously-stored rating_index unchanged. The agency-rating scale (Stage 1)
# maps onto this same index so implied and actual ratings are directly comparable.
RATING_SCALE: tuple[str, ...] = (
    "AAA",
    "AA+", "AA", "AA-",
    "A+", "A", "A-",
    "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-",
    "B+", "B", "B-",
    "CCC+", "CCC", "CCC-",
    "CC", "C", "D",
)

# The six S&P financial-risk profiles, indexed 1..6 (1 = strongest). Index 0 is
# left unused so the printed index matches S&P's 1..6 convention.
FINANCIAL_RISK_PROFILES: tuple[str, ...] = (
    "",            # 0 — unused (keeps 1-based indexing readable)
    "Minimal",
    "Modest",
    "Intermediate",
    "Significant",
    "Aggressive",
    "Highly Leveraged",
)

# The six business-risk profiles, indexed 1..6 (1 = strongest).
BUSINESS_RISK_PROFILES: tuple[str, ...] = (
    "",            # 0 — unused
    "Excellent",
    "Strong",
    "Satisfactory",
    "Fair",
    "Weak",
    "Vulnerable",
)


def rating_index(letter: str) -> int:
    """Return a rating letter's position in RATING_SCALE (0 = AAA). Raises on unknown."""
    return RATING_SCALE.index(letter)


def is_investment_grade(letter: str) -> bool:
    """True for BBB- and above (the investment-grade / speculative-grade boundary)."""
    return RATING_SCALE.index(letter) <= RATING_SCALE.index("BBB-")


# ── Tunable parameters ───────────────────────────────────────────────────────

# All rating knobs live in one config dict so the grids and anchor matrix can be
# recalibrated (e.g. against real agency ratings in Phase B) without code changes.
#
# grids:    each sub-factor maps to {source, higher_is_better, edges}. `source` is
#           the RatioResult key it reads. `edges` are the FIVE boundaries between
#           the six financial-risk profiles, ordered from the profile-1 side toward
#           the profile-6 side (descending for higher-is-better metrics, ascending
#           for lower-is-better) — see _grid_profile.
# weights:  how the three sub-factor profile indices blend into one financial-risk
#           index. FFO/Debt is weighted highest (the strongest distress predictor).
# anchor_matrix: 6×6 grid [business_risk-1][financial_risk-1] → rating letter. Rows
#           are business risk 1..6 (Excellent…Vulnerable); columns financial risk
#           1..6 (Minimal…Highly Leveraged). Cells are midpoint approximations of
#           S&P's published anchor table.
DEFAULT_RATING_CONFIG: dict = {
    "grids": {
        # FFO/Debt (proxy = CFO/gross debt). Higher is healthier. S&P bands:
        # Minimal ≥60%, Modest 45–60%, Intermediate 30–45%, Significant 20–30%,
        # Aggressive 12–20%, Highly Leveraged <12%.
        "ffo_to_debt": {
            "source": "cash_flow_to_debt",
            "higher_is_better": True,
            "edges": [0.60, 0.45, 0.30, 0.20, 0.12],
        },
        # Debt/EBITDA (= net_debt/EBITDA via `leverage`). Lower is healthier. S&P:
        # Minimal <1.5×, Modest 1.5–2×, Intermediate 2–3×, Significant 3–4×,
        # Aggressive 4–5×, Highly Leveraged >5×.
        "debt_to_ebitda": {
            "source": "leverage",
            "higher_is_better": False,
            "edges": [1.5, 2.0, 3.0, 4.0, 5.0],
        },
        # EBITDA/Interest coverage. Higher is healthier. S&P:
        # Minimal >15×, Modest 10–15×, Intermediate 6–10×, Significant 3–6×,
        # Aggressive 2–3×, Highly Leveraged <2×.
        "ebitda_to_interest": {
            "source": "interest_coverage",
            "higher_is_better": True,
            "edges": [15.0, 10.0, 6.0, 3.0, 2.0],
        },
    },
    "weights": {
        "ffo_to_debt": 0.45,
        "debt_to_ebitda": 0.35,
        "ebitda_to_interest": 0.20,
    },
    # 1 = Excellent business risk … 6 = Vulnerable. Used until a per-issuer input is
    # supplied, so the implied rating reflects the financial profile primarily.
    "business_risk_default": 3,
    # Minimum number of sub-factors that must resolve to emit a rating at all.
    "min_subfactors": 2,
    # rows = business risk 1..6 (Excellent…Vulnerable);
    # cols = financial risk 1..6 (Minimal…Highly Leveraged).
    "anchor_matrix": [
        ["AAA", "AA",  "A+",  "A-",  "BBB", "BBB-"],   # 1 Excellent
        ["AA",  "A+",  "A-",  "BBB", "BB+", "BB"],     # 2 Strong
        ["A",   "BBB+","BBB", "BB+", "BB",  "B+"],     # 3 Satisfactory
        ["BBB+","BBB", "BB+", "BB",  "BB-", "B"],      # 4 Fair
        ["BBB-","BB+", "BB",  "BB-", "B+",  "B-"],     # 5 Weak
        ["BB",  "BB-", "B+",  "B",   "B-",  "CCC+"],   # 6 Vulnerable
    ],
}


@dataclass(frozen=True)
class RatingConfig:
    """
    Typed, validated view over a rating config dict (see DEFAULT_RATING_CONFIG).

    from_dict deep-merges a (possibly partial) dict over DEFAULT_RATING_CONFIG so a
    config saved before a future knob is added still loads — the same forward/back
    compat mechanism as score.ScoreConfig.
    """
    grids: dict
    weights: dict
    business_risk_default: int
    min_subfactors: int
    anchor_matrix: list

    @classmethod
    def from_dict(cls, d: dict | None) -> "RatingConfig":
        d = d or {}
        in_grids = d.get("grids") or {}
        merged_grids: dict[str, dict] = {}
        for key, defaults in DEFAULT_RATING_CONFIG["grids"].items():
            g = {**defaults, **(in_grids.get(key) or {})}
            merged_grids[key] = {
                "source": str(g["source"]),
                "higher_is_better": bool(g["higher_is_better"]),
                "edges": [float(e) for e in g["edges"]],
            }
        weights = {
            k: float(v)
            for k, v in {**DEFAULT_RATING_CONFIG["weights"], **(d.get("weights") or {})}.items()
        }
        anchor = d.get("anchor_matrix") or DEFAULT_RATING_CONFIG["anchor_matrix"]
        return cls(
            grids=merged_grids,
            weights=weights,
            business_risk_default=int(d.get("business_risk_default", DEFAULT_RATING_CONFIG["business_risk_default"])),
            min_subfactors=int(d.get("min_subfactors", DEFAULT_RATING_CONFIG["min_subfactors"])),
            anchor_matrix=[list(row) for row in anchor],
        )

    def to_dict(self) -> dict:
        return {
            "grids": {k: dict(v) for k, v in self.grids.items()},
            "weights": dict(self.weights),
            "business_risk_default": self.business_risk_default,
            "min_subfactors": self.min_subfactors,
            "anchor_matrix": [list(row) for row in self.anchor_matrix],
        }


# Materialised default config, used as the keyword default below.
DEFAULT = RatingConfig.from_dict(DEFAULT_RATING_CONFIG)


# ── Result container ─────────────────────────────────────────────────────────

@dataclass
class ImpliedRatingResult:
    """
    Carries every output of compute_implied_rating() for one (issuer, period).

    Attributes:
        implied_rating:         the rating letter, e.g. "BBB-".
        rating_index:           its position in RATING_SCALE (0 = AAA). The canonical
                                numeric form used by migration prediction / notching.
        financial_risk_profile: name of the blended financial-risk band, e.g.
                                "Intermediate".
        financial_risk_index:   the blended band index 1..6.
        business_risk_index:    the business-risk axis used (input or default), 1..6.
        subscores:              per-sub-factor audit: {sub_factor: {value, profile,
                                source_ratio, overridden}} — records which RatioResult
                                fed each sub-factor and the band it landed in.
        notes:                  human-readable explanation lines (e.g. the FFO/Debt
                                proxy caveat, EBITDA-override, near-edge band).
    """
    implied_rating: str
    rating_index: int
    financial_risk_profile: str
    financial_risk_index: int
    business_risk_index: int
    subscores: dict[str, dict[str, Any]]
    notes: list[str]


# ── Mapping helpers ──────────────────────────────────────────────────────────

def _grid_profile(value: float, edges: list[float], higher_is_better: bool) -> int:
    """
    Bucket a ratio value into a financial-risk profile index 1..6.

    The discrete sibling of score._ramp: instead of a continuous penalty it returns
    the band the value falls in. `edges` holds the five boundaries between the six
    bands, ordered from the profile-1 side toward profile-6:
      - higher_is_better (FFO/Debt, coverage): edges DESCENDING; value at/above
        edges[0] → 1, …, below edges[4] → 6.
      - lower_is_better  (Debt/EBITDA):        edges ASCENDING;  value at/below
        edges[0] → 1, …, above edges[4] → 6.
    """
    profile = 1
    for edge in edges:
        if higher_is_better:
            if value >= edge:
                return profile
        else:
            if value <= edge:
                return profile
        profile += 1
    return 6  # worse than every boundary → the bottom band


def _metric_value(ratios: dict[str, Any], source_name: str) -> float | None:
    """
    Pull a ratio value by name, guarding against MissingRatio entries (mirrors
    score._val). Returns None when the ratio is missing or wasn't computed.
    """
    r = ratios.get(source_name)
    if isinstance(r, RatioResult):
        return r.value
    return None


def compute_implied_rating(
    ratios: dict[str, Any],
    *,
    business_risk: int | None = None,
    config: RatingConfig = DEFAULT,
) -> ImpliedRatingResult | None:
    """
    Map a period's extracted ratios to an implied S&P-style rating letter.

    Args:
        ratios:        the dict from extract.extract_all() (RatioResult / MissingRatio).
        business_risk: optional business-risk index 1..6 (1 = Excellent). When None,
                       config.business_risk_default is used so a rating is always
                       computable from financials alone.
        config:        RatingConfig (grids, weights, anchor matrix). Defaults to
                       DEFAULT, the materialised DEFAULT_RATING_CONFIG.

    Returns:
        ImpliedRatingResult, or None when fewer than config.min_subfactors of the
        three sub-factors resolve (we never guess a rating from one ratio).
    """
    notes: list[str] = []
    subscores: dict[str, dict[str, Any]] = {}

    # EBITDA-negative guard (the same insight as score.py's sign-aware override):
    # negative/zero EBITDA makes Debt/EBITDA and EBITDA/Interest sign-flip or
    # explode, so a money-losing issuer would look deceptively strong on those
    # axes. Force both to the bottom band (Highly Leveraged) instead.
    ebitda_value = recover_ebitda(ratios)
    ebitda_negative = ebitda_value is not None and ebitda_value <= 0
    _ebitda_driven = {"debt_to_ebitda", "ebitda_to_interest"}

    resolved: dict[str, int] = {}   # sub_factor → profile index 1..6

    for sub, grid in config.grids.items():
        source = grid["source"]
        value = _metric_value(ratios, source)

        if ebitda_negative and sub in _ebitda_driven:
            # Forced to band 6 regardless of the (sign-flipped) ratio value.
            profile = 6
            resolved[sub] = profile
            subscores[sub] = {
                "value": value,
                "profile": profile,
                "profile_name": FINANCIAL_RISK_PROFILES[profile],
                "source_ratio": source,
                "overridden": True,
            }
            continue

        if value is None:
            subscores[sub] = {
                "value": None,
                "profile": None,
                "profile_name": None,
                "source_ratio": source,
                "overridden": False,
            }
            continue

        profile = _grid_profile(value, grid["edges"], grid["higher_is_better"])
        resolved[sub] = profile
        subscores[sub] = {
            "value": value,
            "profile": profile,
            "profile_name": FINANCIAL_RISK_PROFILES[profile],
            "source_ratio": source,
            "overridden": False,
        }

    if ebitda_negative:
        notes.append(
            f"EBITDA ≤ 0 ({ebitda_value:,.0f}); Debt/EBITDA and interest-coverage "
            "sub-factors forced to Highly Leveraged."
        )

    # Never guess from a single ratio.
    if len(resolved) < config.min_subfactors:
        return None

    # Blend the resolved sub-factor profile indices into one financial-risk index,
    # renormalising the weights over whichever sub-factors resolved.
    total_w = sum(config.weights.get(sub, 0.0) for sub in resolved)
    if total_w <= 0:
        # No weight assigned to the resolved sub-factors — fall back to a plain mean.
        blended = sum(resolved.values()) / len(resolved)
    else:
        blended = sum(config.weights.get(sub, 0.0) * p for sub, p in resolved.items()) / total_w

    if len(resolved) < len(config.grids):
        missing = sorted(set(config.grids) - set(resolved))
        notes.append(
            f"{len(resolved)} of {len(config.grids)} sub-factors available "
            f"(missing: {', '.join(missing)}); weights renormalised."
        )

    fr_index = max(1, min(6, round(blended)))

    # Note when the blended value sits near a band edge (the categorical cliff): the
    # implied letter could plausibly be one band either way.
    frac = blended - int(blended)
    if 0.0 < blended < 6.0 and (frac >= 0.66 or frac <= 0.34) and abs(blended - fr_index) > 0.15:
        alt = max(1, min(6, fr_index + (1 if blended > fr_index else -1)))
        if alt != fr_index:
            notes.append(
                f"Financial-risk profile near a band edge (blended {blended:.2f}); "
                f"{FINANCIAL_RISK_PROFILES[alt]} is the nearest alternative."
            )

    br_index = business_risk if business_risk is not None else config.business_risk_default
    br_index = max(1, min(6, int(br_index)))
    if business_risk is None:
        notes.append(
            f"Business risk assumed {BUSINESS_RISK_PROFILES[br_index]} (default); "
            "supply a per-issuer business-risk input to refine."
        )

    letter = config.anchor_matrix[br_index - 1][fr_index - 1]

    # FFO/Debt proxy caveat — always surfaced so the rating is honestly labelled.
    if "ffo_to_debt" in resolved:
        notes.append("FFO/Debt approximated by CFO/gross-debt (cash_flow_to_debt).")

    return ImpliedRatingResult(
        implied_rating=letter,
        rating_index=RATING_SCALE.index(letter),
        financial_risk_profile=FINANCIAL_RISK_PROFILES[fr_index],
        financial_risk_index=fr_index,
        business_risk_index=br_index,
        subscores=subscores,
        notes=notes,
    )


# ── Rating Outlook signal (Stage 0) ──────────────────────────────────────────
#
# A deterministic directional signal — Positive / Stable / Negative — for where an
# issuer's rating is headed. It ships WITHOUT any agency data: the key insight is
# that a rating *level* doesn't predict change, but two transformations of the
# ratio-derived score do:
#
#   1. TREND (momentum) — the recent slope of the implied rating and/or the 0–100
#      stress score. A worsening trend signals downgrade pressure even before the
#      implied letter moves (the letter is coarse/sticky; the score is continuous).
#   2. GAP (mean-reversion) — implied rating_index − agency rating_index. Agencies
#      are through-the-cycle and lag fundamentals, so when our implied rating sits
#      below the agency's, the agency tends to catch down (downgrade pressure), and
#      vice-versa. The gap is None until agency data is ingested (Stage 1); the
#      signal then runs on trend alone.
#
# This is the interpretable baseline the future ML model must beat, and its two
# components are themselves model features. Crucially the score feeds the signal,
# never the reverse (no circularity) — consistent with keeping the migration model
# a separate parallel signal from the stress score.
#
# Sign convention throughout: rating_index and the stress score are both
# "higher = worse", so a POSITIVE delta/gap means deterioration → downgrade.

OUTLOOK_POSITIVE = "Positive"   # rating expected to improve (upgrade pressure)
OUTLOOK_STABLE = "Stable"       # no material directional pressure
OUTLOOK_NEGATIVE = "Negative"   # rating expected to deteriorate (downgrade pressure)


DEFAULT_OUTLOOK_CONFIG: dict = {
    # Number of most-recent periods the trend is measured over (first vs. last).
    "window": 4,
    # |implied rating_index change| over the window at/above this (in notches) sets
    # the trend direction outright.
    "rating_notch_threshold": 1,
    # Fallback when the implied letter hasn't moved: |stress-score change| over the
    # window at/above this many points sets the trend direction (catches sub-notch
    # drift the coarse letter misses).
    "score_delta_threshold": 10.0,
    # |implied − agency| at/above this many notches sets the gap direction.
    "gap_notch_threshold": 1,
}


@dataclass(frozen=True)
class OutlookConfig:
    """Typed, deep-merging view over DEFAULT_OUTLOOK_CONFIG (same pattern as RatingConfig)."""
    window: int
    rating_notch_threshold: int
    score_delta_threshold: float
    gap_notch_threshold: int

    @classmethod
    def from_dict(cls, d: dict | None) -> "OutlookConfig":
        m = {**DEFAULT_OUTLOOK_CONFIG, **(d or {})}
        return cls(
            window=int(m["window"]),
            rating_notch_threshold=int(m["rating_notch_threshold"]),
            score_delta_threshold=float(m["score_delta_threshold"]),
            gap_notch_threshold=int(m["gap_notch_threshold"]),
        )

    def to_dict(self) -> dict:
        return {
            "window": self.window,
            "rating_notch_threshold": self.rating_notch_threshold,
            "score_delta_threshold": self.score_delta_threshold,
            "gap_notch_threshold": self.gap_notch_threshold,
        }


OUTLOOK_DEFAULT = OutlookConfig.from_dict(DEFAULT_OUTLOOK_CONFIG)


@dataclass
class RatingOutlookResult:
    """
    Carries the directional rating signal plus its full reasoning.

    Attributes:
        outlook:       OUTLOOK_POSITIVE | OUTLOOK_STABLE | OUTLOOK_NEGATIVE.
        trend_pressure: -1 (improving), 0 (flat), +1 (worsening) from the trend.
        gap_pressure:   -1, 0, +1 from the implied-vs-agency gap (0 when no agency).
        gap:           implied − agency rating_index, or None when no agency rating.
        rating_change: implied rating_index change over the window (+ = worse), or None.
        score_change:  stress-score change over the window (+ = worse), or None.
        reasons:       human-readable explanation lines (the auditable "why").
        periods_used:  how many periods the trend was measured over.
    """
    outlook: str
    trend_pressure: int
    gap_pressure: int
    gap: int | None
    rating_change: int | None
    score_change: float | None
    reasons: list[str]
    periods_used: int


def _sign_threshold(delta: float, threshold: float) -> int:
    """+1 if delta ≥ +threshold, -1 if delta ≤ -threshold, else 0."""
    if delta >= threshold:
        return 1
    if delta <= -threshold:
        return -1
    return 0


def rating_outlook(
    series: list[dict[str, Any]],
    *,
    agency_rating_index: int | None = None,
    config: OutlookConfig = OUTLOOK_DEFAULT,
) -> RatingOutlookResult | None:
    """
    Derive the directional Rating Outlook for one issuer from its score history.

    Args:
        series: the issuer's per-period points, each {"period_end": "YYYY-MM-DD",
                "rating_index": int | None, "score": float | None}. Any order;
                sorted oldest→newest internally. rating_index/score may be missing.
        agency_rating_index: the issuer's CURRENT agency rating on the same 0=AAA
                index, when known (Stage 1). Enables the mean-reversion gap term.
        config: OutlookConfig (window + thresholds). Defaults to OUTLOOK_DEFAULT.

    Returns:
        RatingOutlookResult, or None when there is no usable history at all (no
        ratings and no scores).
    """
    # Newest-last, then take the trailing window.
    pts = sorted(series, key=lambda p: p.get("period_end") or "")
    window = pts[-config.window:] if config.window > 0 else pts

    rated = [p for p in window if p.get("rating_index") is not None]
    scored = [p for p in window if p.get("score") is not None]

    if not rated and not scored and agency_rating_index is None:
        return None

    reasons: list[str] = []

    # ── Trend pressure ───────────────────────────────────────────────────────
    rating_change: int | None = None
    score_change: float | None = None
    trend_pressure = 0

    if len(rated) >= 2:
        rating_change = int(rated[-1]["rating_index"]) - int(rated[0]["rating_index"])
    if len(scored) >= 2:
        score_change = float(scored[-1]["score"]) - float(scored[0]["score"])

    # The implied letter moving ≥ threshold notches sets direction outright; else
    # fall back to the finer (continuous) stress-score drift.
    if rating_change is not None and abs(rating_change) >= config.rating_notch_threshold:
        trend_pressure = 1 if rating_change > 0 else -1
        first_letter = RATING_SCALE[int(rated[0]["rating_index"])]
        last_letter = RATING_SCALE[int(rated[-1]["rating_index"])]
        verb = "deteriorated" if trend_pressure > 0 else "improved"
        reasons.append(
            f"Implied rating {verb} from {first_letter} to {last_letter} over the last "
            f"{len(rated)} periods ({'downgrade' if trend_pressure > 0 else 'upgrade'} pressure)."
        )
    elif score_change is not None:
        trend_pressure = _sign_threshold(score_change, config.score_delta_threshold)
        if trend_pressure != 0:
            verb = "rose" if trend_pressure > 0 else "fell"
            reasons.append(
                f"Stress score {verb} {abs(score_change):.0f} pts over the last "
                f"{len(scored)} periods, while the implied letter held "
                f"({'downgrade' if trend_pressure > 0 else 'upgrade'} pressure)."
            )

    periods_used = max(len(rated), len(scored))
    if trend_pressure == 0 and periods_used >= 2:
        reasons.append("No material trend in the implied rating or stress score.")
    elif periods_used < 2:
        reasons.append("Insufficient history to establish a trend.")

    # ── Gap pressure (mean-reversion; needs an agency rating) ─────────────────
    gap: int | None = None
    gap_pressure = 0
    latest_rating = rated[-1]["rating_index"] if rated else None
    if agency_rating_index is not None and latest_rating is not None:
        gap = int(latest_rating) - int(agency_rating_index)
        gap_pressure = _sign_threshold(gap, config.gap_notch_threshold)
        if gap_pressure != 0:
            implied_letter = RATING_SCALE[int(latest_rating)]
            agency_letter = RATING_SCALE[int(agency_rating_index)]
            rel = "below" if gap > 0 else "above"
            reasons.append(
                f"Implied {implied_letter} is {abs(gap)} notch(es) {rel} the agency's "
                f"{agency_letter} ({'downgrade' if gap_pressure > 0 else 'upgrade'} pressure)."
            )

    # ── Combine ──────────────────────────────────────────────────────────────
    combined = trend_pressure + gap_pressure
    if combined >= 1:
        outlook = OUTLOOK_NEGATIVE
    elif combined <= -1:
        outlook = OUTLOOK_POSITIVE
    else:
        outlook = OUTLOOK_STABLE
        # When trend and gap actively disagree (net out to 0), say so rather than
        # implying there was no signal at all.
        if trend_pressure != 0 and gap_pressure != 0 and trend_pressure != gap_pressure:
            reasons.append("Trend and agency-gap signals offset each other — net Stable.")

    return RatingOutlookResult(
        outlook=outlook,
        trend_pressure=trend_pressure,
        gap_pressure=gap_pressure,
        gap=gap,
        rating_change=rating_change,
        score_change=score_change,
        reasons=reasons,
        periods_used=periods_used,
    )


# ── Issue-level seniority notching (Stage 4) ─────────────────────────────────
#
# A specific bond is notched off the ISSUER rating for expected recovery: senior
# secured recovers more than the issuer average (notched up = a better, lower
# index), subordinated recovers less (notched down = worse, higher index). Senior
# unsecured sits at the issuer level. Pure index arithmetic on RATING_SCALE — which
# is exactly why rating_index is the canonical representation. Deltas are notch
# COUNTS on the index axis (negative = better, since lower index = better rating).
NOTCHING_DEFAULT: dict[str, int] = {
    "senior_secured": -1,    # one notch up (better recovery)
    "senior_unsecured": 0,   # at the issuer level
    "subordinated": 2,       # two notches down (worse recovery)
    "other": 0,
}


def notch_instrument(
    issuer_rating_index: int | None,
    seniority: str,
    notching: dict[str, int] = NOTCHING_DEFAULT,
) -> int | None:
    """
    Notch an issuer rating_index to an instrument-level index by seniority.

    Returns the notched index (clamped to the scale), or None when the issuer index
    is unknown. notch_instrument(8, "senior_secured") → 7 (BBB → BBB+); a
    subordinated instrument of the same issuer → 10 (BB+).
    """
    if issuer_rating_index is None:
        return None
    delta = notching.get(seniority, 0)
    return max(0, min(len(RATING_SCALE) - 1, issuer_rating_index + delta))
