"""
Threshold calibration for the credit-stress score.

THREE-TIER CALIBRATION ARCHITECTURE
===================================
The scoring config (src/score.DEFAULT_CONFIG) is meant to be produced by a
layered calibration pipeline. Only Tier 1 is implemented in this module; the
later tiers have documented extension points so the architecture is explicit.

  Tier 1 — Statistical re-centering  (THIS MODULE, implemented)
      Re-derive the healthy/severe thresholds of the 10 Phase-1 rules from the
      point-in-time backtest data, so each rule's "no points" and "full points"
      boundaries sit where healthy and distressed issuers actually separate
      instead of at analytical convention. Entry point: calibrate_thresholds(),
      which returns a partial config dict (the Tier 1 base) — the {rules: {...}}
      entries for the 10 calibrated keys, weights unchanged.

  Tier 2 — Sector adjustment  (NOT built — needs the benchmark layer)
      Shift the Tier 1 thresholds per peer group (asset-light tech vs
      capital-heavy industrials vs financials), because "healthy" leverage and
      coverage differ by sector. Extension point: sector_adjusted_config(base,
      sector_group) — currently a stub that raises NotImplementedError.

  Tier 3 — Analyst override  (NOT built — audited)
      A reviewed, logged manual-override layer applied on top of Tier 1/2 for
      named issuers or rules, with provenance for audit. Extension point:
      apply_analyst_overrides(config, overrides) — currently a stub.

SCOPE GUARANTEE
===============
Only the 10 rules in score._ADDITIONAL_RULE_RATIOS are calibrated. Freeman's 9
original rules (score._CORE_RULE_KEYS) stayed clean in the 95-case backtest and
are NEVER read or written by this module. score_cap, weights, the LLM caps, the
escalation floor and the threshold are all left untouched.

METHODOLOGY (Tier 1)
====================
For each of the 10 rules, using every point-in-time snapshot in
data/backtest_results.json where the ratio is non-null, tagged by the owning
case's label (distressed / healthy):

  * Polarity is read from the existing DEFAULT_CONFIG entry and preserved:
      severe < healthy  -> "lower is worse"  (e.g. coverage ratios)
      severe > healthy  -> "higher is worse" (e.g. debt_to_equity)

  * severe (full-points) threshold = the value that maximizes Youden's J
      (J = sensitivity + specificity - 1 = TPR - FPR) for a distressed-vs-healthy
      classifier in the rule's polarity. Youden's J is chosen over F1 because the
      snapshot sample is heavily class-imbalanced (≈10x more healthy snapshots
      than distressed for some rules); Youden's J is prevalence-independent, so
      it targets distributional separation rather than the base rate. F1 at the
      threshold is reported alongside for reference.

  * healthy (zero-points) threshold = the healthy-side percentile at which
      ~CLEAN_FRACTION (90%) of healthy snapshots fall in the no-points zone:
      90th percentile of the healthy distribution for "higher is worse", 10th
      percentile for "lower is worse".

  * Anchoring: the spec (DEFAULT_CONFIG) value is retained when the data-derived
      value is within a relative band (ANCHOR_REL_BAND = 20% of |spec|) — "the
      data agrees". The value moves only on clear disagreement. A spec value of
      exactly 0 has a zero band (0 is arbitrary), so any non-zero data moves it.

  * The ramp divides by (severe - healthy), so the two are forced strictly
      different and correctly ordered for the polarity; a minimal widening is
      applied and reported if a data-derived pair would collide or invert.

OUTPUTS
=======
calibrate_thresholds() returns the partial config + a per-rule report. The CLI
(`python -m src.calibrate`) writes data/calibrated_thresholds.json and prints
the comparison table. NOTHING is auto-applied to score.DEFAULT_CONFIG — the
artifact is reviewed first, then applied in a follow-up.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from src.score import (
    DEFAULT_CONFIG,
    _ADDITIONAL_RULE_RATIOS,
    _CORE_RULE_KEYS,
    ScoreConfig,
)

# ── Paths ──────────────────────────────────────────────────────────────────────
DATA_DIR = pathlib.Path(__file__).parent.parent / "data"
RESULTS_PATH = DATA_DIR / "backtest_results.json"
CALIBRATED_PATH = DATA_DIR / "calibrated_thresholds.json"

# ── Tunables (documented in the module docstring) ──────────────────────────────
CLEAN_FRACTION = 0.90      # fraction of healthy snapshots that should score zero
ANCHOR_REL_BAND = 0.20     # keep spec when data is within 20% of |spec|
MIN_SAMPLES = 5            # per class; below this we keep spec (insufficient data)


# ── Small statistics helpers (dependency-free) ─────────────────────────────────

def _percentile(values: list[float], pct: float) -> float:
    """
    Linear-interpolation percentile (pct in [0, 100]); matches numpy's default.
    `values` need not be sorted. Empty list raises ValueError (caller guards).
    """
    if not values:
        raise ValueError("percentile of empty sequence")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    rank = (pct / 100.0) * (len(xs) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(xs) - 1)
    frac = rank - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def _predict_distressed(value: float, threshold: float, higher_worse: bool) -> bool:
    """One rule's binary call: is this value on the 'worse' side of the threshold?"""
    return value >= threshold if higher_worse else value <= threshold


