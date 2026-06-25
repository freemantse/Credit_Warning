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

-- ── covenants: Stage 2c additive extension (SUPABASE_LLM_SCHEMA.md §3) ──────────
-- Additive, all nullable/defaulted, so the existing 7-field inserts keep working.
-- The ported richer extractor populates the full set; near_limit/cushion/cushion_pct
-- are DERIVED IN CODE (not emitted by the LLM). covenant_type stays free TEXT
-- (no CHECK), so the 4->8 vocabulary widening needs no DDL.
-- NOTE: run this ALTER in the Supabase SQL Editor BEFORE the ported pass writes;
-- 2c-i golden-set validation is extraction-only and does NOT require it.
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS covenant_subtype   TEXT;     -- maintenance | incurrence | springing | negative | cross_default | min_liquidity
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS ratio_name         TEXT;     -- verbatim name, e.g. "Consolidated Net Leverage Ratio"
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS unit               TEXT;     -- ratio | usd | percent
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS testing_frequency  TEXT;     -- e.g. "quarterly", "at all times", "when availability < $X"
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS is_springing       BOOLEAN;  -- tested only when a trigger is hit
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS springing_trigger  TEXT;     -- the activating condition, if springing
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS step_down          TEXT;     -- step-down/step-up schedule if disclosed
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS is_maintenance     BOOLEAN;  -- TRUE = breach can trigger default; FALSE = incurrence-only
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS cushion            DOUBLE PRECISION;  -- DERIVED IN CODE
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS cushion_pct        DOUBLE PRECISION;  -- DERIVED IN CODE
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS section_confidence TEXT;     -- high (heading-anchored) | low (chunk fallback)
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS null_reason        TEXT;     -- why a nullable field is null (never guessed)

-- ── covenants: Stage 2c-iii additive extension (language-based near_limit) ──────
-- Restores LLM_COVENANT §7.2 condition 3 as a footnote-level grounded LLM judgment
-- (extract_covenant_breach). near_limit_reason distinguishes a language-set flag
-- ("waiver/breach disclosed") from a numeric one ("cushion" / "breach"). The
-- evidence_quote + section are the data foundation for the later on-demand evidence
-- button (verbatim breach/waiver sentence, distinct from the covenant's own
-- evidence_quote; the section it was disclosed in). All nullable/defaulted, so the
-- existing inserts keep working. RUN THIS in the Supabase SQL Editor BEFORE the
-- 2c-iii-enabled live pipeline writes here; golden-set validation is extraction-
-- only and does NOT require it.
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS near_limit_reason         TEXT;  -- "cushion" | "breach" (numeric) | "waiver/breach disclosed" (language)
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS near_limit_evidence_quote TEXT;  -- verbatim breach/waiver sentence (language path only)
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS near_limit_section        TEXT;  -- "Debt footnote" | "MD&A" (where the breach was disclosed)


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


