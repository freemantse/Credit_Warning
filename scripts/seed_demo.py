"""
One-command, end-to-end demo seed for the rating-migration system.

Runs the whole pipeline on the curated real-data roster (data/cases.csv) so the app
shows MODEL P(upgrade)/P(downgrade) instead of the rule-based outlook fallback:

  1. seed_cases            — upsert the roster into the `cases` table (resolves CIKs)
  2. track each issuer     — EDGAR XBRL → ratios + implied_ratings + maturities
  3. seed_agency_ratings   — real Moody's/Fitch/Egan-Jones trajectories → agency_ratings
  4. build_labels          — agency events + period_ends → rating_labels (lookahead-free)
  5. train + register      — load_training_matrix → train_all → save_model/registry
  6. walk-forward vintages — train_vintages(cutoffs) → data/model_vintages/ (event backtest)
  7. score config          — derive_score_config → save_score_config (model-learned weights)
  8. predict               — build_scoring_matrix → predict_and_store → migration_predictions
  9. walk-forward eval     — walk_forward_eval → data/migration_eval.json (scorecard)

Requires the operator's environment: Supabase creds (.env.local) + EDGAR network for
the tracking step. Idempotent — re-running upserts. Run the consolidated SQL schema
first (supabase/schema.sql).

Usage:
    python3 -m scripts.seed_demo
    python3 -m scripts.seed_demo --skip-track     # reuse already-tracked ratios
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
EVAL_PATH = _ROOT / "data" / "migration_eval.json"
VINTAGE_DIR = _ROOT / "data" / "model_vintages"


def _hr(title: str) -> None:
    print(f"\n{'='*72}\n  {title}\n{'='*72}")


def _split_dates(period_ends: list[str]) -> list[str]:
    """
    Pick interior walk-forward cutoff dates from the observed period_ends, evenly
    spaced by rank so each split has both train and test rows. Deduped + sorted.

    The quantiles span WIDE — from the 10th percentile up — so there's an EARLY
    vintage (~2014) that predates the oldest backtest events (the 2016 oil-bust
    defaults). A recent-only cutoff set would leave those events unscorable (no
    vintage trained before them → data_gap).
    """
    uniq = sorted(set(period_ends))
    if len(uniq) < 3:
        return uniq[:1]
    # Interior quantile positions (avoid the very first/last so neither side is empty).
    picks = {uniq[int(q * (len(uniq) - 1))] for q in (0.1, 0.3, 0.5, 0.7, 0.85)}
    return sorted(picks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the full migration-model demo")
    parser.add_argument("--skip-track", action="store_true",
                        help="Skip the EDGAR tracking step (reuse stored ratios)")
    parser.add_argument("--periods", type=int, default=None,
                        help="Cap annual periods per issuer (default: full history)")
    args = parser.parse_args()

    # ── 1. Cases ──────────────────────────────────────────────────────────────
    _hr("1/9  Seeding case roster")
    from scripts import seed_cases
    seed_cases.main()

    # ── 2. Track each roster issuer (ratios + implied ratings + maturities) ────
    from src.backtest import load_cases
    roster = load_cases()
    if not args.skip_track:
        _hr(f"2/9  Tracking {len(roster)} issuers from EDGAR (ratios + implied ratings)")
        from src.track import track
        for c in roster:
            ident = (c.get("cik") or c.get("ticker") or "").strip()  # CIK is authoritative
            if not ident:
                continue
            try:
                track(ident, n_periods=args.periods, include_llm=False)
            except Exception as e:
                print(f"  ! track failed for {c.get('ticker') or ident}: {e}")
    else:
        _hr("2/9  Skipping EDGAR tracking (--skip-track)")

    # ── 3. Agency ratings (real LSEG trajectories) ─────────────────────────────
    _hr("3/9  Ingesting real agency ratings (Moody's / Fitch / Egan-Jones)")
    from scripts import seed_agency_ratings
    seed_agency_ratings.main()

    # ── 4. Labels ──────────────────────────────────────────────────────────────
    _hr("4/9  Building ML labels (rating_labels)")
    from scripts import build_labels
    build_labels.main()

    # ── 5-9. Model pipeline ────────────────────────────────────────────────────
    from src.model.features import load_training_matrix, build_scoring_matrix
    from src.model.train import (
        train_all, save_model, derive_score_config, train_vintages, DEFAULT_ARTIFACT,
    )
    from src.model.predict import predict_and_store
    from src.model.evaluate import walk_forward_eval
    from src.store import save_model_registry, save_score_config

    _hr("5/9  Assembling the training matrix")
    df = load_training_matrix()
    print(f"  training matrix: {len(df)} rows, "
          f"{df['cik'].nunique() if not df.empty else 0} issuers")
    if df.empty:
        print("\nNo labeled training rows — check that tracking + agency-rating ingest "
              "produced overlapping (cik, period_end). Stopping after labels.")
        return

    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    split = max(df["period_end"])  # active model trains on the full labeled history

    _hr(f"6/9  Training the active model (version {version})")
    bundle, metrics = train_all(df, split, version=version)
    path = save_model(bundle, DEFAULT_ARTIFACT)
    save_model_registry(
        version=version, artifact_path=path, feature_list=bundle["feature_columns"],
        train_window={"split_date": split, "n_train": metrics["n_train"], "n_test": metrics["n_test"]},
        metrics=metrics,
    )
    print(f"  saved model → {path}")

    # Walk-forward vintages (point-in-time models for the event backtest).
    cutoffs = _split_dates(list(df["period_end"]))
    _hr(f"7/9  Training walk-forward vintages at {cutoffs}")
    # Clear stale vintages first: a prior run's cutoffs (from older data/roster) would
    # otherwise linger in the dir and be picked by select_vintage in the backtest.
    if VINTAGE_DIR.exists():
        for old in VINTAGE_DIR.glob("*.joblib"):
            old.unlink()
    try:
        vintages = train_vintages(df, cutoffs, out_dir=str(VINTAGE_DIR))
        print(f"  wrote {len(vintages)} vintages → {VINTAGE_DIR}")
    except Exception as e:
        print(f"  ! vintage training skipped: {e}")

    _hr("8/9  Deriving model-learned stress-score weights + writing predictions")
    try:
        save_score_config(derive_score_config(bundle))
        print("  saved model-learned score_config")
    except Exception as e:
        print(f"  ! score-config derivation skipped: {e}")
    try:
        # per_agency=True so each issuer-period carries one row per covering agency;
        # predict._iter_predict_rows combines them (noisy-OR) into the issuer-level
        # "any-agency deterioration" probability that is stored/served.
        n = predict_and_store(bundle, build_scoring_matrix(per_agency=True))
        print(f"  wrote {n} migration_predictions rows")
    except Exception as e:
        print(f"  ! prediction step failed: {e}")

    _hr("9/9  Walk-forward evaluation (scorecard)")
    try:
        eval_result = walk_forward_eval(df, cutoffs)
        EVAL_PATH.write_text(json.dumps({"migration": eval_result}, indent=2))
        print(f"  wrote scorecard → {EVAL_PATH}")
    except Exception as e:
        print(f"  ! walk-forward eval skipped: {e}")

    _hr("Done")
    print("The portfolio now shows model P(up)/P(down); issuer pages overlay the "
          "agency rating and headline the prediction.")


if __name__ == "__main__":
    main()
