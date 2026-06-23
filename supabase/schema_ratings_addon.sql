-- ============================================================================
-- Credit Warning System — Ratings Data Workstream add-on (Stage 1)
-- ----------------------------------------------------------------------------
-- Run this in the Supabase SQL Editor AFTER the base supabase/schema.sql.
--
-- Adds the real-agency-rating layer used as ground-truth labels + features for
-- the rating-migration model: a notch lookup (rating_scale), the event-grain
-- source of truth (agency_ratings), and the frozen ML target table
-- (rating_labels). Plus two crosswalk columns on companies.
--
-- Idempotent and re-runnable, matching the base schema's conventions
-- (CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT EXISTS, DROP POLICY before CREATE).
--
-- Invariants (mirror src/rating.py and src/ratings/):
--   * CIK is canonical; tickers/PermID/RIC are mutable attributes.
--   * Dates are TEXT "YYYY-MM-DD".
--   * rating_index: 0 = AAA … 21 = D, higher = worse. SAME axis as
--     implied_ratings.rating_index, so implied and agency ratings compare directly.
--   * IG/HY boundary: index 9 (BBB-/Baa3, IG) → 10 (BB+/Ba1, HY).
-- ============================================================================


-- ── companies: crosswalk columns ─────────────────────────────────────────────
-- The LSEG identifiers resolved to this CIK (see src/ratings/crosswalk.py).
ALTER TABLE companies ADD COLUMN IF NOT EXISTS lseg_permid TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS lseg_ric    TEXT;


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


-- ── Indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_agency_ratings_cik   ON agency_ratings (cik, agency, effective_date);
CREATE INDEX IF NOT EXISTS idx_rating_labels_cik     ON rating_labels (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_companies_lseg_permid ON companies (lseg_permid);
CREATE INDEX IF NOT EXISTS idx_companies_lseg_ric    ON companies (lseg_ric);


-- ── Row-Level Security (public read, service-role writes — like the base schema) ─
ALTER TABLE rating_scale    ENABLE ROW LEVEL SECURITY;
ALTER TABLE agency_ratings  ENABLE ROW LEVEL SECURITY;
ALTER TABLE rating_labels   ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read rating_scale" ON rating_scale;
CREATE POLICY "Public read rating_scale" ON rating_scale FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read agency_ratings" ON agency_ratings;
CREATE POLICY "Public read agency_ratings" ON agency_ratings FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read rating_labels" ON rating_labels;
CREATE POLICY "Public read rating_labels" ON rating_labels FOR SELECT USING (true);
