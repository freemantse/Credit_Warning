"""
Load the file-based LSEG drops and turn the rating history into rating-change
EVENTS, without hardcoding LSEG's column names.

Two inputs (see the data contract in the workstream doc):
  universe_xref.csv      — PermID, RIC, CommonName, TickerSymbol, CUSIP, ISIN
  us_ratings_history.csv — Date, Instrument(RIC), CommonName, plus per-agency
                           rating + rating-effective-date columns whose names carry
                           a BondRatingSrc=MDY/FTC/SPI token and a ".date" companion.

Column names vary across LSEG field configurations, so detect_columns() resolves
them by PATTERN (agency token + presence/absence of "date"), not by literal name.
extract_events() then collapses the month-end panel into the actual change events
per (RIC, agency), normalised onto the rating_index axis via scale.normalize_rating.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.ratings.scale import normalize_rating, STATUS_DEFAULT, STATUS_WITHDRAWN


# Agency code → substrings that identify its columns (case-insensitive).
AGENCY_TOKENS: dict[str, list[str]] = {
    "MDY": ["mdy", "moody"],
    "FTC": ["ftc", "fitch"],
    "SPI": ["spi", "s&p", "sandp", "standardpoor", "spgmi"],
}


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load a drop CSV as strings (ratings/dates stay verbatim; NaN→'' on use)."""
    return pd.read_csv(path, dtype=str, keep_default_na=True)


def detect_columns(columns: list[str]) -> dict[str, Any]:
    """
    Resolve the meaningful columns of the history CSV by pattern.

    Returns:
        {
          "date_col": str | None,          # the per-row month-end snapshot date
          "instrument_col": str | None,    # the RIC / instrument id
          "name_col": str | None,          # the common name
          "agencies": {AGENCY: {"rating_col": str, "date_col": str | None}}
        }
    For each agency, the column carrying its token WITHOUT "date" is the rating; the
    one WITH "date" is its rating-effective-date companion (may be absent).
    """
    cols = list(columns)
    low = {c: c.lower() for c in cols}

    def find(pred) -> str | None:
        return next((c for c in cols if pred(low[c])), None)

    # Exact "date" first (the row snapshot date), else a leading "date" that isn't
    # one of the agency effective-date columns.
    date_col = find(lambda l: l == "date") or find(
        lambda l: l.startswith("date") and "src" not in l and not any(
            t in l for toks in AGENCY_TOKENS.values() for t in toks
        )
    )
    instrument_col = find(lambda l: "instrument" in l) or find(
        lambda l: l == "ric" or l.endswith(".ric") or l.endswith("_ric")
    )
    name_col = find(lambda l: "commonname" in l or "company" in l or l == "name")

    agencies: dict[str, dict[str, str | None]] = {}
    for ag, toks in AGENCY_TOKENS.items():
        ag_cols = [c for c in cols if any(t in low[c] for t in toks)]
        if not ag_cols:
            continue
        rating_c = next((c for c in ag_cols if "date" not in low[c]), None)
        date_c = next((c for c in ag_cols if "date" in low[c]), None)
        if rating_c is not None:
            agencies[ag] = {"rating_col": rating_c, "date_col": date_c}

    return {
        "date_col": date_col,
        "instrument_col": instrument_col,
        "name_col": name_col,
        "agencies": agencies,
    }


def _action(prev_idx: int | None, idx: int | None, status: str) -> str:
    """Classify a rating action relative to the previous event for that issuer/agency."""
    if status == STATUS_DEFAULT:
        return "default"
    if status == STATUS_WITHDRAWN:
        return "withdrawn"
    if prev_idx is None or idx is None:
        return "new"
    if idx < prev_idx:
        return "upgrade"      # lower index = better
    if idx > prev_idx:
        return "downgrade"
    return "affirm"


def _clean(v: Any) -> str | None:
    """NaN / blank → None; else the stripped string."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def extract_events(history: pd.DataFrame, cols: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """
    Collapse the rating-history panel into per-(RIC, agency) change events.

    For each agency, events are de-duplicated by their effective date when the
    agency ".date" column is present (the authoritative change date); otherwise the
    per-row snapshot date is used and consecutive identical ratings are collapsed.
    Each event is normalised onto the rating_index axis and tagged with its action.

    Returns a list of event dicts (newest-agnostic; sorted per key by effective_date):
        {ric, agency, effective_date, rating_index, rating_raw, rating_status, rating_action}
    """
    if cols is None:
        cols = detect_columns(list(history.columns))

    inst_col = cols["instrument_col"]
    row_date_col = cols["date_col"]
    if inst_col is None:
        raise ValueError("Could not detect an instrument/RIC column in the ratings history CSV")

    events: list[dict[str, Any]] = []

    for agency, ac in cols["agencies"].items():
        rating_col = ac["rating_col"]
        eff_col = ac["date_col"]
        date_col = eff_col or row_date_col
        if date_col is None:
            raise ValueError(f"No effective-date or snapshot-date column for agency {agency}")

        sub = history[[inst_col, rating_col, date_col]].copy()
        sub.columns = ["ric", "rating_raw", "eff_date"]

        for ric, grp in sub.groupby("ric", sort=False):
            ric = _clean(ric)
            if ric is None:
                continue
            # One row per distinct effective date, ordered chronologically; the last
            # rating seen for a given date wins (stable for already-deduped drops).
            seen: dict[str, str | None] = {}
            for _, r in grp.iterrows():
                eff = _clean(r["eff_date"])
                if eff is None:
                    continue
                seen[eff] = _clean(r["rating_raw"])

            prev_idx: int | None = None
            prev_status: str | None = None
            for eff in sorted(seen):
                idx, status = normalize_rating(seen[eff])
                # Collapse to genuine changes: skip when neither the notch nor the
                # status moved since the previous retained event.
                if prev_status is not None and idx == prev_idx and status == prev_status:
                    continue
                events.append({
                    "ric": ric,
                    "agency": agency,
                    "effective_date": eff,
                    "rating_index": idx,
                    "rating_raw": seen[eff],
                    "rating_status": status,
                    "rating_action": _action(prev_idx, idx, status),
                })
                prev_idx, prev_status = idx, status

    return events
