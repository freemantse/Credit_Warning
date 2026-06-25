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
  - Business risk is a DATA-DERIVED PROXY (industry via SIC, scale via revenue, and
    profitability level + volatility via EBITDA margin) — see _business_risk_index
    and compute_implied_ratings_series. It captures S&P's data-observable
    business-risk factors only; competitive advantage and operating efficiency
    (genuinely qualitative) are NOT modelled and would need an analyst/LLM layer.
    Calling compute_implied_rating directly without a business_risk falls back to a
    mid default, so a rating is still computable from financials alone.
  - The financial-risk ratios are read off S&P-style Standard / Medial / Low
    benchmark tables selected by trailing cash-flow volatility (volatility_class).

Returns None (never a guess) when fewer than two of the three sub-factors resolve —
mirroring extract.py's "never fabricate a number" contract.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from src.extract import RatioResult, recover_ebitda

# Trailing window (in annual periods, inclusive of the period being rated) over
# which profitability- and cash-flow-volatility are measured for the business-risk
# proxy and the volatility-class benchmark selection. The window is STRICTLY
# trailing (never reads future periods) because `implied_rating_index` is a feature
# in the walk-forward migration model — using future data would leak look-ahead.
BUSINESS_RISK_TRAILING_WINDOW = 5


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


# SIC major groups in Division H (Finance, Insurance, Real Estate): 60 depository,
# 61 nondepository credit, 62 brokers, 63 insurance carriers, 64 insurance agents,
# 65 real estate, 67 holding/investment offices (66 is unused in the SIC scheme).
_FINANCIAL_SIC_PREFIXES = {"60", "61", "62", "63", "64", "65", "67"}


