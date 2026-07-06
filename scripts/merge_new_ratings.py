"""
NON-DESTRUCTIVE append of a new LSEG rating-history drop into the canonical
``data/agency_ratings.csv`` — used for the July-2026 test batch from Steven.

Unlike ``scripts.build_agency_ratings_csv`` (which REBUILDS the CSV from
``ratings_history_raw.csv`` and would clobber the existing 10.9k rows now that the
original raw drop is gone), this reads the new small drop, runs it through the SAME
resolve pipeline (extract_events_long → build_crosswalk → attach_cik → normalize), and
APPENDS the resulting canonical rows to the existing CSV, deduping on the primary key
(cik, agency, effective_date) with existing rows winning.

Inputs (new format from the July-2026 pull):
  data/ratings_history.csv   Instrument,OrganizationPermID,Ticker,CompanyCommonName,
                             RatingAgency,Rating,RatingAction,RatingDate,
                             RatingSourceDescription,RatingTypeDescription
  data/distress_events.csv   (same, minus RatingAction/RatingTypeDescription)
  data/universe_xref.csv     RIC → ticker/name crosswalk (for CIK resolution)

The parser (src.ratings.ingest.detect_long_columns) expects the OLD long format:
  Instrument, Company Common Name, Date, Issuer Rating, Rating Source Description,
  Rating Type Description
so we reshape to EXACTLY those six columns (dropping RatingAgency/RatingAction which
would otherwise shadow the Rating column in the parser's first-match detection).

Usage:  python3 -m scripts.merge_new_ratings
"""
from __future__ import annotations

import pathlib
import pandas as pd

from src.ratings.ingest import load_csv, extract_events_long
from src.ratings.crosswalk import build_crosswalk, attach_cik, default_resolver
from scripts.build_agency_ratings_csv import CANONICAL_COLUMNS

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
NEW_RATINGS = DATA / "ratings_history.csv"
DISTRESS = DATA / "distress_events.csv"
XREF_CSV = DATA / "universe_xref.csv"
CANONICAL = DATA / "agency_ratings.csv"
ADAPTED_TMP = DATA / "_new_ratings_longformat.csv"   # transient, gitignored dir


def _reshape_to_long(paths: list[pathlib.Path]) -> pd.DataFrame:
    """Union the new drop(s) and rename to the parser's expected 6-column long format."""
    frames = []
    for p in paths:
        if not p.exists():
            continue
        d = pd.read_csv(p, dtype=str)
        out = pd.DataFrame({
            "Instrument": d["Instrument"],
            "Company Common Name": d["CompanyCommonName"],
            "Date": d["RatingDate"],
            "Issuer Rating": d["Rating"],
            "Rating Source Description": d["RatingSourceDescription"],
            # distress_events.csv has no RatingTypeDescription → default it
            "Rating Type Description": d.get("RatingTypeDescription", "Issuer-level Rating"),
        })
        frames.append(out)
    df = pd.concat(frames, ignore_index=True).drop_duplicates()
    return df


def _resolve(long_df: pd.DataFrame) -> list[dict]:
    """Same resolve path as build_agency_ratings_csv._resolve_lseg, on the reshaped drop."""
    long_df.to_csv(ADAPTED_TMP, index=False)
    history = load_csv(ADAPTED_TMP)
    xref = load_csv(XREF_CSV)

    events = extract_events_long(history)
    event_rics = {e["ric"] for e in events}
    print(f"reshaped drop → {len(events)} long-term events across {len(event_rics)} RICs")

    cols = list(xref.columns)
    ric_col = next((c for c in cols if c.lower() == "ric" or "instrument" in c.lower()), cols[0])
    xref_used = xref[xref[ric_col].isin(event_rics)].copy()

    resolved, _unmatched = build_crosswalk(xref_used, resolver=default_resolver)
    meta_by_ric = {ric: {"ticker": info.get("ticker"), "name": info.get("name")}
                   for ric, info in resolved.items()}
    attached = attach_cik(events, resolved)
    print(f"resolved {len(resolved)}/{len(event_rics)} RICs to a CIK → {len(attached)} events")

    rows = []
    for e in attached:
        meta = meta_by_ric.get(e.get("source_ric"), {})
        rows.append({
            "cik": str(e["cik"]).zfill(10),
            "agency": e["agency"],
            "effective_date": e["effective_date"],
            "rating_index": e.get("rating_index"),
            "rating_raw": e.get("rating_raw"),
            "rating_status": e.get("rating_status"),
            "rating_action": e.get("rating_action"),
            "source_permid": e.get("source_permid"),
            "source_ric": e.get("source_ric"),
            "ticker": meta.get("ticker"),
            "company_name": meta.get("name"),
            "source": "lseg",
        })
    ADAPTED_TMP.unlink(missing_ok=True)
    return rows


def main() -> None:
    long_df = _reshape_to_long([NEW_RATINGS, DISTRESS])
    new_rows = _resolve(long_df)
    new_df = pd.DataFrame(new_rows, columns=list(CANONICAL_COLUMNS))

    existing = pd.read_csv(CANONICAL, dtype=str)
    before = len(existing)
    existing_ciks = set(existing["cik"])

    combined = pd.concat([existing, new_df], ignore_index=True)
    # Dedup on PK; keep FIRST occurrence = existing rows win over re-resolved duplicates.
    combined = combined.drop_duplicates(subset=["cik", "agency", "effective_date"], keep="first")
    combined = combined.sort_values(["cik", "agency", "effective_date"]).reset_index(drop=True)
    combined.to_csv(CANONICAL, index=False)

    added = len(combined) - before
    new_ciks = set(new_df["cik"]) - existing_ciks
    print(f"\nagency_ratings.csv: {before} → {len(combined)} rows  (+{added} new rating actions)")
    print(f"brand-new issuers (CIKs not previously present): {len(new_ciks)} → {sorted(new_ciks)}")
    print(f"issuers touched by this drop: {sorted(set(new_df['ticker'].dropna()))}")


if __name__ == "__main__":
    main()
