-- ============================================================================
-- Credit Warning System — Supabase Schema
-- ----------------------------------------------------------------------------
-- Run this in the Supabase SQL Editor: https://app.supabase.com → SQL Editor
--
-- Safe to run on a fresh project, and safe to re-run: every statement is
-- idempotent (CREATE ... IF NOT EXISTS, plus DROP POLICY IF EXISTS before each
-- CREATE POLICY), so re-applying never errors.
--
-- Identity model
--   The CIK (SEC Central Index Key) is the canonical key for every company.
--   It is assigned once and never changes, unlike tickers and company names,
--   which change across rebrands, ticker swaps, and reincorporations. Ticker
--   and name are therefore stored as mutable *attributes* on the `companies`
--   table, never as primary keys.
-- ============================================================================


-- ── companies ────────────────────────────────────────────────────────────────
-- Canonical company identity, keyed on the permanent CIK.
-- name / tickers / former_names are refreshable snapshots from the EDGAR
-- submissions JSON (see src.ingest.get_company_info).
CREATE TABLE IF NOT EXISTS companies (
  cik           TEXT PRIMARY KEY,                  -- zero-padded 10-digit, e.g. "0000320193"
  name          TEXT NOT NULL DEFAULT '',          -- current legal/display name
  tickers       JSONB NOT NULL DEFAULT '[]',       -- current ticker symbol(s)
  exchanges     JSONB NOT NULL DEFAULT '[]',       -- exchanges the tickers trade on
  former_names  JSONB NOT NULL DEFAULT '[]',       -- [{name, from, to}] prior names
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW() -- when this snapshot was last refreshed
);


-- ── ratios ───────────────────────────────────────────────────────────────────
-- Deterministic ratio results, one row per (cik, period_end, ratio_name).
CREATE TABLE IF NOT EXISTS ratios (
  cik               TEXT NOT NULL,
  period_end        TEXT NOT NULL,                 -- fiscal year-end, e.g. "2023-09-30"
  ratio_name        TEXT NOT NULL,                 -- e.g. "leverage", "free_cash_flow"
  value             DOUBLE PRECISION NOT NULL,
  inputs_json       JSONB NOT NULL DEFAULT '{}',   -- raw dollar inputs used in the formula
  source_tags_json  JSONB NOT NULL DEFAULT '{}',   -- winning XBRL tag per input
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (cik, period_end, ratio_name)
);


-- ── llm_findings ─────────────────────────────────────────────────────────────
-- LLM qualitative findings, one row per distinct concern+quote per (cik, period).
CREATE TABLE IF NOT EXISTS llm_findings (
  id             BIGSERIAL PRIMARY KEY,
  cik            TEXT NOT NULL,
  period_end     TEXT NOT NULL,
  concern        TEXT NOT NULL,                    -- qualitative issue label
  severity       TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
  evidence_quote TEXT NOT NULL,                    -- verbatim quote from the filing
  source         TEXT NOT NULL,                    -- e.g. "10-K 2023-12-31, MD&A"
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (cik, period_end, concern, evidence_quote)
);


-- ── Indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_ratios_cik         ON ratios (cik);
CREATE INDEX IF NOT EXISTS idx_ratios_cik_period  ON ratios (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_findings_cik       ON llm_findings (cik, period_end);
-- GIN index so ticker → cik lookups (companies WHERE tickers @> '["AAPL"]') stay fast.
CREATE INDEX IF NOT EXISTS idx_companies_tickers  ON companies USING GIN (tickers);


-- ── Row-Level Security ───────────────────────────────────────────────────────
-- Reads use the public anon key (browser); writes go through the service-role
-- key (Python backend), which bypasses RLS automatically. So we only need
-- public SELECT policies — no INSERT/UPDATE/DELETE policies for the anon key.
ALTER TABLE companies    ENABLE ROW LEVEL SECURITY;
ALTER TABLE ratios       ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_findings ENABLE ROW LEVEL SECURITY;

-- DROP-then-CREATE makes the policy block re-runnable (CREATE POLICY has no
-- IF NOT EXISTS, so a bare re-run would otherwise fail with "already exists").
DROP POLICY IF EXISTS "Public read companies"    ON companies;
CREATE POLICY "Public read companies"    ON companies    FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read ratios"       ON ratios;
CREATE POLICY "Public read ratios"       ON ratios       FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read llm_findings" ON llm_findings;
CREATE POLICY "Public read llm_findings" ON llm_findings FOR SELECT USING (true);


-- ============================================================================
-- OPTIONAL — destructive reset
-- ----------------------------------------------------------------------------
-- Only needed when migrating from the OLD ticker-keyed schema (where `ratios`
-- and `llm_findings` were keyed on `ticker`). There is no pure-SQL way to
-- backfill CIKs — resolution requires an EDGAR lookup — so the migration path
-- is: drop the old tables, re-run the CREATE statements above, then re-track
-- each issuer once (EDGAR responses are cached, so re-tracking is fast).
--
-- To use: uncomment the three lines below, run them FIRST, then run the rest
-- of this file. THIS DELETES ALL STORED DATA.
--
-- DROP TABLE IF EXISTS ratios;
-- DROP TABLE IF EXISTS llm_findings;
-- DROP TABLE IF EXISTS companies;
-- ============================================================================
