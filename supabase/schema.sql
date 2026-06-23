-- ============================================================================
-- Credit Warning System — Supabase Schema
-- ----------------------------------------------------------------------------
-- Run this in the Supabase SQL Editor: https://app.supabase.com → SQL Editor
--
-- Safe to run on a fresh project, and safe to re-run: every statement is
-- idempotent (CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT EXISTS, idempotent
-- upserts, plus DROP POLICY IF EXISTS before each CREATE POLICY), so re-applying
-- never errors.
--
-- This file consolidates what used to be three scripts, organized into stages:
--   Stage 0 — Core           companies, ratios, llm_findings, debt_maturities,
--                            covenants, loss_provisions, bond_instruments, cases,
--                            score_config, implied_ratings
--   Stage 1 — Ratings layer  rating_scale (+ seed), agency_ratings, rating_labels,
--                            and the lseg_permid / lseg_ric crosswalk columns on companies
--   Stage 3 — Migration model migration_predictions, model_registry
-- Tables are grouped by stage below; Indexes and Row-Level Security are each
-- consolidated into a single section at the end.
--
-- Identity model
--   The CIK (SEC Central Index Key) is the canonical key for every company.
--   It is assigned once and never changes, unlike tickers and company names,
--   which change across rebrands, ticker swaps, and reincorporations. Ticker
--   and name are therefore stored as mutable *attributes* on the `companies`
--   table, never as primary keys. The LSEG PermID / RIC are likewise mutable
--   attributes resolved to a CIK (see src/ratings/crosswalk.py).
--
-- Ratings invariants (mirror src/rating.py and src/ratings/):
--   * Dates are TEXT "YYYY-MM-DD".
--   * rating_index: 0 = AAA … 21 = D, higher = worse. SAME axis across
--     implied_ratings, rating_scale, agency_ratings, and rating_labels, so
--     implied and agency ratings compare directly.
--   * IG/HY boundary: index 9 (BBB-/Baa3, IG) → 10 (BB+/Ba1, HY).
--   * Migration sign convention (index space, higher = worse):
--       notch_change = rating_index(later) − rating_index(period_end)
--       → POSITIVE = DOWNGRADE, negative = upgrade.
-- ============================================================================


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ STAGE 0 — CORE TABLES                                                      ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

-- ── companies ────────────────────────────────────────────────────────────────
-- Canonical company identity, keyed on the permanent CIK.
-- name / tickers / former_names are refreshable snapshots from the EDGAR
-- submissions JSON (see src.ingest.get_company_info). lseg_permid / lseg_ric are
-- the LSEG identifiers resolved to this CIK (see src/ratings/crosswalk.py).
CREATE TABLE IF NOT EXISTS companies (
  cik           TEXT PRIMARY KEY,                  -- zero-padded 10-digit, e.g. "0000320193"
  name          TEXT NOT NULL DEFAULT '',          -- current legal/display name
  tickers       JSONB NOT NULL DEFAULT '[]',       -- current ticker symbol(s)
  exchanges     JSONB NOT NULL DEFAULT '[]',       -- exchanges the tickers trade on
  former_names  JSONB NOT NULL DEFAULT '[]',       -- [{name, from, to}] prior names
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),-- when this snapshot was last refreshed
  last_refreshed TIMESTAMPTZ,                       -- when this issuer was last re-tracked from EDGAR (NULL = never; auto-refresh cron picks NULLs first)
  lseg_permid   TEXT,                              -- LSEG PermID resolved to this CIK
  lseg_ric      TEXT                               -- LSEG RIC resolved to this CIK
);

-- Additive migrations for projects created before these columns existed.
-- Safe to re-run (IF NOT EXISTS); existing rows stay NULL. The auto-refresh cron
-- orders by last_refreshed ascending (NULLs first), so legacy rows refresh first.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_refreshed TIMESTAMPTZ;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS lseg_permid    TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS lseg_ric       TEXT;


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


