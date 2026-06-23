"""
End-to-end Stage 3 test: train → calibrate → predict → attribute → evaluate, on a
synthetic labeled matrix. Skipped if scikit-learn isn't installed.
"""

import pytest

pytest.importorskip("sklearn")

import numpy as np
import pandas as pd

from src.model.features import FEATURE_COLUMNS
from src.model.train import train_all, save_model
from src.model import predict as predict_mod
from src.model.evaluate import walk_forward_eval


def _synthetic_matrix(n=320, seed=0):
    """
    Build a labeled matrix with a STRONG, monotone-consistent signal: downgrades are
    driven by high leverage / high stress score / worse implied rating, spread across
    2015–2022 so walk-forward splits have data on both sides.
    """
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        lev = float(rng.uniform(0, 10))
        score = float(rng.uniform(0, 100))
        risk = 0.6 * (lev / 10) + 0.4 * (score / 100)
        label = 1 if risk > 0.6 else (-1 if risk < 0.2 else 0)
        default = bool(label == 1 and risk > 0.85)
        row = {c: np.nan for c in FEATURE_COLUMNS}
        row.update({
            "cik": f"C{i:04d}",
            "period_end": f"{2015 + i % 8}-12-31",
            "agency": "MDY",
            "leverage": lev,
            "stress_score": score,
            "implied_rating_index": int(round(risk * 15)),
            "label_12m": label,
            "default_12m": default,
        })
        rows.append(row)
    return pd.DataFrame(rows)


def _feature_row(**feats):
    row = {c: np.nan for c in FEATURE_COLUMNS}
    row.update(feats)
    return pd.DataFrame([row]).reindex(columns=FEATURE_COLUMNS).apply(pd.to_numeric, errors="coerce")


def test_train_learns_and_beats_base_rate():
    df = _synthetic_matrix()
    bundle, metrics = train_all(df, "2019-12-31", version="test")
    assert "downgrade" in bundle["heads"]
    m = metrics["heads"]["downgrade"]["model"]
    assert m["pr_auc"] is not None
    # A strong signal → PR-AUC well above the no-skill base rate.
    assert m["pr_auc"] > max(0.6, m["base_rate"])


def test_predictions_order_by_risk_and_are_monotone():
    df = _synthetic_matrix()
    bundle, _ = train_all(df, "2019-12-31", version="test")

    high = _feature_row(leverage=9.0, stress_score=90.0, implied_rating_index=12)
    low = _feature_row(leverage=1.0, stress_score=10.0, implied_rating_index=2)
    p_high = float(predict_mod.predict_proba_all(bundle, high)["downgrade"][0])
    p_low = float(predict_mod.predict_proba_all(bundle, low)["downgrade"][0])
    assert p_high > p_low

    # Monotone constraint: raising leverage cannot LOWER modeled downgrade risk.
    low_bumped = low.copy()
    low_bumped.iloc[0, low_bumped.columns.get_loc("leverage")] = 9.5
    p_bumped = float(predict_mod.predict_proba_all(bundle, low_bumped)["downgrade"][0])
    assert p_bumped >= p_low - 1e-9


def test_attribution_names_a_risk_driver():
    df = _synthetic_matrix()
    bundle, _ = train_all(df, "2019-12-31", version="test")
    high = _feature_row(leverage=9.0, stress_score=90.0, implied_rating_index=12)
    drivers = predict_mod.attribute(bundle, high, "downgrade", top_n=5)
    assert drivers
    risk_feats = {"leverage", "stress_score", "implied_rating_index"}
    top = drivers[0]
    assert top["feature"] in risk_feats
    assert top["contribution"] > 0          # the actual (high) value RAISED downgrade risk
    assert top["direction"] == "raises"


def test_predict_rows_aggregates_per_issuer_period():
    df = _synthetic_matrix()
    bundle, _ = train_all(df, "2019-12-31", version="test")
    rows = predict_mod.predict_rows(bundle, df.head(20))
    assert rows
    r = rows[0]
    assert {"cik", "period_end", "p_downgrade", "drivers_json", "model_version"} <= set(r)
    assert r["model_version"] == "test"
    assert r["horizon_months"] == 12


def test_save_load_roundtrip(tmp_path):
    df = _synthetic_matrix()
    bundle, _ = train_all(df, "2019-12-31", version="test")
    path = save_model(bundle, str(tmp_path / "m.joblib"))
    loaded = predict_mod.load_model(path)
    x = _feature_row(leverage=8.0, stress_score=80.0, implied_rating_index=11)
    p1 = float(predict_mod.predict_proba_all(bundle, x)["downgrade"][0])
    p2 = float(predict_mod.predict_proba_all(loaded, x)["downgrade"][0])
    assert p1 == pytest.approx(p2)


def test_walk_forward_eval_runs():
    df = _synthetic_matrix()
    result = walk_forward_eval(df, ["2018-12-31", "2020-12-31"])
    assert result["aggregate"]["downgrade"]["mean_pr_auc_model"] is not None
    assert len(result["per_split"]) == 2
    # Final-split confusion split by IG/HY bucket is present.
    assert "confusion_by_bucket_final" in result


def test_derive_score_config_learns_weights():
    from src.model.train import derive_score_config, RULE_TO_FEATURE
    from src.score import DEFAULT_CONFIG

    df = _synthetic_matrix()
    bundle, _ = train_all(df, "2019-12-31", version="test")
    cfg = derive_score_config(bundle)
    rules = cfg["rules"]
    # All 8 rules present; current_ratio is gone.
    assert set(RULE_TO_FEATURE) <= set(rules)
    assert "current_ratio<1.5x" not in rules
    # Weights are renormalised to the DEFAULT total (94), and at least one differs
    # from DEFAULT (they were learned, not copied).
    total = sum(rules[r]["weight"] for r in RULE_TO_FEATURE)
    default_total = sum(DEFAULT_CONFIG["rules"][r]["weight"] for r in RULE_TO_FEATURE)
    assert abs(total - default_total) <= 1.0
    assert any(rules[r]["weight"] != DEFAULT_CONFIG["rules"][r]["weight"] for r in RULE_TO_FEATURE)
    # Ramps are preserved (only weights are learned).
    assert rules["leverage>5x"]["healthy"] == DEFAULT_CONFIG["rules"]["leverage>5x"]["healthy"]


def test_train_vintages_and_select(tmp_path):
    from pathlib import Path
    from src.model.train import train_vintages, select_vintage

    df = _synthetic_matrix()
    vintages = train_vintages(df, ["2018-12-31", "2020-12-31"], out_dir=str(tmp_path / "v"))
    assert len(vintages) == 2
    assert all(Path(v["path"]).exists() for v in vintages)
    # Pick the latest vintage trained strictly before the snapshot date (no leakage).
    assert select_vintage(vintages, "2021-06-30").endswith("2020-12-31.joblib")
    assert select_vintage(vintages, "2019-06-30").endswith("2018-12-31.joblib")
    assert select_vintage(vintages, "2017-01-01") is None
