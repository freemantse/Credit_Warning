# SUPABASE_LLM_SCHEMA.md — LLM Extraction Target Schema (Supabase)

**Status:** Schema design — the "Supabase first" foundation. Authoritative for the LLM port: the ported extractor and the rewritten LLM specs write against THIS schema. DDL is written in Freeman's existing conventions (idempotent `IF NOT EXISTS`, period_end keying, `BIGSERIAL` ids, `TIMESTAMPTZ`, RLS public-read, indexes) so it drops into his `supabase/schema.sql` cleanly.
**Scope (first port):** three tables — `covenants` (EXTEND his existing), `going_concern` (NEW), `llm_debt_maturities` (NEW). Loss-provisions / asset-composition / capex-split are deferred to a fast second wave.
**Do not apply to live Supabase until reviewed** and (for the `covenants` ALTER) until Freeman is given a heads-up.

---

## 1. The synthesis (what this schema is)

Decided: **adopt Freeman's architecture + your richer fields + the spec's improvements.** His structure is cleaner than the flattened SQLite (`llm_extractions`); your `Covenant`/`Compliance` *fields* are richer than his thin tables; the specs add cushion-in-code, the going-concern Tier-1/Tier-2 split, and `section_confidence`. This schema merges all three.

Inherited from Freeman's conventions (kept for every table here):
- **Per-row, not flattened** — one row per covenant / per going-concern finding / per maturity bucket.
- **Keyed on `period_end`** (not `accession`) — so LLM data joins natively to `ratios`, `score`, and the backtest, all of which are period-keyed.
- `BIGSERIAL` surrogate id + a natural `UNIQUE` key; `TIMESTAMPTZ created_at`; idempotent DDL; RLS enabled with a public-read policy; an index on `(cik, period_end)`.

---

## 2. Why covenants EXTENDS his table but maturity gets a NEW table

The distinction is **how many writers each table has after the port** (not arbitrary):

| Table | Writers after port | Decision |
|---|---|---|
| `covenants` | ONE — the ported richer extractor *replaces* his `extract_debt_footnote` covenant pass | EXTEND his table (additive `ADD COLUMN`) |
| `llm_debt_maturities` | TWO, permanently — his XBRL `debt_maturities` stays primary; LLM is a fallback writer | NEW separate table (no collision) |
| `going_concern` | ONE — he has no going-concern extraction today | NEW table |

> **Dependency flag:** "covenants extends his" assumes the deferred **Stage 2c** decision is *replace his covenant pass*. If 2c instead runs both his pass and the ported pass concurrently, two writers hit one table → revisit (either dedupe on the shared UNIQUE key, or split into `llm_covenants`). The additive `ADD COLUMN`s below are safe regardless (his pass keeps working; new columns default NULL); only the one-writer assumption is the open item.

---

## 3. Table 1 — `covenants` (EXTEND Freeman's existing)

His table stays; we add nullable columns. His existing covenant pass keeps inserting its 7 fields unaffected; the ported extractor populates the full set.

```sql
-- Additive extension of the existing covenants table. All new columns are
-- nullable (or have defaults), so existing inserts keep working unchanged.
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS covenant_subtype   TEXT;     -- maintenance | incurrence | springing | negative | cross_default | min_liquidity
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS ratio_name         TEXT;     -- verbatim name, e.g. "Consolidated Net Leverage Ratio"
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS unit               TEXT;     -- ratio | usd | percent
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS testing_frequency  TEXT;     -- e.g. "quarterly", "at all times", "when availability < $X"
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS is_springing       BOOLEAN;  -- tested only when a trigger is hit
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS springing_trigger  TEXT;     -- the activating condition, if springing
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS step_down          TEXT;     -- step-down/step-up schedule if disclosed
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS is_maintenance     BOOLEAN;  -- TRUE = breach can trigger default; FALSE = incurrence-only
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS cushion            DOUBLE PRECISION;  -- DERIVED IN CODE (§7 LLM_COVENANT); NOT extracted
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS cushion_pct        DOUBLE PRECISION;  -- DERIVED IN CODE
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS section_confidence TEXT;     -- high (heading-anchored) | low (chunk fallback)
ALTER TABLE covenants ADD COLUMN IF NOT EXISTS null_reason        TEXT;     -- why a nullable field is null (never guessed)
```

