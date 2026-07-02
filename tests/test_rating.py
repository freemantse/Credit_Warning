"""Tests for src/rating.py — pure arithmetic, no network or LLM."""


from src.extract import RatioResult
from src.rating import (
    RATING_SCALE,
    DEFAULT,
    RatingConfig,
    compute_implied_rating,
    compute_implied_ratings_series,
    is_investment_grade,
    rating_index,
    _grid_profile,
    _industry_risk,
    _volatility_class,
    _business_risk_index,
    _trailing_mean,
)


def make_ratio(name, value, inputs=None):
    return RatioResult(
        name=name, value=value, inputs=inputs or {}, source_tags={}, period_end="2023-12-31"
    )


def ebitda_inputs(op_inc, dep=0.0):
    """Inputs carrying an EBITDA recover_ebitda() can read back."""
    return {"operating_income": op_inc, "depreciation": dep}


# ── _grid_profile (the bucketing primitive) ──────────────────────────────────

def test_grid_profile_higher_is_better():
    edges = [0.60, 0.45, 0.30, 0.20, 0.12]  # FFO/Debt
    assert _grid_profile(0.80, edges, True) == 1   # Minimal
    assert _grid_profile(0.50, edges, True) == 2   # Modest
    assert _grid_profile(0.35, edges, True) == 3   # Intermediate
    assert _grid_profile(0.25, edges, True) == 4   # Significant
    assert _grid_profile(0.15, edges, True) == 5   # Aggressive
    assert _grid_profile(0.05, edges, True) == 6   # Highly Leveraged


def test_grid_profile_lower_is_better():
    edges = [1.5, 2.0, 3.0, 4.0, 5.0]  # Debt/EBITDA
    assert _grid_profile(1.0, edges, False) == 1
    assert _grid_profile(1.8, edges, False) == 2
    assert _grid_profile(2.5, edges, False) == 3
    assert _grid_profile(3.5, edges, False) == 4
    assert _grid_profile(4.5, edges, False) == 5
    assert _grid_profile(7.0, edges, False) == 6


def test_grid_profile_boundary_inclusive_on_strong_side():
    # A value exactly at the first edge lands in the strongest band.
    assert _grid_profile(0.60, [0.60, 0.45, 0.30, 0.20, 0.12], True) == 1
    assert _grid_profile(1.5, [1.5, 2.0, 3.0, 4.0, 5.0], False) == 1


# ── compute_implied_rating ───────────────────────────────────────────────────

def test_healthy_issuer_is_investment_grade():
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.70),   # FFO/Debt → Minimal
        "leverage": make_ratio("leverage", 1.0, ebitda_inputs(100)),  # Debt/EBITDA → Minimal
        "interest_coverage": make_ratio("interest_coverage", 20.0, ebitda_inputs(100)),  # → Minimal
    }
    res = compute_implied_rating(ratios)
    assert res is not None
    assert is_investment_grade(res.implied_rating)
    # Minimal financial risk + default (Satisfactory) business risk → strong IG.
    assert res.financial_risk_index == 1
    assert res.financial_risk_profile == "Minimal"


def test_distressed_issuer_is_speculative():
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.05),    # FFO/Debt → Highly Leveraged
        "leverage": make_ratio("leverage", 8.0, ebitda_inputs(100)),   # Debt/EBITDA → Highly Leveraged
        "interest_coverage": make_ratio("interest_coverage", 1.0, ebitda_inputs(100)),  # → Highly Leveraged
    }
    res = compute_implied_rating(ratios)
    assert res is not None
    assert not is_investment_grade(res.implied_rating)
    assert res.financial_risk_index == 6


