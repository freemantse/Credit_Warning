"""Tests for src/migration_backtest.py — pure harness logic with an injected model."""

from pytest import approx

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
    # Controls are scored over the SAME trailing max_lead_months window as events
    # (anchored at the control's event_date), so the flagging snapshot must fall inside
    # that window (here 2024-12-31, within 24m of the 2025-12-31 anchor).
    scoring = {"0000000001": [{"period_end": "2024-12-31", "leverage": 9.0}]}  # 0.9 → flag
    cases = [{"cik": "1", "ticker": "X", "event_type": "control", "event_date": "2025-12-31"}]
    out = run_migration_backtest(cases, scoring, VINTAGES, head_prob_fn=stub_head_prob,
                                 feature_columns=FEATURES)
    ctl = out["cases"][0]
    assert ctl["status"] == "false_positive" and ctl["fp_count"] == 1
    assert out["by_event_type"]["control"]["fp_rate"] == 100.0


def test_control_outside_lead_window_is_data_gap():
    # A control whose only snapshot predates the trailing window (here 48m before the
    # anchor) has nothing to score in-window → data_gap, not a free "clean" pass. This
    # is the symmetric-window contract that stops controls being scanned over all history.
    scoring = {"0000000001": [{"period_end": "2021-12-31", "leverage": 9.0}]}
    cases = [{"cik": "1", "ticker": "X", "event_type": "control", "event_date": "2025-12-31"}]
    out = run_migration_backtest(cases, scoring, VINTAGES, head_prob_fn=stub_head_prob,
                                 feature_columns=FEATURES)
    assert out["cases"][0]["status"] == "data_gap"


def test_no_vintage_before_snapshot_is_data_gap():
    # All snapshots predate the earliest vintage cutoff → can't score without leakage.
    scoring = {"0000000001": [{"period_end": "2017-12-31", "leverage": 9.0}]}
    cases = [{"cik": "1", "ticker": "OLD", "event_type": "default", "event_date": "2018-03-31"}]
    out = run_migration_backtest(cases, scoring, VINTAGES, head_prob_fn=stub_head_prob,
                                 feature_columns=FEATURES)
    assert out["cases"][0]["status"] == "data_gap"


def test_deep_history_scores_newer_snapshots_not_data_gap():
    """
    Regression: a case whose OLDEST snapshot predates the earliest vintage must NOT be
    dropped as data_gap — its newer, scorable snapshots still count. (The post-loop
    check used to look at the oldest snapshot's vintage and discard the whole case,
    so deep-history issuers like Ford/GE/PG&E wrongly showed "no data".)
    """
    scoring = {"0000000001": [
        {"period_end": "2017-12-31", "leverage": 9.0},  # predates earliest vintage (2018) → unscorable
        {"period_end": "2019-12-31", "leverage": 8.0},  # vintage 2018 → flag
        {"period_end": "2021-12-31", "leverage": 8.0},  # vintage 2020 → flag
    ]}
    cases = [{"cik": "1", "ticker": "DEEP", "event_type": "downgrade", "event_date": "2022-06-30"}]
    out = run_migration_backtest(cases, scoring, VINTAGES, threshold=0.5,
                                 head_prob_fn=stub_head_prob, feature_columns=FEATURES)
    c = out["cases"][0]
    assert c["status"] == "caught"          # was wrongly "data_gap" before the fix
    assert c["caught"] is True


def test_missing_issuer_rows_is_data_gap():
    cases = [{"cik": "9", "ticker": "GHOST", "event_type": "downgrade", "event_date": "2022-06-30"}]
    out = run_migration_backtest(cases, {}, VINTAGES, head_prob_fn=stub_head_prob,
                                 feature_columns=FEATURES)
    assert out["cases"][0]["status"] == "data_gap"


def test_scores_any_agency_by_noisy_or():
    """
    When an issuer is rated by multiple agencies, each snapshot is scored under EVERY
    covering agency and combined by noisy-OR (the shipped issuer-level 'any-agency'
    signal). Here MDY alone (0.3) would NOT flag at 0.5, but MDY+EJR combine to
    1 − 0.7·0.5 = 0.65 → the case is caught.
    """
    def hp_by_agency(path, X, head):
        return 0.3 if float(X["agency_code"].iloc[0]) == 0 else 0.5   # MDY=0 → .3, EJR=2 → .5

    def events(cik):
        ev = [{"effective_date": "2018-01-01", "rating_index": 8, "rating_status": "rated"}]
        return {"MDY": list(ev), "EJR": list(ev)}

    scoring = {"0000000001": [{"period_end": "2020-12-31", "leverage": 1.0, "agency_code": None}]}
    cases = [{"cik": "1", "ticker": "X", "event_type": "downgrade",
              "event_date": "2021-06-30", "agency": "MDY"}]
    out = run_migration_backtest(cases, scoring, VINTAGES, threshold=0.5,
                                 head_prob_fn=hp_by_agency, agency_events_fn=events,
                                 feature_columns=["leverage", "agency_code"])
    c = out["cases"][0]
    assert c["status"] == "caught"
    assert c["trajectory"][0]["prob"] == approx(0.65)   # 1 − (1−.3)(1−.5)
