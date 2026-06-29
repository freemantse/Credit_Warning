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


def _iter_predict_rows(bundle: dict[str, Any], df, *, top_n: int = 5, skip_keys=None):
    """
    Yield one output row per (cik, period_end). When a period has multiple agency rows
    the probabilities are averaged and the drivers taken from the representative (first)
    row. `skip_keys` is a set of (zero-padded cik, period_end) already scored — skipped
    so a re-run can resume a killed pass without re-doing work.
    """
    import numpy as np
    import pandas as pd

    version = bundle.get("version", "")
    skip = skip_keys or set()
    for (cik, period_end), grp in df.groupby(["cik", "period_end"], sort=False):
        if (str(cik).zfill(10), period_end) in skip:
            continue
        X = grp.reindex(columns=FEATURE_COLUMNS).apply(pd.to_numeric, errors="coerce")
        probs = predict_proba_all(bundle, X)
        yield {
            "cik": cik,
            "period_end": period_end,
            "horizon_months": HORIZON_MONTHS,
            "p_downgrade": float(np.mean(probs["downgrade"])) if "downgrade" in probs else None,
            "p_upgrade": float(np.mean(probs["upgrade"])) if "upgrade" in probs else None,
            "p_distress": float(np.mean(probs["distress"])) if "distress" in probs else None,
            "drivers_json": attribute(bundle, X.iloc[[0]], "downgrade", top_n),
            "model_version": version,
        }


def predict_rows(bundle: dict[str, Any], df, *, top_n: int = 5) -> list[dict]:
    """Eager list of every scored row (full pass, no skipping)."""
    return list(_iter_predict_rows(bundle, df, top_n=top_n))


def predict_and_store(bundle: dict[str, Any], df, *, batch_size: int = 2000,
                      resume: bool = True, prune: bool = True) -> int:
    """
    Score `df` and write to migration_predictions in BATCHES as it goes — so an
    interrupted run never empties the table (unlike clear-then-bulk-write-at-end).

    resume=True skips issuer-periods already scored for this model version, so a re-run
    continues where a killed run stopped. prune=True deletes leftover rows from OTHER
    model versions at the END (replace semantics, with no empty-table window). Returns
    the number of rows written this run.
    """
    import logging
    from src.store import (
        save_migration_predictions_bulk, get_predicted_keys,
        prune_migration_predictions_except_version,
    )

    log = logging.getLogger("model.predict")
    version = bundle.get("version", "")

    skip = get_predicted_keys(version) if resume else set()
    if skip:
        log.info("  resume: %d issuer-periods already scored for %s — skipping", len(skip), version)

    batch: list[dict] = []
    n = 0
    for row in _iter_predict_rows(bundle, df, skip_keys=skip):
        batch.append(row)
        if len(batch) >= batch_size:
            save_migration_predictions_bulk(batch)
            n += len(batch)
            batch = []
            log.info("  wrote %d issuer-periods...", n)
    if batch:
        save_migration_predictions_bulk(batch)
        n += len(batch)
    log.info("  wrote %d issuer-periods (this run)", n)

    if prune:
        prune_migration_predictions_except_version(version)
        log.info("  pruned rows from other model versions (replace)")
    return n


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
                        help="keep rows from other model versions (skip the end-of-run prune)")
    parser.add_argument("--no-resume", action="store_true",
                        help="re-score every issuer-period even if already scored for this version")
    parser.add_argument("--batch-size", type=int, default=2000,
                        help="rows per incremental upsert (smaller = more crash-resilient)")
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

    # No up-front clear: predict_and_store batch-upserts (safe if killed) and prunes
    # other-version rows at the END unless --no-replace.
    n = predict_and_store(bundle, df, batch_size=args.batch_size,
                          resume=not args.no_resume, prune=not args.no_replace)
    print(f"Wrote {n} migration_predictions with model {bundle.get('version')}")
