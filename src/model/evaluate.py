"""
Stage 3 walk-forward backtest of the rating-migration model.

For each cutoff in a sequence of split dates it trains on the past and evaluates
out-of-time on the future (never random K-fold — that leaks the future), then
aggregates. Reports are rare-event focused: PR-AUC, recall@k, calibration, and a
confusion matrix by starting rating bucket (IG vs HY) — plus the LogisticRegression
baseline alongside, so the booster's lift is explicit.

This is the model analogue of src/backtest.py's point-in-time discipline: the
no-look-ahead guarantee lives in dataset.time_split (train window must CLOSE before
the cutoff). Results are written into a `migration` block for the backtest UI.
"""

from __future__ import annotations

from typing import Any

from src.model.dataset import HEADS, make_xy, time_split
from src.model.train import train_all
from src.rating import is_investment_grade, RATING_SCALE


def tune_threshold(y_true, p, *, min_threshold: float = 0.02) -> float | None:
    """
    Pick the probability cutoff that maximizes F1 on these out-of-time predictions.

    The calibrated heads emit probabilities that cluster well below 0.5 on rare
    events, so a fixed 0.5 flags almost nothing. Tuning per head on the pooled
    walk-forward test predictions restores a usable operating point (and lets the
    UI show the actual cutoff). Returns None when there are no positives — the
    caller then falls back to a default threshold.
    """
    import numpy as np
    from sklearn.metrics import precision_recall_curve

    y = np.asarray(y_true, dtype=int)
    p = np.asarray(p, dtype=float)
    if len(y) == 0 or int(y.sum()) == 0:
        return None
    prec, rec, thr = precision_recall_curve(y, p)
    if len(thr) == 0:
        return None
    # precision_recall_curve returns prec/rec of length len(thr)+1; drop the last
    # (the point with no positives predicted) to align with thr.
    prec, rec = prec[:-1], rec[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        f1 = np.where((prec + rec) > 0, 2 * prec * rec / (prec + rec), 0.0)
    best = int(np.argmax(f1))
    return round(float(max(thr[best], min_threshold)), 4)


def confusion_by_bucket(test_df, p_downgrade, threshold: float = 0.5) -> dict[str, dict]:
    """
    Confusion of the downgrade head split by the issuer's starting grade (IG/HY).
    A prediction is positive when p_downgrade ≥ threshold; truth is label_12m == +1.
    """
    import numpy as np
    import pandas as pd

    usable = test_df[test_df["label_12m"].notna()].reset_index(drop=True)
    p = np.asarray(p_downgrade, dtype=float)

    # Bucket by the issuer's starting rating: the agency rating as of period_end when
    # present (the thing being predicted to migrate), else the implied rating.
    if "agency_rating_index" in usable.columns and usable["agency_rating_index"].notna().any():
        bucket_col = "agency_rating_index"
    elif "implied_rating_index" in usable.columns:
        bucket_col = "implied_rating_index"
    else:
        return {}

    def _bucket(idx) -> str | None:
        if idx is None or pd.isna(idx):
            return None
        return "IG" if is_investment_grade(RATING_SCALE[int(idx)]) else "HY"

    buckets = usable[bucket_col].map(_bucket)
    out: dict[str, dict] = {}
    for bucket in ("IG", "HY"):
        mask = (buckets == bucket).to_numpy()
        if not mask.any():
            out[bucket] = {"n": 0}
            continue
        y = (usable["label_12m"] == 1).to_numpy()[mask]
        pred = p[mask] >= threshold
        out[bucket] = {
            "n": int(mask.sum()),
            "tp": int((pred & y).sum()), "fp": int((pred & ~y).sum()),
            "tn": int((~pred & ~y).sum()), "fn": int((~pred & y).sum()),
            "actual_downgrade_rate": round(float(y.mean()), 4),
        }
    return out


def tune_vintage_thresholds(df, vintages: list[dict], *, downgrade_operating_point: float = 0.14) -> dict[str, float]:
    """
    Per-head flag cutoffs tuned on the VINTAGE models — the models the migration
    backtest actually scores with — over the labeled matrix, walk-forward (each row
    scored by the vintage trained strictly before its period_end, via select_vintage).

    This fixes a silent distribution mismatch: the shipped thresholds were tuned on the
    EVAL-split models (walk_forward_eval), but the backtest scores with the data/
    model_vintages/*.joblib vintages, whose probability scale differs — the distress
    cutoff in particular sat ABOVE the vintages' achievable max (≈0.10), guaranteeing
    0% default catch. Tuning on the vintages puts every head's cutoff in range.

    upgrade / distress: max-F1. downgrade: an explicit operating point (default 0.14).
    The downgrade head's precision is ~flat (≈ base rate) across the usable threshold
    range, so max-F1 there only maximizes alert VOLUME (≈60% control false-positive);
    the operating point instead trades catch vs false-alarm rate and sets the issuer-
    page risk band — a product parameter, not a fit. See the backtest catch/FP frontier.
    """
    import numpy as np
    import pandas as pd
    from src.model.train import select_vintage
    from src.model.predict import load_model, predict_proba_all
    from src.model.features import FEATURE_COLUMNS

    obs = df[df["label_12m"].notna()]
    cache: dict[str, Any] = {}
    pooled_y: dict[str, list[int]] = {h: [] for h in HEADS}
    pooled_p: dict[str, list[float]] = {h: [] for h in HEADS}
    for period, grp in obs.groupby("period_end"):
        path = select_vintage(vintages, period)
        if path is None:
            continue
        bundle = cache.get(path) or cache.setdefault(path, load_model(path))
        X = grp.reindex(columns=FEATURE_COLUMNS).apply(pd.to_numeric, errors="coerce")
        probs = predict_proba_all(bundle, X)
        for head in HEADS:
            if head not in probs:
                continue
            is_pos = HEADS[head]["positive"]
            y = grp.apply(lambda r: 1 if is_pos(r) else 0, axis=1).to_numpy()
            pooled_y[head].extend(int(v) for v in y)
            pooled_p[head].extend(float(v) for v in np.asarray(probs[head]))

    out: dict[str, float] = {}
    for head in HEADS:
        if head == "downgrade" and downgrade_operating_point is not None:
            out[head] = round(float(downgrade_operating_point), 4)
            continue
        thr = tune_threshold(pooled_y[head], pooled_p[head])
        if thr is not None:
            out[head] = thr
    return out


def walk_forward_eval(df, split_dates: list[str], **train_kwargs) -> dict[str, Any]:
    """
    Train+evaluate across multiple walk-forward cutoffs; return per-split metrics,
    an aggregate (mean test PR-AUC / recall per head, model vs. baseline), and the
    IG/HY confusion at the final split.
    """
    import numpy as np
    from src.model.predict import predict_proba_all

    per_split: list[dict] = []
    pr_auc_acc: dict[str, list[float]] = {h: [] for h in HEADS}
    base_pr_acc: dict[str, list[float]] = {h: [] for h in HEADS}
    # Pooled out-of-time (truth, prob) per head, for threshold tuning + the no-skill
    # base rate. Pooling across splits gives a more stable operating point than any
    # single fold.
    pooled_y: dict[str, list[int]] = {h: [] for h in HEADS}
    pooled_p: dict[str, list[float]] = {h: [] for h in HEADS}

    last_bundle = None
    last_test_df = None
    for split_date in split_dates:
        bundle, metrics = train_all(df, split_date, **train_kwargs)
        per_split.append(metrics)
        test_df = time_split(df, split_date)[1]
        last_bundle, last_test_df = bundle, test_df
        for head, hm in metrics["heads"].items():
            m = hm.get("model") or {}
            b = hm.get("baseline") or {}
            if m.get("pr_auc") is not None:
                pr_auc_acc[head].append(m["pr_auc"])
            if b.get("pr_auc") is not None:
                base_pr_acc[head].append(b["pr_auc"])
        # Pool this split's out-of-time predictions per head (skip heads the bundle
        # couldn't train, e.g. a fold with no positives).
        for head in HEADS:
            if head not in bundle["heads"]:
                continue
            X_te, y_te, _ = make_xy(test_df, head)
            if not len(X_te):
                continue
            p = predict_proba_all(bundle, X_te).get(head)
            if p is None:
                continue
            pooled_y[head].extend(int(v) for v in np.asarray(y_te))
            pooled_p[head].extend(float(v) for v in np.asarray(p))

    def _mean(xs: list[float]) -> float | None:
        return round(float(np.mean(xs)), 4) if xs else None

    # Per-head tuned flag threshold (max-F1 on pooled OOT preds) and no-skill floor.
    thresholds: dict[str, float] = {}
    for head in HEADS:
        thr = tune_threshold(pooled_y[head], pooled_p[head])
        if thr is not None:
            thresholds[head] = thr

    aggregate = {
        head: {
            "mean_pr_auc_model": _mean(pr_auc_acc[head]),
            "mean_pr_auc_baseline": _mean(base_pr_acc[head]),
            "mean_base_rate": _mean([float(v) for v in pooled_y[head]]),
            "n_splits_scored": len(pr_auc_acc[head]),
        }
        for head in HEADS
    }

    confusion: dict[str, dict] = {}
    if last_bundle is not None and last_test_df is not None and "downgrade" in last_bundle["heads"]:
        X_te, _, _ = make_xy(last_test_df, "downgrade")
        if len(X_te):
            p = predict_proba_all(last_bundle, X_te)["downgrade"]
            # Use the tuned downgrade cutoff so the confusion matrix reflects the
            # operating point the backtest actually flags at (not a dead 0.5).
            confusion = confusion_by_bucket(last_test_df, p, threshold=thresholds.get("downgrade", 0.5))

    return {
        "split_dates": split_dates,
        "per_split": per_split,
        "aggregate": aggregate,
        "thresholds": thresholds,
        "confusion_by_bucket_final": confusion,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Walk-forward backtest the migration model")
    parser.add_argument("--splits", required=True, help="comma-separated cutoff dates, e.g. 2018-12-31,2020-12-31,2022-12-31")
    parser.add_argument("--matrix", default=None, help="training-matrix CSV (else from Supabase)")
    parser.add_argument("--out", default="data/migration_eval.json")
    # Calibration knobs (passed straight through to train_all) for the recalibration sweep.
    parser.add_argument("--class-weight", choices=["balanced", "none"], default=None,
                        help="rare-class weighting; 'none' lets the booster learn the true prior")
    parser.add_argument("--calib-method", choices=["isotonic", "sigmoid"], default=None,
                        help="probability calibration method")
    parser.add_argument("--calibration-frac", type=float, default=None,
                        help="recent-holdout fraction for calibration")
    parser.add_argument("--vintages-dir", default="data/model_vintages",
                        help="walk-forward vintages the backtest scores with; thresholds "
                             "are tuned on THESE (not the eval-split models) when present")
    parser.add_argument("--downgrade-op", type=float, default=0.14,
                        help="downgrade-head operating point (balanced catch/false-alarm); "
                             "its precision is ~flat so this is a product choice, not a fit")
    args = parser.parse_args()

    if args.matrix:
        import pandas as pd
        df = pd.read_csv(args.matrix, dtype={"cik": str, "period_end": str, "agency": str})
    else:
        from src.model.features import load_training_matrix
        df = load_training_matrix()

    train_kwargs: dict[str, Any] = {}
    if args.class_weight is not None:
        train_kwargs["class_weight"] = None if args.class_weight == "none" else args.class_weight
    if args.calib_method is not None:
        train_kwargs["calib_method"] = args.calib_method
    if args.calibration_frac is not None:
        train_kwargs["calibration_frac"] = args.calibration_frac

    result = walk_forward_eval(df, [s.strip() for s in args.splits.split(",")], **train_kwargs)

    # Prefer thresholds tuned on the VINTAGE models the backtest actually scores with
    # (data/model_vintages/). The eval-split thresholds are kept for reference — the
    # mismatch between them is exactly what produced the silent 0% default catch.
    from pathlib import Path
    vdir = Path(args.vintages_dir)
    vintages = sorted(
        ({"cutoff": p.stem, "path": str(p)} for p in vdir.glob("*.joblib")),
        key=lambda v: v["cutoff"],
    ) if vdir.exists() else []
    if vintages:
        result["thresholds_eval_models"] = result["thresholds"]
        result["thresholds"] = tune_vintage_thresholds(df, vintages, downgrade_operating_point=args.downgrade_op)
        print(f"Thresholds tuned on {len(vintages)} vintages: {result['thresholds']}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"migration": result}, indent=2))
    print(json.dumps(result["aggregate"], indent=2))
    print(f"\nFull report → {args.out}")
