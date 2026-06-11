"""Tests for src/backtest.py — point-in-time correctness is the key invariant."""

import pytest
from datetime import date

from src.backtest import (
    DEFAULT_STEPS,
    _filter_periods_point_in_time,
    _resolve_case_cik,
    load_cases,
    score_issuer_at_date,
    evaluate_distressed_case,
    evaluate_healthy_case,
    build_scorecard,
    compare_to_baseline,
)

# Facts where one period was filed BEFORE eval_date and one AFTER
FACTS_WITH_TWO_PERIODS = {
    "facts": {
        "us-gaap": {
            "OperatingIncomeLoss": {
                "units": {
                    "USD": [
                        # Filed before eval_date → should be included
                        {"end": "2022-12-31", "val": 500_000, "filed": "2023-02-15", "form": "10-K"},
                        # Filed after eval_date → must be excluded (no look-ahead)
                        {"end": "2023-12-31", "val": 100_000, "filed": "2024-02-15", "form": "10-K"},
                    ]
                }
            }
        }
    }
}


def test_point_in_time_excludes_future_filings():
    eval_date = date(2023, 6, 1)  # between the two filing dates
    periods = _filter_periods_point_in_time(FACTS_WITH_TWO_PERIODS, eval_date)
    # Only the 2022-12-31 period (filed 2023-02-15) should be available
    assert "2022-12-31" in periods
    assert "2023-12-31" not in periods


def test_point_in_time_includes_same_day_filings():
    eval_date = date(2024, 2, 15)  # exactly the filing date of the second period
    periods = _filter_periods_point_in_time(FACTS_WITH_TWO_PERIODS, eval_date)
    assert "2023-12-31" in periods


def test_future_dated_facts_are_not_periods():
    # 10-Ks carry facts ABOUT future periods (e.g. expected debt maturities
    # tagged end=2026 inside a 2015 filing). Those must never be selected as
    # scoreable fiscal periods — only periods that ended before filing count.
    facts = {
        "facts": {
            "us-gaap": {
                "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive": {
                    "units": {
                        "USD": [
                            {"end": "2026-11-30", "val": 1_000_000,
                             "filed": "2016-03-16", "form": "10-K"},
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            {"end": "2015-12-31", "val": 9_000_000,
                             "filed": "2016-03-16", "form": "10-K"},
                        ]
                    }
                },
            }
        }
    }
    periods = _filter_periods_point_in_time(facts, date(2016, 4, 13))
    assert periods == ["2015-12-31"]


def test_no_periods_before_any_filing():
    eval_date = date(2020, 1, 1)  # before all filings
    periods = _filter_periods_point_in_time(FACTS_WITH_TWO_PERIODS, eval_date)
    assert periods == []


def test_score_at_date_uses_only_available_data():
    # With eval_date before 2023-12-31 filing, extraction should use 2022-12-31 data
    # But we don't have enough tags in this fixture to compute ratios,
    # so we just verify it doesn't crash and returns a scored Snapshot
    eval_date = date(2023, 6, 1)
    snap = score_issuer_at_date(FACTS_WITH_TWO_PERIODS, eval_date, threshold=50)
    assert isinstance(snap.score, float)
    assert isinstance(snap.stressed, bool)
    assert snap.has_data is True
    assert snap.period_end == "2022-12-31"
    # Per-metric values are captured for the metrics-by-year view; with this
    # sparse fixture they're present but None (not computable).
    assert "leverage" in snap.ratios and "liquidity" in snap.ratios


def test_score_zero_when_no_periods_available():
    eval_date = date(2010, 1, 1)  # way before any filings
    snap = score_issuer_at_date(FACTS_WITH_TWO_PERIODS, eval_date, threshold=50)
    assert snap.score == 0.0
    assert snap.stressed is False
    # has_data is what distinguishes "no filings yet" from a genuine low score.
    assert snap.has_data is False


# ── Case library loading ─────────────────────────────────────────────────────

