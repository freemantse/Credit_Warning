"""
Precompute point-in-time MARKET features per (cik, period_end) from the LSEG equity
drop (data/lseg_equity_prices/*.csv) + gross debt from the ratios store, and write
data/market_features.csv. Measure-first: this feeds the walk-forward eval so we can
decide whether distance-to-default / equity signals help BEFORE wiring them into the
live pipeline.

Grain matches the model: one row per (cik, period_end) present in the ratios store.
Strictly causal — each row uses only daily prices dated ≤ period_end (see
src.model.market.asof_market_features).

Usage:
    python3 -m scripts.build_market_features            # full run → data/market_features.csv
    python3 -m scripts.build_market_features --limit 50 # smoke test on N issuers
"""

from __future__ import annotations

import argparse
import bisect
import glob
import pathlib

import pandas as pd

from src.model.market import asof_market_features

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
PRICE_DIR = DATA / "lseg_equity_prices"
OUT = DATA / "market_features.csv"


def _cik_to_ric() -> dict[str, str]:
    """cik → its most-common RIC (the equity file to read), from agency_ratings.csv."""
    ar = pd.read_csv(DATA / "agency_ratings.csv", dtype=str).dropna(subset=["source_ric", "cik"])
    out: dict[str, str] = {}
    for cik, grp in ar.groupby("cik"):
        out[str(cik).zfill(10)] = grp["source_ric"].value_counts().index[0]
    return out


def _load_price_series(ric: str):
    """(dates, closes, mcaps, shares) ascending by date for a RIC, or None if no file.
    Blanks become None so the pure feature functions can skip them."""
    path = PRICE_DIR / (ric.replace(".", "_") + ".csv")
    if not path.exists():
        return None
    df = pd.read_csv(path)
    ren = {"Close Price": "close", "Date": "date",
           "Company Market Capitalization": "mcap", "Outstanding Shares": "shares"}
    df = df.rename(columns=ren)
    if "date" not in df or "close" not in df:
        return None
    df = df[df["date"].notna()].sort_values("date")
    def col(name):
        if name not in df:
            return [None] * len(df)
        return [None if pd.isna(x) else float(x) for x in df[name]]
    return df["date"].astype(str).tolist(), col("close"), col("mcap"), col("shares")


def _gross_debt(period_ratios: dict) -> float | None:
    """gross_debt = total_debt + short_term_debt, read from a ratio's raw dollar inputs."""
    for rk in ("debt_to_assets", "cash_flow_to_debt"):
        r = period_ratios.get(rk)
        inp = r.get("inputs") if isinstance(r, dict) else None
        if isinstance(inp, dict) and inp.get("total_debt") is not None:
            td = float(inp["total_debt"])
            st = inp.get("short_term_debt")
            return td + (float(st) if st is not None else 0.0)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Precompute point-in-time market features")
    ap.add_argument("--limit", type=int, default=None, help="only process N issuers (smoke test)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    from src.store import get_ratios_grouped
    print("loading ratios (cik → period → ratios) from store …", flush=True)
    ratios = get_ratios_grouped()
    ciks = sorted(ratios)
    if args.limit:
        ciks = ciks[: args.limit]
    cik_ric = _cik_to_ric()
    print(f"{len(ciks)} issuers with ratios; {len(glob.glob(str(PRICE_DIR/'*.csv')))} price files", flush=True)

    rows, n_priced = [], 0
    for i, cik in enumerate(ciks):
        ric = cik_ric.get(cik)
        series = _load_price_series(ric) if ric else None
        if series:
            n_priced += 1
            dates, closes, mcaps, shares = series
        for period_end in sorted(ratios[cik]):
            gd = _gross_debt(ratios[cik][period_end])
            if series:
                k = bisect.bisect_right(dates, period_end)   # point-in-time: only ≤ period_end
                f = asof_market_features(dates[:k], closes[:k], mcaps[:k], shares[:k], period_end, gd)
            else:
                f = {"equity_ret_12m": None, "equity_vol": None,
                     "market_leverage": None, "distance_to_default": None}
            rows.append({
                "cik": cik, "period_end": period_end,
                "distance_to_default": f["distance_to_default"],
                "equity_vol": f["equity_vol"],
                "equity_ret_12m": f["equity_ret_12m"],
                "market_leverage": f["market_leverage"],
            })
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(ciks)} issuers", flush=True)

    df = pd.DataFrame(rows)
    for c in ("distance_to_default", "equity_vol", "equity_ret_12m", "market_leverage"):
        df[c] = df[c].round(6)
    df.to_csv(args.out, index=False)
    tot = len(df)
    print(f"\nwrote {args.out}: {tot} (cik, period_end) rows")
    print(f"  issuers with a price file: {n_priced}/{len(ciks)}")
    for c in ("distance_to_default", "equity_vol", "equity_ret_12m", "market_leverage"):
        nn = int(df[c].notna().sum())
        print(f"  {c:<22} non-null: {nn}/{tot} ({100*nn/tot:.1f}%)")


if __name__ == "__main__":
    main()
