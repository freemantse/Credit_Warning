# Credit Warning System

An automated **credit early-warning tool for corporate-bond portfolios**. It monitors SEC
filings (10-K / 10-Q) and surfaces credit stress *before* it becomes a problem.

For each tracked company it:

- Extracts auditable financial ratios from XBRL data (EBITDA margin, leverage, interest
  coverage, free cash flow, liquidity).
- Computes a **stress score (0–100)** combining:
  - Six quantitative ratio rules (XBRL-based, fully traceable to source tags).
  - Qualitative risk signals from the located MD&A prose and footnotes (LLM review of
    management tone, covenants, litigation).
  - Debt-maturity concentration ("maturity wall") risk.
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
| Core logic | `src/` | Ingest, extract, score, store, LLM review (pure Python) |
| Backend API | `api/main.py` | FastAPI, routes under `/api/*` |
| Frontend | `app/`, `lib/` | Next.js 14 + React 18 dashboard |
| Persistence | `supabase/schema.sql` | Hosted PostgreSQL (Supabase) |
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
       → score   (ratios + findings + maturities → 0–100)
       → frontend renders portfolio table + issuer detail
```

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

Do **not** set `PYTHON_API_URL` in production — it is for local dev only.

See [`DEPLOY.md`](./DEPLOY.md) for the full deployment guide.