def test_negative_ebitda_forces_bottom_band_subfactors():
    # FFO/Debt looks healthy, but EBITDA is negative → Debt/EBITDA & coverage forced to band 6.
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.50),
        "leverage": make_ratio("leverage", -2.0, ebitda_inputs(-100)),  # EBITDA = -100
        "interest_coverage": make_ratio("interest_coverage", -1.0, ebitda_inputs(-100)),
    }
    res = compute_implied_rating(ratios)
    assert res is not None
    assert res.subscores["debt_to_ebitda"]["profile"] == 6
    assert res.subscores["debt_to_ebitda"]["overridden"] is True
    assert res.subscores["ebitda_to_interest"]["profile"] == 6
    assert res.subscores["ebitda_to_interest"]["overridden"] is True
    assert any("EBITDA" in n for n in res.notes)


def test_returns_none_when_too_few_subfactors():
    # Only one sub-factor resolves → no guess.
    ratios = {"cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.40)}
    assert compute_implied_rating(ratios) is None


def test_two_of_three_subfactors_renormalises():
    # Coverage missing; FFO/Debt and Debt/EBITDA present → still rated.
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.35),   # Intermediate (3)
        "leverage": make_ratio("leverage", 2.5, ebitda_inputs(100)),  # Intermediate (3)
    }
    res = compute_implied_rating(ratios)
    assert res is not None
    assert res.financial_risk_index == 3
    assert any("renormalised" in n for n in res.notes)


def test_business_risk_input_changes_rating():
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.35),   # Intermediate
        "leverage": make_ratio("leverage", 2.5, ebitda_inputs(100)),  # Intermediate
        "interest_coverage": make_ratio("interest_coverage", 8.0, ebitda_inputs(100)),  # Intermediate
    }
    excellent = compute_implied_rating(ratios, business_risk=1)
    vulnerable = compute_implied_rating(ratios, business_risk=6)
    assert excellent is not None and vulnerable is not None
    # Better business risk → better (lower-index) rating for the same financials.
    assert excellent.rating_index < vulnerable.rating_index


def test_subscores_record_source_ratio():
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.35),
        "leverage": make_ratio("leverage", 2.5, ebitda_inputs(100)),
        "interest_coverage": make_ratio("interest_coverage", 8.0, ebitda_inputs(100)),
    }
    res = compute_implied_rating(ratios)
    assert res.subscores["ffo_to_debt"]["source_ratio"] == "cash_flow_to_debt"
    assert res.subscores["debt_to_ebitda"]["source_ratio"] == "leverage"


# ── Anchor-matrix invariants ─────────────────────────────────────────────────

def test_anchor_matrix_is_valid_and_monotonic():
    m = DEFAULT.anchor_matrix
    assert len(m) == 6 and all(len(row) == 6 for row in m)
    # Every cell is a real rating on the scale.
    for row in m:
        for letter in row:
            assert letter in RATING_SCALE
    # Ratings worsen (index increases) left→right across each row and top→bottom
    # down each column — the matrix must be non-improving in both directions.
    for r in range(6):
        for c in range(6):
            if c + 1 < 6:
                assert rating_index(m[r][c]) <= rating_index(m[r][c + 1])
            if r + 1 < 6:
                assert rating_index(m[r][c]) <= rating_index(m[r + 1][c])


def test_rating_scale_helpers():
    assert rating_index("AAA") == 0
    assert is_investment_grade("BBB-") is True
    assert is_investment_grade("BB+") is False


# ── Business-risk proxy + volatility-adjusted tables ─────────────────────────

def period_ratios(ffo, lev, cov, margin, revenue, op_inc=100.0):
    """A full period's ratios dict for the orchestrator (positive EBITDA)."""
    return {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", ffo),
        "leverage": make_ratio("leverage", lev, ebitda_inputs(op_inc)),
        "interest_coverage": make_ratio("interest_coverage", cov, ebitda_inputs(op_inc)),
        "ebitda_margin": make_ratio("ebitda_margin", margin, {"revenue": revenue}),
    }


