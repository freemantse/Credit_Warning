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

    last_bundle = None
    last_test_df = None
    for split_date in split_dates:
        bundle, metrics = train_all(df, split_date, **train_kwargs)
        per_split.append(metrics)
        last_bundle, last_test_df = bundle, time_split(df, split_date)[1]
        for head, hm in metrics["heads"].items():
            m = hm.get("model") or {}
            b = hm.get("baseline") or {}
            if m.get("pr_auc") is not None:
                pr_auc_acc[head].append(m["pr_auc"])
            if b.get("pr_auc") is not None:
                base_pr_acc[head].append(b["pr_auc"])

    def _mean(xs: list[float]) -> float | None:
        return round(float(np.mean(xs)), 4) if xs else None

    aggregate = {
        head: {
            "mean_pr_auc_model": _mean(pr_auc_acc[head]),
            "mean_pr_auc_baseline": _mean(base_pr_acc[head]),
            "n_splits_scored": len(pr_auc_acc[head]),
        }
        for head in HEADS
    }

    confusion: dict[str, dict] = {}
    if last_bundle is not None and last_test_df is not None and "downgrade" in last_bundle["heads"]:
        X_te, _, _ = make_xy(last_test_df, "downgrade")
        if len(X_te):
            p = predict_proba_all(last_bundle, X_te)["downgrade"]
            confusion = confusion_by_bucket(last_test_df, p)

    return {
        "split_dates": split_dates,
        "per_split": per_split,
        "aggregate": aggregate,
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
    args = parser.parse_args()

    if args.matrix:
        import pandas as pd
        df = pd.read_csv(args.matrix, dtype={"cik": str, "period_end": str, "agency": str})
    else:
        from src.model.features import load_training_matrix
        df = load_training_matrix()

    result = walk_forward_eval(df, [s.strip() for s in args.splits.split(",")])
    from pathlib import Path
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"migration": result}, indent=2))
    print(json.dumps(result["aggregate"], indent=2))
    print(f"\nFull report → {args.out}")
