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

from src.model.dataset import HEADS, make_xy, time_split, calibration_bins
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


def catch_fp_frontier(
    y_true, p, *,
    recall_targets: tuple[float, ...] = (0.70, 0.80, 0.90),
    fpr_targets: tuple[float, ...] = (0.05, 0.10, 0.20, 0.30),
) -> dict[str, Any] | None:
    """
    The catch-rate vs false-positive frontier for ONE head, from its pooled
    out-of-time (truth, prob) predictions — the honest answer to "what does catching
    X% of events cost in false alarms?".

    catch = recall = TPR (share of issuer-periods that DID migrate which we flag);
    false-positive rate = FPR (share of non-migrating issuer-periods we wrongly flag).
    Both are read off the ROC curve. Returns, per head:
      - at_recall: for each target catch (70/80/90%), the lowest threshold reaching it
        and the FPR + precision it costs;
      - at_fpr: for each false-positive budget, the best catch achievable and its threshold.
    None when a single class (no curve). Note: this is issuer-period recall; the
    event-level backtest catch (multiple snapshots per case, with lead time) runs
    somewhat higher — use this to choose the operating point, the backtest to confirm
    catch + lead at it.
    """
    import numpy as np
    from sklearn.metrics import roc_curve

    y = np.asarray(y_true, dtype=int)
    p = np.asarray(p, dtype=float)
    if len(y) == 0 or int(y.sum()) == 0 or int(y.sum()) == len(y):
        return None
    fpr, tpr, thr = roc_curve(y, p)          # all monotonic increasing in index
    pos, neg = int(y.sum()), int(len(y) - y.sum())

    def _prec(tp_rate: float, fp_rate: float) -> float | None:
        tp, fp = tp_rate * pos, fp_rate * neg
        return round(tp / (tp + fp), 4) if (tp + fp) > 0 else None

    at_recall: dict[str, Any] = {}
    for r in recall_targets:
        i = int(np.argmax(tpr >= r))         # first index reaching catch r
        at_recall[f"{int(r*100)}"] = {
            "catch": round(float(tpr[i]), 4), "fpr": round(float(fpr[i]), 4),
            "threshold": round(float(thr[i]), 4) if np.isfinite(thr[i]) else None,
            "precision": _prec(float(tpr[i]), float(fpr[i])),
        }
    at_fpr: dict[str, Any] = {}
    for f in fpr_targets:
        idx = np.where(fpr <= f)[0]
        if len(idx):
            j = int(idx[-1])                 # max catch with FPR within budget
            at_fpr[f"{int(f*100)}"] = {
                "catch": round(float(tpr[j]), 4), "fpr": round(float(fpr[j]), 4),
                "threshold": round(float(thr[j]), 4) if np.isfinite(thr[j]) else None,
                "precision": _prec(float(tpr[j]), float(fpr[j])),
            }
    return {"n": int(len(y)), "n_positive": pos, "base_rate": round(pos / len(y), 4),
            "at_recall": at_recall, "at_fpr": at_fpr}


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


def _ig_hy_bucket(idx) -> str | None:
    """IG (BBB- and up) / HY split of a starting agency rating index, or None if absent."""
    import pandas as pd

    if idx is None or (isinstance(idx, float) and pd.isna(idx)):
        return None
    return "IG" if is_investment_grade(RATING_SCALE[int(idx)]) else "HY"