def test_industry_risk_longest_prefix_match():
    # 4-digit override beats the 2-digit major group; unknown → default.
    assert _industry_risk("4911", DEFAULT) == 1   # electric utility (low risk)
    assert _industry_risk("4900", DEFAULT) == 2   # utilities major group
    assert _industry_risk("2834", DEFAULT) == 2   # pharma overrides 28 (chemicals = 3)
    assert _industry_risk("2899", DEFAULT) == 3   # chemicals, no 4-digit override
    assert _industry_risk("1311", DEFAULT) == 5   # oil & gas extraction (high risk)
    assert _industry_risk(None, DEFAULT) == 3     # missing SIC → default
    assert _industry_risk("9999", DEFAULT) == 3   # unmapped → default


def test_volatility_class_thresholds():
    assert _volatility_class([0.30, 0.31, 0.29], DEFAULT) == "low"       # std ≈ 0.008
    assert _volatility_class([0.30, 0.22, 0.36], DEFAULT) == "medial"    # std ≈ 0.057
    assert _volatility_class([0.5, 0.1, 0.6, 0.05], DEFAULT) == "standard"
    assert _volatility_class([0.30], DEFAULT) is None                    # too little history


def test_business_risk_proxy_strong_vs_weak():
    # Low-risk industry + large scale + high, stable margin → Excellent/Strong.
    strong, _ = _business_risk_index("4911", 60e9, 0.40, 0.01, DEFAULT)
    # High-risk industry + tiny scale + thin, volatile margin → Weak/Vulnerable.
    weak, _ = _business_risk_index("1311", 0.4e9, 0.05, 0.20, DEFAULT)
    assert strong <= 2
    assert weak >= 5
    assert strong < weak


def test_low_volatility_table_relaxes_band():
    # cash_flow_to_debt = 0.40 → Intermediate (3) on the STANDARD table but
    # Minimal (1) on the relaxed LOW table; the rating should be no worse.
    ratios = {
        "cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.40),
        "leverage": make_ratio("leverage", 2.5, ebitda_inputs(100)),
        "interest_coverage": make_ratio("interest_coverage", 8.0, ebitda_inputs(100)),
    }
    std = compute_implied_rating(ratios, volatility_class="standard")
    low = compute_implied_rating(ratios, volatility_class="low")
    assert std.subscores["ffo_to_debt"]["profile"] == 3
    assert low.subscores["ffo_to_debt"]["profile"] == 1
    assert low.rating_index <= std.rating_index
    assert any("volatility benchmark table" in n.lower() for n in low.notes)


def test_series_proxy_lifts_excellent_issuer_above_A():
    # The all-Satisfactory default caps the implied rating at "A". With an
    # excellent-industry, large-scale, high-margin issuer the proxy should now
    # reach AA/AAA territory (rating_index < that of "A").
    results = {
        f"{y}-12-31": period_ratios(0.70, 1.0, 20.0, 0.40, 60e9)
        for y in (2019, 2020, 2021)
    }
    out = compute_implied_ratings_series(results, sic="4911")
    latest = out["2021-12-31"]
    assert latest.business_risk_index == 1               # Excellent
    assert latest.financial_risk_index == 1              # Minimal
    assert latest.rating_index < rating_index("A")       # beats the old "A" cap
    assert latest.business_risk["volatility_class"] == "low"
    assert latest.business_risk["sic"] == "4911"


def test_series_volatility_window_is_trailing_no_lookahead():
    # Stable margins/cash-flows 2016–2019, then a wild swing in 2020–2021. A
    # period rated in the STABLE era must NOT see the later volatility.
    margins = {"2016": 0.30, "2017": 0.30, "2018": 0.30, "2019": 0.30, "2020": 0.05, "2021": 0.55}
    cf =      {"2016": 0.35, "2017": 0.35, "2018": 0.35, "2019": 0.35, "2020": 0.05, "2021": 0.70}
    results = {
        f"{y}-12-31": period_ratios(cf[y], 2.5, 8.0, margins[y], 5e9)
        for y in margins
    }
    out = compute_implied_ratings_series(results, sic=None)  # industry → default 3

    early = out["2018-12-31"].business_risk
    late = out["2021-12-31"].business_risk
    # 2018 sees only 2016–2018 (all stable) → low margin volatility + low CF class.
    assert early["margin_volatility_tier"] <= 2
    assert early["volatility_class"] == "low"
    # 2021's trailing window includes the swing → high volatility, standard table.
    assert late["margin_volatility_tier"] >= 5
    assert late["volatility_class"] in ("medial", "standard")
    # Stability in the early era yields a better (lower) business-risk index.
    assert out["2018-12-31"].business_risk_index < out["2021-12-31"].business_risk_index