-- ── bond_instruments ─────────────────────────────────────────────────────────
-- LLM-extracted individual debt instruments + their seniority, from the debt
-- footnote (see footnote_review.extract_bond_instruments). Drives issue-level
-- notching (rating.notch_instrument) and the senior-secured screen. Numeric fields
-- are nullable (verbatim-backed only). Same anti-hallucination contract as covenants.
CREATE TABLE IF NOT EXISTS bond_instruments (
  id               BIGSERIAL PRIMARY KEY,
  cik              TEXT NOT NULL,
  period_end       TEXT NOT NULL,
  instrument_name  TEXT NOT NULL,                   -- e.g. "5.25% Senior Secured Notes due 2027"
  seniority        TEXT NOT NULL CHECK (seniority IN ('senior_secured', 'senior_unsecured', 'subordinated', 'other')),
  principal_amount DOUBLE PRECISION,                -- face/principal in dollars, if quoted
  coupon           DOUBLE PRECISION,                -- coupon rate %, if quoted
  maturity_year    INTEGER,                         -- year of maturity, if quoted
  evidence_quote   TEXT NOT NULL,                   -- verbatim quote from the filing
  source           TEXT NOT NULL,                   -- e.g. "10-K 2023-09-30, Debt"
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (cik, period_end, instrument_name, evidence_quote)
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


-- ── implied_ratings ──────────────────────────────────────────────────────────
-- S&P-style implied credit rating per (cik, period_end), derived deterministically
-- from the stored ratios (see src.rating.compute_implied_rating). One row per
-- period — a separate table from `ratios` because a rating is one categorical
-- result per period, not one value per ratio_name. subscores_json carries the
-- per-sub-factor audit trail (which ratio fed each, the band it landed in).
CREATE TABLE IF NOT EXISTS implied_ratings (
  cik                    TEXT NOT NULL,
  period_end             TEXT NOT NULL,                  -- fiscal year-end, e.g. "2023-09-30"
  implied_rating         TEXT NOT NULL,                  -- rating letter, e.g. "BBB-"
  rating_index           INTEGER NOT NULL,               -- position in RATING_SCALE (0 = AAA)
  financial_risk_profile TEXT NOT NULL,                  -- e.g. "Intermediate"
  financial_risk_index   INTEGER NOT NULL,               -- 1..6 (1 = Minimal)
  business_risk_index    INTEGER NOT NULL,               -- 1..6 (1 = Excellent); default until supplied
  subscores_json         JSONB NOT NULL DEFAULT '{}',    -- {sub_factor: {value, profile, source_ratio, overridden}}
  notes_json             JSONB NOT NULL DEFAULT '[]',    -- human-readable explanation lines
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (cik, period_end)
);


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ STAGE 1 — RATINGS LAYER                                                    ║
-- ║ Real-agency-rating layer used as ground-truth labels + features for the    ║
-- ║ rating-migration model. (companies crosswalk columns live in Stage 0.)     ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

-- ── rating_scale ─────────────────────────────────────────────────────────────
-- Canonical notch lookup: rating_index ↔ S&P/Fitch notation ↔ Moody's notation ↔
-- grade. The single source of truth for the index↔notation mapping on the SQL
-- side; src/ratings/scale.py holds the matching mapping for the Python pipeline.
CREATE TABLE IF NOT EXISTS rating_scale (
  rating_index INTEGER PRIMARY KEY,        -- 0 = AAA … 21 = D
  sp_fitch     TEXT NOT NULL,              -- S&P / Fitch notation, e.g. "BBB-"
  moody        TEXT,                       -- Moody's notation, e.g. "Baa3" (NULL where none, e.g. D)
  grade        TEXT NOT NULL CHECK (grade IN ('IG', 'HY', 'D'))
);

-- Seed (mirror of src.ratings.scale.RATING_SCALE_ROWS). Idempotent upsert.
INSERT INTO rating_scale (rating_index, sp_fitch, moody, grade) VALUES
  (0,  'AAA',  'Aaa',  'IG'),
  (1,  'AA+',  'Aa1',  'IG'),
  (2,  'AA',   'Aa2',  'IG'),
  (3,  'AA-',  'Aa3',  'IG'),
  (4,  'A+',   'A1',   'IG'),
  (5,  'A',    'A2',   'IG'),
  (6,  'A-',   'A3',   'IG'),
  (7,  'BBB+', 'Baa1', 'IG'),
  (8,  'BBB',  'Baa2', 'IG'),
  (9,  'BBB-', 'Baa3', 'IG'),
  (10, 'BB+',  'Ba1',  'HY'),
  (11, 'BB',   'Ba2',  'HY'),
  (12, 'BB-',  'Ba3',  'HY'),
  (13, 'B+',   'B1',   'HY'),
  (14, 'B',    'B2',   'HY'),
  (15, 'B-',   'B3',   'HY'),
  (16, 'CCC+', 'Caa1', 'HY'),
  (17, 'CCC',  'Caa2', 'HY'),
  (18, 'CCC-', 'Caa3', 'HY'),
  (19, 'CC',   'Ca',   'HY'),
  (20, 'C',    'C',    'HY'),
  (21, 'D',    NULL,   'D')
ON CONFLICT (rating_index) DO UPDATE
  SET sp_fitch = EXCLUDED.sp_fitch, moody = EXCLUDED.moody, grade = EXCLUDED.grade;


-- ── agency_ratings ───────────────────────────────────────────────────────────
-- Source of truth, EVENT-grain: one row per rating ACTION per agency per issuer.
-- rating_index is NULL for non-notch statuses (withdrawn / not_rated); the status
-- is captured in rating_status. Raw LSEG ids are retained per row for audit.
CREATE TABLE IF NOT EXISTS agency_ratings (
  cik            TEXT NOT NULL,
  agency         TEXT NOT NULL CHECK (agency IN ('MDY', 'FTC', 'SPI')),
  effective_date TEXT NOT NULL,            -- "YYYY-MM-DD" the action took effect
  rating_index   INTEGER,                  -- 0..21; NULL for withdrawn / not_rated
  rating_raw     TEXT,                     -- raw agency notation as pulled (audit)
  rating_status  TEXT NOT NULL CHECK (rating_status IN ('rated', 'withdrawn', 'not_rated', 'default')),
  rating_action  TEXT,                     -- new | upgrade | downgrade | affirm | withdrawn | default
  source_permid  TEXT,                     -- LSEG PermID this row was sourced from
  source_ric     TEXT,                     -- LSEG RIC this row was sourced from
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (cik, agency, effective_date)
);


-- ── rating_labels ────────────────────────────────────────────────────────────
-- ML TARGET table, grain (cik, period_end, agency) to join 1:1 with ratios /
-- implied_ratings. Frozen as a TABLE (not a view) so the as-of and forward windows
-- are reproducible and lookahead-free.
--
-- Sign convention (index space, higher = worse):
--   notch_change_Nm = rating_index(period_end + N) − rating_index(period_end)
--                     → POSITIVE = DOWNGRADE, negative = upgrade.
--   label_Nm        = sign(notch_change_Nm) ∈ {-1, 0, +1}
--                     → +1 = DOWNGRADE, -1 = upgrade, 0 = stable.
--   Any horizon whose window extends past the dataset's last date, or where the
--   as-of / forward rating is unknown, is left NULL (right-edge censoring — not a
--   fabricated "stable").
CREATE TABLE IF NOT EXISTS rating_labels (
  cik              TEXT NOT NULL,
  period_end       TEXT NOT NULL,          -- a financial period_end from ratios/implied_ratings
  agency           TEXT NOT NULL CHECK (agency IN ('MDY', 'FTC', 'SPI')),
  rating_index     INTEGER,                -- rating as of period_end (NULL if unrated then)
  rating_index_3m  INTEGER,
  rating_index_6m  INTEGER,
  rating_index_12m INTEGER,
  label_3m         INTEGER CHECK (label_3m  IN (-1, 0, 1)),
  label_6m         INTEGER CHECK (label_6m  IN (-1, 0, 1)),
  label_12m        INTEGER CHECK (label_12m IN (-1, 0, 1)),
  notch_change_12m INTEGER,                -- signed; + = downgrade
  default_12m      BOOLEAN NOT NULL DEFAULT FALSE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (cik, period_end, agency)
);


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ STAGE 3 — RATING-MIGRATION MODEL                                           ║
-- ║ Both tables are WRITTEN OFFLINE (by the src/model CLI trainer/predictor)   ║
-- ║ and READ by the API/screen — the model never runs in the serverless hot    ║
-- ║ path.                                                                       ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

-- ── migration_predictions ────────────────────────────────────────────────────
-- Calibrated P(downgrade)/P(upgrade)/P(default) per (issuer, period, horizon),
-- with the top signed drivers of p_downgrade.
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
-- The single active model's metadata + walk-forward metrics (one row, id = 'active',
-- like score_config). The fitted estimator itself is a joblib artifact on disk
-- (artifact_path); this row is the provenance/metrics the UI shows and the
-- predictor loads by version.
CREATE TABLE IF NOT EXISTS model_registry (
  id             TEXT PRIMARY KEY,            -- always 'active'
  version        TEXT NOT NULL,               -- e.g. "2024-06-23T..." or a content hash
  artifact_path  TEXT NOT NULL DEFAULT '',    -- on-disk joblib path of the fitted model
  feature_list   JSONB NOT NULL DEFAULT '[]', -- ordered feature columns the model expects
  train_window   JSONB NOT NULL DEFAULT '{}', -- {split_date, n_train, n_test, …}
  metrics_json   JSONB NOT NULL DEFAULT '{}', -- per-head PR-AUC / recall@k / calibration …
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ INDEXES                                                                    ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

-- Stage 0 — Core
CREATE INDEX IF NOT EXISTS idx_ratios_cik           ON ratios (cik);
CREATE INDEX IF NOT EXISTS idx_ratios_cik_period    ON ratios (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_findings_cik         ON llm_findings (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_maturities_cik       ON debt_maturities (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_covenants_cik        ON covenants (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_provisions_cik       ON loss_provisions (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_bond_instruments_cik ON bond_instruments (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_cases_cik            ON cases (cik);
CREATE INDEX IF NOT EXISTS idx_implied_ratings_cik  ON implied_ratings (cik, period_end);
-- GIN index so ticker → cik lookups (companies WHERE tickers @> '["AAPL"]') stay fast.
CREATE INDEX IF NOT EXISTS idx_companies_tickers    ON companies USING GIN (tickers);

-- Stage 1 — Ratings layer
CREATE INDEX IF NOT EXISTS idx_agency_ratings_cik    ON agency_ratings (cik, agency, effective_date);
CREATE INDEX IF NOT EXISTS idx_rating_labels_cik     ON rating_labels (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_companies_lseg_permid ON companies (lseg_permid);
CREATE INDEX IF NOT EXISTS idx_companies_lseg_ric    ON companies (lseg_ric);

-- Stage 3 — Migration model
CREATE INDEX IF NOT EXISTS idx_migration_predictions_cik ON migration_predictions (cik, period_end);


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ ROW-LEVEL SECURITY                                                         ║
-- ║ Reads use the public anon key (browser); writes go through the             ║
-- ║ service-role key (Python backend), which bypasses RLS automatically. So we ║
-- ║ only need public SELECT policies — no INSERT/UPDATE/DELETE policies for     ║
-- ║ the anon key. DROP-then-CREATE makes each policy re-runnable (CREATE POLICY ║
-- ║ has no IF NOT EXISTS, so a bare re-run would otherwise fail).               ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

-- Enable RLS
ALTER TABLE companies             ENABLE ROW LEVEL SECURITY;
ALTER TABLE ratios                ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_findings          ENABLE ROW LEVEL SECURITY;
ALTER TABLE debt_maturities       ENABLE ROW LEVEL SECURITY;
ALTER TABLE covenants             ENABLE ROW LEVEL SECURITY;
ALTER TABLE loss_provisions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE bond_instruments      ENABLE ROW LEVEL SECURITY;
ALTER TABLE cases                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE score_config          ENABLE ROW LEVEL SECURITY;
ALTER TABLE implied_ratings       ENABLE ROW LEVEL SECURITY;
ALTER TABLE rating_scale          ENABLE ROW LEVEL SECURITY;
ALTER TABLE agency_ratings        ENABLE ROW LEVEL SECURITY;
ALTER TABLE rating_labels         ENABLE ROW LEVEL SECURITY;
ALTER TABLE migration_predictions ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_registry        ENABLE ROW LEVEL SECURITY;

-- Public read policies
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

DROP POLICY IF EXISTS "Public read bond_instruments" ON bond_instruments;
CREATE POLICY "Public read bond_instruments" ON bond_instruments FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read cases" ON cases;
CREATE POLICY "Public read cases" ON cases FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read score_config" ON score_config;
CREATE POLICY "Public read score_config" ON score_config FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read implied_ratings" ON implied_ratings;
CREATE POLICY "Public read implied_ratings" ON implied_ratings FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read rating_scale" ON rating_scale;
CREATE POLICY "Public read rating_scale" ON rating_scale FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read agency_ratings" ON agency_ratings;
CREATE POLICY "Public read agency_ratings" ON agency_ratings FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read rating_labels" ON rating_labels;
CREATE POLICY "Public read rating_labels" ON rating_labels FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read migration_predictions" ON migration_predictions;
CREATE POLICY "Public read migration_predictions" ON migration_predictions FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read model_registry" ON model_registry;
CREATE POLICY "Public read model_registry" ON model_registry FOR SELECT USING (true);