def _confusion(distressed: list[float], healthy: list[float], t: float, higher_worse: bool):
    """Return (tp, fp, fn, tn) for classifying 'worse-than-t' as distressed."""
    tp = sum(1 for v in distressed if _predict_distressed(v, t, higher_worse))
    fn = len(distressed) - tp
    fp = sum(1 for v in healthy if _predict_distressed(v, t, higher_worse))
    tn = len(healthy) - fp
    return tp, fp, fn, tn


def _f1(distressed: list[float], healthy: list[float], t: float, higher_worse: bool) -> float:
    tp, fp, fn, _ = _confusion(distressed, healthy, t, higher_worse)
    denom = 2 * tp + fp + fn
    return (2 * tp / denom) if denom else 0.0


def _youden_threshold(distressed: list[float], healthy: list[float], higher_worse: bool):
    """
    Sweep candidate thresholds (the unique observed values) and return the one
    maximizing Youden's J = TPR - FPR, with its (J, f1) for reporting.
    """
    candidates = sorted(set(distressed) | set(healthy))
    best_t, best_j, best_f1 = candidates[0], -2.0, 0.0
    nd, nh = len(distressed), len(healthy)
    for t in candidates:
        tp, fp, fn, tn = _confusion(distressed, healthy, t, higher_worse)
        tpr = tp / nd if nd else 0.0
        fpr = fp / nh if nh else 0.0
        j = tpr - fpr
        if j > best_j:
            best_t, best_j, best_f1 = t, j, _f1(distressed, healthy, t, higher_worse)
    return best_t, best_j, best_f1


# ── Sample collection ──────────────────────────────────────────────────────────

def _collect_samples(results: dict, ratio_name: str) -> tuple[list[float], list[float]]:
    """
    Gather (distressed_values, healthy_values) for one ratio across EVERY
    point-in-time snapshot in the backtest, skipping null/missing readings.
    Snapshot-level (not deduped) so it matches the backtest's own scoring
    granularity — the same 760-healthy-snapshot denominator as the FP rate.
    """
    distressed: list[float] = []
    healthy: list[float] = []
    for case in results.get("cases", []):
        label = case.get("label")
        if label not in ("distressed", "healthy"):
            continue
        bucket = distressed if label == "distressed" else healthy
        for snap in case.get("trajectory", []):
            v = (snap.get("ratios") or {}).get(ratio_name)
            if v is None:
                continue
            bucket.append(float(v))
    return distressed, healthy


# ── Anchoring & ordering ─────────────────────────────────────────────────────

def _anchor(data_val: float, spec_val: float) -> tuple[float, bool]:
    """
    Keep `spec_val` when `data_val` is within the relative anchor band of it
    ("data agrees"); otherwise adopt `data_val`. Returns (chosen, moved).
    A spec of exactly 0 has a zero band, so any non-zero data moves it.
    """
    band = ANCHOR_REL_BAND * abs(spec_val)
    if abs(data_val - spec_val) <= band:
        return spec_val, False
    return data_val, True