def _pooled_vintage_issuer_any(df, vintages: list[dict]):
    """
    Pool ISSUER-LEVEL (truth_any, prob_any) per head over the labeled matrix, scored
    walk-forward by the vintage trained strictly before each row's period_end (via
    select_vintage). Within each (cik, period_end) the per-agency vintage probabilities
    are combined by NOISY-OR (1 − ∏(1 − pₐ)) and the truth is "ANY covering agency
    migrated" — the issuer-level "any-agency deterioration" target the product ships.

    Returns (pooled_y, pooled_p, pooled_bucket): pooled_y/pooled_p are {head: [values]}
    at issuer-period grain; pooled_bucket is the IG/HY starting bucket per issuer-period
    (by the preferred agency = lowest agency_code with a known rating), aligned to
    pooled_y["downgrade"] for the confusion breakdown.
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
    pooled_bucket: list[str | None] = []
    for period, grp in obs.groupby("period_end"):
        path = select_vintage(vintages, period)
        if path is None:
            continue
        bundle = cache.get(path) or cache.setdefault(path, load_model(path))
        grp = grp.reset_index(drop=True)
        X = grp.reindex(columns=FEATURE_COLUMNS).apply(pd.to_numeric, errors="coerce")
        probs = predict_proba_all(bundle, X)
        for head in HEADS:
            if head not in probs:
                continue
            is_pos = HEADS[head]["positive"]
            p_head = np.clip(np.asarray(probs[head], dtype=float), 0.0, 1.0)
            y_head = grp.apply(lambda r: 1 if is_pos(r) else 0, axis=1).to_numpy()
            tmp = pd.DataFrame({
                "cik": grp["cik"].to_numpy(), "y": y_head, "p": p_head,
                "code": grp.get("agency_code"), "ar": grp.get("agency_rating_index"),
            })
            for cik, sub in tmp.groupby("cik", sort=False):
                pooled_y[head].append(int(sub["y"].max()))
                pooled_p[head].append(float(1.0 - np.prod(1.0 - sub["p"].to_numpy())))
                if head == "downgrade":
                    rated = sub[sub["ar"].notna()].sort_values("code")
                    pooled_bucket.append(_ig_hy_bucket(rated["ar"].iloc[0]) if len(rated) else None)
    return pooled_y, pooled_p, pooled_bucket


def _tune_from_pooled(pooled_y, pooled_p, *, downgrade_operating_point: float = 0.14) -> dict[str, float]:
    """Per-head cutoffs from pooled issuer-level (y, p): upgrade/distress max-F1;
    downgrade an explicit product operating point (its precision is ~flat, so max-F1
    there only maximizes alert volume)."""
    out: dict[str, float] = {}
    for head in HEADS:
        if head == "downgrade" and downgrade_operating_point is not None:
            out[head] = round(float(downgrade_operating_point), 4)
            continue
        thr = tune_threshold(pooled_y[head], pooled_p[head])
        if thr is not None:
            out[head] = thr
    return out


def _diag_from_pooled(pooled_y, pooled_p, pooled_bucket, thresholds) -> dict[str, Any]:
    """
    Issuer-level diagnostics for the shipped ("any-agency") signal: per-head base rate,
    reliability bins (mean noisy-OR p vs observed rate — the noisy-OR calibration check),
    the calibration ratio (mean p / base rate; >~1.5 flags noisy-OR overstatement → the
    combiner should fall back to max in predict._noisy_or), the catch/FP frontier, and
    the downgrade head's IG/HY confusion at its shipped cutoff.
    """
    import numpy as np

    diag: dict[str, Any] = {"base_rate": {}, "calibration": {}, "calibration_ratio": {},
                            "frontier": {}, "confusion_by_bucket": {}}
    for head in HEADS:
        y, p = pooled_y[head], pooled_p[head]
        if not y:
            continue
        base = float(np.mean(y))
        mean_p = float(np.mean(p)) if p else 0.0
        diag["base_rate"][head] = round(base, 4)
        diag["calibration"][head] = calibration_bins(y, p)
        diag["calibration_ratio"][head] = round(mean_p / base, 3) if base > 0 else None
        diag["frontier"][head] = catch_fp_frontier(y, p)

    thr = thresholds.get("downgrade")
    yd, pd_ = pooled_y.get("downgrade", []), pooled_p.get("downgrade", [])
    if thr is not None and yd:
        y = np.asarray(yd, dtype=int)
        pred = np.asarray(pd_, dtype=float) >= thr
        buckets = np.asarray(pooled_bucket, dtype=object)
        for b in ("IG", "HY"):
            mask = buckets == b
            if not mask.any():
                diag["confusion_by_bucket"][b] = {"n": 0}
                continue
            yb, prb = y[mask], pred[mask]
            diag["confusion_by_bucket"][b] = {
                "n": int(mask.sum()),
                "tp": int((prb & (yb == 1)).sum()), "fp": int((prb & (yb == 0)).sum()),
                "tn": int((~prb & (yb == 0)).sum()), "fn": int((~prb & (yb == 1)).sum()),
                "actual_downgrade_rate": round(float((yb == 1).mean()), 4),
            }
    return diag


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

    # Catch-rate vs false-positive frontier per head (pooled OOT) — the operating-point
    # chooser: "to catch X% of events, you need threshold T at FPR F%".
    frontier = {head: catch_fp_frontier(pooled_y[head], pooled_p[head]) for head in HEADS}

    return {
        "split_dates": split_dates,
        "per_split": per_split,
        "aggregate": aggregate,
        "thresholds": thresholds,
        "frontier": frontier,
        "confusion_by_bucket_final": confusion,
    }


# ── Per-head hyperparameter / constraint sweep ──────────────────────────────────

# A modest grid — each entry is a full 3-split walk-forward, so keep it small. Each
# `kwargs` is passed straight to walk_forward_eval → train_all. The sweep reports mean
# PR-AUC PER HEAD so the winning config can differ per head (e.g. a balanced distress
# head). It does NOT auto-apply: fold the winners into train_all's defaults/overrides.
SWEEP_GRID = [
    {"label": "baseline", "kwargs": {}},
    {"label": "lr0.03_iter500", "kwargs": {"booster_params": {"learning_rate": 0.03, "max_iter": 500}}},
    {"label": "leaves63", "kwargs": {"booster_params": {"max_leaf_nodes": 63}}},
    {"label": "l2_0.1", "kwargs": {"booster_params": {"l2_regularization": 0.1}}},
    {"label": "leaf50", "kwargs": {"booster_params": {"min_samples_leaf": 50}}},
    {"label": "relax", "kwargs": {"relax_secondary_constraints": True}},
    {"label": "relax_leaves63", "kwargs": {"relax_secondary_constraints": True,
                                           "booster_params": {"max_leaf_nodes": 63}}},
    {"label": "distress_balanced", "kwargs": {"head_overrides": {"distress": {"class_weight": "balanced"}}}},
    {"label": "relax_distress_balanced", "kwargs": {"relax_secondary_constraints": True,
                                                    "head_overrides": {"distress": {"class_weight": "balanced"}}}},
]


def run_sweep(df, split_dates: list[str], grid: list[dict] | None = None) -> dict[str, Any]:
    """
    Run each config in `grid` through the full walk-forward and collect mean test
    PR-AUC per head. Returns {results:[{label, kwargs, pr_auc:{head:val}}...],
    best:{head:{label, pr_auc}}}. Pure measurement — nothing is applied.
    """
    grid = grid or SWEEP_GRID
    results: list[dict[str, Any]] = []
    for i, combo in enumerate(grid, 1):
        print(f"[sweep {i}/{len(grid)}] {combo['label']} …")
        res = walk_forward_eval(df, split_dates, **combo["kwargs"])
        agg = res["aggregate"]
        results.append({
            "label": combo["label"],
            "kwargs": combo["kwargs"],
            "pr_auc": {h: agg[h].get("mean_pr_auc_model") for h in agg},
        })

    heads = list(results[0]["pr_auc"]) if results else []
    best = {}
    for h in heads:
        scored = [(r["label"], r["pr_auc"].get(h)) for r in results if r["pr_auc"].get(h) is not None]
        if scored:
            label, val = max(scored, key=lambda t: t[1])
            best[h] = {"label": label, "pr_auc": val}
    return {"results": results, "best": best, "heads": heads}


def print_sweep(sweep: dict[str, Any]) -> None:
    heads = sweep["heads"]
    fmt = lambda x: f"{x:.4f}" if isinstance(x, (int, float)) else "—"
    print("\nPer-head sweep — mean PR-AUC (higher is better):")
    print(f"  {'config':<26}" + "".join(f"{h:>12}" for h in heads))
    for r in sweep["results"]:
        print(f"  {r['label']:<26}" + "".join(f"{fmt(r['pr_auc'].get(h)):>12}" for h in heads))
    print("\nBest config per head:")
    for h in heads:
        b = sweep["best"].get(h)
        print(f"  {h:<20}{b['label'] if b else '—':<26}{fmt(b['pr_auc']) if b else '—'}")


def print_frontier(frontier: dict) -> None:
    """Print the per-head catch-vs-false-positive frontier: the cost of each catch
    target, and the catch achievable at each false-positive budget."""
    labels = {"downgrade": "Downgrade", "upgrade": "Upgrade", "distress": "Distress (default)"}
    pct = lambda x: f"{x*100:.0f}%" if isinstance(x, (int, float)) else "—"
    items = [(h, f) for h, f in frontier.items() if f]
    if not items:
        return
    print("\nCatch-rate vs false-positive frontier (pooled out-of-time, per head):")
    print("  False-positive rate you must accept to CATCH this share of events:")
    print(f"    {'Head':<20}{'catch 70%':>12}{'catch 80%':>12}{'catch 90%':>12}")
    for h, f in items:
        cells = [(f"FPR {pct(f['at_recall'][r]['fpr'])}" if f["at_recall"].get(r) else "—")
                 for r in ("70", "80", "90")]
        print(f"    {labels.get(h, h.title()):<20}" + "".join(f"{c:>12}" for c in cells))
    print("  Catch you get at a fixed false-positive budget:")
    print(f"    {'Head':<20}{'FPR 5%':>10}{'FPR 10%':>10}{'FPR 20%':>10}{'FPR 30%':>10}")
    for h, f in items:
        cells = [(pct(f["at_fpr"][fp]["catch"]) if f["at_fpr"].get(fp) else "—")
                 for fp in ("5", "10", "20", "30")]
        print(f"    {labels.get(h, h.title()):<20}" + "".join(f"{c:>10}" for c in cells))


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
    parser.add_argument("--sweep", action="store_true",
                        help="run the per-head hyperparameter/constraint grid and report the "
                             "best config per head (does not retrain or write the scorecard)")
    parser.add_argument("--sweep-out", default="data/migration_sweep.json")
    args = parser.parse_args()

    if args.matrix:
        import pandas as pd
        df = pd.read_csv(args.matrix, dtype={"cik": str, "period_end": str, "agency": str})
    else:
        from src.model.features import load_training_matrix
        df = load_training_matrix()

    split_dates = [s.strip() for s in args.splits.split(",")]

    if args.sweep:
        from pathlib import Path
        sweep = run_sweep(df, split_dates)
        Path(args.sweep_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.sweep_out).write_text(json.dumps(sweep, indent=2))
        print_sweep(sweep)
        print(f"\nSweep results → {args.sweep_out}")
        raise SystemExit(0)

    train_kwargs: dict[str, Any] = {}
    if args.class_weight is not None:
        train_kwargs["class_weight"] = None if args.class_weight == "none" else args.class_weight
    if args.calib_method is not None:
        train_kwargs["calib_method"] = args.calib_method
    if args.calibration_frac is not None:
        train_kwargs["calibration_frac"] = args.calibration_frac

    result = walk_forward_eval(df, split_dates, **train_kwargs)

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
        # Score the vintages once at ISSUER level (any-agency, noisy-OR) and derive both
        # the shipped thresholds and the issuer-level diagnostics from the same pooling.
        pooled_y, pooled_p, pooled_bucket = _pooled_vintage_issuer_any(df, vintages)
        result["thresholds_eval_models"] = result["thresholds"]
        result["thresholds"] = _tune_from_pooled(pooled_y, pooled_p,
                                                  downgrade_operating_point=args.downgrade_op)
        result["vintage_diagnostics"] = _diag_from_pooled(
            pooled_y, pooled_p, pooled_bucket, result["thresholds"])
        print(f"Thresholds tuned on {len(vintages)} vintages (issuer-level any-agency): "
              f"{result['thresholds']}")
        # Noisy-OR calibration check: mean issuer-level p vs observed "any" rate. A ratio
        # far above 1 means the independence assumption overstates — consider max().
        cr = result["vintage_diagnostics"]["calibration_ratio"]
        print("Issuer-level any-agency calibration (mean p / base rate; ~1 = calibrated):")
        for head in HEADS:
            if cr.get(head) is not None:
                flag = "  ⚠ inflated — consider max() combiner" if cr[head] > 1.5 else ""
                print(f"  {head:<10} ratio={cr[head]:.2f}"
                      f"  base={result['vintage_diagnostics']['base_rate'].get(head)}{flag}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"migration": result}, indent=2))

    # Modeler-facing scorecard — this is the view that used to live on the /backtest
    # page; it's CLI-only now. Mean PR-AUC per head, model vs. logistic baseline,
    # judged against the no-skill floor (the event's base rate). Higher is better.
    # The full machine-readable report is the JSON written above.
    agg = result["aggregate"]
    labels = {"downgrade": "Downgrade", "upgrade": "Upgrade", "distress": "Distress (default)"}
    n_splits = max((agg[h].get("n_splits_scored") or 0 for h in agg), default=0)
    fmt = lambda x: f"{x:.4f}" if isinstance(x, (int, float)) else "—"
    print(f"\nWalk-forward accuracy (out-of-time) — mean PR-AUC across {n_splits} splits:")
    print(f"  {'Head':<20}{'PR-AUC':>9}{'baseline':>11}{'no-skill':>11}{'splits':>8}")
    for head in agg:
        a = agg[head]
        print(f"  {labels.get(head, head.title()):<20}{fmt(a.get('mean_pr_auc_model')):>9}"
              f"{fmt(a.get('mean_pr_auc_baseline')):>11}{fmt(a.get('mean_base_rate')):>11}"
              f"{(a.get('n_splits_scored') or 0):>8}")
    if result.get("frontier"):
        print_frontier(result["frontier"])
    print(f"\nFull report → {args.out}")
