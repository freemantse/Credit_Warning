"""
Batch-track every company in the canonical agency-ratings universe through EDGAR, to
build the deterministic ratio FEATURES the migration model trains on.

RESUMABLE BY DESIGN: it reads which CIKs already have stored ratios and skips them, so
you can stop this at any time (Ctrl-C) and just run it again to continue — already
tracked companies are skipped instantly (and EDGAR responses are disk-cached anyway).
Writes are idempotent upserts, so re-tracking never duplicates.

LLM footnote review is OFF (include_llm=False): fast, no API cost. The two LLM-derived
features (covenant/provision counts) stay null for these issuers; the booster handles
missing values.

Progress is printed per company (ok / skip / fail) with a running tally, so you can
watch it live (foreground) or `tail -f` a log (background).

Usage:
    python3 -m scripts.track_universe                 # track all not-yet-tracked
    python3 -m scripts.track_universe --limit 150     # pilot: first N untracked
    python3 -m scripts.track_universe --retry-failed   # also re-attempt prior failures
"""

from __future__ import annotations

import argparse
import pathlib

from src.ratings.ingest import load_csv
from src.store import get_ratios_grouped
from src.track import track

CANONICAL_CSV = pathlib.Path(__file__).resolve().parent.parent / "data" / "agency_ratings.csv"


def _universe() -> list[tuple[str, str | None]]:
    """Unique (cik, ticker) pairs from the canonical CSV, sorted for stable resume order."""
    df = load_csv(CANONICAL_CSV)
    seen: dict[str, str | None] = {}
    for _, r in df.iterrows():
        cik = str(r["cik"]).zfill(10)
        if cik not in seen:
            tkr = r.get("ticker")
            seen[cik] = str(tkr).strip() if tkr is not None and str(tkr).strip() else None
    return sorted(seen.items())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="track only the first N untracked (pilot)")
    args = ap.parse_args()

    if not CANONICAL_CSV.exists():
        raise SystemExit(f"Missing {CANONICAL_CSV} — run scripts.build_agency_ratings_csv first.")

    universe = _universe()
    already = set(get_ratios_grouped().keys())   # CIKs that already have ratios → skip
    pending = [(cik, tkr) for cik, tkr in universe if cik not in already]
    if args.limit is not None:
        pending = pending[: args.limit]

    print(f"Universe: {len(universe)} issuers | already tracked: {len(already)} | "
          f"to track now: {len(pending)}")

    ok = failed = 0
    failures: list[str] = []
    for i, (cik, tkr) in enumerate(pending, 1):
        ident = tkr or cik              # track() accepts a bare CIK too (resolve_identifier)
        try:
            track(ident, include_llm=False)
            ok += 1
            status = "ok"
        except Exception as e:          # delisted / non-US / no XBRL / parse error → skip, log
            failed += 1
            failures.append(f"{ident} ({cik}): {type(e).__name__}: {str(e)[:120]}")
            status = "FAIL"
        print(f"[{i}/{len(pending)}] {ident:<8} {status}   (ok={ok} fail={failed})", flush=True)

    print(f"\nDone this pass: {ok} tracked, {failed} failed/skipped.")
    if failures:
        print("Failures (companies with no usable US filings, expected for foreign/delisted):")
        for f in failures[:50]:
            print(f"  - {f}")
        if len(failures) > 50:
            print(f"  … and {len(failures) - 50} more.")
    print(f"\nTrainable so far (CIKs with ratios): {len(already) + ok}. "
          f"Re-run to continue; build_labels + train next.")


if __name__ == "__main__":
    main()
