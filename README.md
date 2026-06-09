# Credit Warning System

An automated **credit early-warning tool for corporate-bond portfolios**. It monitors SEC
filings (10-K / 10-Q) and surfaces credit stress *before* it becomes a problem.

For each tracked company it:

- Extracts auditable financial ratios from XBRL data (leverage, interest coverage, free
  cash flow, liquidity).
- Computes a **stress score (0–100)** combining:
  - Quantitative ratio thresholds (XBRL-based, fully traceable to source tags).
  - Qualitative risk signals from filing prose (LLM review of management tone, covenants,
    litigation).
  - Debt-maturity concentration ("maturity wall") risk.
- Tracks that score over time to reveal deterioration trends, and flags covenant proximity.

### Core design principle

There is a strict division between **deterministic parsing** and **LLM review**:

- **All numbers** come from deterministic XBRL extraction — every ratio carries its raw
  inputs and source tags so it is auditable.
- **The LLM never produces numbers or scores** — only qualitative findings with evidence
  quotes.
- Missing data **fails loud** (`MissingDataError`); defaults are never substituted.
- The backtest is strictly **point-in-time** — no look-ahead bias.

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
       → optional LLM review (10-K text → qualitative findings + covenants)
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

# Point-in-time backtest over data/cases.csv
python3 -m src.backtest
python3 -m src.backtest --threshold 40    # try a different stress threshold
```

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

---

## Remaining Work / Roadmap

The pipeline runs end-to-end, but a few pieces are scaffolded yet not fully
validated. What's left:

**1. LLM qualitative analysis & footnote extraction** — the LLM path exists
(`src/llm_review.py`, `src/footnote_review.py`, `src/sections.py`) but needs:
section location validated/tuned across more filers; broader format coverage
(older plain-text filings and 10-Qs — only 10-K HTML is handled today); a
labeled set to evaluate extraction accuracy; finalized scoring weight; and LLM
result caching.

**2. Backtesting** — the point-in-time harness exists (`src/backtest.py`) but
needs a vetted case library (`data/cases.csv`), threshold tuning against
catch rate / lead time / false positives, a decision on including LLM findings,
and CI wiring.

**3. Broader ingestion (future)** — 8-K event filings, earnings-call
transcripts, and sell-side reports are planned but not started.
