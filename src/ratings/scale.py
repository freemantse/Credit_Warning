"""
Rating notation ↔ rating_index normalization.

The canonical index axis is src.rating.RATING_SCALE (0 = AAA … 21 = D, higher =
worse) — the SAME axis the implied rating uses, so agency and implied ratings are
directly comparable. This module maps the three agencies' notations onto it and
classifies non-notch statuses (withdrawn / not-rated / default).

It is the Python-side source of truth for the mapping; supabase/schema.sql
seeds an equivalent `rating_scale` table for SQL-side joins (kept in sync by hand;
test_ratings_scale asserts they agree).
"""

from __future__ import annotations

import re

from src.rating import RATING_SCALE, rating_index


# Moody's notation → S&P/Fitch-equivalent letter (then → index via RATING_SCALE).
MOODY_TO_SP: dict[str, str] = {
    "Aaa": "AAA",
    "Aa1": "AA+", "Aa2": "AA", "Aa3": "AA-",
    "A1": "A+", "A2": "A", "A3": "A-",
    "Baa1": "BBB+", "Baa2": "BBB", "Baa3": "BBB-",
    "Ba1": "BB+", "Ba2": "BB", "Ba3": "BB-",
    "B1": "B+", "B2": "B", "B3": "B-",
    "Caa1": "CCC+", "Caa2": "CCC", "Caa3": "CCC-",
    "Ca": "CC", "C": "C",
}

# Non-notch status tokens (compared upper-cased).
WITHDRAWN_TOKENS = {"WD", "WR", "WITHDRAWN"}                 # WR = Moody's "rating withdrawn"
NOT_RATED_TOKENS = {"NR", "NA", "N.A.", "UNSOLICITED", ""}   # not rated / no rating
DEFAULT_TOKENS = {"D", "SD", "RD", "DD", "DDD"}              # S&P SD/D, Fitch RD/DD/DDD/D

# Status values.
STATUS_RATED = "rated"
STATUS_WITHDRAWN = "withdrawn"
STATUS_NOT_RATED = "not_rated"
STATUS_DEFAULT = "default"

# S&P/Fitch letters → index, derived once from the canonical scale (upper-cased keys).
_SP_TO_INDEX: dict[str, int] = {letter.upper(): i for i, letter in enumerate(RATING_SCALE)}
_MOODY_TO_INDEX: dict[str, int] = {k.upper(): rating_index(v) for k, v in MOODY_TO_SP.items()}

# Leading rating token: letters, an optional digit (Moody's Aa1/Baa2/…), an
# optional +/- modifier (S&P AA-/BBB+). Captures "Baa2", "BBB+", "AA-", "CCC", "WR".
_TOKEN_RE = re.compile(r"[A-Za-z]+[0-9]?[+-]?")


def normalize_rating(raw: str | None) -> tuple[int | None, str]:
    """
    Map a raw agency rating string to (rating_index, status).

    Handles S&P/Fitch ("BBB-"), Moody's ("Baa3"), and non-notch tokens
    (withdrawn "WD"/"WR", not-rated "NR"/blank, default "D"/"SD"/"RD"). Watch/
    outlook decorations and unsolicited markers after the core token are ignored
    (only the leading rating token is read).

    Returns:
        (rating_index, status):
          - rated:      (0..21, "rated")
          - withdrawn:  (None, "withdrawn")
          - not_rated:  (None, "not_rated")  — also the fallback for unrecognised input
          - default:    (21,   "default")    — index pinned to D
    """
    if raw is None:
        return (None, STATUS_NOT_RATED)
    s = str(raw).strip()
    if not s:
        return (None, STATUS_NOT_RATED)

    m = _TOKEN_RE.match(s)
    token = (m.group(0) if m else s).upper()

    if token in DEFAULT_TOKENS:
        return (rating_index("D"), STATUS_DEFAULT)
    if token in WITHDRAWN_TOKENS:
        return (None, STATUS_WITHDRAWN)
    if token in NOT_RATED_TOKENS:
        return (None, STATUS_NOT_RATED)
    if token in _SP_TO_INDEX:
        return (_SP_TO_INDEX[token], STATUS_RATED)
    if token in _MOODY_TO_INDEX:
        return (_MOODY_TO_INDEX[token], STATUS_RATED)
    # Unrecognised notation — treat as not-rated rather than guessing a notch.
    return (None, STATUS_NOT_RATED)


def grade_for_index(idx: int | None) -> str | None:
    """IG (≤ BBB-), HY (BB+ … C), or D (default). None when idx is None."""
    if idx is None:
        return None
    if idx >= rating_index("D"):
        return "D"
    if idx <= rating_index("BBB-"):
        return "IG"
    return "HY"


# Rows mirrored by the SQL seed in schema.sql (rating_index, sp_fitch,
# moody, grade). Built from the canonical scale so the two never drift.
_SP_TO_MOODY: dict[str, str] = {v: k for k, v in MOODY_TO_SP.items()}
RATING_SCALE_ROWS: list[tuple[int, str, str | None, str]] = [
    (i, letter, _SP_TO_MOODY.get(letter), grade_for_index(i))
    for i, letter in enumerate(RATING_SCALE)
]
