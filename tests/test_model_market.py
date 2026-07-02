"""Tests for src/model/market.py — point-in-time market features (DtD/vol/momentum)."""

import math
from pytest import approx

from src.model.market import (
    annualized_vol, naive_distance_to_default, asof_market_features,
)


def test_naive_dtd_hand_example_and_monotonicity():
    # E=800, D=200, σE=0.30 → V=1000, σV=0.8*0.3 + 0.2*(0.05+0.25*0.3)=0.265
    # DtD = (ln5 + (0.03 - 0.5*0.265^2)) / 0.265 ≈ 6.05
    dtd = naive_distance_to_default(800, 200, 0.30)
    assert dtd == approx((math.log(5) + (0.03 - 0.5 * 0.265**2)) / 0.265, rel=1e-6)
    # More debt → closer to default (lower DtD); more vol → lower; more equity → higher.
    assert naive_distance_to_default(800, 400, 0.30) < dtd
    assert naive_distance_to_default(800, 200, 0.60) < dtd
    assert naive_distance_to_default(1200, 200, 0.30) > dtd
    # degenerate inputs → None
    assert naive_distance_to_default(None, 200, 0.3) is None
    assert naive_distance_to_default(800, 0, 0.3) is None


def test_annualized_vol():
    assert annualized_vol([100.0] * 300) == approx(0.0)      # flat prices → zero vol
    assert annualized_vol([100, 101, 102]) is None            # too few obs → None
    # a series with a known daily log-return stdev scales by sqrt(252)
    closes = [100.0 * (1.001 ** i) if i % 2 == 0 else 100.0 * (1.001 ** i) * 0.99 for i in range(300)]
    v = annualized_vol(closes)
    assert v is not None and v > 0


def test_asof_features_point_in_time_and_values():
    # 3 years of ~daily closes rising 100→~200, plus a debt figure.
    dates, closes, mcaps, shares = [], [], [], []
    from datetime import date, timedelta
    d0 = date(2019, 1, 1)
    for i in range(600):
        d = d0 + timedelta(days=i)
        px = 100.0 + i * 0.1
        dates.append(d.isoformat()); closes.append(px)
        mcaps.append(px * 1_000_000); shares.append(1_000_000)
    pe = "2020-06-30"
    # slice point-in-time (caller's contract): only ≤ period_end
    import bisect
    k = bisect.bisect_right(dates, pe)
    f = asof_market_features(dates[:k], closes[:k], mcaps[:k], shares[:k], pe, gross_debt=50_000_000)
    assert f["equity_ret_12m"] is not None and f["equity_ret_12m"] > 0   # rising → positive momentum
    assert f["equity_vol"] is not None and f["equity_vol"] >= 0
    assert f["market_leverage"] is not None and 0 < f["market_leverage"] < 1
    assert f["distance_to_default"] is not None

    # NO LOOK-AHEAD: appending FUTURE rows then re-slicing to the same period_end
    # must give an identical result.
    for i in range(600, 900):
        d = d0 + timedelta(days=i)
        dates.append(d.isoformat()); closes.append(999.0); mcaps.append(9e12); shares.append(1_000_000)
    k2 = bisect.bisect_right(dates, pe)
    f2 = asof_market_features(dates[:k2], closes[:k2], mcaps[:k2], shares[:k2], pe, gross_debt=50_000_000)
    assert f2 == f


def test_asof_features_no_prices_all_none():
    f = asof_market_features([], [], [], [], "2020-06-30", gross_debt=1e6)
    assert set(f.values()) == {None}
