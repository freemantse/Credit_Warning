"""
Load the file-based LSEG drop and turn the rating history into rating-change EVENTS.

The drop is a LONG action log (one row per dated rating ACTION):
    Instrument, Company Common Name, Date, Issuer Rating,
    Rating Source Description, Rating Type Description
where "Rating Source Description" encodes BOTH the agency and the rating class
(e.g. "Moody's Senior Unsecured", "Egan-Jones Senior Unsecured", "Fitch Long-term
Issuer Default Rating"). This is the shape of the demo's real data drop
(data/ratings_history_raw.csv).

detect_long_columns() resolves the columns by pattern (names vary across LSEG
configs); extract_events_long() then collapses the log into the actual change events
per (RIC, agency), normalised onto the rating_index axis via scale.normalize_rating.

The companion universe_xref.csv (PermID, RIC, CommonName, TickerSymbol, CUSIP, ISIN)
is consumed by src.ratings.crosswalk to resolve each RIC → canonical CIK.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.ratings.scale import normalize_rating, STATUS_DEFAULT, STATUS_WITHDRAWN


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load a drop CSV as strings (ratings/dates stay verbatim; NaN→'' on use)."""
    return pd.read_csv(path, dtype=str, keep_default_na=True)


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


# ── Canonical long-term series selection ──────────────────────────────────────
#
# Every row of the drop is a single dated rating action and "Rating Source
# Description" carries both the agency and the rating class. We only keep the
# issuer-level LONG-TERM
# senior rating for the three US NRSROs that have broad coverage in the drop —
# Moody's (MDY), Fitch (FTC), Egan-Jones (EJR). DBRS / R&I / JCR have negligible
# US coverage and are skipped; S&P is absent from the drop.
#
# An issuer often carries several long-term series per agency (e.g. Moody's keeps a
# "Long-term Issuer Rating" AND a "Senior Unsecured" line, one of which may be
# withdrawn while the other continues). LONG_TERM_SOURCES lists the EXACT source
# descriptions that ARE the issuer-level long-term rating, per agency — listed
# exactly (not by substring) so the noisy look-alikes ("Moody's LGD Senior
# Unsecured", "…Bank Credit Facility", "…Commercial Paper", "…Probability of
# Default") are never mistaken for the issuer rating. Among the allowed series an
# issuer actually has, _pick_canonical_series chooses the richest one (most
# actions, then most-recently maintained) as that issuer/agency's trajectory.

LONG_TERM_SOURCES: dict[str, list[str]] = {
    "MDY": [
        "Moody's Long-term Issuer Rating",
        "Moody's Senior Unsecured",
        "Moody's Corporate Family Rating",
        "Moody's Derived Long-term Issuer Rating",
        "Moody's Backed Senior Unsecured",
        "Moody's Long-term Senior Unsecured MTN Rating",
    ],
    "FTC": [
        "Fitch Long-term Issuer Default Rating",
        "Fitch Long-term Issuer Rating",
        "Fitch Senior Unsecured",
        "Fitch Backed Senior Unsecured",
    ],
    "EJR": [
        "Egan-Jones Senior Unsecured",
    ],
}

# source description (exact) → agency code, built once from LONG_TERM_SOURCES.
_SOURCE_TO_AGENCY: dict[str, str] = {
    src: agency for agency, srcs in LONG_TERM_SOURCES.items() for src in srcs
}


def detect_long_columns(columns: list[str]) -> dict[str, str | None]:
    """
    Resolve the meaningful columns of a LONG action-log CSV by pattern.

    Returns {instrument_col, date_col, name_col, rating_col, source_col}. Names
    vary across LSEG configs, so each is matched case-insensitively by substring.
    """
    cols = list(columns)
    low = {c: c.lower() for c in cols}

    def find(pred) -> str | None:
        return next((c for c in cols if pred(low[c])), None)

    return {
        "instrument_col": find(lambda l: "instrument" in l) or find(lambda l: l == "ric"),
        # The action date — a bare "date" that isn't a "rating source"/"type" label.
        "date_col": find(lambda l: l == "date") or find(lambda l: l.startswith("date")),
        "name_col": find(lambda l: "common name" in l or "company" in l or l == "name"),
        # The rating value column ("Issuer Rating"); avoid the "rating source"/"rating
        # type" descriptor columns, which also contain "rating".
        "rating_col": find(lambda l: "rating" in l and "source" not in l and "type" not in l),
        "source_col": find(lambda l: "source" in l),
    }


