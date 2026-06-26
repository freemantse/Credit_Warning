"""
Stage 3 predictor (offline / CLI). Loads a fitted bundle, produces CALIBRATED
P(downgrade)/P(upgrade)/P(default), and — for auditability — the top signed feature
DRIVERS behind each downgrade probability.

Attribution without a heavy dependency: a deterministic BASELINE-DIFFERENCE method.
For a prediction p(x), each feature j is reset to its training-median baseline and
the probability re-evaluated; the shift Δⱼ = p(x) − p(x with xⱼ:=baseline) is that
feature's signed contribution (positive = the actual value pushed risk UP). It's a
faithful, monotone-consistent decomposition that reads like SHAP ("↑ Debt/EBITDA
raised downgrade risk +6pts") with zero extra install; swap in shap here later.

Predictions are written to migration_predictions (one row per cik+period_end+horizon),
read-only to the API/screen — the model never runs in the serverless hot path.
"""

from __future__ import annotations

from typing import Any

from src.model.dataset import HORIZON_MONTHS
from src.model.features import FEATURE_COLUMNS


def load_model(path: str) -> dict[str, Any]:
    import joblib
    return joblib.load(path)


def _proba(estimator, X) -> Any:
    """Positive-class probabilities for a 1+-row feature frame."""
    return estimator.predict_proba(X)[:, 1]


def predict_proba_all(bundle: dict[str, Any], X) -> dict[str, Any]:
    """{head: probability-array} for every head the bundle actually trained."""
    return {head: _proba(h["estimator"], X) for head, h in bundle["heads"].items()}


def attribute(bundle: dict[str, Any], x_row, head: str = "downgrade", top_n: int = 5) -> list[dict]:
    """
    Top-N signed baseline-difference drivers for one row's `head` probability.

    x_row is a 1-row DataFrame in FEATURE_COLUMNS order. Returns drivers sorted by
    |contribution|, each {feature, value, baseline, contribution, direction}.
    """
    if head not in bundle["heads"]:
        return []
    import pandas as pd

    est = bundle["heads"][head]["estimator"]
    medians = bundle["baseline_medians"]
    feats = bundle["feature_columns"]

    x = x_row.reindex(columns=feats).copy()
    p_actual = float(_proba(est, x)[0])

    drivers: list[dict] = []
    for col in feats:
        x2 = x.copy()
        x2.iloc[0, x2.columns.get_loc(col)] = medians.get(col, 0.0)
        delta = p_actual - float(_proba(est, x2)[0])
        if abs(delta) < 1e-4:
            continue
        raw_val = x.iloc[0][col]
        drivers.append({
            "feature": col,
            "value": None if pd.isna(raw_val) else round(float(raw_val), 4),
            "baseline": round(float(medians.get(col, 0.0)), 4),
            "contribution": round(delta, 4),     # + = raised downgrade prob
            "direction": "raises" if delta > 0 else "lowers",
        })
    drivers.sort(key=lambda d: abs(d["contribution"]), reverse=True)
    return drivers[:top_n]


def predict_rows(bundle: dict[str, Any], df, *, top_n: int = 5) -> list[dict]:
    """
    Predict for a feature matrix, one output row per (cik, period_end). When a
    period has multiple agency rows, the probabilities are averaged and the drivers
    taken from the representative (first) row.
    """
    import logging
    import numpy as np
    import pandas as pd

    log = logging.getLogger("model.predict")
    version = bundle.get("version", "")
    out: list[dict] = []
    for (cik, period_end), grp in df.groupby(["cik", "period_end"], sort=False):
        X = grp.reindex(columns=FEATURE_COLUMNS).apply(pd.to_numeric, errors="coerce")
        probs = predict_proba_all(bundle, X)
        row = {
            "cik": cik,
            "period_end": period_end,
            "horizon_months": HORIZON_MONTHS,
            "p_downgrade": float(np.mean(probs["downgrade"])) if "downgrade" in probs else None,
            "p_upgrade": float(np.mean(probs["upgrade"])) if "upgrade" in probs else None,
            "p_distress": float(np.mean(probs["distress"])) if "distress" in probs else None,
            "drivers_json": attribute(bundle, X.iloc[[0]], "downgrade", top_n),
            "model_version": version,
        }
        out.append(row)
        if len(out) % 2000 == 0:
            log.info("  scored %d issuer-periods...", len(out))
    log.info("  scored %d issuer-periods", len(out))
    return out


def predict_and_store(bundle: dict[str, Any], df) -> int:
    """Predict for `df` and bulk-write to migration_predictions. Returns row count."""
    from src.store import save_migration_predictions_bulk

    rows = predict_rows(bundle, df)
    save_migration_predictions_bulk(rows)
    return len(rows)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
    cli_log = logging.getLogger("model.predict")

    parser = argparse.ArgumentParser(description="Predict rating migrations + store them")
    parser.add_argument("--model", default="data/migration_model.joblib", help="joblib artifact")
    parser.add_argument("--matrix", default=None, help="feature-matrix CSV (else assemble from Supabase)")
    parser.add_argument("--no-replace", action="store_true",
                        help="upsert without clearing migration_predictions first (keeps stale rows)")
    args = parser.parse_args()

    cli_log.info("Loading model %s...", args.model)
    bundle = load_model(args.model)
    if args.matrix:
        import pandas as pd
        df = pd.read_csv(args.matrix, dtype={"cik": str, "period_end": str, "agency": str})
    else:
        from src.model.features import load_training_matrix
        cli_log.info("Assembling feature matrix from Supabase...")
        df = load_training_matrix()
    cli_log.info("Matrix: %d rows", len(df))

    if not args.no_replace:
        from src.store import clear_migration_predictions
        clear_migration_predictions()
        print("Cleared migration_predictions (replace mode).")

    n = predict_and_store(bundle, df)
    print(f"Wrote {n} migration_predictions with model {bundle.get('version')}")
