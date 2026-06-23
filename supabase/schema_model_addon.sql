-- ============================================================================
-- Credit Warning System — Rating-Migration Model add-on (Stage 3)
-- ----------------------------------------------------------------------------
-- Run in the Supabase SQL Editor AFTER the base schema + ratings add-on.
--
-- Two tables, both WRITTEN OFFLINE (by the src/model CLI trainer/predictor) and
-- READ by the API/screen — the model never runs in the serverless hot path:
--   migration_predictions — calibrated P(downgrade)/P(upgrade)/P(default) per
--                           (issuer, period, horizon), with the top signed drivers.
--   model_registry        — the single active model's provenance + metrics
--                           (one row, id = 'active'), like score_config.
--
-- Idempotent + RLS public-read, matching the base schema's conventions.
-- ============================================================================


-- ── migration_predictions ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_predictions (
  cik             TEXT NOT NULL,
  period_end      TEXT NOT NULL,
  horizon_months  INTEGER NOT NULL DEFAULT 12,
  p_downgrade     DOUBLE PRECISION,            -- calibrated P(rating worse within horizon)
  p_upgrade       DOUBLE PRECISION,            -- calibrated P(rating better within horizon)
  p_default       DOUBLE PRECISION,            -- calibrated P(default within horizon)
  drivers_json    JSONB NOT NULL DEFAULT '[]', -- top signed feature contributions to p_downgrade
  model_version   TEXT NOT NULL DEFAULT '',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (cik, period_end, horizon_months)
);


-- ── model_registry ───────────────────────────────────────────────────────────
-- The active model's metadata + walk-forward metrics. The fitted estimator itself
-- is a joblib artifact on disk (artifact_path); this row is the provenance/metrics
-- the UI shows and the predictor loads by version.
CREATE TABLE IF NOT EXISTS model_registry (
  id             TEXT PRIMARY KEY,            -- always 'active'
  version        TEXT NOT NULL,               -- e.g. "2024-06-23T..." or a content hash
  artifact_path  TEXT NOT NULL DEFAULT '',    -- on-disk joblib path of the fitted model
  feature_list   JSONB NOT NULL DEFAULT '[]', -- ordered feature columns the model expects
  train_window   JSONB NOT NULL DEFAULT '{}', -- {split_date, n_train, n_test, …}
  metrics_json   JSONB NOT NULL DEFAULT '{}', -- per-head PR-AUC / recall@k / calibration …
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── Indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_migration_predictions_cik ON migration_predictions (cik, period_end);


-- ── Row-Level Security (public read, service-role writes) ─────────────────────
ALTER TABLE migration_predictions ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_registry        ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read migration_predictions" ON migration_predictions;
CREATE POLICY "Public read migration_predictions" ON migration_predictions FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read model_registry" ON model_registry;
CREATE POLICY "Public read model_registry" ON model_registry FOR SELECT USING (true);
