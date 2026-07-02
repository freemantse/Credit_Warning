"""Tests for src/model/dataset.py — heads, time-split, make_xy, monotone map (pure)."""

import pandas as pd

from src.model.dataset import (
    monotone_constraints, time_split, make_xy, observed, recall_at_k,
)
from src.model.features import FEATURE_COLUMNS


def _row(period_end, label_12m=0, distress_12m=False, **feats):
    base = {c: None for c in FEATURE_COLUMNS}
    base.update({"cik": "C1", "period_end": period_end, "agency": "MDY",
                 "label_12m": label_12m, "distress_12m": distress_12m})
    base.update(feats)
    return base


def test_monotone_signs_per_head():
    lev = FEATURE_COLUMNS.index("leverage")
    cov = FEATURE_COLUMNS.index("interest_coverage")
    down = monotone_constraints("downgrade")
    up = monotone_constraints("upgrade")
    distress = monotone_constraints("distress")
    # Leverage raises downgrade & distress risk (+1), lowers upgrade odds (−1).
    assert down[lev] == 1 and distress[lev] == 1 and up[lev] == -1
    # Coverage is the opposite.
    assert down[cov] == -1 and up[cov] == 1
    assert len(down) == len(FEATURE_COLUMNS)


def test_monotone_constraints_relax_secondary():
    from src.model.features import CORE_CONSTRAINED_FEATURES

    base = monotone_constraints("downgrade")
    relaxed = monotone_constraints("downgrade", relax_secondary=True)
    assert len(relaxed) == len(FEATURE_COLUMNS)
    for i, col in enumerate(FEATURE_COLUMNS):
        if col in CORE_CONSTRAINED_FEATURES:
            assert relaxed[i] == base[i]          # core keeps its credit-coherent direction
        else:
            assert relaxed[i] == 0                # everything else is unconstrained
    assert relaxed[FEATURE_COLUMNS.index("leverage")] == 1               # core stays
    assert relaxed[FEATURE_COLUMNS.index("implied_vs_agency_gap")] == 0  # secondary freed


def test_time_split_respects_label_window():
    df = pd.DataFrame([
        _row("2018-12-31", label_12m=1),
        _row("2019-12-31", label_12m=1),
        _row("2020-12-31", label_12m=0),
    ])
    train, test = time_split(df, "2020-06-30")
    # 2018 window closes 2019-12-31 ≤ cutoff → train. 2020 is after the cutoff → test.
    # 2019's window closes 2020-12-31 > cutoff and its period ≤ cutoff → neither (censored gap).
    assert list(train["period_end"]) == ["2018-12-31"]
    assert list(test["period_end"]) == ["2020-12-31"]


def test_make_xy_targets_and_observed_filter():
    df = pd.DataFrame([
        _row("2019-12-31", label_12m=1, leverage=5.0),
        _row("2019-12-31", label_12m=-1, leverage=1.0),
        _row("2019-12-31", label_12m=0, leverage=2.0),
        _row("2019-12-31", label_12m=None, leverage=9.0),   # censored → excluded
    ])
    assert len(observed(df)) == 3
    _, y_down, _ = make_xy(df, "downgrade")
    _, y_up, _ = make_xy(df, "upgrade")
    assert list(y_down) == [1, 0, 0]    # only label_12m == +1
    assert list(y_up) == [0, 1, 0]      # only label_12m == −1


def test_make_xy_X_has_feature_columns():
    df = pd.DataFrame([_row("2019-12-31", label_12m=1, leverage=5.0)])
    X, _, _ = make_xy(df, "downgrade")
    assert list(X.columns) == list(FEATURE_COLUMNS)
    assert X.iloc[0]["leverage"] == 5.0


def test_make_xy_masks_market_features_for_distress_only():
    from src.model.features import MARKET_FEATURES
    df = pd.DataFrame([_row("2019-12-31", label_12m=1, distress_12m=True,
                            distance_to_default=5.0, equity_vol=0.3,
                            equity_ret_12m=0.1, market_leverage=0.4)])
    Xd, _, _ = make_xy(df, "downgrade")
    Xu, _, _ = make_xy(df, "upgrade")
    Xx, _, _ = make_xy(df, "distress")
    # downgrade + upgrade use the market features …
    assert Xd.iloc[0]["distance_to_default"] == 5.0 and Xu.iloc[0]["distance_to_default"] == 5.0
    # … the distress head masks them all to NaN (inert for its booster).
    for c in MARKET_FEATURES:
        assert pd.isna(Xx.iloc[0][c])


def test_recall_at_k():
    # 10 items, 2 positives ranked 1st and 3rd; top-20% (k=2) catches one of two.
    y = [1, 0, 1, 0, 0, 0, 0, 0, 0, 0]
    p = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.0]
    assert recall_at_k(y, p, 0.2) == 0.5
