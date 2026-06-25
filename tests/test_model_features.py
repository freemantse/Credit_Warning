"""Tests for src/model/features.py — point-in-time feature assembly (Stage 2)."""

from pytest import approx

from src.model.features import (
    ratio_features,
    build_issuer_features,
    merge_labels,
    to_dataframe,
    FEATURE_COLUMNS,
    FEATURE_DIRECTIONS,
    ID_COLUMNS,
    TARGET_COLUMNS,
)


def sr(value, inputs=None):
    """A stored grouped ratio dict ({value, inputs, source_tags})."""
    return {"value": value, "inputs": inputs or {}, "source_tags": {}}


def ebitda_inputs(op_inc=100.0, dep=0.0):
    return {"operating_income": op_inc, "depreciation": dep}


# ── ratio_features ───────────────────────────────────────────────────────────

def test_ratio_features_levels_and_deltas():
    now = {"leverage": sr(4.0), "liquidity": sr(1.2)}
    prev = {"leverage": sr(3.0), "liquidity": sr(1.5)}
    f = ratio_features(now, prev)
    assert f["leverage"] == 4.0
    assert f["leverage_yoy"] == 1.0
    assert f["liquidity_yoy"] == approx(-0.3)
    # A ratio absent from both periods → level and delta both None.
    assert f["debt_to_assets"] is None and f["debt_to_assets_yoy"] is None


# ── build_issuer_features ────────────────────────────────────────────────────

def _rising_leverage_history():
    # Leverage 2.0 → 4.0 → 6.0 (EBITDA positive so no sign override): stress score
    # rises, while the implied letter stays BBB+ (index 7) → outlook leans Negative.
    periods = ["2019-12-31", "2020-12-31", "2021-12-31"]
    ratios = {
        "2019-12-31": {"leverage": sr(2.0, ebitda_inputs())},
        "2020-12-31": {"leverage": sr(4.0, ebitda_inputs())},
        "2021-12-31": {"leverage": sr(6.0, ebitda_inputs())},
    }
    implied = {p: {"rating_index": 7, "financial_risk_index": 3} for p in periods}
    return periods, ratios, implied


def test_build_issuer_features_basic():
    periods, ratios, implied = _rising_leverage_history()
    feats = build_issuer_features(periods, ratios_by_period=ratios, implied_by_period=implied)
    assert set(feats) == set(periods)
    last = feats["2021-12-31"]
    assert last["leverage"] == 6.0
    assert last["leverage_yoy"] == 2.0
    assert isinstance(last["stress_score"], float)
    assert last["stress_score_yoy"] > 0          # score rose as leverage climbed
    assert last["implied_rating_index"] == 7
    # Letter flat but score climbed ≥ threshold → outlook trend = downgrade pressure.
    assert last["outlook_trend_pressure"] == 1


def test_build_issuer_features_is_causal_no_lookahead():
    # Features for a period must not change when LATER periods are added/removed.
    periods, ratios, implied = _rising_leverage_history()
    full = build_issuer_features(periods, ratios_by_period=ratios, implied_by_period=implied)
    truncated = build_issuer_features(
        ["2019-12-31", "2020-12-31"], ratios_by_period=ratios, implied_by_period=implied
    )
    assert truncated["2019-12-31"] == full["2019-12-31"]
    assert truncated["2020-12-31"] == full["2020-12-31"]


# ── merge_labels ─────────────────────────────────────────────────────────────

def test_merge_labels_attaches_gap_and_targets():
    features_by_cik = {
        "C1": {"2019-12-31": {**{c: None for c in FEATURE_COLUMNS}, "implied_rating_index": 7}}
    }
    labels = {
        "C1": {
            "2019-12-31": {
                "MDY": {"rating_index": 5, "label_3m": 0, "label_6m": 0, "label_12m": 1,
                        "notch_change_12m": 2, "distress_12m": False},
                "FTC": {"rating_index": 6, "label_3m": 0, "label_6m": 1, "label_12m": 1,
                        "notch_change_12m": 1, "distress_12m": False},
            }
        }
    }
    rows = merge_labels(features_by_cik, labels)
    assert len(rows) == 2                          # one row per agency
    mdy = next(r for r in rows if r["agency"] == "MDY")
    assert mdy["agency_rating_index"] == 5
    assert mdy["implied_vs_agency_gap"] == 2       # implied 7 − agency 5 (implied worse)
    assert mdy["label_12m"] == 1
    assert mdy["time_in_rating_months"] is None    # no events supplied


def test_merge_labels_skips_periods_without_features():
    features_by_cik = {"C1": {}}                   # no feature row for the period
    labels = {"C1": {"2019-12-31": {"MDY": {"rating_index": 5, "label_12m": 1}}}}
    assert merge_labels(features_by_cik, labels) == []


def test_time_in_rating_from_events():
    features_by_cik = {"C1": {"2020-12-31": {**{c: None for c in FEATURE_COLUMNS}, "implied_rating_index": 8}}}
    labels = {"C1": {"2020-12-31": {"MDY": {"rating_index": 8, "label_12m": 0}}}}
    events = {"C1": {"MDY": [
        {"effective_date": "2018-06-30", "rating_index": 8},
        {"effective_date": "2020-03-31", "rating_index": 8},
    ]}}
    rows = merge_labels(features_by_cik, labels, agency_events_by_cik=events)
    # Last action on/before 2020-12-31 is 2020-03-31 → 9 months in rating.
    assert rows[0]["time_in_rating_months"] == 9


# ── to_dataframe + spec integrity ────────────────────────────────────────────

def test_to_dataframe_orders_columns():
    rows = merge_labels(
        {"C1": {"2019-12-31": {**{c: 0 for c in FEATURE_COLUMNS}, "implied_rating_index": 7}}},
        {"C1": {"2019-12-31": {"MDY": {"rating_index": 5, "label_12m": 1, "distress_12m": False}}}},
    )
    df = to_dataframe(rows)
    assert list(df.columns)[:3] == ID_COLUMNS
    assert "label_12m" in df.columns
    assert df.iloc[0]["implied_vs_agency_gap"] == 2


def test_feature_directions_cover_all_features():
    # Every model feature has a monotone direction declared for Stage 3 constraints.
    for col in FEATURE_COLUMNS:
        assert col in FEATURE_DIRECTIONS, col
        assert FEATURE_DIRECTIONS[col] in (-1, 0, 1)
