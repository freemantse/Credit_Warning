# Credit Warning System

An automated **credit early-warning tool for corporate-bond portfolios**. It monitors SEC
filings (10-K / 10-Q) and surfaces credit stress *before* it becomes a problem.

For each tracked company it:

- Extracts auditable financial ratios from XBRL data (EBITDA margin, leverage, interest
  coverage, cash-flow-to-debt, free cash flow, liquidity, current ratio, debt-to-assets).
- Computes a **stress score (0–100)** combining:
  - Nine quantitative rules — eight XBRL financial ratios plus a debt-maturity
    concentration ("maturity wall") rule — all traceable to source tags.
  - Qualitative risk signals from the located MD&A prose and footnotes (LLM review of
    management tone, covenants, litigation).
  - All rule weights and thresholds are **tunable and backtestable** from the UI — see
    the Backtest page below.
- Tracks that score over time to reveal deterioration trends, and flags covenant proximity.

### Core design principle

There is a strict division between **deterministic parsing** and **LLM review**:

- **All numbers** come from deterministic XBRL extraction — every ratio carries its raw
  inputs and source tags so it is auditable.
- **The LLM never produces numbers or scores** — only qualitative findings with evidence
  quotes. Every evidence quote (findings, covenants, loss provisions) is verified to appear
  verbatim in the exact excerpt sent to the model; unverifiable findings are dropped.
- Missing data **fails loud** (`MissingDataError`); defaults are never substituted.
- The backtest is strictly **point-in-time** — no look-ahead bias. Each distressed
  issuer is scored *walking backward from its own credit-event date* (the Chapter 11
  / bankruptcy filing date), not from today — so "caught early" means the model
  crossed the stress threshold months *before* that event.

---

## Architecture

| Layer | Where | What |
|-------|-------|------|
| Core logic | `src/` | Ingest, extract, score, implied rating, store, LLM review (pure Python) |
| Rating-migration model | `src/model/`, `src/ratings/` | ML layer: train → calibrated P(upgrade/downgrade/default) + walk-forward event backtest (offline; predictions persisted for the app to read) |
| Backend API | `api/main.py` | FastAPI, routes under `/api/*` |
| Frontend | `app/`, `lib/` | Next.js 14 + React 18 dashboard |
| Persistence | `supabase/schema.sql` | Hosted PostgreSQL (Supabase) — issuer ratios / findings / implied ratings, real `agency_ratings` + derived `rating_labels`, model `migration_predictions` + `model_registry`, the backtest `cases` library, and the active `score_config` |
| Data cache | `cache/` | On-disk SEC EDGAR responses (immutable, never stale) |

**Data source:** [SEC EDGAR](https://data.sec.gov) — free, authoritative XBRL company
facts, filing metadata, and filing text. All rows are keyed on **CIK** (SEC's permanent
identifier), never ticker.

### Request flow

```
ticker → ingest (resolve CIK, fetch XBRL, cached)
       → extract (compute ratios, with audit trail)
       → store   (bulk insert to Supabase)
       → optional LLM review  fetch 10-K once → locate_sections
                              → MD&A (Item 7)   → quote-verified findings
                              → debt footnote   → covenants
                              → contingencies   → loss provisions
       → score   (ratios + findings + maturities → 0–100, via the active scoring config)
       → frontend renders portfolio table + issuer detail
```

### Rating-migration model (offline)

#### In plain language

Alongside the rule-based stress score, the system has a **machine-learning model that
predicts where a company's credit rating is headed** over the next **12 months** —
specifically the probability that it will be **downgraded**, **upgraded**, or
**default**. Think of the stress score as a doctor's checklist and this model as a
second opinion that has *studied the histories of many companies* and learned which
patterns of financial deterioration tended to precede an actual rating cut.

**How it learned (the training data).** The model is taught from a consolidated history
of **real agency ratings** — one cleaned "single source of truth" file
(`data/agency_ratings.csv`) built from a dense action-by-action history from LSEG covering
**Moody's, Fitch, and Egan-Jones**.

After cleaning, resolving company identifiers, and de-duplicating, this yields about
**~1,645 unique companies and ~10,950 dated rating actions across three agencies**
(Moody's, Fitch, Egan-Jones), spanning **2003–2026**. For each of
those companies the model's *inputs* are **not** taken from the rating file — they are
**recomputed from the company's own SEC filings** (the same auditable XBRL ratios the
stress score uses), so every input is traceable to a source document.

**Important constraint — US companies only.** Inputs come from **SEC EDGAR**, which only
covers **US filers**. Foreign issuers and many ADRs in the raw data have no US 10-K and
are dropped — that is the main reason the *usable* universe is smaller than the raw data.

**What it produces.** For every company-period it outputs three **calibrated
probabilities** (downgrade / upgrade / distress over 12 months — "distress" = a transition
into the CCC+/default tail) *plus the top financial drivers behind each number* (e.g.
"rising leverage", "falling interest coverage"), so a prediction is never a black box.
These land in the `migration_predictions` table, which the portfolio and issuer pages read.

#### The pipeline

It never runs in the API hot path — it trains on a schedule (local / CI / cron) and
persists its outputs, which the app then reads:

```
data/agency_ratings.csv  (consolidated real ratings: Moody's/Fitch/Egan-Jones)
   → load_agency_ratings  (the single source of truth → agency_ratings table)
   → track_universe       (each company's SEC filings → auditable XBRL ratio features)
   → build_labels         (period_end + the next rating event → a lookahead-free label)
   → train                (one active model on all history + walk-forward "vintages")
   → predict              (calibrated P(up/down/distress) + drivers → migration_predictions)
   → evaluate             (out-of-time scorecard → data/migration_eval.json)
```

The issuer page shows each company's prediction from `migration_predictions` (falling
back to the rule-based Rating Outlook until the model is trained). The `/backtest` page
replays the **vintages** point-in-time over the case library — each snapshot scored by a
model trained strictly *before* that date, so there is no look-ahead.

