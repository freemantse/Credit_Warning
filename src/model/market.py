"""
Forward-looking MARKET features for the rating-migration model, from daily equity
prices (the LSEG drop). All point-in-time: every feature at `period_end` uses only
prices dated on/before it — the no-look-ahead guarantee the backtest depends on.

Pure and dependency-light (math/statistics/datetime) so it unit-tests without a DB and
is reused by both the offline precompute (scripts.build_market_features) and, if the
features prove out, the live feature assembly (features.build_issuer_features).

Features (per cik, period_end):
  equity_ret_12m      — trailing ~12-month equity return (momentum). Lower = worse.
  equity_vol          — annualised stdev of daily log returns over a trailing window. Higher = worse.
  market_leverage     — D / (D + E), D = gross debt, E = market cap. Higher = worse.
  distance_to_default — Bharath–Shumway "naive" DtD. Higher = safer.
"""

from __future__ import annotations

import math
import statistics
from datetime import date

VOL_WINDOW = 252          # trailing trading days for volatility (~1 year)
MIN_VOL_OBS = 40          # need at least this many daily returns to trust the vol
RISK_FREE = 0.03          # constant r for the DtD drift term (a feature, not a fit)
HORIZON_YEARS = 1.0


def annualized_vol(closes: list[float]) -> float | None:
    """Annualised stdev of daily log returns from a close series (chronological).
    None when there aren't enough usable returns."""
    px = [c for c in closes if c is not None and c > 0]
    if len(px) < MIN_VOL_OBS + 1:
        return None
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    if len(rets) < MIN_VOL_OBS:
        return None
    return statistics.pstdev(rets) * math.sqrt(252)


def naive_distance_to_default(
    equity_value: float | None, gross_debt: float | None, equity_vol: float | None,
    *, r: float = RISK_FREE, T: float = HORIZON_YEARS,
) -> float | None:
    """
    Bharath–Shumway (2008) *naive* distance-to-default:
      V  = E + D                         (asset value ≈ equity + debt)
      σV = (E/V)·σE + (D/V)·(0.05 + 0.25·σE)   (naive asset-vol proxy)
      DtD = [ln(V/D) + (r − ½σV²)·T] / (σV·√T)
    Higher DtD = further from default. None if any input is missing/degenerate.
    """
    E, D, sE = equity_value, gross_debt, equity_vol
    if E is None or D is None or sE is None or E <= 0 or D <= 0 or sE <= 0:
        return None
    V = E + D
    sV = (E / V) * sE + (D / V) * (0.05 + 0.25 * sE)
    if sV <= 0:
        return None
    return (math.log(V / D) + (r - 0.5 * sV * sV) * T) / (sV * math.sqrt(T))


def _months_before(period_end: str, months: int) -> str:
    d = date.fromisoformat(period_end)
    m = d.month - 1 - months
    y = d.year + m // 12
    mo = m % 12 + 1
    # clamp day to 28 to avoid month-length issues; we compare as a cutoff only
    return f"{y:04d}-{mo:02d}-{min(d.day, 28):02d}"


def asof_market_features(
    hist_dates: list[str], hist_closes: list[float | None],
    hist_mcaps: list[float | None], hist_shares: list[float | None],
    period_end: str, gross_debt: float | None,
) -> dict[str, float | None]:
    """
    Compute the market features as of `period_end` from an issuer's daily series.
    `hist_*` MUST be ascending by date and already restricted to date ≤ period_end
    (the caller slices point-in-time). Returns all-None when there's no usable price.
    """
    none = {"equity_ret_12m": None, "equity_vol": None,
            "market_leverage": None, "distance_to_default": None}
    # most-recent non-null close (as-of price)
    close_now = next((c for c in reversed(hist_closes) if c is not None and c > 0), None)
    if close_now is None:
        return dict(none)

    # market cap: most-recent non-null; fall back to close × most-recent shares
    mcap_now = next((m for m in reversed(hist_mcaps) if m is not None and m > 0), None)
    if mcap_now is None:
        sh = next((s for s in reversed(hist_shares) if s is not None and s > 0), None)
        mcap_now = close_now * sh if sh is not None else None

    # trailing 12-month return: last close on/before (period_end − 12m)
    cutoff = _months_before(period_end, 12)
    close_then = None
    for dt, c in zip(hist_dates, hist_closes):
        if dt <= cutoff and c is not None and c > 0:
            close_then = c
    ret_12m = (close_now / close_then - 1.0) if close_then else None

    vol = annualized_vol(hist_closes[-VOL_WINDOW:])
    # market leverage D/(D+E) ∈ [0,1) by construction; only defined with a positive
    # market cap and non-negative debt (else the ratio is meaningless / blows up).
    mkt_lev = (gross_debt / (gross_debt + mcap_now)
               if (gross_debt is not None and gross_debt >= 0 and mcap_now is not None and mcap_now > 0)
               else None)
    dtd = naive_distance_to_default(mcap_now, gross_debt, vol)

    return {"equity_ret_12m": ret_12m, "equity_vol": vol,
            "market_leverage": mkt_lev, "distance_to_default": dtd}