def _enforce_order(healthy: float, severe: float, higher_worse: bool) -> tuple[float, float, bool]:
    """
    Guarantee severe is strictly more extreme than healthy in the rule's polarity
    (the ramp divides by their gap). Returns (healthy, severe, widened).
    """
    correct = (severe > healthy) if higher_worse else (severe < healthy)
    if correct and severe != healthy:
        return healthy, severe, False
    # Collision or inversion: nudge severe outward by a scale-aware minimal gap.
    gap = max(abs(healthy), abs(severe), 1.0) * 0.05
    severe = healthy + gap if higher_worse else healthy - gap
    return healthy, severe, True


# ── Per-rule calibration ─────────────────────────────────────────────────────

def calibrate_rule(rule_key: str, ratio_name: str, spec_rule: dict, results: dict) -> dict:
    """
    Calibrate one rule. Returns a report dict carrying old/new thresholds, the
    chosen new {weight, healthy, severe}, F1 before/after, and the healthy
    snapshot FP count before/after (snapshots where the rule scores > 0).
    """
    spec_healthy = float(spec_rule["healthy"])
    spec_severe = float(spec_rule["severe"])
    weight = float(spec_rule["weight"])
    higher_worse = spec_severe > spec_healthy  # polarity from the spec

    distressed, healthy = _collect_samples(results, ratio_name)

    # Healthy FP count (snapshots scoring > 0) under a given healthy threshold.
    def healthy_fp(h: float) -> int:
        # > 0 points means the value is strictly past `h` toward severe.
        return sum(1 for v in healthy if (v > h if higher_worse else v < h))

    report: dict[str, Any] = {
        "rule_key": rule_key,
        "ratio_name": ratio_name,
        "polarity": "higher_worse" if higher_worse else "lower_worse",
        "n_distressed": len(distressed),
        "n_healthy": len(healthy),
        "old": {"healthy": spec_healthy, "severe": spec_severe},
        "weight": weight,
        "note": "",
    }

    if len(distressed) < MIN_SAMPLES or len(healthy) < MIN_SAMPLES:
        # Not enough data to trust a re-centering — keep the spec values.
        report.update({
            "new": {"healthy": spec_healthy, "severe": spec_severe},
            "f1_before": _f1(distressed, healthy, spec_severe, higher_worse) if distressed and healthy else None,
            "f1_after": _f1(distressed, healthy, spec_severe, higher_worse) if distressed and healthy else None,
            "fp_before": healthy_fp(spec_healthy),
            "fp_after": healthy_fp(spec_healthy),
            "moved_healthy": False,
            "moved_severe": False,
            "note": f"insufficient data (d={len(distressed)}, h={len(healthy)}) — kept spec",
        })
        return report

    # Data-derived candidates.
    data_severe, youden_j, _ = _youden_threshold(distressed, healthy, higher_worse)
    pct = 90.0 if higher_worse else 10.0
    data_healthy = _percentile(healthy, pct)

    def _outside_band(data_val: float, spec_val: float) -> bool:
        return abs(data_val - spec_val) > ANCHOR_REL_BAND * abs(spec_val)

    # ── Healthy (zero-points) threshold ──────────────────────────────────────
    # Adopt the data percentile ONLY when it both (a) clearly disagrees with the
    # spec and (b) REDUCES this rule's healthy false positives. This makes the
    # FP count monotone: calibration can never make a rule over-flag more healthy
    # snapshots than it does today.
    adopt_h = _outside_band(data_healthy, spec_healthy) and (
        healthy_fp(data_healthy) < healthy_fp(spec_healthy)
    )
    new_healthy = data_healthy if adopt_h else spec_healthy

    # ── Severe (full-points) threshold ───────────────────────────────────────
    # Adopt the Youden-optimal severe ONLY when it (a) clearly disagrees, (b)
    # strictly IMPROVES F1 over the spec severe, and (c) preserves the rule's
    # polarity ordering against the chosen healthy threshold. This rejects both
    # the degenerate Youden picks (F1 → 0) and the dollar-scale artifacts
    # (e.g. moody_adjusted_fcf) that would otherwise corrupt the threshold.
    f1_spec = _f1(distressed, healthy, spec_severe, higher_worse)
    f1_data = _f1(distressed, healthy, data_severe, higher_worse)
    polarity_ok = (data_severe > new_healthy) if higher_worse else (data_severe < new_healthy)
    adopt_s = _outside_band(data_severe, spec_severe) and (f1_data > f1_spec) and polarity_ok
    new_severe = data_severe if adopt_s else spec_severe

    # Final safety: the ramp divides by (severe - healthy), so force a correctly
    # ordered, strictly-different pair. This only fires when keeping the spec
    # severe would invert against a healthy threshold that moved past it
    # (e.g. debt_to_equity, whose healthy must rise above the spec severe of 3×).
    new_healthy, new_severe, widened = _enforce_order(new_healthy, new_severe, higher_worse)

    f1_after = _f1(distressed, healthy, new_severe, higher_worse)
    report.update({
        "new": {"healthy": round(new_healthy, 6), "severe": round(new_severe, 6)},
        "youden_j": round(youden_j, 3),
        "f1_before": round(f1_spec, 3),
        "f1_after": round(f1_after, 3),
        "fp_before": healthy_fp(spec_healthy),
        "fp_after": healthy_fp(new_healthy),
        "moved_healthy": new_healthy != spec_healthy,
        "moved_severe": new_severe != spec_severe,
    })
    notes = []
    if widened:
        notes.append("severe widened to keep polarity vs raised healthy")
    if f1_after < 0.20:
        notes.append("weak separation (F1<0.2) — metric barely discriminates")
    if not adopt_s and not adopt_h:
        notes.append("data agrees with spec — kept")
    report["note"] = "; ".join(notes)
    return report


