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
  - All rule weights and thresholds live in one backend config (`DEFAULT_CONFIG` in
    `src/score.py`); when the migration model is trained they are **replaced by
    model-learned weights**. They are not editable from the UI.
- Tracks that score over time to reveal deterioration trends, and flags covenant proximity.

## Contents

- [Core design principle](#core-design-principle)
- [Architecture](#architecture)
  - [Request flow](#request-flow)
  - [Rating-migration model (offline)](#rating-migration-model-offline)
    - [In plain language](#in-plain-language)
    - [Forward-looking market features (distance-to-default)](#forward-looking-market-features-distance-to-default)
    - [The pipeline](#the-pipeline)
    - [Known limitations & data constraints](#known-limitations--data-constraints)
- [Stress score rationale](#stress-score-rationale)
  - [Why additive rules](#why-additive-rules)
  - [The eight core rules and their weights](#the-eight-core-rules-and-their-weights)
  - [How a rule scores: the ramp](#how-a-rule-scores-the-ramp)
  - [Two robustness rules](#two-robustness-rules)
  - [The LLM contribution is capped](#the-llm-contribution-is-capped)
  - [Where the weights come from](#where-the-weights-come-from)
- [Tech stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Running locally](#running-locally)
  - [CLI tools (no web server needed)](#cli-tools-no-web-server-needed)
  - [Rating-migration model (training & prediction)](#rating-migration-model-training--prediction)
  - [Data files (`data/`)](#data-files-data)
  - [Tests](#tests)
- [Deployment (Vercel)](#deployment-vercel)
  - [Automatic issuer refresh (Vercel Cron)](#automatic-issuer-refresh-vercel-cron)

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

**How the three agencies are combined.** The three agencies rate the same company
independently and often *disagree* (they agree on direction only ~64% of the time), so
there is no single "true rating". The model is trained **pooled** over all three — one
row per (company, period, agency), each labelled by *that agency's* own 12-month move —
with the **agency identity itself as a feature**, so the booster can learn that e.g.
Egan-Jones downgrades roughly twice as often as Moody's. At serving time the per-agency
probabilities are then combined into a single **issuer-level "will *any* agency
deteriorate"** number via noisy-OR (`1 − ∏(1 − pₐ)`) — the question the product actually
answers. (We deliberately do *not* train three separate models; see the limitations below.)

#### Forward-looking market features (distance-to-default)

The accounting ratios above are backward-looking and update only quarterly. To give the
model a **forward-looking** signal, the **downgrade** and **upgrade** heads also read four
market features computed from daily equity prices (an LSEG drop, ~2002→present): the Merton
**distance-to-default** (Bharath–Shumway "naive" form, using market cap + gross debt +
equity volatility), **12-month equity momentum**, **annualised equity volatility**, and
**market leverage** (debt / (debt + market cap)). Every value is strictly point-in-time —
only prices dated on/before the period_end — so the walk-forward guarantee holds
(`src/model/market.py`, precomputed by `scripts.build_market_features`).

This is the credit literature's single most reliable lever, and it is the first feature
addition that measurably helped (an earlier batch of purely ratio-derived features was
tried and reverted for not helping). Out-of-time walk-forward, adding the market block:

| Head | PR-AUC (before → after) | Catch @10% false-positive |
|---|---|---|
| **Downgrade** | 0.220 → **0.246** | 25% → **30%** |
| **Upgrade** | 0.290 → **0.311** | 33% → **35%** |
| Distress | 0.190 (unchanged) | ~59% |

The **distress head deliberately excludes** the market features — they lifted the migration
heads but slightly hurt distress precision, and because the three heads are independent
classifiers each can use its own feature set (`dataset.make_xy` masks them for distress).
Honest caveat: this is a real, replicable gain in *discrimination*, but migration
prediction still tops out well short of "catch 90% at a low false-alarm rate" — the market
signal shifts the catch/false-positive frontier up, it doesn't remove the ceiling.

#### The pipeline

It never runs in the API hot path — it trains on a schedule (local / CI / cron) and
persists its outputs, which the app then reads:

```
data/agency_ratings.csv  (consolidated real ratings: Moody's/Fitch/Egan-Jones)
   → load_agency_ratings  (the single source of truth → agency_ratings table)
   → track_universe       (each company's SEC filings → auditable XBRL ratio features)
   → build_labels         (period_end + the next rating event → a lookahead-free label)
   → build_market_features (LSEG daily equity prices → point-in-time distance-to-default,
                            equity momentum & volatility → data/market_features.csv)
   → train                (one active model on all history + walk-forward "vintages")
   → predict              (calibrated P(up/down/distress) + drivers → migration_predictions)
   → evaluate             (out-of-time scorecard + catch/false-positive frontier → data/migration_eval.json)
```

The issuer page shows each company's prediction from `migration_predictions` (falling
back to the rule-based Rating Outlook until the model is trained). The `/backtest` page
replays the **vintages** point-in-time over the case library — each snapshot scored by a
model trained strictly *before* that date, so there is no look-ahead.

#### Known limitations & data constraints

These bound what the model can do today. They are **data/label limits, not modelling
bugs** — more expressive models don't fix them; more (and more balanced) labelled events
would.

- **Migration events are rare, and thin at the top of the scale.** Of ~20k observed
  issuer-period-agency rows (1,466 issuers with a usable 12-month outcome), only about
  **2,230 downgrades / 2,385 upgrades** and a **few hundred distress** events are
  positives; the rest are "stable". De-duplicated to one-per-issuer-period that is
  ~**1,210 downgrades / 1,544 upgrades**. **Upgrades from high grades barely exist** —
  roughly **120 from A, 6 from AA, 0 from AAA** — so the upgrade signal for A-and-above
  is essentially unlearnable (a AAA issuer *cannot* be upgraded). Downgrade/distress are
  the better-supported directions; treat high-grade *upgrade* probabilities as weak.

- **The three agencies disagree.** When two or more rate the same company-period they
  agree on direction only **~64%** of the time (median starting-rating spread 1 notch,
  up to 3 at the 90th percentile). This genuine disagreement is real label noise that
  caps achievable accuracy — it is why the target is framed as "*any* agency" rather
  than a single consolidated rating.

- **Agency coverage doesn't span the full history or universe.** Egan-Jones history
  starts only in **~2015** (Moody's ~2007, Fitch ~2008), and no single agency covers
  every issuer (roughly EJR 1,210 / Moody's 903 / Fitch 493 distinct issuers). Their
  base rates differ sharply (EJR downgrades ~15.9% of periods vs Moody's ~8.5%, Fitch
  ~5.6%) — hence conditioning on agency identity, and why an issuer's number can lean on
  whichever agencies happen to cover it.

- **Why not a separate model per agency?** It sounds cleaner but is worse here: splitting
  the already-scarce events three ways starves the rare cells (high-grade upgrades would
  drop to single digits per agency, then again per walk-forward fold) and loses
  cross-agency coverage. Pooling with an agency *feature* keeps every row and every
  issuer while still letting the model learn each agency's behaviour — the bias/variance
  trade-off strongly favours it in this rare-event regime.

- **Right-edge censoring & point-in-time.** A 12-month outcome that would close after the
  dataset's last date is left **unlabelled** (never assumed "stable"), and every split is
  **walk-forward** (train on the past, test on the future) — so metrics are honest but
  the most recent periods contribute no training labels yet.

- **US filers only.** As above, inputs come from SEC EDGAR, so foreign issuers/ADRs are
  dropped — the usable universe is smaller than the raw ratings file.

---

## Stress score rationale

The stress score is a **deterministic, additive 0–100 penalty model** (`src/score.py`,
`compute_score`). Every point is traceable to a named rule and its raw inputs, so any score
can be fully explained and audited back to source XBRL tags. Higher = more credit stress;
**a score ≥ 50 (`STRESS_THRESHOLD`) is treated as "stressed"** everywhere in the system.

### Why additive rules

Rather than one summary ratio or an opaque model, the score is a sum of independent rule
penalties. This is a deliberate design choice:

- **Auditability.** Each rule fires independently and records the points it contributed in
  a `breakdown` dict, so the dashboard can show *why* an issuer scored what it did — no
  black box.
- **No single ratio catches distress.** Leverage, cash generation, liquidity, solvency, and
  refinancing risk fail in different ways and at different times; a distressed issuer usually
  trips several rules at once. Summing independent signals is robust to any one ratio being
  missing or temporarily flattering.
- **Graceful with missing data.** A missing ratio contributes 0 (it is never penalised and
  never assumed), so incomplete XBRL never fabricates or inflates a score.

### The eight core rules and their weights

Points are grouped by what they measure. Debt serviceability dominates, led by the two
strongest empirical distress predictors — **leverage** and **cash-flow-to-debt**. Thresholds
are calibrated to rating-agency grids and distress research, so a speculative-grade reading
already carries roughly half a rule's points.

| Group (max) | Rule | Max pts | Healthy → Severe | What it captures |
|---|---|---|---|---|
| **Debt serviceability (46)** | Leverage (net debt / EBITDA) | 17 | 3.0× → 6.0× | Debt burden vs. earnings — the single strongest distress predictor |
| | Cash-flow-to-debt (FFO / debt) | 15 | 30% → 10% | Ability to repay debt from operating cash flow |
| | Interest coverage (EBITDA / interest) | 14 | 4.0× → 1.0× | Can earnings service the interest bill |
| **Earnings / cash (24)** | Profitability (EBITDA margin) | 14 | 10% → −5% | Core earnings power |
| | Free cash flow (FCF margin) | 10 | 0% → −10% | Cash generation after capex |
| **Liquidity (9)** | Liquidity (cash / short-term debt) | 9 | 1.0× → 0.25× | Near-term ability to meet obligations |
| **Solvency (9)** | Debt-to-assets (gearing) | 9 | 40% → 65% | Balance-sheet leverage |
| **Refinancing (6)** | Maturity wall | 6 | 30% → 80% | Debt-maturity concentration / rollover risk |

Combined, the eight rules cap at **94 points**; the capped LLM signals (below) supply the
remaining headroom to 100.

### How a rule scores: the ramp

Each rule maps its ratio onto points with a linear ramp (`_ramp`): **0 points while the
ratio is at or healthier than the `healthy` threshold**, rising continuously to the rule's
maximum as the ratio worsens toward `severe`, and clamped at the maximum beyond it. The
direction (higher-is-worse vs. lower-is-worse) is inferred from the sign of
`severe − healthy`, so the same formula handles leverage (rising is bad) and coverage
(falling is bad) alike.

### Two robustness rules

These guard against the failure mode where a deeply distressed issuer reads as *healthy*:

- **Sign-aware EBITDA override.** Leverage (`net_debt / EBITDA`) and coverage
  (`EBITDA / interest`) both flip sign when EBITDA is negative, which would let a
  money-losing issuer score 0 on those ramps. When EBITDA ≤ 0 we force both rules to full
  penalty. We branch on the *EBITDA sign*, not the ratio sign — so a negative leverage
  caused by a net-cash position with **positive** EBITDA correctly scores 0 (that's strength,
  not distress).
- **Distress escalation floor.** If ≥ 4 core rules are "severe" (≥ 80% of their max), the
  final score is floored at **60 (High Risk)** regardless of the sum, so compounding distress
  can't slip under the threshold.

### The LLM contribution is capped

Qualitative signals from the LLM review (high-severity findings, covenant proximity, loss
provisions) can add points, but the **combined LLM contribution is capped at 15**. This is
deliberate: the LLM can nudge a score at the margin but **cannot by itself push an issuer
past the 50-point stress threshold** — only the deterministic, auditable ratios can do that.

### Where the weights come from

The weights are **backend config, not a UI control.** `DEFAULT_CONFIG` in `src/score.py`
holds the calibrated defaults above and is the single source of truth. It is overridden in
exactly one way:

- **The migration model.** When the ML model has been trained, the eight core weights are
  **replaced by model-learned weights** derived from it (`src/model/train.derive_score_config`),
  persisted to the `score_config` table in Supabase, and read back by the API as the active
  config. The defaults are the pre-training fallback.

Changing the weights by hand means editing `DEFAULT_CONFIG` (or writing the `score_config`
row). The Backtest page does **not** edit weights — it replays the case library point-in-time
using whatever the active config already is (you can vary the stress `--threshold` and
history depth, not the rule weights).

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
- **Run the backtest** — set the point-in-time history depth (snapshots per case) and
  **Run Backtest** to replay the case library and get a scorecard. The run always uses the
  **active** stress-score config; the rule weights themselves are not editable here (they
  live in `DEFAULT_CONFIG` / the trained `score_config` — see Stress score rationale).

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
