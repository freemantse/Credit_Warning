"""
Build the frozen ML label table (rating_labels) from the stored agency ratings and
financial period_ends.

This is the store-driven orchestrator around src.ratings.labels.build_rating_labels:
  1. read every agency-rating event       (store.get_agency_ratings_grouped)
  2. read the financial period_ends per CIK (store.get_ratios_grouped — the periods
     that actually have features to score)
  3. for each (cik, period_end, agency): the rating now and at +3/+6/+12m, the signed
     migration labels, the 12m notch change, and distress_12m — with right-edge
     censoring past the data's last observed action (data_max_date).
  4. upsert into rating_labels                (store.save_rating_labels_bulk)

The pure core (compute_labels / period_ends_by_cik / flatten_events) takes the
grouped dicts directly so it can be unit-tested without Supabase.

By default this REPLACES rating_labels (clears it first) so the table mirrors the freshly
built set — a plain upsert would leave stale rows for (cik, period_end, agency) combos no
longer produced (e.g. an agency dropped from the universe). Pass --no-replace to upsert
without clearing.

Run AFTER load_agency_ratings + tracking. Usage:
    python3 -m scripts.build_labels
    python3 -m scripts.build_labels --no-replace   # incremental upsert, keep existing rows
"""

from __future__ import annotations

from typing import Any

from src.ratings.labels import build_rating_labels


def flatten_events(agency_grouped: dict[str, dict[str, list[dict]]]) -> list[dict[str, Any]]:
    """
    Flatten store.get_agency_ratings_grouped (cik → agency → [rows]) into the flat
    event list build_rating_labels consumes ({cik, agency, effective_date,
    rating_index, rating_status}).
    """
    events: list[dict[str, Any]] = []
    for cik, by_agency in agency_grouped.items():
        for agency, rows in by_agency.items():
            for r in rows:
                events.append({
                    "cik": cik,
                    "agency": agency,
                    "effective_date": r["effective_date"],
                    "rating_index": r.get("rating_index"),
                    "rating_status": r["rating_status"],
                })
    return events


def period_ends_by_cik(ratios_grouped: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Map cik → sorted financial period_ends (the rows that have features to label)."""
    return {cik: sorted(periods.keys()) for cik, periods in ratios_grouped.items()}


def compute_labels(
    agency_grouped: dict[str, dict[str, list[dict]]],
    ratios_grouped: dict[str, dict[str, Any]],
    *,
    horizons: tuple[int, ...] = (3, 6, 12),
) -> list[dict[str, Any]]:
    """
    Pure label build from the two grouped store reads. data_max_date is the last
    observed agency action — horizon windows past it are censored (left None), never
    fabricated as 'stable'. Returns [] when there are no agency events to anchor on.
    """
    events = flatten_events(agency_grouped)
    if not events:
        return []
    data_max_date = max(e["effective_date"] for e in events)
    return build_rating_labels(
        events,
        period_ends_by_cik(ratios_grouped),
        data_max_date=data_max_date,
        horizons=horizons,
    )


def main() -> None:
    import argparse

    from src.store import (
        get_agency_ratings_grouped,
        get_ratios_grouped,
        save_rating_labels_bulk,
        clear_rating_labels,
    )

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-replace", action="store_true",
                    help="upsert without clearing rating_labels first (keeps stale rows)")
    args = ap.parse_args()

    import time

    print("Reading agency ratings from Supabase...", flush=True)
    t = time.perf_counter()
    agency_grouped = get_agency_ratings_grouped()
    n_events = sum(len(rows) for by in agency_grouped.values() for rows in by.values())
    print(f"  {n_events} events / {len(agency_grouped)} issuers ({time.perf_counter() - t:.1f}s)", flush=True)

    print("Reading tracked financial periods (ratios)...", flush=True)
    t = time.perf_counter()
    ratios_grouped = get_ratios_grouped()
    print(f"  {len(ratios_grouped)} tracked issuers ({time.perf_counter() - t:.1f}s)", flush=True)

    print("Computing lookahead-free labels...", flush=True)
    t = time.perf_counter()
    labels = compute_labels(agency_grouped, ratios_grouped)
    print(f"  built {len(labels)} label rows ({time.perf_counter() - t:.1f}s)", flush=True)

    if not labels:
        print("No labels built — load agency ratings (scripts.load_agency_ratings) and "
              "track issuers (ratios) first.")
        return

    if not args.no_replace:
        print("Clearing rating_labels (replace mode)...", flush=True)
        clear_rating_labels()

    # Chunk the upsert (a single ~30k-row request can exceed payload limits) and show progress.
    BATCH = 500
    for i in range(0, len(labels), BATCH):
        save_rating_labels_bulk(labels[i:i + BATCH])
        print(f"  saved {min(i + BATCH, len(labels))}/{len(labels)}", flush=True)

    observed = sum(1 for r in labels if r.get("label_12m") is not None)
    downgrades = sum(1 for r in labels if r.get("label_12m") == 1)
    upgrades = sum(1 for r in labels if r.get("label_12m") == -1)
    distresses = sum(1 for r in labels if r.get("distress_12m"))
    print(f"Built {len(labels)} rating_labels rows across "
          f"{len({r['cik'] for r in labels})} issuers.")
    print(f"  12m-observed: {observed}  (downgrade={downgrades}, upgrade={upgrades}, "
          f"distress={distresses})")


if __name__ == "__main__":
    main()