# ── Tier 1 entry point ─────────────────────────────────────────────────────────

def calibrate_thresholds(
    results_path: pathlib.Path | str = RESULTS_PATH,
    base_config: dict = DEFAULT_CONFIG,
) -> tuple[dict, list[dict]]:
    """
    TIER 1 base config.

    Reads the backtest results JSON and re-centers the 10 rules in
    score._ADDITIONAL_RULE_RATIOS. Returns:
      (partial_config, reports)
        partial_config — {"rules": {rule_key: {weight, healthy, severe}, ...}} for
                          the 10 calibrated keys only (a partial config ready to be
                          deep-merged over DEFAULT_CONFIG via ScoreConfig.from_dict).
        reports        — per-rule calibration report dicts (for the table).

    Freeman's 9 core rules (_CORE_RULE_KEYS) are never included.
    """
    results = json.loads(pathlib.Path(results_path).read_text())

    partial: dict[str, dict] = {"rules": {}}
    reports: list[dict] = []
    for rule_key, ratio_name in _ADDITIONAL_RULE_RATIOS.items():
        assert rule_key not in _CORE_RULE_KEYS, f"refusing to calibrate core rule {rule_key}"
        spec_rule = base_config["rules"][rule_key]
        rep = calibrate_rule(rule_key, ratio_name, spec_rule, results)
        reports.append(rep)
        partial["rules"][rule_key] = {
            "weight": rep["weight"],                 # unchanged
            "healthy": rep["new"]["healthy"],
            "severe": rep["new"]["severe"],
        }
    return partial, reports


# Rules whose data-derived thresholds are adopted under SELECTIVE calibration:
# the ones that reduced healthy FPs while holding or improving F1 in the full
# Tier 1 pass. The remaining three are kept at spec (see SELECTIVE_KEEP_SPEC).
SELECTIVE_ADOPT_KEYS = (
    "asset_coverage<1.5x",
    "tangible_asset_coverage<1x",
    "liquidation_asset_coverage<0.7x",
    "ocf_ebitda_conversion<0.7x",
    "rcf_net_debt<15%",
    "maturity_coverage_near_term<1x",
    "revenue_yoy_growth<-5%",
)
# Kept at spec, with the reason recorded in the artifact's _meta.notes.
SELECTIVE_KEEP_SPEC = {
    "debt_to_equity>2x": "Weak separator (full-pass calibrated F1 0.12); re-centering "
                         "would raise healthy to ~9.45x and effectively disable the rule. "
                         "Kept at spec to preserve the points that help catch Garrett/Patriot.",
    "quick_ratio<1x": "Weak separator (full-pass calibrated F1 0.25). Kept at spec — "
                      "do not over-trust a barely-discriminating signal.",
    "moody_adjusted_fcf_negative": "REQUIRES METRIC REDESIGN — moody_adjusted_fcf is a raw "
                                   "dollar amount, not size-normalized, so percentile/Youden "
                                   "thresholds are company-size artifacts. Re-express as a ratio "
                                   "(FCF/debt or FCF/revenue) before Tier 1 calibration is "
                                   "meaningful. Not moved.",
}