-- ── cases ────────────────────────────────────────────────────────────────────
-- Backtest case library: the roster of distressed issuers (with their
-- bankruptcy/Chapter-11 date) and healthy controls (with a pinned anchor date)
-- that the point-in-time backtest evaluates. Migrated out of data/cases.csv so
-- the roster can be edited from the UI. case_id is a human-readable stable slug
-- (e.g. "hertz-2020"); cik is the authoritative SEC identifier used to fetch
-- filings (delisted tickers still resolve via CIK). event_date is stored as TEXT
-- ("YYYY-MM-DD"), mirroring how the CSV / ratios.period_end carry dates.
CREATE TABLE IF NOT EXISTS cases (
  case_id       TEXT PRIMARY KEY,                  -- stable slug, e.g. "hertz-2020"
  company_name  TEXT NOT NULL DEFAULT '',          -- display name from EDGAR submissions
  ticker        TEXT NOT NULL DEFAULT '',          -- current/last ticker (may be blank for delisted)
  cik           TEXT NOT NULL,                     -- zero-padded 10-digit, authoritative id
  label         TEXT NOT NULL CHECK (label IN ('distressed', 'healthy')),
  event_date    TEXT,                              -- "YYYY-MM-DD"; Ch.11 date (distressed) or pinned anchor (healthy)
  notes         TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── score_config ─────────────────────────────────────────────────────────────
-- The single active stress-score parameter set (one row, id = 'active'). Holds
-- the full ScoreConfig dict (weights, ramp thresholds, caps, escalation). When
-- the row is absent, src.score.DEFAULT_CONFIG is used, which reproduces the
-- original hard-coded behavior. Edited and applied from the backtest UI's
-- "Scoring parameters" panel ("Apply to portfolio").
CREATE TABLE IF NOT EXISTS score_config (
  id          TEXT PRIMARY KEY,                    -- always 'active'
  config      JSONB NOT NULL,                      -- full ScoreConfig dict
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── Indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_ratios_cik         ON ratios (cik);
CREATE INDEX IF NOT EXISTS idx_ratios_cik_period  ON ratios (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_findings_cik       ON llm_findings (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_maturities_cik     ON debt_maturities (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_covenants_cik      ON covenants (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_provisions_cik     ON loss_provisions (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_cases_cik          ON cases (cik);
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
ALTER TABLE cases           ENABLE ROW LEVEL SECURITY;
ALTER TABLE score_config    ENABLE ROW LEVEL SECURITY;

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

DROP POLICY IF EXISTS "Public read cases" ON cases;
CREATE POLICY "Public read cases" ON cases FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read score_config" ON score_config;
CREATE POLICY "Public read score_config" ON score_config FOR SELECT USING (true);


-- ── going_concern ──────────────────────────────────────────────────────────────
-- LLM-extracted going-concern findings (LLM_EXTRACTOR_PORT Stage 2b;
-- SUPABASE_LLM_SCHEMA.md §4, §6). One row per finding. Tier 1 = formal
-- substantial-doubt language (high confidence); Tier 2 = soft survival-linked
-- precursor (low confidence, requires non-empty adverse_conditions).
-- NOTE: run this in the Supabase SQL Editor BEFORE the live pipeline writes here.
-- Golden-set validation is extraction-only and does NOT require this table.
CREATE TABLE IF NOT EXISTS going_concern (
  id                 BIGSERIAL PRIMARY KEY,
  cik                TEXT NOT NULL,
  period_end         TEXT NOT NULL,
  tier               INTEGER NOT NULL CHECK (tier IN (1, 2)),                  -- 1 = formal substantial-doubt | 2 = soft precursor
  confidence         TEXT NOT NULL CHECK (confidence IN ('high','low')),        -- high for tier 1, low for tier 2
  status             TEXT,                                                      -- in_compliance | breach | waiver_obtained | going_concern_doubt | not_disclosed
  going_concern_flag BOOLEAN NOT NULL DEFAULT FALSE,                            -- formal substantial-doubt present
  source_party       TEXT CHECK (source_party IN ('auditor','management')),     -- who expressed it
  doubt_alleviated   BOOLEAN,                                                   -- tier 1 only: doubt stated as alleviated by management's plans; NULL for tier 2
  adverse_conditions JSONB NOT NULL DEFAULT '[]',                              -- REQUIRED non-empty for tier 2; [] for tier 1
  description        TEXT,                                                      -- one-sentence summary
  evidence_quote     TEXT NOT NULL,                                             -- verbatim, contiguous span
  section            TEXT,                                                      -- auditor report / GC footnote / MD&A / risk factors
  section_confidence TEXT,                                                      -- high | low
  source             TEXT NOT NULL,                                             -- e.g. "10-K 2023-09-30, Auditor's Report"
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (cik, period_end, tier, evidence_quote)
);

CREATE INDEX IF NOT EXISTS idx_going_concern_cik ON going_concern (cik, period_end);

ALTER TABLE going_concern ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Public read going_concern" ON going_concern;
CREATE POLICY "Public read going_concern" ON going_concern FOR SELECT USING (true);