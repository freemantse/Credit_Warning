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
  value             DOUBLE PRECISION,              -- NULL when the ratio couldn't be computed
  inputs_json       JSONB NOT NULL DEFAULT '{}',   -- raw dollar inputs used (subset that resolved, if missing)
  source_tags_json  JSONB NOT NULL DEFAULT '{}',   -- winning XBRL tag per resolved input
  missing_json      JSONB,                         -- NULL if computed; else {missing_inputs:[...], reason:str}
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
  source_url     TEXT NOT NULL DEFAULT '',         -- EDGAR doc URL for deep-link traceability
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (cik, period_end, concern, evidence_quote)
);


-- ── debt_maturities ──────────────────────────────────────────────────────────
-- Deterministic long-term-debt maturity schedule, one row per bucket per
-- (cik, period_end). Sourced entirely from XBRL (see extract.debt_maturity_schedule),
-- so each row carries the winning us-gaap tag for audit. `bucket` is one of
-- "y1".."y5" or "thereafter".
CREATE TABLE IF NOT EXISTS debt_maturities (
  cik         TEXT NOT NULL,
  period_end  TEXT NOT NULL,                       -- fiscal year-end, e.g. "2023-09-30"
  bucket      TEXT NOT NULL,                        -- "y1".."y5" | "thereafter"
  value       DOUBLE PRECISION NOT NULL,            -- principal due in that bucket
  source_tag  TEXT NOT NULL DEFAULT '',             -- winning XBRL tag
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (cik, period_end, bucket)
);


-- ── covenants ────────────────────────────────────────────────────────────────
-- LLM-extracted maintenance covenants from the debt footnote. Numeric fields are
-- nullable (kept only when the number appears verbatim in evidence_quote); a row
-- may carry just a qualitative near_limit flag plus the quote.
CREATE TABLE IF NOT EXISTS covenants (
  id              BIGSERIAL PRIMARY KEY,
  cik             TEXT NOT NULL,
  period_end      TEXT NOT NULL,
  covenant_type   TEXT NOT NULL,                    -- max_leverage | min_coverage | min_net_worth | other
  threshold       DOUBLE PRECISION,                 -- the limit, if reliably parsed
  direction       TEXT NOT NULL CHECK (direction IN ('max', 'min')),
  reported_actual DOUBLE PRECISION,                 -- current level, if disclosed
  near_limit      BOOLEAN NOT NULL DEFAULT FALSE,   -- sits close to the limit
  evidence_quote  TEXT NOT NULL,                    -- verbatim quote from the filing
  source          TEXT NOT NULL,                    -- e.g. "10-K 2023-09-30, Debt"
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (cik, period_end, covenant_type, evidence_quote)
);


-- ── loss_provisions ────────────────────────────────────────────────────────────
-- LLM-extracted litigation/contingency provisions from the commitments &
-- contingencies footnote. provision_amount is nullable (verbatim-backed only).
CREATE TABLE IF NOT EXISTS loss_provisions (
  id               BIGSERIAL PRIMARY KEY,
  cik              TEXT NOT NULL,
  period_end       TEXT NOT NULL,
  matter           TEXT NOT NULL,                   -- short label of the matter
  provision_amount DOUBLE PRECISION,                -- accrued amount, if reliably parsed
  is_material      BOOLEAN NOT NULL DEFAULT FALSE,
  qualitative_flag TEXT NOT NULL DEFAULT '',        -- e.g. "reasonably possible loss, not accrued"
  evidence_quote   TEXT NOT NULL,                   -- verbatim quote from the filing
  source           TEXT NOT NULL,                   -- e.g. "10-K 2023-09-30, Contingencies"
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (cik, period_end, matter, evidence_quote)
);


-- ── Indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_ratios_cik         ON ratios (cik);
CREATE INDEX IF NOT EXISTS idx_ratios_cik_period  ON ratios (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_findings_cik       ON llm_findings (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_maturities_cik     ON debt_maturities (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_covenants_cik      ON covenants (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_provisions_cik     ON loss_provisions (cik, period_end);
-- GIN index so ticker → cik lookups (companies WHERE tickers @> '["AAPL"]') stay fast.
CREATE INDEX IF NOT EXISTS idx_companies_tickers  ON companies USING GIN (tickers);


-- ── Row-Level Security ───────────────────────────────────────────────────────
-- Reads use the public anon key (browser); writes go through the service-role
-- key (Python backend), which bypasses RLS automatically. So we only need
-- public SELECT policies — no INSERT/UPDATE/DELETE policies for the anon key.
ALTER TABLE companies       ENABLE ROW LEVEL SECURITY;
ALTER TABLE ratios          ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_findings    ENABLE ROW LEVEL SECURITY;
ALTER TABLE debt_maturities ENABLE ROW LEVEL SECURITY;
ALTER TABLE covenants       ENABLE ROW LEVEL SECURITY;
ALTER TABLE loss_provisions ENABLE ROW LEVEL SECURITY;

-- DROP-then-CREATE makes the policy block re-runnable (CREATE POLICY has no
-- IF NOT EXISTS, so a bare re-run would otherwise fail with "already exists").
DROP POLICY IF EXISTS "Public read companies"    ON companies;
CREATE POLICY "Public read companies"    ON companies    FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read ratios"       ON ratios;
CREATE POLICY "Public read ratios"       ON ratios       FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read llm_findings" ON llm_findings;
CREATE POLICY "Public read llm_findings" ON llm_findings FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read debt_maturities" ON debt_maturities;
CREATE POLICY "Public read debt_maturities" ON debt_maturities FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read covenants" ON covenants;
CREATE POLICY "Public read covenants" ON covenants FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read loss_provisions" ON loss_provisions;
CREATE POLICY "Public read loss_provisions" ON loss_provisions FOR SELECT USING (true);