def calibrate_thresholds_selective(
    adopt_keys: tuple[str, ...] = SELECTIVE_ADOPT_KEYS,
    results_path: pathlib.Path | str = RESULTS_PATH,
    base_config: dict = DEFAULT_CONFIG,
) -> tuple[dict, list[dict]]:
    """
    TIER 1 base config — SELECTIVE adoption variant.

    Same per-rule calibration as calibrate_thresholds(), but the data-derived
    thresholds are adopted ONLY for `adopt_keys`. Every other calibrated rule is
    pinned to its DEFAULT_CONFIG (spec) value — used for the weak separators
    (debt_to_equity, quick_ratio) and the non-ratio metric (moody_adjusted_fcf).

    Returns (partial_config, reports). The partial carries all 10 calibrated
    rules (adopted or spec) plus a "_meta" block (ignored by ScoreConfig.from_dict)
    documenting which were adopted, which were kept, and why.
    """
    _, reports = calibrate_thresholds(results_path, base_config)
    by_key = {r["rule_key"]: r for r in reports}

    partial: dict = {"rules": {}}
    adopted, kept = [], []
    for rule_key, rep in by_key.items():
        spec_rule = base_config["rules"][rule_key]
        if rule_key in adopt_keys:
            partial["rules"][rule_key] = {
                "weight": rep["weight"],
                "healthy": rep["new"]["healthy"],
                "severe": rep["new"]["severe"],
            }
            adopted.append(rule_key)
        else:
            partial["rules"][rule_key] = {
                "weight": float(spec_rule["weight"]),
                "healthy": float(spec_rule["healthy"]),
                "severe": float(spec_rule["severe"]),
            }
            kept.append(rule_key)

    partial["_meta"] = {
        "tier": 1,
        "adoption": "selective",
        "adopted_from_data": adopted,
        "kept_at_spec": kept,
        "notes": {k: SELECTIVE_KEEP_SPEC[k] for k in kept if k in SELECTIVE_KEEP_SPEC},
    }
    return partial, reports


# ── Tier 2 / Tier 3 extension points (NOT built) ───────────────────────────────

def sector_adjusted_config(base: dict, sector_group: str) -> dict:
    """
    TIER 2 (NOT IMPLEMENTED) — sector-relative threshold adjustment.

    Would take the Tier 1 `base` partial config and shift each threshold for the
    given `sector_group` using the benchmark layer (peer-group distributions of
    each ratio), so an asset-light software firm and a capital-heavy industrial
    are judged against their own peers rather than one global cutoff.

    Depends on the benchmark layer, which is not built yet.
    """
    raise NotImplementedError(
        "Tier 2 sector adjustment requires the benchmark layer (not built). "
        "Provide a partial-config transform here once peer-group distributions exist."
    )


def apply_analyst_overrides(config: dict, overrides: list[dict]) -> dict:
    """
    TIER 3 (NOT IMPLEMENTED) — audited analyst-override layer.

    Would apply reviewed, logged manual overrides (per rule and/or per issuer) on
    top of the Tier 1/2 config, each carrying provenance (who/when/why) for audit.
    """
    raise NotImplementedError(
        "Tier 3 analyst overrides (audited) not built. Apply reviewed per-rule / "
        "per-issuer overrides with provenance on top of the Tier 1/2 config here."
    )


# ── Validation (mirrors api/main.py._validate_config guards) ───────────────────