**His existing columns are reused as-is** (no change): `id, cik, period_end, covenant_type, threshold, direction, reported_actual, near_limit, evidence_quote, source, created_at`, and `UNIQUE (cik, period_end, covenant_type, evidence_quote)`.

**Behavioral changes the port introduces (no DDL, but contract changes):**
- `near_limit` becomes **DERIVED IN CODE** from `cushion` (spec §7.2: cushion_pct ≤ 10%, OR breach, OR waiver/amendment language) — no longer LLM-emitted. (His column already exists; the port just computes it instead of trusting the model.)
- `covenant_type` vocabulary widens 4 → 8. His column is free `TEXT` (no CHECK), so this needs **no DDL** — but the extractor/validator whitelist must accept the 8-value superset (see §6) or it will silently drop the new types.
- `direction` keeps his CHECK `IN ('max','min')` — the port must **normalize** your model's `"maximum"/"minimum"` → `"max"/"min"` at write time.

---

## 4. Table 2 — `going_concern` (NEW)

Merges your `Compliance` model (status enum + `going_concern_flag`) with the spec's Tier-1/Tier-2 design. One row per finding (per-row, like his other tables).

```sql
CREATE TABLE IF NOT EXISTS going_concern (
  id                 BIGSERIAL PRIMARY KEY,
  cik                TEXT NOT NULL,
  period_end         TEXT NOT NULL,
  tier               INTEGER NOT NULL CHECK (tier IN (1, 2)),         -- 1 = formal substantial-doubt | 2 = soft precursor
  confidence         TEXT NOT NULL CHECK (confidence IN ('high','low')),  -- high for tier 1, low for tier 2
  status             TEXT,                                            -- your enum: in_compliance | breach | waiver_obtained | going_concern_doubt | not_disclosed
  going_concern_flag BOOLEAN NOT NULL DEFAULT FALSE,                  -- formal substantial-doubt present
  source_party       TEXT CHECK (source_party IN ('auditor','management')),  -- who expressed it
  doubt_alleviated   BOOLEAN,                                         -- tier 1 only: doubt stated as alleviated by management's plans; NULL for tier 2
  adverse_conditions JSONB NOT NULL DEFAULT '[]',                     -- REQUIRED non-empty for tier 2 (the present conditions justifying it); [] for tier 1
  description        TEXT,                                            -- one-sentence summary (your Compliance.description)
  evidence_quote     TEXT NOT NULL,                                   -- verbatim, contiguous span
  section            TEXT,                                            -- which section it came from (auditor report / GC footnote / MD&A)
  section_confidence TEXT,                                            -- high | low
  source             TEXT NOT NULL,                                   -- e.g. "10-K 2023-09-30, Auditor's Report"
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (cik, period_end, tier, evidence_quote)
);
```

**Mapping from your `Compliance` model:**
- Your `going_concern_flag = TRUE` / `status = 'going_concern_doubt'` → a **Tier-1** row (`tier=1, confidence='high', going_concern_flag=TRUE`).
- The spec's **Tier-2** soft precursors (which your binary flag doesn't capture today) → `tier=2, confidence='low'`, with `adverse_conditions` populated — this is the early-warning value the spec adds on top of your extractor.
- Your `status` values `breach` / `waiver_obtained` are **covenant-compliance**, not going-concern — those feed the covenant table's `near_limit`/breach derivation (§3), NOT this table. Only `going_concern_doubt` and the soft precursors land here. (This split is the cleanup: your `Compliance` object currently bundles covenant-compliance and going-concern; the synthesis separates them to the right tables.)

---

## 5. Table 3 — `llm_debt_maturities` (NEW, fallback to his XBRL `debt_maturities`)

Separate table (Option B): his XBRL `debt_maturities` is untouched and stays primary; this holds the LLM-extracted maturity wall for filings where XBRL is absent/incomplete. Structure mirrors his table for easy future consolidation.

```sql
CREATE TABLE IF NOT EXISTS llm_debt_maturities (
  cik            TEXT NOT NULL,
  period_end     TEXT NOT NULL,
  bucket         TEXT NOT NULL,                    -- "y1".."y5" | "thereafter"  (his naming, normalized from your year1-5/thereafter)
  value          DOUBLE PRECISION NOT NULL,        -- principal due in that bucket, USD (normalize your "millions" to his unit — confirm his unit in store.py)
  evidence_quote TEXT,                             -- verbatim supporting sentence (LLM is grounded)
  source         TEXT NOT NULL DEFAULT '',         -- e.g. "10-K 2023-09-30, Debt footnote (LLM)"
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (cik, period_end, bucket)
);
```