---

## Tech stack

**Backend (Python):** FastAPI, Uvicorn, Supabase client, Anthropic (Claude), Requests,
python-dotenv, pytest.

**Frontend (TypeScript/React):** Next.js 14, React 18, Recharts, Tailwind CSS, TypeScript.

**Infra:** Supabase (managed PostgreSQL), Vercel (Next.js + Python serverless function).

---

## Prerequisites

- Python 3.9+ and pip
- Node.js 18+ and npm
- A Supabase project (URL + service-role key)
- *(Optional)* An Anthropic API key — only needed for the LLM qualitative review

---

## Setup

```bash
# 1. Environment
cp .env.local.example .env.local
# Edit .env.local and fill in:
#   SUPABASE_URL
#   SUPABASE_SERVICE_ROLE_KEY
#   ANTHROPIC_API_KEY        (optional — enables LLM review)
#   PYTHON_API_URL           (local dev only; remove on Vercel)

# 2. Dependencies
pip install -r requirements.txt
npm install

# 3. Database
# Open the Supabase SQL editor and run the contents of supabase/schema.sql
# Then seed the backtest case library (one-time; idempotent):
python3 -m scripts.seed_cases
```

> `.env.local` is gitignored — never commit credentials.

---

## Running locally

Start both servers with one command:

```bash
./start.sh
# Backend  → http://localhost:8000
# Frontend → http://localhost:3000
```

Or run them separately:

```bash
python3 -m uvicorn api.main:app --reload --port 8000   # backend
npm run dev                                             # frontend (:3000)
```

### CLI tools (no web server needed)

```bash
# Track an issuer and print its ratios / stress score
python3 -m src.track AAPL
python3 -m src.track AAPL --no-llm        # skip the LLM pass
python3 -m src.track AAPL --periods 8     # show 8 most recent annual periods

# Point-in-time backtest over the case library (~21 real bankruptcies + ~7 healthy
# controls). Each distressed case is scored quarterly walking BACKWARD from its own
# bankruptcy/Chapter-11 date (the "event date"); lead time = months from the first
# stress flag to that event, and ≥ early-months counts as an early warning.
python3 -m src.backtest                    # run + compare against the frozen baseline
python3 -m src.backtest --save-baseline    # freeze this run as the new reference
python3 -m src.backtest --threshold 40     # experiment with a different stress threshold
python3 -m src.backtest --early-months 12  # stricter early-warning bar (default: 6 months)
python3 -m src.backtest --cases other.csv  # run against an explicit CSV instead of Supabase

# The roster lives in the Supabase `cases` table (seeded from data/cases.csv via
# scripts.seed_cases); the CLI falls back to the CSV when Supabase is unavailable.
# Outputs: data/backtest_report.txt (human), data/backtest_results.json (per-case
# score trajectories + the scoring config used), data/backtest_baseline.json.
# Exit codes: 0 = ok, 1 = regression vs baseline (CI-friendly), 2 = harness failure.

# Look up a delisted/bankrupt company's CIK by name
python3 -m src.ingest --name "Sears Holdings"
```

