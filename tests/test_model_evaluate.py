"""Tests for src/model/evaluate.py — frontier + issuer-level any-agency threshold tuning."""


from src.model.evaluate import (
    catch_fp_frontier, _tune_from_pooled, _diag_from_pooled, _ig_hy_bucket,
)
from src.rating import rating_index


def test_frontier_separable():
    # Positives all score high, negatives all low → catch everything at zero false alarms.
    y = [1] * 20 + [0] * 180
    p = [0.9] * 20 + [0.1] * 180
    f = catch_fp_frontier(y, p)
    assert f is not None
    assert f["base_rate"] == 0.1 and f["n_positive"] == 20
    c90 = f["at_recall"]["90"]
    assert c90["catch"] >= 0.9 and c90["fpr"] == 0.0 and c90["precision"] == 1.0


def test_frontier_monotonic_tradeoff():
    # Overlapping but graded: positives 0.4–1.0, negatives 0.0–0.6 (non-separable).
    pos = [0.4 + 0.6 * (i / 49) for i in range(50)]
    neg = [0.0 + 0.6 * (i / 149) for i in range(150)]
    y = [1] * 50 + [0] * 150
    p = pos + neg
    f = catch_fp_frontier(y, p)
    assert f["base_rate"] == 0.25
    # Catching MORE events costs at least as high a false-positive rate.
    assert f["at_recall"]["70"]["fpr"] <= f["at_recall"]["90"]["fpr"]
    # A looser false-positive budget yields at least as much catch.
    assert f["at_fpr"]["10"]["catch"] <= f["at_fpr"]["30"]["catch"]
    # Every catch target is met.
    for r in ("70", "80", "90"):
        assert f["at_recall"][r]["catch"] >= int(r) / 100


def test_frontier_single_class_returns_none():
    assert catch_fp_frontier([0] * 50, [0.3] * 50) is None
    assert catch_fp_frontier([], []) is None


# ── issuer-level any-agency threshold tuning / diagnostics ────────────────────

def test_ig_hy_bucket():
    assert _ig_hy_bucket(rating_index("BBB-")) == "IG"   # IG/HY boundary is inclusive
    assert _ig_hy_bucket(rating_index("BB+")) == "HY"
    assert _ig_hy_bucket(None) is None


def test_tune_from_pooled_downgrade_is_operating_point():
    pooled_y = {"downgrade": [0, 1, 0, 1], "upgrade": [0, 0, 1, 1], "distress": [0, 0, 0, 1]}
    pooled_p = {"downgrade": [.1, .2, .15, .3], "upgrade": [.1, .2, .6, .7], "distress": [.05, .05, .1, .4]}
    thr = _tune_from_pooled(pooled_y, pooled_p, downgrade_operating_point=0.14)
    assert thr["downgrade"] == 0.14                       # product operating point, not a fit
    assert "upgrade" in thr and "distress" in thr         # these are max-F1 tuned


def test_diag_from_pooled_confusion_and_calibration():
    y = {"downgrade": [0, 0, 1, 1], "upgrade": [], "distress": []}
    p = {"downgrade": [0.1, 0.2, 0.6, 0.7], "upgrade": [], "distress": []}
    bucket = ["IG", "HY", "IG", "HY"]                     # aligned to pooled_y["downgrade"]
    diag = _diag_from_pooled(y, p, bucket, {"downgrade": 0.5})
    assert diag["base_rate"]["downgrade"] == 0.5
    assert diag["calibration_ratio"]["downgrade"] is not None
    conf = diag["confusion_by_bucket"]
    # thr 0.5 → preds [F,F,T,T]; each bucket has one tn (y0,p<.5) and one tp (y1,p≥.5).
    for b in ("IG", "HY"):
        assert conf[b]["n"] == 2
        assert (conf[b]["tp"], conf[b]["fp"], conf[b]["tn"], conf[b]["fn"]) == (1, 0, 1, 0)