def validate_merged_config(partial: dict, base_config: dict = DEFAULT_CONFIG) -> dict:
    """
    Deep-merge `partial` over DEFAULT_CONFIG and assert it passes the same guards
    api/main.py._validate_config enforces. Returns the normalized full config dict.
    Raises ValueError on any violation (so the artifact is never silently bad).
    """
    unknown = set((partial.get("rules") or {}).keys()) - set(base_config["rules"].keys())
    if unknown:
        raise ValueError(f"unknown rule keys: {', '.join(sorted(unknown))}")

    cfg = ScoreConfig.from_dict(partial)

    for key, r in cfg.rules.items():
        if not (0 <= r["weight"] <= 100):
            raise ValueError(f"{key}.weight must be in [0, 100]")
        if r["healthy"] == r["severe"]:
            raise ValueError(f"{key}: healthy and severe must differ (ramp divides by their gap)")
    if not (1 <= cfg.score_cap <= 100):
        raise ValueError("score_cap must be in [1, 100]")
    if cfg.score_cap != base_config["score_cap"]:
        raise ValueError(f"score_cap changed ({cfg.score_cap} != {base_config['score_cap']}) — Tier 1 must not touch it")
    return cfg.to_dict()


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _fmt_pair(d: dict) -> str:
    return f"{d['healthy']:>9.4g} / {d['severe']:<9.4g}"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Tier 1 threshold calibration")
    ap.add_argument("--selective", action="store_true",
                    help="Adopt data thresholds only for the strong rules; keep weak "
                         "separators and moody_adjusted_fcf at spec.")
    args = ap.parse_args(argv)

    if args.selective:
        partial, reports = calibrate_thresholds_selective()
        out_path = DATA_DIR / "calibrated_thresholds_selective.json"
        mode = "SELECTIVE"
        adopt = set(SELECTIVE_ADOPT_KEYS)
    else:
        partial, reports = calibrate_thresholds()
        out_path = CALIBRATED_PATH
        mode = "FULL"
        adopt = None  # all rules adopted per the conservative policy

    # Validate the merged config (Step 3) before writing the artifact.
    try:
        validate_merged_config(partial)
        valid_msg = "PASS — merged config passes all guards (healthy≠severe, weights∈[0,100], score_cap=100)"
    except ValueError as e:
        valid_msg = f"FAIL — {e}"

    # Write the artifact (Step 2): partial config of the 10 re-centered rules.
    out_path.write_text(json.dumps(partial, indent=2))

    # Comparison table.
    print(f"\nTier 1 calibration [{mode}] — {len(reports)} rules "
          f"(Freeman's 9 core rules untouched)\n")
    hdr = (f"{'rule':<32} {'old h/severe':>21}  {'used h/severe':>21}  "
           f"{'F1 b→a':>11}  {'src':>5}")
    print(hdr)
    print("-" * len(hdr))
    used_rules = partial["rules"]
    for r in reports:
        rk = r["rule_key"]
        used = used_rules[rk]
        # "src" = whether the artifact uses the data value or the spec value.
        is_spec = (used["healthy"] == r["old"]["healthy"] and used["severe"] == r["old"]["severe"])
        src = "spec" if (adopt is not None and rk not in adopt) or is_spec else "data"
        f1b = "n/a" if r["f1_before"] is None else f"{r['f1_before']:.2f}"
        f1a = "n/a" if r["f1_after"] is None else f"{r['f1_after']:.2f}"
        f1a_disp = f1a if src == "data" else "—(spec)"
        print(f"{rk:<32} {_fmt_pair(r['old']):>21}  {_fmt_pair(used):>21}  "
              f"{f1b:>5}→{f1a_disp:<7}  {src:>5}")

    print(f"\nValidation: {valid_msg}")
    print(f"Artifact written: {out_path}")
    if mode == "SELECTIVE":
        m = partial["_meta"]
        print(f"Adopted from data ({len(m['adopted_from_data'])}): {', '.join(m['adopted_from_data'])}")
        print(f"Kept at spec ({len(m['kept_at_spec'])}): {', '.join(m['kept_at_spec'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