def test_load_cases_new_schema(tmp_path):
    p = tmp_path / "cases.csv"
    p.write_text(
        "case_id,company_name,ticker,cik,label,event_date,notes\n"
        "hertz-2020,Hertz,HTZ,0001657853,distressed,2020-05-22,Chapter 11\n"
        "aapl,Apple,AAPL,0000320193,healthy,2025-12-31,Control\n"
    )
    cases = load_cases(p)
    assert len(cases) == 2
    assert cases[0]["case_id"] == "hertz-2020"
    assert cases[0]["cik"] == "0001657853"
    assert cases[1]["label"] == "healthy"


def test_load_cases_old_schema(tmp_path):
    # Rows from the pre-CIK 4-column schema parse with the new keys absent.
    p = tmp_path / "cases.csv"
    p.write_text("ticker,label,event_date,notes\nHTZ,distressed,2020-05-22,bankruptcy\n")
    cases = load_cases(p)
    assert cases[0]["ticker"] == "HTZ"
    assert cases[0].get("cik") is None


def test_load_cases_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_cases(tmp_path / "nope.csv")


# ── Case resolution ──────────────────────────────────────────────────────────

def test_resolve_case_prefers_cik(monkeypatch):
    # If the CIK column is set, ticker resolution must not be touched at all —
    # a delisted ticker would raise.
    import src.ingest as ingest

    def _boom(ticker):
        raise AssertionError("get_cik must not be called when a CIK is provided")

    monkeypatch.setattr(ingest, "get_cik", _boom)
    cik = _resolve_case_cik({"cik": "320193", "ticker": "WRONG"})
    assert cik == "0000320193"


def test_resolve_case_falls_back_to_ticker(monkeypatch):
    import src.ingest as ingest
    monkeypatch.setattr(ingest, "get_cik", lambda ticker: "0000789019")
    # Blank cik column (and the old 4-column schema, where it's absent entirely)
    assert _resolve_case_cik({"cik": "", "ticker": "MSFT"}) == "0000789019"
    assert _resolve_case_cik({"ticker": "MSFT"}) == "0000789019"


# ── Per-case evaluation ──────────────────────────────────────────────────────

def test_distressed_case_data_gap():
    # The only filing post-dates every evaluation date → the model never had
    # data, which must be classified data_gap, not missed.
    facts = {
        "facts": {
            "us-gaap": {
                "OperatingIncomeLoss": {
                    "units": {
                        "USD": [
                            {"end": "2023-12-31", "val": 1, "filed": "2024-02-15", "form": "10-K"},
                        ]
                    }
                }
            }
        }
    }
    result = evaluate_distressed_case(facts, event_date=date(2020, 6, 1), threshold=50)
    assert result["status"] == "data_gap"
    assert result["lead_months"] is None
    # Anchor (T-0) plus DEFAULT_STEPS-1 backward steps.
    assert len(result["trajectory"]) == DEFAULT_STEPS


def test_healthy_case_counts_evaluated_periods():
    result = evaluate_healthy_case(
        FACTS_WITH_TWO_PERIODS, anchor_date=date(2024, 6, 1), threshold=50
    )
    assert result["status"] == "clean"
    assert result["fp_count"] == 0
    # Not all snapshots had data (filings start Feb 2023), so the denominator
    # must be the evaluated count, not the full step count.
    assert 0 < result["periods_evaluated"] < DEFAULT_STEPS


# ── Scorecard math ───────────────────────────────────────────────────────────

def _distressed(case_id, status, lead=None):
    return {"case_id": case_id, "label": "distressed", "status": status,
            "lead_months": lead, "caught": status == "caught"}


def _healthy(case_id, fp, evaluated, status="clean"):
    return {"case_id": case_id, "label": "healthy", "status": status,
            "fp_count": fp, "periods_evaluated": evaluated}


