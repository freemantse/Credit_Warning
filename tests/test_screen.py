"""Tests for src/screen.py + rating.notch_instrument (pure ranking/filter)."""

from src.rating import notch_instrument, rating_index, RATING_SCALE, OUTLOOK_NEGATIVE, OUTLOOK_STABLE, OUTLOOK_POSITIVE
from src.screen import build_screen_rows, SENIOR_SECURED


class _Outlook:
    """Minimal stand-in for RatingOutlookResult (build_screen_rows reads .outlook)."""
    def __init__(self, outlook):
        self.outlook = outlook


# ── notch_instrument ─────────────────────────────────────────────────────────

def test_notching_directions():
    bbb = rating_index("BBB")  # 8
    assert notch_instrument(bbb, "senior_secured") == rating_index("BBB+")   # up one
    assert notch_instrument(bbb, "senior_unsecured") == bbb                  # unchanged
    assert notch_instrument(bbb, "subordinated") == rating_index("BB+")      # down two
    assert notch_instrument(None, "senior_secured") is None


def test_notching_clamps_to_scale():
    assert notch_instrument(0, "senior_secured") == 0                  # AAA can't go higher
    assert notch_instrument(len(RATING_SCALE) - 1, "subordinated") == len(RATING_SCALE) - 1


# ── build_screen_rows ────────────────────────────────────────────────────────

def _instrument(name, seniority=SENIOR_SECURED):
    return {"instrument_name": name, "seniority": seniority, "principal_amount": None,
            "coupon": None, "maturity_year": None, "evidence_quote": "q", "source": "s"}


def _scaffold():
    issuers_map = {
        "C1": {"ticker": "AAA1", "name": "Healthy A"},
        "C2": {"ticker": "BBB2", "name": "Healthy BBB"},
        "C3": {"ticker": "JUNK", "name": "Junky"},
        "C4": {"ticker": "NEG", "name": "Deteriorating"},
    }
    implied = {
        "C1": {"2023-12-31": {"rating_index": rating_index("A")}},      # 5 (healthy)
        "C2": {"2023-12-31": {"rating_index": rating_index("BBB")}},    # 8 (healthy)
        "C3": {"2023-12-31": {"rating_index": rating_index("BB")}},     # 11 (junk → excluded)
        "C4": {"2023-12-31": {"rating_index": rating_index("BBB")}},    # 8 but Negative outlook
    }
    instruments = {
        "C1": {"2023-12-31": [_instrument("A SrSec Notes"), _instrument("A Sub Notes", "subordinated")]},
        "C2": {"2023-12-31": [_instrument("BBB SrSec Notes")]},
        "C3": {"2023-12-31": [_instrument("Junk SrSec Notes")]},
        "C4": {"2023-12-31": [_instrument("Neg SrSec Notes")]},
    }
    outlooks = {
        "C1": _Outlook(OUTLOOK_STABLE),
        "C2": _Outlook(OUTLOOK_POSITIVE),
        "C3": _Outlook(OUTLOOK_STABLE),
        "C4": _Outlook(OUTLOOK_NEGATIVE),
    }
    return issuers_map, implied, instruments, outlooks


def test_screen_filters_and_ranks():
    issuers_map, implied, instruments, outlooks = _scaffold()
    rows = build_screen_rows(
        issuers_map=issuers_map,
        implied_grouped=implied,
        instruments_grouped=instruments,
        outlook_by_cik=outlooks,
        min_rating_index=rating_index("BBB-"),   # 9
    )
    tickers = [r["ticker"] for r in rows]
    # C3 (BB, below the IG floor) and C4 (Negative outlook) are excluded; only the
    # senior-secured instruments of C1 and C2 remain (C1's subordinated note dropped).
    assert tickers == ["AAA1", "BBB2"]            # ranked: A (notched AA-→ better) before BBB
    a_row = rows[0]
    assert a_row["seniority"] == SENIOR_SECURED
    assert a_row["instrument_notched_rating"] == "A+"   # A (5) notched up one → A+ (4)
    assert a_row["issuer_implied_rating"] == "A"
    assert a_row["outlook"] == OUTLOOK_STABLE


def test_screen_includes_negative_when_not_excluded():
    issuers_map, implied, instruments, outlooks = _scaffold()
    rows = build_screen_rows(
        issuers_map=issuers_map,
        implied_grouped=implied,
        instruments_grouped=instruments,
        outlook_by_cik=outlooks,
        min_rating_index=rating_index("BBB-"),
        exclude_negative_outlook=False,
    )
    assert "NEG" in [r["ticker"] for r in rows]


def test_screen_subordinated_only_when_requested():
    issuers_map, implied, instruments, outlooks = _scaffold()
    rows = build_screen_rows(
        issuers_map=issuers_map,
        implied_grouped=implied,
        instruments_grouped=instruments,
        outlook_by_cik=outlooks,
        min_rating_index=rating_index("BBB-"),
        seniority="subordinated",
    )
    # Only C1 has a subordinated note; it notches DOWN two (A=5 → BBB+=7).
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAA1"
    assert rows[0]["instrument_notched_rating"] == "BBB+"