def test_series_omits_unratable_periods():
    # A period with only one sub-factor can't be rated and is dropped, while a
    # complete period in the same series still rates.
    results = {
        "2020-12-31": {"cash_flow_to_debt": make_ratio("cash_flow_to_debt", 0.40)},
        "2021-12-31": period_ratios(0.35, 2.5, 8.0, 0.18, 5e9),
    }
    out = compute_implied_ratings_series(results, sic=None)
    assert "2020-12-31" not in out
    assert "2021-12-31" in out


# ── Business-risk smoothing (sticky axis) ────────────────────────────────────

def _config_with_smoothing(window):
    """A RatingConfig identical to DEFAULT but with the level-smoothing window set."""
    d = DEFAULT.to_dict()
    d["business_risk"]["smoothing_window"] = window
    return RatingConfig.from_dict(d)


def test_trailing_mean_is_window_bounded_and_none_safe():
    assert _trailing_mean([1.0, 2.0, 3.0, 4.0], 3) == 3.0   # mean of last 3
    assert _trailing_mean([None, 2.0, None, 4.0], 2) == 3.0  # None dropped → mean(2,4)
    assert _trailing_mean([None, None], 3) is None
    assert _trailing_mean([5.0], 3) == 5.0


def test_smoothing_reduces_business_risk_swings():
    # Margins alternate high/low: the single-year margin_level tier alternates
    # (jumpy) without smoothing, but a trailing average holds it steady.
    margins = [0.30, 0.10, 0.30, 0.10, 0.30, 0.10]
    results = {
        f"{2014 + i}-12-31": period_ratios(0.35, 2.5, 8.0, m, 5e9)
        for i, m in enumerate(margins)
    }

    def movement(window):
        out = compute_implied_ratings_series(results, sic=None, config=_config_with_smoothing(window))
        idx = [out[p].business_risk_index for p in sorted(out)]
        return sum(abs(idx[i] - idx[i - 1]) for i in range(1, len(idx)))

    assert movement(1) > 0                 # jumpy without smoothing
    assert movement(3) < movement(1)       # smoothing damps the swings


def test_smoothing_uses_trailing_average_no_lookahead():
    # Revenue grows each year; the smoothed revenue for a period must equal the
    # trailing mean (this + prior periods only) — never influenced by later years.
    revs = [1e9, 2e9, 3e9, 4e9, 5e9]
    results = {
        f"{2017 + i}-12-31": period_ratios(0.35, 2.5, 8.0, 0.18, rv)
        for i, rv in enumerate(revs)
    }
    out = compute_implied_ratings_series(results, sic=None, config=_config_with_smoothing(3))
    assert out["2017-12-31"].business_risk["revenue"] == 1e9           # only itself
    assert out["2019-12-31"].business_risk["revenue"] == 2e9           # mean(1,2,3)
    assert out["2019-12-31"].business_risk["level_smoothing_window"] == 3


def test_smoothing_window_one_uses_single_period_value():
    # window=1 reproduces the pre-smoothing behaviour: level inputs = this period.
    results = {
        f"{2018 + i}-12-31": period_ratios(0.35, 2.5, 8.0, m, 5e9)
        for i, m in enumerate([0.30, 0.05, 0.30])
    }
    out = compute_implied_ratings_series(results, sic=None, config=_config_with_smoothing(1))
    assert out["2019-12-31"].business_risk["ebitda_margin"] == 0.05   # the dip itself, not an average