def test_scorecard_excludes_gaps_and_errors_from_catch_rate():
    cases = [
        _distressed("a", "caught", 12.0),
        _distressed("b", "missed"),
        _distressed("c", "data_gap"),
        _distressed("d", "error"),
        _healthy("h", 0, 12),
    ]
    summary = build_scorecard(cases, threshold=50, early_months=6.0)
    assert summary["caught"] == 1
    assert summary["total_distressed"] == 2   # caught + missed only
    assert summary["catch_rate"] == 50.0
    assert summary["data_gaps"] == 1
    assert summary["errors"] == 1


def test_scorecard_median_lead_even_count():
    cases = [
        _distressed("a", "caught", 10.0),
        _distressed("b", "caught", 20.0),
    ]
    summary = build_scorecard(cases, threshold=50, early_months=6.0)
    # statistics.median of [10, 20] is 15 — the old sorted[n//2] gave 20.
    assert summary["median_lead_months"] == 15.0
    assert summary["mean_lead_months"] == 15.0


def test_scorecard_early_warning_boundary():
    cases = [
        _distressed("late", "caught", 5.9),   # caught but too late to act
        _distressed("early", "caught", 6.0),  # exactly at the cutoff counts
    ]
    summary = build_scorecard(cases, threshold=50, early_months=6.0)
    assert summary["caught"] == 2
    assert summary["early_warning_caught"] == 1
    assert summary["early_warning_rate"] == 50.0


def test_scorecard_fp_rate_uses_evaluated_periods():
    cases = [_healthy("h1", 2, 8), _healthy("h2", 0, 12)]
    summary = build_scorecard(cases, threshold=50, early_months=6.0)
    assert summary["fp_periods"] == 2
    assert summary["healthy_periods_evaluated"] == 20
    assert summary["fp_rate"] == 10.0


# ── Baseline comparison ──────────────────────────────────────────────────────

def _results(cases):
    return {"cases": cases, "summary": {}}


def test_baseline_newly_missed_is_regression():
    base = _results([_distressed("a", "caught", 12.0)])
    cur = _results([_distressed("a", "missed")])
    diff = compare_to_baseline(cur, base)
    assert diff["regressed"] is True
    assert any("a" in r for r in diff["regressions"])


def test_baseline_lead_drop_within_tolerance_ok():
    base = _results([_distressed("a", "caught", 12.0)])
    cur = _results([_distressed("a", "caught", 10.0)])  # drop of 2 < tolerance 3
    diff = compare_to_baseline(cur, base, lead_tolerance_months=3.0)
    assert diff["regressed"] is False


def test_baseline_lead_drop_beyond_tolerance_regresses():
    base = _results([_distressed("a", "caught", 12.0)])
    cur = _results([_distressed("a", "caught", 8.0)])   # drop of 4 > tolerance 3
    diff = compare_to_baseline(cur, base, lead_tolerance_months=3.0)
    assert diff["regressed"] is True


def test_baseline_newly_caught_is_improvement_only():
    base = _results([_distressed("a", "missed")])
    cur = _results([_distressed("a", "caught", 9.0)])
    diff = compare_to_baseline(cur, base)
    assert diff["regressed"] is False
    assert any("newly caught" in i for i in diff["improvements"])


def test_baseline_vanished_case_is_regression():
    base = _results([_distressed("a", "caught", 12.0), _distressed("b", "caught", 5.0)])
    cur = _results([_distressed("a", "caught", 12.0)])
    diff = compare_to_baseline(cur, base)
    assert diff["regressed"] is True
    assert any("missing" in r for r in diff["regressions"])


def test_baseline_new_case_is_neutral():
    base = _results([_distressed("a", "caught", 12.0)])
    cur = _results([_distressed("a", "caught", 12.0), _distressed("z", "missed")])
    diff = compare_to_baseline(cur, base)
    assert diff["regressed"] is False
    assert any("z" in n for n in diff["new_cases"])


def test_baseline_healthy_fp_increase_regresses():
    base = _results([_healthy("h", 0, 12)])
    cur = _results([_healthy("h", 2, 12, status="false_positive")])
    diff = compare_to_baseline(cur, base)
    assert diff["regressed"] is True