**Precedence rule (READ side — enforced in the consumer, not the schema):**
> For a given `(cik, period_end)`: read `debt_maturities` (XBRL) first. If it has NO rows for that period, fall back to `llm_debt_maturities`. XBRL is always authoritative when present; the LLM table only fills the gaps XBRL left.

**Consolidation path (later, when synced with Freeman):** if you later prefer the single-table Option A, add a `source_method TEXT` column to his `debt_maturities` and migrate these rows in with `source_method='llm'`, then enforce precedence at write. Kept separate now to need zero coordination.

**Normalization to confirm:** your maturity amounts are USD **millions**; his `debt_maturities.value` is "principal due" — confirm his unit (raw USD vs millions) in `store.py`/`extract.py` before writing, so the fallback doesn't mix units. (Open item §7.)

---

## 6. RLS + indexes for the new tables (his conventions)

```sql
-- Indexes
CREATE INDEX IF NOT EXISTS idx_going_concern_cik       ON going_concern (cik, period_end);
CREATE INDEX IF NOT EXISTS idx_llm_maturities_cik      ON llm_debt_maturities (cik, period_end);

-- Row-Level Security: public read (browser anon key); writes via service-role key bypass RLS.
ALTER TABLE going_concern       ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_debt_maturities ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read going_concern" ON going_concern;
CREATE POLICY "Public read going_concern" ON going_concern FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read llm_debt_maturities" ON llm_debt_maturities;
CREATE POLICY "Public read llm_debt_maturities" ON llm_debt_maturities FOR SELECT USING (true);
```
(`covenants` already has its index and RLS policy from his schema — the ALTER adds columns only, so no new policy needed.)

---

## 7. Field reconciliation & open items (what the port code must handle)

**Name/format normalizations (your model → this schema):**
| Your `llm_extractor.py` | This schema | Note |
|---|---|---|
| `Covenant.threshold_value` | `threshold` | rename |
| `Covenant.evidence` | `evidence_quote` | rename |
| `Covenant.direction = "maximum"/"minimum"` | `direction = "max"/"min"` | **normalize** (his CHECK) |
| `Covenant.covenant_type` (8 bare types) | `covenant_type` (8 superset) | reconcile vocab; his column has no CHECK |
| `MaturityYear.year_label` ("Year 1"/"2026"/…) | `bucket` ("y1".."y5"/"thereafter") | reuse your `_year_bucket()` mapping, output his naming |
| `MaturityYear.amount_millions` | `value` | **confirm unit** vs his XBRL table |
| `Compliance.status='going_concern_doubt'` / `going_concern_flag` | `going_concern` Tier-1 row | split out of Compliance |
| `Compliance.status` breach/waiver | covenant `near_limit`/breach (code-derived) | NOT going_concern |

**Open items (resolve during the port / spec rewrite):**
1. **2c dependency** — does the ported covenant pass *replace* his (one writer, extend works) or *run alongside* (two writers, revisit)? Schema assumes replace.
2. **Maturity unit** — confirm his `debt_maturities.value` unit before the LLM fallback writes, to avoid millions-vs-raw mismatch.
3. **`covenant_type` vocab** — the validator whitelist must accept the 8-value superset or it silently drops new types (his DB won't object; the code will).
4. **`near_limit` / `cushion` derived in code** — the port must compute these post-extraction (spec §7); the prompt must NOT emit them.
5. **Tier-2 going-concern** — net-new behavior your extractor lacks; the rewritten going-concern spec/prompt supplies it (`adverse_conditions` required, else the row is boilerplate and dropped).

---

## 8. What this unblocks

With this schema settled, the next artifacts write against it:
- **The LLM spec rewrite** (next) — `LLM_COVENANT.md` / `LLM_GOING_CONCERN.md` re-pointed to "port your extractor → populate these tables," keeping the spec improvements (two-pass recall, cushion-in-code, GC tiers).
- **The ported extractor** (Stage 2 build) — your `llm_extractor.py` logic adapted to Freeman's pipeline, writing here via new `save_covenants`(extended)/`save_going_concern`/`save_llm_debt_maturities` functions in his `store.py` style.

This schema is the contract both depend on — review it before either is written.
