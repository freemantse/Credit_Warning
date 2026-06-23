"""Tests for src/migration_backtest.py — pure harness logic with an injected model."""

from src.migration_backtest import run_migration_backtest


# Deterministic stand-in for the model: P(event) = min(1, leverage/10), so a higher
# leverage snapshot flags. `path` (the vintage) is ignored — we only test the
# catch/lead/false-positive + vintage-selection logic, not the model.
def stub_head_prob(path, X, head):
    lev = float(X["leverage"].iloc[0])
    return min(1.0, lev / 10.0)


VINTAGES = [{"cutoff": "2018-12-31", "path": "v0"}, {"cutoff": "2020-12-31", "path": "v1"}]
FEATURES = ["leverage"]


def _scoring(rows):
    return {"0000000001": rows[0], "0000000002": rows[1]}


def test_downgrade_caught_early_and_control_clean():
    scoring = {
        "0000000001": [
            {"period_end": "2019-12-31", "leverage": 3.0},   # 0.3 → no flag
            {"period_end": "2020-12-31", "leverage": 6.0},   # 0.6 → flag
            {"period_end": "2021-12-31", "leverage": 8.0},   # 0.8 → flag
        ],
        "0000000002": [
            {"period_end": "2022-12-31", "leverage": 1.0},   # 0.1 → no flag (control)
            {"period_end": "2023-12-31", "leverage": 1.2},
        ],
    }
    cases = [
        {"cik": "1", "ticker": "AAA", "event_type": "downgrade", "event_date": "2022-06-30"},
        {"cik": "2", "ticker": "CTL", "event_type": "control", "event_date": "2025-12-31"},
    ]
    out = run_migration_backtest(cases, scoring, VINTAGES, threshold=0.5,
                                 head_prob_fn=stub_head_prob, feature_columns=FEATURES)

    down = next(c for c in out["cases"] if c["event_type"] == "downgrade")
    assert down["status"] == "caught" and down["caught"] is True
    # Earliest flag is the OLDEST flagged snapshot (2020-12-31) → ~18 months lead.
    assert 17 <= down["lead_months"] <= 19
    assert down["early_warning"] is True

    ctl = next(c for c in out["cases"] if c["event_type"] == "control")
    assert ctl["status"] == "clean" and ctl["fp_count"] == 0

    agg = out["by_event_type"]
    assert agg["downgrade"]["catch_rate"] == 100.0
    assert agg["downgrade"]["caught"] == 1
    assert agg["control"]["fp_rate"] == 0.0


def test_control_false_positive_counts():
    scoring = {"0000000001": [{"period_end": "2021-12-31", "leverage": 9.0}]}  # 0.9 → flag
    cases = [{"cik": "1", "ticker": "X", "event_type": "control", "event_date": "2025-12-31"}]
    out = run_migration_backtest(cases, scoring, VINTAGES, head_prob_fn=stub_head_prob,
                                 feature_columns=FEATURES)
    ctl = out["cases"][0]
    assert ctl["status"] == "false_positive" and ctl["fp_count"] == 1
    assert out["by_event_type"]["control"]["fp_rate"] == 100.0


def test_no_vintage_before_snapshot_is_data_gap():
    # All snapshots predate the earliest vintage cutoff → can't score without leakage.
    scoring = {"0000000001": [{"period_end": "2017-12-31", "leverage": 9.0}]}
    cases = [{"cik": "1", "ticker": "OLD", "event_type": "default", "event_date": "2018-03-31"}]
    out = run_migration_backtest(cases, scoring, VINTAGES, head_prob_fn=stub_head_prob,
                                 feature_columns=FEATURES)
    assert out["cases"][0]["status"] == "data_gap"


def test_missing_issuer_rows_is_data_gap():
    cases = [{"cik": "9", "ticker": "GHOST", "event_type": "downgrade", "event_date": "2022-06-30"}]
    out = run_migration_backtest(cases, {}, VINTAGES, head_prob_fn=stub_head_prob,
                                 feature_columns=FEATURES)
    assert out["cases"][0]["status"] == "data_gap"
