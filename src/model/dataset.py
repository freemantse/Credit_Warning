"""
Stage 3 dataset plumbing: turn the feature matrix into model-ready (X, y) per
prediction head, with a TIME-RESPECTING split and the per-head monotone constraints.

Three heads, all over the 12-month horizon:
  downgrade — y = (label_12m == +1)   (rating got worse)
  upgrade   — y = (label_12m == -1)   (rating got better)
  default   — y = default_12m         (a default occurred)

Only rows whose 12-month outcome is OBSERVED (label_12m not null — censored rows
are null per the label builder) are usable, so right-edge censoring never leaks in
as a fake "stable"/"no-default".

No look-ahead: the split trains only on rows whose label window CLOSES on/before the
split date and tests on rows after it (walk-forward), the temporal analogue of the
feature pipeline's causal guarantee.

sklearn is imported lazily inside the metric helpers so this module (and anything
importing FEATURE/​head definitions) stays importable without the ML stack.
"""

from __future__ import annotations

from typing import Any, Callable

from src.model.features import FEATURE_COLUMNS, FEATURE_DIRECTIONS
from src.ratings.labels import add_months


# Per-head spec. `monotone_sign` multiplies FEATURE_DIRECTIONS (which is expressed
# w.r.t. downgrade risk): the upgrade head flips it (a feature that raises downgrade
# risk lowers upgrade odds); default shares the downgrade direction.
HEADS: dict[str, dict[str, Any]] = {
    "downgrade": {"monotone_sign": 1,  "positive": lambda r: r.get("label_12m") == 1},
    "upgrade":   {"monotone_sign": -1, "positive": lambda r: r.get("label_12m") == -1},
    "default":   {"monotone_sign": 1,  "positive": lambda r: bool(r.get("default_12m"))},
}

HORIZON_MONTHS = 12


def monotone_constraints(head: str) -> list[int]:
    """Per-feature monotone constraint array (in FEATURE_COLUMNS order) for a head."""
    sign = HEADS[head]["monotone_sign"]
    return [sign * FEATURE_DIRECTIONS.get(col, 0) for col in FEATURE_COLUMNS]


def observed(df):
    """Rows whose 12-month outcome is observed (label_12m not null)."""
    return df[df["label_12m"].notna()]


def make_xy(df, head: str):
    """
    Build (X, y) for one head from the matrix.

    X is the FEATURE_COLUMNS as floats (NaNs preserved — the histogram booster
    handles them natively; the logistic baseline imputes). y is the binary head
    target over the observed rows. Returns (X, y, index).
    """
    import numpy as np
    import pandas as pd

    usable = observed(df)
    is_pos: Callable[[dict], bool] = HEADS[head]["positive"]
    y = usable.apply(lambda r: 1 if is_pos(r) else 0, axis=1).astype(int)

    X = usable.reindex(columns=FEATURE_COLUMNS).copy()
    # Coerce everything to float; booleans → 0/1; non-numeric → NaN.
    for col in FEATURE_COLUMNS:
        X[col] = pd.to_numeric(X[col], errors="coerce").astype(float)
    return X, y.reset_index(drop=True), usable.index


def time_split(df, split_date: str):
    """
    Walk-forward split. Train = rows whose 12-month label window CLOSES on/before
    split_date (fully observed and in the past); test = rows with period_end strictly
    after split_date. Returns (train_df, test_df).
    """
    closes = df["period_end"].map(lambda p: add_months(p, HORIZON_MONTHS))
    train = df[closes <= split_date]
    test = df[df["period_end"] > split_date]
    return train.copy(), test.copy()


def recent_holdout(train_df, frac: float = 0.2):
    """
    Carve the most RECENT `frac` of the training periods off as a calibration set
    (time-respecting — calibration data is later than fit data, never interleaved).
    Returns (fit_df, calib_df). Falls back to no holdout when there are too few periods.
    """
    periods = sorted(train_df["period_end"].unique())
    if len(periods) < 3:
        return train_df.copy(), train_df.iloc[0:0].copy()
    cut_idx = max(1, int(len(periods) * (1 - frac)))
    cut_date = periods[cut_idx - 1]
    fit = train_df[train_df["period_end"] <= cut_date]
    calib = train_df[train_df["period_end"] > cut_date]
    if calib.empty:
        return train_df.copy(), train_df.iloc[0:0].copy()
    return fit.copy(), calib.copy()


# ── Metrics (sklearn, lazy) ──────────────────────────────────────────────────

def recall_at_k(y_true, p, k_frac: float = 0.1) -> float | None:
    """Recall among the top-k_frac highest-probability predictions (rare-event focus)."""
    import numpy as np

    y = np.asarray(y_true)
    p = np.asarray(p)
    n_pos = int(y.sum())
    if n_pos == 0 or len(y) == 0:
        return None
    k = max(1, int(len(y) * k_frac))
    top = np.argsort(-p)[:k]
    return float(y[top].sum()) / n_pos


def calibration_bins(y_true, p, n_bins: int = 10) -> list[dict]:
    """Reliability bins: mean predicted prob vs. observed positive rate per decile."""
    import numpy as np

    y = np.asarray(y_true, dtype=float)
    p = np.asarray(p, dtype=float)
    if len(y) == 0:
        return []
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    out = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi if i < n_bins - 1 else p <= hi)
        if not mask.any():
            continue
        out.append({
            "bin": [round(float(lo), 2), round(float(hi), 2)],
            "n": int(mask.sum()),
            "mean_pred": round(float(p[mask].mean()), 4),
            "observed_rate": round(float(y[mask].mean()), 4),
        })
    return out


def classification_metrics(y_true, p, *, k_frac: float = 0.1) -> dict[str, Any]:
    """
    Rare-event-focused scorecard for one head: PR-AUC (primary), ROC-AUC, Brier,
    base rate, recall@k, and calibration bins. PR-AUC/ROC require both classes;
    they are None on a single-class slice.
    """
    import numpy as np
    from sklearn.metrics import average_precision_score, roc_auc_score, brier_score_loss

    y = np.asarray(y_true)
    p = np.asarray(p, dtype=float)
    both_classes = len(np.unique(y)) > 1
    return {
        "n": int(len(y)),
        "base_rate": round(float(y.mean()), 4) if len(y) else None,
        "pr_auc": round(float(average_precision_score(y, p)), 4) if both_classes else None,
        "roc_auc": round(float(roc_auc_score(y, p)), 4) if both_classes else None,
        "brier": round(float(brier_score_loss(y, p)), 4) if len(y) else None,
        "recall_at_10pct": recall_at_k(y, p, k_frac),
        "calibration": calibration_bins(y, p),
    }
