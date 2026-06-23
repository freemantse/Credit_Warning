"""
Rating-migration model (Stages 2–3).

  features.py — assemble the point-in-time, lookahead-free training/scoring matrix
                by joining rating_labels (targets) to the ratio / implied-rating /
                outlook features. (Stage 2 — buildable now.)

Stage 3 (train.py / predict.py / evaluate.py) adds the calibrated, monotonic
LightGBM heads + logistic baseline + SHAP explanations + walk-forward backtest;
those train once real agency-rating data is ingested.
"""