def financial_sector_note(sic: str | None) -> str | None:
    """
    Explain why a financial-sector issuer isn't rated, or None if it isn't financial.

    The implied rating uses S&P's INDUSTRIAL cash-flow/leverage framework
    (FFO/Debt, Debt/EBITDA, EBITDA/Interest), which structurally doesn't fit banks,
    insurers, BDCs, and funds — they fund via deposits/float and report investment
    income, not EBITDA. For those (SIC 6000–6799) we surface this note instead of a
    blank rating. For NON-financial issuers an absent rating is a data/extraction
    gap, not a structural one, so this returns None (no excuse offered).
    """
    if not sic:
        return None
    digits = "".join(ch for ch in str(sic) if ch.isdigit())
    if len(digits) >= 2 and digits[:2] in _FINANCIAL_SIC_PREFIXES:
        return (
            "Not rated: financial-sector issuer (bank / insurer / fund). The "
            "Debt/EBITDA model doesn't apply; a capital-adequacy assessment is "
            "future work."
        )
    return None


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
        # `edges` is S&P's STANDARD-volatility table; `edges_by_volatility` carries
        # the relaxed Medial/Low tables (see volatility_class_edges below). A
        # low-volatility issuer is allowed weaker ratios for the same band.
        "ffo_to_debt": {
            "source": "cash_flow_to_debt",
            "higher_is_better": True,
            "edges": [0.60, 0.45, 0.30, 0.20, 0.12],
            "edges_by_volatility": {
                "standard": [0.60, 0.45, 0.30, 0.20, 0.12],
                "medial":   [0.50, 0.35, 0.23, 0.15, 0.09],
                "low":      [0.40, 0.25, 0.15, 0.10, 0.06],
            },
        },
        # Debt/EBITDA (= net_debt/EBITDA via `leverage`). Lower is healthier. S&P:
        # Minimal <1.5×, Modest 1.5–2×, Intermediate 2–3×, Significant 3–4×,
        # Aggressive 4–5×, Highly Leveraged >5×.
        "debt_to_ebitda": {
            "source": "leverage",
            "higher_is_better": False,
            "edges": [1.5, 2.0, 3.0, 4.0, 5.0],
            "edges_by_volatility": {
                "standard": [1.5, 2.0, 3.0, 4.0, 5.0],
                "medial":   [1.75, 2.5, 3.5, 4.5, 5.5],
                "low":      [2.0, 3.0, 4.0, 5.0, 6.0],
            },
        },
        # EBITDA/Interest coverage. Higher is healthier. S&P:
        # Minimal >15×, Modest 10–15×, Intermediate 6–10×, Significant 3–6×,
        # Aggressive 2–3×, Highly Leveraged <2×.
        "ebitda_to_interest": {
            "source": "interest_coverage",
            "higher_is_better": True,
            "edges": [15.0, 10.0, 6.0, 3.0, 2.0],
            "edges_by_volatility": {
                "standard": [15.0, 10.0, 6.0, 3.0, 2.0],
                "medial":   [12.0, 8.0, 5.0, 2.5, 1.75],
                "low":      [10.0, 6.0, 4.0, 2.0, 1.5],
            },
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
    # ── Business-risk proxy ──────────────────────────────────────────────────
    # S&P's Business Risk Profile = CICRA (country + industry risk) + competitive
    # position (competitive advantage, scale/scope/diversity, operating efficiency,
    # profitability level + volatility). We approximate the DATA-OBSERVABLE subset:
    # industry risk (from SIC), scale (revenue), profitability level (EBITDA margin)
    # and profitability volatility (trailing std-dev of that margin). Competitive
    # advantage / operating efficiency are genuinely qualitative and NOT captured —
    # a future LLM/analyst layer would refine them. The four sub-tiers (each 1..6,
    # 1 = strongest) blend by weighted mean → round → 1..6. Industry risk carries
    # the largest weight because CICRA caps the business-risk profile in S&P.
    # All knobs live here so the proxy can be recalibrated without code changes.
    "business_risk": {
        "weights": {
            "industry": 0.40,
            "scale": 0.25,
            "margin_level": 0.20,
            "margin_volatility": 0.15,
        },
        # Business risk should evolve slowly (a company's industry/scale/competitive
        # position changes structurally, not annually). The LEVEL inputs (revenue,
        # EBITDA margin) are therefore averaged over the most-recent N periods
        # (strictly trailing) before being mapped to tiers, so a one-year blip can't
        # flip the anchor-matrix row. Set to 1 to disable (use single-period values).
        # NOTE: the volatility sub-factor is deliberately NOT smoothed — rising
        # profitability volatility genuinely signals rising business risk.
        "smoothing_window": 3,
        # Revenue ($) → scale tier (higher_is_better: bigger issuer = lower risk).
        # Edges DESCENDING: ≥ $50bn → 1 (Excellent) … < $0.5bn → 6.
        "scale_edges": [50e9, 15e9, 5e9, 1.5e9, 0.5e9],
        # EBITDA margin level → tier (higher_is_better). Edges DESCENDING.
        "margin_level_edges": [0.30, 0.22, 0.15, 0.08, 0.03],
        # EBITDA-margin volatility (trailing std-dev, in margin points) → tier.
        # LOWER is better, so edges ASCENDING: ≤ 1.5pp → 1 (very stable) … > 12pp → 6.
        "margin_volatility_edges": [0.015, 0.03, 0.05, 0.08, 0.12],
        # Industry-risk tier (1 Very-Low … 6 Very-High risk) by SIC prefix, matched
        # LONGEST-prefix-first (so "2834" pharma overrides "28" chemicals). Midpoint
        # approximations of S&P's published industry risk assessments; unmatched SICs
        # fall back to `default_industry_risk`.
        "default_industry_risk": 3,
        "sic_industry_risk": {
            # Agriculture / extractive / construction — cyclical, commodity-exposed.
            "01": 4, "02": 4, "07": 4, "08": 4, "09": 4,
            "10": 5, "12": 5, "13": 5, "14": 5,          # mining, oil & gas extraction
            "15": 5, "16": 5, "17": 5,                    # construction
            # Manufacturing.
            "20": 2, "21": 2,                             # food, tobacco — defensive
            "22": 5, "23": 5,                             # textiles, apparel
            "24": 4, "25": 4, "26": 4, "27": 4,           # lumber, furniture, paper, printing
            "28": 3, "2834": 2, "2836": 2,                # chemicals; pharma/biologics defensive
            "29": 5,                                      # petroleum refining
            "30": 4, "31": 5, "32": 4, "33": 5, "34": 4,  # rubber, leather, stone, primary/fab metals
            "35": 3, "36": 4, "37": 4, "371": 5, "372": 4,# machinery, electronics, transport equip (autos/aero)
            "38": 3, "39": 4,                             # instruments / medical devices; misc mfg
            # Transportation, communications, utilities.
            "40": 4, "41": 4, "42": 4, "44": 4, "45": 5, "47": 4,  # rail/transit/trucking/water/air
            "48": 3,                                      # communications / telecom
            "49": 2, "4911": 1, "4931": 2, "4932": 2,     # regulated utilities — low risk
            # Trade.
            "50": 4, "51": 4, "52": 4, "53": 4, "54": 3, "55": 4,  # wholesale / retail (food retail 54 defensive)
            "56": 5, "57": 4, "58": 4, "59": 4,
            # Finance / insurance / real estate (mostly out of scope — no financials resolve).
            "60": 3, "61": 3, "62": 3, "63": 3, "64": 3, "65": 4, "67": 3,
            # Services.
            "70": 5, "72": 4, "73": 3, "75": 4, "78": 4, "79": 5,  # hotels/biz svcs/software/amusement
            "80": 3, "82": 3, "83": 3, "87": 3,
        },
    },
    # ── Cash-flow volatility class (benchmark-table selection) ───────────────
    # S&P reads the financial-risk ratios off one of three benchmark tables by the
    # issuer's cash-flow volatility. We classify from the trailing std-dev of
    # cash_flow_to_debt (the FFO/Debt proxy) and select the matching grid table.
    # ASCENDING: std ≤ low_max → "low"; ≤ medial_max → "medial"; else "standard".
    "volatility_class_edges": {
        "low_max": 0.04,
        "medial_max": 0.09,
    },
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
    business_risk: dict
    volatility_class_edges: dict

    @classmethod
    def from_dict(cls, d: dict | None) -> "RatingConfig":
        d = d or {}
        in_grids = d.get("grids") or {}
        merged_grids: dict[str, dict] = {}
        for key, defaults in DEFAULT_RATING_CONFIG["grids"].items():
            g = {**defaults, **(in_grids.get(key) or {})}
            merged: dict[str, Any] = {
                "source": str(g["source"]),
                "higher_is_better": bool(g["higher_is_better"]),
                "edges": [float(e) for e in g["edges"]],
            }
            ev = g.get("edges_by_volatility")
            if ev:
                merged["edges_by_volatility"] = {
                    str(cls_): [float(e) for e in edges] for cls_, edges in ev.items()
                }
            merged_grids[key] = merged
        weights = {
            k: float(v)
            for k, v in {**DEFAULT_RATING_CONFIG["weights"], **(d.get("weights") or {})}.items()
        }
        anchor = d.get("anchor_matrix") or DEFAULT_RATING_CONFIG["anchor_matrix"]

        # Business-risk proxy config (deep-merged over the default block).
        br_def = DEFAULT_RATING_CONFIG["business_risk"]
        br_in = d.get("business_risk") or {}
        business_risk = {
            "weights": {**br_def["weights"], **(br_in.get("weights") or {})},
            "smoothing_window": max(1, int(br_in.get("smoothing_window", br_def["smoothing_window"]))),
            "scale_edges": [float(e) for e in (br_in.get("scale_edges") or br_def["scale_edges"])],
            "margin_level_edges": [float(e) for e in (br_in.get("margin_level_edges") or br_def["margin_level_edges"])],
            "margin_volatility_edges": [float(e) for e in (br_in.get("margin_volatility_edges") or br_def["margin_volatility_edges"])],
            "default_industry_risk": int(br_in.get("default_industry_risk", br_def["default_industry_risk"])),
            "sic_industry_risk": {
                str(k): int(v)
                for k, v in {**br_def["sic_industry_risk"], **(br_in.get("sic_industry_risk") or {})}.items()
            },
        }
        vce_def = DEFAULT_RATING_CONFIG["volatility_class_edges"]
        vce_in = d.get("volatility_class_edges") or {}
        volatility_class_edges = {
            "low_max": float(vce_in.get("low_max", vce_def["low_max"])),
            "medial_max": float(vce_in.get("medial_max", vce_def["medial_max"])),
        }
        return cls(
            grids=merged_grids,
            weights=weights,
            business_risk_default=int(d.get("business_risk_default", DEFAULT_RATING_CONFIG["business_risk_default"])),
            min_subfactors=int(d.get("min_subfactors", DEFAULT_RATING_CONFIG["min_subfactors"])),
            anchor_matrix=[list(row) for row in anchor],
            business_risk=business_risk,
            volatility_class_edges=volatility_class_edges,
        )

    def to_dict(self) -> dict:
        return {
            "grids": {k: dict(v) for k, v in self.grids.items()},
            "weights": dict(self.weights),
            "business_risk_default": self.business_risk_default,
            "min_subfactors": self.min_subfactors,
            "anchor_matrix": [list(row) for row in self.anchor_matrix],
            "business_risk": {
                "weights": dict(self.business_risk["weights"]),
                "smoothing_window": self.business_risk["smoothing_window"],
                "scale_edges": list(self.business_risk["scale_edges"]),
                "margin_level_edges": list(self.business_risk["margin_level_edges"]),
                "margin_volatility_edges": list(self.business_risk["margin_volatility_edges"]),
                "default_industry_risk": self.business_risk["default_industry_risk"],
                "sic_industry_risk": dict(self.business_risk["sic_industry_risk"]),
            },
            "volatility_class_edges": dict(self.volatility_class_edges),
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
        business_risk:          audit of the business-risk proxy: the sub-tiers
                                (industry/scale/margin level/margin volatility) and
                                inputs that produced business_risk_index, plus the
                                cash-flow volatility class used for the benchmark
                                table. Empty when business risk wasn't derived
                                (e.g. the legacy default or a directly-supplied index).
    """
    implied_rating: str
    rating_index: int
    financial_risk_profile: str
    financial_risk_index: int
    business_risk_index: int
    subscores: dict[str, dict[str, Any]]
    notes: list[str]
    business_risk: dict[str, Any] = field(default_factory=dict)


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


def _input_value(ratios: dict[str, Any], ratio_names: tuple[str, ...], key: str) -> float | None:
    """
    Read a raw dollar INPUT (e.g. "revenue", "total_assets") from the first of
    `ratio_names` that carries it. Both RatioResult and MissingRatio expose an
    `inputs` dict — even a ratio that failed to compute may still carry the
    inputs that DID resolve — so we read the attribute rather than type-check.
    """
    for name in ratio_names:
        r = ratios.get(name)
        if r is None:
            continue
        v = getattr(r, "inputs", {}).get(key)
        if v is not None:
            return float(v)
    return None


def _series_volatility(values: list[float]) -> float | None:
    """Population std-dev of a series, or None when fewer than two points exist."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    return statistics.pstdev(clean)


def _trailing_mean(values: list[float], window: int) -> float | None:
    """
    Mean of the most-recent `window` non-None values, or None when none exist.
    Used to smooth the business-risk LEVEL inputs (revenue, margin) so the axis
    evolves gradually instead of reacting to a single noisy year. `values` must be
    in chronological order; only its trailing slice is read, so it stays
    strictly point-in-time (no look-ahead).
    """
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return statistics.fmean(clean[-window:])


def _industry_risk(sic: str | None, config: RatingConfig) -> int:
    """
    Map a SIC code to an industry-risk tier 1..6 (1 = lowest risk) by LONGEST
    matching prefix, falling back to the configured default. A CICRA proxy: the
    industry/sector half of S&P's business-risk profile.
    """
    br = config.business_risk
    default = br["default_industry_risk"]
    if not sic:
        return default
    digits = "".join(ch for ch in str(sic) if ch.isdigit())
    if not digits:
        return default
    table = br["sic_industry_risk"]
    # Longest prefix wins (4-digit override beats 2-digit major group).
    for length in range(len(digits), 0, -1):
        tier = table.get(digits[:length])
        if tier is not None:
            return int(tier)
    return default


def _volatility_class(cf_series: list[float], config: RatingConfig) -> str | None:
    """
    Classify cash-flow volatility into "low" / "medial" / "standard" from the
    trailing std-dev of cash_flow_to_debt (the FFO/Debt proxy). Returns None when
    there's too little history to judge, so the caller falls back to the STANDARD
    benchmark table (the conservative default).
    """
    std = _series_volatility(cf_series)
    if std is None:
        return None
    edges = config.volatility_class_edges
    if std <= edges["low_max"]:
        return "low"
    if std <= edges["medial_max"]:
        return "medial"
    return "standard"


def _business_risk_index(
    sic: str | None,
    revenue: float | None,
    ebitda_margin: float | None,
    margin_volatility: float | None,
    config: RatingConfig,
) -> tuple[int, dict[str, Any]]:
    """
    Blend the data-observable business-risk sub-tiers into one 1..6 index.

    Industry risk (from SIC) is always present; scale, profitability level, and
    profitability volatility contribute only when their input resolved, with the
    weights renormalised over whichever sub-tiers are available (the same
    discipline as the financial-risk blend). Returns (index, rationale) where the
    rationale records each sub-tier and its input for the audit trail.
    """
    br = config.business_risk
    weights = br["weights"]

    tiers: dict[str, int] = {"industry": _industry_risk(sic, config)}
    used_w: dict[str, float] = {"industry": weights["industry"]}
    rationale: dict[str, Any] = {
        "sic": str(sic) if sic else None,
        "industry_tier": tiers["industry"],
    }

    if revenue is not None and revenue > 0:
        tiers["scale"] = _grid_profile(revenue, br["scale_edges"], higher_is_better=True)
        used_w["scale"] = weights["scale"]
        rationale["scale_tier"] = tiers["scale"]
        rationale["revenue"] = revenue
    if ebitda_margin is not None:
        tiers["margin_level"] = _grid_profile(ebitda_margin, br["margin_level_edges"], higher_is_better=True)
        used_w["margin_level"] = weights["margin_level"]
        rationale["margin_level_tier"] = tiers["margin_level"]
        rationale["ebitda_margin"] = ebitda_margin
    if margin_volatility is not None:
        tiers["margin_volatility"] = _grid_profile(margin_volatility, br["margin_volatility_edges"], higher_is_better=False)
        used_w["margin_volatility"] = weights["margin_volatility"]
        rationale["margin_volatility_tier"] = tiers["margin_volatility"]
        rationale["margin_volatility"] = margin_volatility

    total_w = sum(used_w.values())
    blended = sum(used_w[k] * tiers[k] for k in tiers) / total_w if total_w > 0 else 3.0
    br_index = max(1, min(6, round(blended)))
    rationale["business_risk_index"] = br_index
    rationale["business_risk_profile"] = BUSINESS_RISK_PROFILES[br_index]
    return br_index, rationale


def compute_implied_rating(
    ratios: dict[str, Any],
    *,
    business_risk: int | None = None,
    volatility_class: str | None = None,
    business_risk_rationale: dict[str, Any] | None = None,
    config: RatingConfig = DEFAULT,
) -> ImpliedRatingResult | None:
    """
    Map a period's extracted ratios to an implied S&P-style rating letter.

    Args:
        ratios:            the dict from extract.extract_all() (RatioResult / MissingRatio).
        business_risk:     optional business-risk index 1..6 (1 = Excellent). When None,
                           config.business_risk_default is used so a rating is always
                           computable from financials alone. compute_implied_ratings_series
                           supplies a data-derived value (see _business_risk_index).
        volatility_class:  optional "low"/"medial"/"standard" selecting which benchmark
                           table the sub-factor grids read (S&P's volatility-adjusted
                           tables). None → the STANDARD table (the conservative default).
        business_risk_rationale: optional audit dict (from _business_risk_index) recorded
                           on the result and used to flag the rating as data-derived.
        config:            RatingConfig (grids, weights, anchor matrix). Defaults to
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

        # Select the volatility-adjusted benchmark table when one is requested and
        # available; otherwise fall back to the grid's STANDARD `edges`.
        edges = grid["edges"]
        if volatility_class and "edges_by_volatility" in grid:
            edges = grid["edges_by_volatility"].get(volatility_class, edges)

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

        profile = _grid_profile(value, edges, grid["higher_is_better"])
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

    # Flag when a relaxed (non-standard) benchmark table was used — the rating is
    # only comparable to a Standard-volatility issuer's after accounting for this.
    if volatility_class in ("low", "medial"):
        notes.append(
            f"{volatility_class.capitalize()}-volatility benchmark table applied "
            "(stable cash flows allow weaker ratios at the same band)."
        )

    br_index = business_risk if business_risk is not None else config.business_risk_default
    br_index = max(1, min(6, int(br_index)))
    if business_risk is None:
        notes.append(
            f"Business risk assumed {BUSINESS_RISK_PROFILES[br_index]} (default); "
            "supply a per-issuer business-risk input to refine."
        )
    elif business_risk_rationale is not None:
        notes.append(
            f"Business risk {BUSINESS_RISK_PROFILES[br_index]} (data-derived proxy: "
            "industry / scale / profitability level + volatility)."
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
        business_risk=dict(business_risk_rationale) if business_risk_rationale else {},
    )


def compute_implied_ratings_series(
    results_by_period: dict[str, dict[str, Any]],
    *,
    sic: str | None = None,
    config: RatingConfig = DEFAULT,
) -> dict[str, ImpliedRatingResult]:
    """
    Compute implied ratings for an issuer's full period history in one pass,
    deriving the per-period business-risk index and volatility-class benchmark
    selection from data — the single entry point both track pipelines use.

    For each period it builds STRICTLY-TRAILING windows (this period and up to
    BUSINESS_RISK_TRAILING_WINDOW-1 prior periods — never future ones, so the
    resulting implied_rating_index is safe to feed the walk-forward migration
    model) of:
      - EBITDA margin → profitability level + volatility (the trailing std-dev), and
      - cash_flow_to_debt → the cash-flow volatility class (low/medial/standard).
    Scale (revenue) and industry (SIC) complete the business-risk proxy.

    Business-risk SMOOTHING: the level inputs (revenue, EBITDA margin) are averaged
    over the trailing config.business_risk["smoothing_window"] periods before being
    mapped to tiers, so the business-risk axis evolves gradually rather than flipping
    the rating on a single noisy year. The volatility sub-factor is left reactive.

    Args:
        results_by_period: period_end → ratios dict (from extract.extract_all).
        sic:               the issuer's SIC code (industry-risk proxy); may be None.
        config:            RatingConfig. Defaults to DEFAULT.

    Returns:
        period_end → ImpliedRatingResult, omitting periods that can't be rated
        (compute_implied_rating returned None) — same contract as before.
    """
    out: dict[str, ImpliedRatingResult] = {}
    periods = sorted(results_by_period)  # period_end strings sort chronologically
    win = BUSINESS_RISK_TRAILING_WINDOW
    smooth = config.business_risk["smoothing_window"]

    for i, period in enumerate(periods):
        ratios = results_by_period[period]
        trailing = periods[max(0, i - win + 1): i + 1]

        margin_series = [_metric_value(results_by_period[p], "ebitda_margin") for p in trailing]
        cf_series = [_metric_value(results_by_period[p], "cash_flow_to_debt") for p in trailing]
        revenue_series = [_input_value(results_by_period[p], ("ebitda_margin", "fcf_margin"), "revenue") for p in trailing]

        # Volatility stays reactive (raw trailing std-dev); LEVEL inputs are
        # trailing-averaged over the smoothing window so business risk is sticky.
        margin_volatility = _series_volatility(margin_series)
        vol_class = _volatility_class(cf_series, config)
        revenue = _trailing_mean(revenue_series, smooth)
        ebitda_margin = _trailing_mean(margin_series, smooth)

        br_index, rationale = _business_risk_index(
            sic, revenue, ebitda_margin, margin_volatility, config
        )
        rationale["volatility_class"] = vol_class or "standard"
        rationale["level_smoothing_window"] = smooth

        r = compute_implied_rating(
            ratios,
            business_risk=br_index,
            volatility_class=vol_class,
            business_risk_rationale=rationale,
            config=config,
        )
        if r is not None:
            out[period] = r
    return out


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