def _pick_canonical_series(
    actions_by_source: dict[str, dict[str, str | None]],
    priority: list[str],
) -> tuple[str | None, dict[str, str | None]]:
    """
    Choose the canonical issuer-level long-term series among the allowed ones an
    issuer/agency actually has.

    `actions_by_source` maps source-description → {effective_date: raw_rating}. The
    winner is the series with the MOST distinct dated actions, tie-broken by the
    most-recent last action, then by the priority order (a stable final tie-break).
    Returns (source_description | None, {date: raw}).
    """
    best_src: str | None = None
    best_key: tuple[int, str, int] | None = None
    for src, actions in actions_by_source.items():
        if not actions:
            continue
        last_date = max(actions)
        # Higher rank (earlier in priority) wins the final tie — negate the index.
        rank = -priority.index(src) if src in priority else -len(priority)
        key = (len(actions), last_date, rank)
        if best_key is None or key > best_key:
            best_key, best_src = key, src
    return best_src, (actions_by_source.get(best_src, {}) if best_src else {})


def extract_events_long(
    history: pd.DataFrame,
    *,
    agencies: tuple[str, ...] = ("MDY", "FTC", "EJR"),
    cols: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """
    Collapse a LONG action-log drop into per-(RIC, agency) change events.

    For each (instrument, agency) we read every allowed long-term source series,
    pick the canonical one (_pick_canonical_series), normalise each dated action onto
    the rating_index axis, and keep only genuine changes (notch or status moved),
    tagging each with its action. Each event dict is:
        {ric, agency, effective_date, rating_index, rating_raw, rating_status, rating_action}

    Only Moody's / Fitch / Egan-Jones issuer-level long-term ratings are retained;
    every other source description (LGD, bank facility, commercial paper, PDR,
    short-term, DBRS/R&I/JCR) is ignored.
    """
    if cols is None:
        cols = detect_long_columns(list(history.columns))
    inst_col, date_col = cols.get("instrument_col"), cols.get("date_col")
    rating_col, source_col = cols.get("rating_col"), cols.get("source_col")
    if not (inst_col and date_col and rating_col and source_col):
        raise ValueError(
            "Could not detect instrument/date/rating/source columns in the long ratings CSV"
        )

    wanted_agencies = set(agencies)

    # Gather: ric → agency → source_description → {effective_date: raw_rating}.
    gathered: dict[str, dict[str, dict[str, dict[str, str | None]]]] = {}
    for _, r in history.iterrows():
        source = _clean(r[source_col])
        agency = _SOURCE_TO_AGENCY.get(source) if source else None
        if agency is None or agency not in wanted_agencies:
            continue
        ric = _clean(r[inst_col])
        eff = _clean(r[date_col])
        if ric is None or eff is None:
            continue
        # Last raw rating for a given (source, date) wins (stable for deduped drops).
        gathered.setdefault(ric, {}).setdefault(agency, {}).setdefault(source, {})[eff] = _clean(
            r[rating_col]
        )

    events: list[dict[str, Any]] = []
    for ric, by_agency in gathered.items():
        for agency, by_source in by_agency.items():
            _src, actions = _pick_canonical_series(by_source, LONG_TERM_SOURCES[agency])
            prev_idx: int | None = None
            prev_status: str | None = None
            for eff in sorted(actions):
                idx, status = normalize_rating(actions[eff])
                # Collapse to genuine changes: skip when neither notch nor status moved.
                if prev_status is not None and idx == prev_idx and status == prev_status:
                    continue
                events.append({
                    "ric": ric,
                    "agency": agency,
                    "effective_date": eff,
                    "rating_index": idx,
                    "rating_raw": actions[eff],
                    "rating_status": status,
                    "rating_action": _action(prev_idx, idx, status),
                })
                prev_idx, prev_status = idx, status

    return events
