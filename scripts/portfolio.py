"""
Manage the curated dashboard portfolio (the `companies.in_portfolio` watchlist).

Issuers are tracked into the DB to TRAIN the model (scripts.track_universe) without
appearing in the dashboard — the dashboard (/api/issuers) shows only in_portfolio rows.
This CLI is the manual way to add/remove members; the app's "track a ticker" button
(POST /api/track) also adds automatically.

Operates on ALREADY-TRACKED issuers (membership needs features to score). To pull in a
brand-new name, track it first (src.track / the app), then `add` it here.

Usage:
    python3 -m scripts.portfolio list
    python3 -m scripts.portfolio add AAPL MSFT 0000320193
    python3 -m scripts.portfolio remove HTZ
"""

from __future__ import annotations

import argparse

from src.store import get_issuers, set_portfolio


def _index() -> tuple[dict[str, str], set[str]]:
    """(ticker→cik, {cik}) over every tracked company, for offline token resolution."""
    by_ticker: dict[str, str] = {}
    ciks: set[str] = set()
    for iss in get_issuers():                      # all tracked (not portfolio-only)
        ciks.add(iss["cik"])
        tkr = (iss.get("ticker") or "").upper()
        if tkr:
            by_ticker.setdefault(tkr, iss["cik"])
    return by_ticker, ciks


def _resolve(token: str, by_ticker: dict[str, str], ciks: set[str]) -> str | None:
    """Resolve a ticker-or-CIK token to a tracked CIK, or None if not tracked."""
    t = token.strip().upper()
    if t.zfill(10) in ciks:
        return t.zfill(10)
    return by_ticker.get(t)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="print the current portfolio members")
    p_add = sub.add_parser("add", help="add tracked issuer(s) to the portfolio")
    p_add.add_argument("tokens", nargs="+", help="tickers and/or CIKs")
    p_rm = sub.add_parser("remove", help="remove issuer(s) from the portfolio")
    p_rm.add_argument("tokens", nargs="+", help="tickers and/or CIKs")
    args = ap.parse_args()

    if args.cmd == "list":
        members = get_issuers(portfolio_only=True)
        print(f"Portfolio: {len(members)} issuer(s)")
        for m in sorted(members, key=lambda r: r.get("ticker") or r["cik"]):
            print(f"  {(m.get('ticker') or '—'):<8} {m['cik']}  {m.get('name','')}")
        return

    by_ticker, ciks = _index()
    member = args.cmd == "add"
    done = 0
    for tok in args.tokens:
        cik = _resolve(tok, by_ticker, ciks)
        if cik is None:
            print(f"  ! {tok}: not tracked — track it first (src.track / the app), then add.")
            continue
        if set_portfolio(cik, member):
            print(f"  {'+' if member else '-'} {tok} ({cik})")
            done += 1
        else:
            print(f"  ! {tok} ({cik}): update affected no row.")
    print(f"{'Added' if member else 'Removed'} {done} issuer(s).")


if __name__ == "__main__":
    main()
