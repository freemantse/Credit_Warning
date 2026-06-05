-- Credit Warning System — Supabase Schema
-- Run this in the Supabase SQL Editor: https://app.supabase.com → SQL Editor

-- Stores deterministic ratio results per (ticker, period)
CREATE TABLE IF NOT EXISTS ratios (
  ticker            TEXT NOT NULL,
  period_end        TEXT NOT NULL,
  ratio_name        TEXT NOT NULL,
  value             DOUBLE PRECISION NOT NULL,
  inputs_json       JSONB NOT NULL DEFAULT '{}',
  source_tags_json  JSONB NOT NULL DEFAULT '{}',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (ticker, period_end, ratio_name)
);

-- Stores LLM qualitative findings per (ticker, period)
CREATE TABLE IF NOT EXISTS llm_findings (
  id             BIGSERIAL PRIMARY KEY,
  ticker         TEXT NOT NULL,
  period_end     TEXT NOT NULL,
  concern        TEXT NOT NULL,
  severity       TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
  evidence_quote TEXT NOT NULL,
  source         TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (ticker, period_end, concern, evidence_quote)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_ratios_ticker       ON ratios (ticker);
CREATE INDEX IF NOT EXISTS idx_ratios_ticker_period ON ratios (ticker, period_end);
CREATE INDEX IF NOT EXISTS idx_findings_ticker     ON llm_findings (ticker, period_end);

-- Row-Level Security
ALTER TABLE ratios       ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_findings ENABLE ROW LEVEL SECURITY;

-- Allow public read access (frontend uses anon key for reads)
CREATE POLICY "Public read ratios"
  ON ratios FOR SELECT USING (true);

CREATE POLICY "Public read llm_findings"
  ON llm_findings FOR SELECT USING (true);

-- Service role (used by Python backend) bypasses RLS automatically.
-- No INSERT/UPDATE/DELETE policies needed for the anon key.
