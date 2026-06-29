"""
Stage 3 trainer (offline / CLI — never the API hot path).

For each head (downgrade / upgrade / default) it fits, on a walk-forward split:
  - a MONOTONIC histogram gradient booster (sklearn HistGradientBoostingClassifier
    with per-feature monotonic_cst from FEATURE_DIRECTIONS) — the auditability-first
    model: e.g. higher leverage can only RAISE modeled downgrade risk, never lower
    it. (Swap in LightGBM here later with no interface change.)
  - an interpretable LogisticRegression BASELINE (imputed + scaled) — the floor the
    booster must beat, reported side by side.
  - sigmoid (Platt) CALIBRATION on a time-respecting recent holdout, so the reported
    probability means what it says.

Class imbalance is left to the loss + calibration (class_weight defaults to None):
the walk-forward sweep found balancing inflated probabilities without improving
calibration, while sigmoid on a 0.35 holdout calibrates best. Metrics are rare-event
focused (PR-AUC / recall@k / calibration), never bare accuracy.

The fitted bundle is a plain dict of sklearn estimators + the feature list + the
per-feature baseline medians (for attribution) — joblib-serialisable, no custom
classes to unpickle.
"""

from __future__ import annotations

import argparse
from typing import Any

from src.model.dataset import (
    HEADS, HORIZON_MONTHS, make_xy, time_split, recent_holdout,
    monotone_constraints, classification_metrics,
)
from src.model.features import FEATURE_COLUMNS
from src.score import DEFAULT_CONFIG


_VALIDATION_FRACTION = 0.15


def _build_booster(monotone: list[int], random_state: int, *, early_stopping: bool = True,
                   class_weight: str | None = None):
    """
    Histogram gradient booster for one head. `class_weight` controls the rare-class
    handling: "balanced" up-weights rare downgrades/defaults for recall but inflates
    probabilities; None lets the booster learn the true prior (better calibrated).
    The shipped value is chosen by the walk-forward calibration eval (evaluate.py).
    """
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(
        loss="log_loss",
        learning_rate=0.05,
        max_iter=300,
        max_leaf_nodes=31,
        l2_regularization=1.0,
        early_stopping=early_stopping,
        validation_fraction=_VALIDATION_FRACTION if early_stopping else None,
        monotonic_cst=monotone,           # auditability-first: credit-coherent directions
        class_weight=class_weight,        # None: native prior (calibrated); "balanced": recall-first
        random_state=random_state,
    )


def _can_early_stop(y) -> bool:
    """
    HistGradientBoosting's early-stopping holdout is a STRATIFIED split, so the
    minority class must be big enough to populate both folds (sklearn requires ≥2,
    and the 15% validation fold should hold ≥1 positive to be meaningful). For rare
    heads — e.g. `default`, with only a handful of positives — that split raises
    "least populated class has only 1 member"; there we train on the fixed max_iter
    (l2-regularised) without early stopping rather than crash.
    """
    n_min = int(y.value_counts().min())
    return n_min >= 2 and n_min * _VALIDATION_FRACTION >= 1


def _build_baseline():
    from sklearn.pipeline import Pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression

    return Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("lr", LogisticRegression(class_weight="balanced", max_iter=1000, C=1.0)),
    ])


def _calibrate(booster, X_cal, y_cal, *, method: str = "isotonic"):
    """
    Calibrate a prefit booster on the recent holdout; None if not possible.

    `method="sigmoid"` (Platt) is robust to a small, regime-shifted calibration set;
    `method="isotonic"` is non-parametric but overfits a tiny holdout (it produced
    the step artifacts in the pre-recalibration reliability bins).
    """
    from sklearn.calibration import CalibratedClassifierCV

    if X_cal is None or len(y_cal) == 0 or y_cal.nunique() < 2:
        return None
    cal = CalibratedClassifierCV(booster, method=method, cv="prefit")
    cal.fit(X_cal, y_cal)
    return cal