The **Backtest page** (`/backtest`) also lets you do this without the CLI:
- **Manage the case library** — add a company (by ticker or CIK) or remove one;
  changes persist to the Supabase `cases` table.
- **Tune the scoring parameters** — edit the rule weights, ramp thresholds, stress
  threshold, and escalation floor, then **Run Backtest** to *test* them (a transient
  run that does not touch the portfolio), or **Apply to portfolio** to persist them
  as the active config the live dashboard scores with. Defaults reproduce the
  built-in behavior; "Reset to defaults" restores them.

### Rating-migration model (training & prediction)

The training labels come from one consolidated **source-of-truth** rating file,
`data/agency_ratings.csv` (~1,645 US issuers, ~10,950 rating actions, three agencies —
Moody's, Fitch, Egan-Jones). This file is **committed**; the raw LSEG drop it was built
from is no longer in the repo, so `scripts.build_agency_ratings_csv` is only for a fresh
LSEG re-drop. Normal retrains start from the committed CSV:

```bash
# (Re)build the source of truth — ONLY when a new raw LSEG drop is added. Resolves company
# identifiers against EDGAR and prints a usable-issuer scorecard.
python3 -m scripts.build_agency_ratings_csv          # → data/agency_ratings.csv
```

Then run the model pipeline (needs Supabase creds + EDGAR network). `load_agency_ratings`
and `build_labels` clear-then-write by default, so each table mirrors the current source —
`reset_training_tables` is optional:

```bash
python3 -m scripts.load_agency_ratings      # source-of-truth CSV → agency_ratings (replaces)
python3 -m scripts.track_universe           # each company's SEC filings → ratio features
#   └ add --distressed-only to track just the issuers that hit the CCC+/default tail first
python3 -m scripts.build_labels             # period_end + next event → rating_labels (replaces)
python3 -m src.model.train --split-date 2022-12-31   # active model + vintages → data/
python3 -m src.model.predict                # calibrated P(up/down/distress) → migration_predictions
python3 -m src.model.evaluate --splits 2018-12-31,2020-12-31,2022-12-31   # scorecard → data/migration_eval.json
```

Tracking each company is the slow step (SEC EDGAR is throttled to 8 requests/second and
cached on disk); training itself is minutes. Predictions are written to Supabase
(`migration_predictions`); the app reads them from there. Training is offline by
design — see Deployment for the optional cron trigger.

### Data files (`data/`)

Committed (inputs / reference): `agency_ratings.csv` (consolidated source-of-truth ratings),
`cases.csv` (backtest roster seed), `backtest_baseline.json` (frozen regression reference),
and `model_vintages/` + `migration_eval.json` (read by the `/backtest` page). Everything
else under `data/` is generated and gitignored — the local `store.db`, backtest run
outputs, the trained `migration_model.joblib`, and the licensed LSEG crosswalk
(`universe_xref.csv`).

### Tests

```bash
python3 -m pytest
```

---

## Deployment (Vercel)

Vercel auto-detects the Next.js frontend and the Python serverless function from
`vercel.json`. In **Project Settings → Environment Variables** set:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `ANTHROPIC_API_KEY` (if using LLM review)
- `CRON_SECRET` (enables/secures the daily auto-refresh — see below)

Do **not** set `PYTHON_API_URL` in production — it is for local dev only.

### Automatic issuer refresh (Vercel Cron)

A daily Vercel Cron job (`vercel.json` → `crons`, `0 6 * * *` = 06:00 UTC) calls
`GET /api/cron/refresh-all`, which re-tracks every portfolio issuer from EDGAR so
newly-filed 10-Ks flow into history with no manual action. It refreshes only the
deterministic ratio + debt-maturity data (the LLM pass is always skipped).

- Set a random `CRON_SECRET` in the project's env vars. Vercel automatically
  sends it as `Authorization: Bearer ${CRON_SECRET}` on cron invocations; the
  endpoint rejects calls without it (401), so it isn't publicly abusable.
- Issuers are processed oldest-refreshed-first within a ~50s wall-clock budget
  (under the 60s function `maxDuration`). If the portfolio is too large for one
  run, the remainder is picked up on the next day's run (rotation via the
  `companies.last_refreshed` column). On a Vercel Pro plan you can raise
  `maxDuration` and/or use a finer cron schedule.

See [`DEPLOY.md`](./DEPLOY.md) for the full deployment guide.