def train_all(
    df,
    split_date: str,
    *,
    calibration_frac: float = 0.35,
    random_state: int = 0,
    version: str = "dev",
    class_weight: str | None = None,
    calib_method: str = "sigmoid",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Train all heads on a walk-forward split and return (bundle, metrics).

    Defaults are the recalibration winner from the walk-forward sweep (evaluate.py):
    class_weight=None + sigmoid (Platt) + a 0.35 calibration holdout. Vs the prior
    balanced+isotonic+0.2 config this cut Brier ~6%, halved the isotonic step
    artifacts, lifted recall@10%/PR-AUC ~18-20%, and reduced out-of-time
    over-prediction (mean predicted / observed base rate) from ~1.81× to ~1.68×.
    The residual is temporal base-rate drift (e.g. the post-2020 downgrade-rate
    drop), which calibration on past data cannot fully anticipate.

    bundle = {version, horizon_months, feature_columns, baseline_medians,
              heads:{head:{estimator, baseline, calibrated}}}. metrics carries the
      out-of-time (test) PR-AUC/recall/calibration for the model AND the baseline.
    A head with a single class in its fit data is recorded as 'insufficient' and skipped.
    """
    import logging
    import numpy as np

    log = logging.getLogger("model.train")
    train_df, test_df = time_split(df, split_date)
    fit_df, cal_df = recent_holdout(train_df, calibration_frac)
    log.info("Training %d heads | split=%s | train=%d test=%d rows",
             len(HEADS), split_date, len(train_df), len(test_df))

    # Per-feature medians (head-independent — X is identical across heads) for attribution.
    X_for_medians, _, _ = make_xy(train_df, "downgrade")
    medians: dict[str, float] = {}
    for col in FEATURE_COLUMNS:
        m = np.nanmedian(X_for_medians[col].to_numpy(dtype=float)) if len(X_for_medians) else np.nan
        medians[col] = float(m) if np.isfinite(m) else 0.0

    bundle: dict[str, Any] = {
        "version": version,
        "horizon_months": HORIZON_MONTHS,
        "feature_columns": list(FEATURE_COLUMNS),
        "baseline_medians": medians,
        "heads": {},
    }
    metrics: dict[str, Any] = {
        "version": version, "split_date": split_date,
        "n_train": int(len(train_df)), "n_test": int(len(test_df)),
        "class_weight": class_weight, "calib_method": calib_method, "heads": {},
    }

    for head in HEADS:
        X_fit, y_fit, _ = make_xy(fit_df, head)
        X_all, y_all, _ = make_xy(train_df, head)   # full train for the baseline
        if y_fit.nunique() < 2:
            metrics["heads"][head] = {"status": "insufficient", "positives": int(y_fit.sum())}
            log.info("  head '%s': insufficient (%d positives) — skipped", head, int(y_fit.sum()))
            continue

        early_stop = _can_early_stop(y_fit)
        log.info("  head '%s': fitting (%d positives, early_stop=%s)...", head, int(y_fit.sum()), early_stop)
        booster = _build_booster(
            monotone_constraints(head), random_state, early_stopping=early_stop,
            class_weight=class_weight,
        ).fit(X_fit, y_fit)
        X_cal, y_cal, _ = make_xy(cal_df, head) if not cal_df.empty else (None, None, [])
        calibrated = _calibrate(booster, X_cal, y_cal, method=calib_method)
        estimator = calibrated if calibrated is not None else booster

        baseline = _build_baseline().fit(X_all, y_all)

        head_metrics: dict[str, Any] = {
            "status": "ok", "calibrated": calibrated is not None, "early_stopping": early_stop,
        }
        X_te, y_te, _ = make_xy(test_df, head)
        if len(y_te) and y_te.nunique() > 0:
            head_metrics["model"] = classification_metrics(y_te, estimator.predict_proba(X_te)[:, 1])
            head_metrics["baseline"] = classification_metrics(y_te, baseline.predict_proba(X_te)[:, 1])

        bundle["heads"][head] = {
            "estimator": estimator,
            "baseline": baseline,
            "calibrated": calibrated is not None,
        }
        metrics["heads"][head] = head_metrics
        log.info("  head '%s': done (calibrated=%s)", head, calibrated is not None)

    return bundle, metrics


def save_model(bundle: dict[str, Any], path: str) -> str:
    """Persist the fitted bundle to disk via joblib. Returns the path."""
    import joblib
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path)
    return path


# ── Model-learned stress-score weights ───────────────────────────────────────

# Each of the 8 deterministic stress-score rules ↔ the model feature it measures.
# (current_ratio retired in Part 0.)
RULE_TO_FEATURE: dict[str, str] = {
    "profitability": "ebitda_margin",
    "leverage>5x": "leverage",
    "coverage<2x": "interest_coverage",
    "cash_flow_to_debt<30%": "cash_flow_to_debt",
    "fcf_negative": "fcf_margin",
    "liquidity<1x": "liquidity",
    "debt_to_assets>40%": "debt_to_assets",
    "maturity_wall": "maturity_near_term_pct",
}


def derive_score_config(bundle: dict[str, Any], base: dict | None = None) -> dict:
    """
    Turn the model's learned importances into the deterministic stress-score WEIGHTS.

    Uses the downgrade head's logistic baseline (standardized coefficients, so they
    are comparable across features). Each rule's weight = normalized |coefficient|
    of its mapped feature, scaled so the 8 weights sum to the same total as the
    DEFAULT config (the ramps/caps/escalation are kept). Falls back to `base`
    unchanged when the downgrade head or its baseline is unavailable.
    """
    import copy
    base = copy.deepcopy(base if base is not None else DEFAULT_CONFIG)

    head = bundle.get("heads", {}).get("downgrade")
    if not head or "baseline" not in head:
        return base
    try:
        pipe = head["baseline"]
        coef = pipe.named_steps["lr"].coef_[0]
        # The LogisticRegression coefficients align to the imputer's OUTPUT columns,
        # not the raw FEATURE_COLUMNS — the imputer drops all-NaN features. Map each
        # coefficient back to its feature name so the rule→feature lookup is correct.
        kept = list(pipe.named_steps["impute"].get_feature_names_out())
        coef_by_feature = {name: float(c) for name, c in zip(kept, coef)}
    except Exception:
        return base

    raw = {rule: abs(coef_by_feature.get(feat, 0.0)) for rule, feat in RULE_TO_FEATURE.items()}
    total_raw = sum(raw.values())
    target_total = sum(base["rules"][r]["weight"] for r in RULE_TO_FEATURE if r in base["rules"])
    if total_raw <= 0 or target_total <= 0:
        return base  # degenerate (all-zero coefficients) → keep defaults

    for rule, w in raw.items():
        if rule in base["rules"]:
            base["rules"][rule]["weight"] = round(w / total_raw * target_total, 1)
    return base


# ── Walk-forward vintages (background training; no-leakage backtest) ──────────

def train_vintages(
    df,
    cutoffs: list[str],
    *,
    out_dir: str = "data/model_vintages",
    random_state: int = 0,
) -> list[dict[str, Any]]:
    """
    Train one model per cutoff date (each on data whose label window closes ≤ cutoff)
    and persist it to `out_dir/<cutoff>.joblib`. Returns
    [{cutoff, path, metrics}, …] sorted ascending — the newest is the active model.
    These vintages let the migration backtest score a snapshot at date T with a model
    that only saw data before T (no leakage).
    """
    out: list[dict[str, Any]] = []
    for cutoff in sorted(cutoffs):
        bundle, metrics = train_all(df, cutoff, random_state=random_state, version=cutoff)
        path = save_model(bundle, f"{out_dir}/{cutoff}.joblib")
        out.append({"cutoff": cutoff, "path": path, "metrics": metrics})
    return out


def select_vintage(vintages: list[dict[str, Any]], as_of: str) -> str | None:
    """Path of the latest vintage whose cutoff is strictly before `as_of` (else None)."""
    eligible = [v for v in vintages if v["cutoff"] < as_of]
    return max(eligible, key=lambda v: v["cutoff"])["path"] if eligible else None


# ── CLI ───────────────────────────────────────────────────────────────────────

DEFAULT_ARTIFACT = "data/migration_model.joblib"


def _load_matrix(matrix_csv: str | None):
    """Load the training matrix from a CSV, or assemble it from Supabase."""
    if matrix_csv:
        import pandas as pd
        return pd.read_csv(matrix_csv, dtype={"cik": str, "period_end": str, "agency": str})
    from src.model.features import load_training_matrix
    return load_training_matrix()


if __name__ == "__main__":
    import json
    import logging
    from datetime import datetime, timezone

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
    cli_log = logging.getLogger("model.train")

    parser = argparse.ArgumentParser(description="Train the rating-migration model")
    parser.add_argument("--split-date", required=True, help="walk-forward cutoff, YYYY-MM-DD")
    parser.add_argument("--matrix", default=None, help="optional training-matrix CSV (else from Supabase)")
    parser.add_argument("--out", default=DEFAULT_ARTIFACT, help="joblib artifact path")
    parser.add_argument("--no-registry", action="store_true", help="skip writing model_registry")
    parser.add_argument("--no-score-config", action="store_true",
                        help="skip persisting the model-learned stress-score weights")
    parser.add_argument("--class-weight", choices=["balanced", "none"], default=None,
                        help="rare-class weighting (default: train_all's default)")
    parser.add_argument("--calib-method", choices=["isotonic", "sigmoid"], default=None,
                        help="probability calibration method (default: train_all's default)")
    parser.add_argument("--calibration-frac", type=float, default=None,
                        help="recent-holdout fraction for calibration (default: train_all's default)")
    args = parser.parse_args()

    cli_log.info("Loading training matrix%s...", "" if args.matrix is None else f" from {args.matrix}")
    df = _load_matrix(args.matrix)
    cli_log.info("Matrix: %d rows / %d issuers", len(df), df["cik"].nunique() if len(df) else 0)
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # CLI overrides for the calibration knobs; omitted flags fall back to train_all's defaults.
    overrides: dict[str, Any] = {}
    if args.class_weight is not None:
        overrides["class_weight"] = None if args.class_weight == "none" else args.class_weight
    if args.calib_method is not None:
        overrides["calib_method"] = args.calib_method
    if args.calibration_frac is not None:
        overrides["calibration_frac"] = args.calibration_frac
    bundle, metrics = train_all(df, args.split_date, version=version, **overrides)
    path = save_model(bundle, args.out)
    print(json.dumps(metrics, indent=2))
    print(f"\nSaved model {version} → {path}")

    if not args.no_registry:
        try:
            from src.store import save_model_registry
            save_model_registry(
                version=version, artifact_path=path,
                feature_list=bundle["feature_columns"],
                train_window={"split_date": args.split_date,
                              "n_train": metrics["n_train"], "n_test": metrics["n_test"]},
                metrics=metrics,
            )
            print("Registry updated (model_registry id='active').")
        except Exception as e:
            print(f"[registry update skipped: {e}]")

    if not args.no_score_config:
        try:
            from src.store import save_score_config
            learned = derive_score_config(bundle)
            save_score_config(learned)
            weights = {r: learned["rules"][r]["weight"] for r in RULE_TO_FEATURE if r in learned["rules"]}
            print(f"Learned stress-score weights persisted: {weights}")
        except Exception as e:
            print(f"[score-config update skipped: {e}]")
