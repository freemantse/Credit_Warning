# Credit Warning System — Deploy & Run

A FastAPI (Python) backend + Next.js (React) frontend, deployed together on Vercel.

## Architecture

| Component | Tech | Local port | Vercel |
|-----------|------|-----------|--------|
| Backend   | FastAPI (`api/main.py`) | 8000 | Serverless function (`api/main.py`, 60s max) |
| Frontend  | Next.js 14 (`app/`)     | 3000 | Static + SSR |
| Database  | Supabase (Postgres)     | —    | Hosted |

All backend routes use the `/api/` prefix. Vercel rewrites `/api/*` → the Python
function; locally Next.js proxies `/api/*` to `http://localhost:8000` via `PYTHON_API_URL`.

## Prerequisites

- Python 3.9+ and `pip`
- Node.js 18+ and `npm`
- A Supabase project (URL + service role key)
- (Optional) An Anthropic API key for the LLM qualitative review

## Environment variables

Copy the example and fill in credentials:

```bash
cp .env.local.example .env.local
```

| Variable | Required | Notes |
|----------|----------|-------|
| `SUPABASE_URL` | yes | Supabase → Settings → API |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | secret, backend only |
| `ANTHROPIC_API_KEY` | optional | only for LLM qualitative review |
| `CRON_SECRET` | optional | secures `/api/cron/*` (daily refresh + model training) |
| `PYTHON_API_URL` | local only | `http://localhost:8000`; leave unset on Vercel |

Never commit `.env.local`.

## Run locally

Install dependencies:

```bash
python3 -m pip install -r requirements.txt   # backend
npm install                                  # frontend
```

Start both servers at once:

```bash
./start.sh
```

- Backend → http://localhost:8000
- Frontend → http://localhost:3000
- `Ctrl+C` stops both.

Or run them separately:

```bash
# Backend
python3 -m uvicorn api.main:app --reload --port 8000

# Frontend (new terminal)
npm run dev
```

## Tests

```bash
python3 -m pytest
```

## The rating-migration ML model (what it is, in brief)

A supervised model that predicts the **direction of a company's next credit-rating
change over 12 months** — P(downgrade), P(upgrade), P(distress) — as a second opinion to
the rule-based stress score. ("distress" = a transition into the CCC+/default tail.)

- **Training labels:** one consolidated source-of-truth file, `data/agency_ratings.csv`
  — **~1,645 US issuers, ~10,950 real rating actions, three agencies** (Moody's, Fitch,
  Egan-Jones), 2003–2026, built by `scripts.build_agency_ratings_csv` from the LSEG rating
  drop (deduped on `cik, agency, effective_date`).
- **Inputs (features):** recomputed from each company's **SEC EDGAR XBRL** filings (the
  same auditable ratios as the stress score) — **US filers only**; foreign issuers/ADRs
  are dropped.
- **Algorithm:** per head, a monotonic **HistGradientBoostingClassifier** (credit-coherent
  constraints — e.g. higher leverage can only raise downgrade risk), with an isotonic
  probability calibration and a LogisticRegression baseline it must beat. **Walk-forward**
  splits only (train on the past, test on the future — no look-ahead).
- **Outputs:** calibrated probabilities + top drivers → `migration_predictions`.

## Model artifacts & offline training

The rating-migration model trains **offline** — locally, in CI, or via the cron trigger
`POST /api/cron/train-model` (guarded by `CRON_SECRET`). A full walk-forward run can
exceed the 60 s serverless budget, so the heavy job is expected to run as a longer-lived
process; the deployed function only ever **reads** persisted outputs:

- **Supabase:** `migration_predictions` (shown on issuer pages) and `model_registry`
  (active-model provenance).
- **Repo bundle (on disk):** `data/migration_eval.json` (the `/backtest` scorecard) and
  `data/model_vintages/` (the point-in-time event backtest). These are committed so the
  deployed `/backtest` page has data.

`data/migration_model.joblib` (the trained model itself) is **gitignored**: nothing in the
deployed app reads it — predictions are served from Supabase.

### Rebuilding / retraining on the full universe

```bash
# 1. (Re)build the single source of truth (resolves identifiers vs EDGAR; prints scorecard)
python3 -m scripts.build_agency_ratings_csv

# 2. Optional CLEAN SLATE — wipe the previous run's training data before reloading.
#    DESTRUCTIVE: truncates agency_ratings, rating_labels, migration_predictions and
#    clears the active model_registry row. Features (ratios/implied_ratings) are kept.
python3 -m scripts.reset_training_tables --yes

# 3. Load labels, build features (slow: EDGAR is throttled 8 req/s, disk-cached), train.
python3 -m scripts.load_agency_ratings
python3 -m scripts.track_universe        # full universe (or --distressed-only for the CCC+/default names)
python3 -m scripts.build_labels
python3 -m src.model.train --split-date 2022-12-31
python3 -m src.model.predict
python3 -m src.model.evaluate
```

## Deploy to Vercel

> ⚠️ **Set the environment variables in Vercel before (or right after) your first
> deploy.** `.env.local` is **not** uploaded — the deployed Python function reads
> only Vercel's env vars. If `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` are
> missing, the build still succeeds and the deploy shows **Ready**, but every API
> call returns **500** and the frontend shows *"Cannot reach API."* (The function
> raises `RuntimeError: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set`.)

1. Push the repo to GitHub and import it in Vercel (or use the `vercel` CLI).
2. In **Vercel → Project → Settings → Environment Variables**, add the following,
   scoped to **Production** *and* **Preview** (so preview deploys work too):

   | Variable | Required | Scope |
   |----------|----------|-------|
   | `SUPABASE_URL` | **yes** | Production + Preview |
   | `SUPABASE_SERVICE_ROLE_KEY` | **yes** | Production + Preview (keep secret — no `NEXT_PUBLIC_` prefix) |
   | `ANTHROPIC_API_KEY` | optional | only if using LLM review |
   | `CRON_SECRET` | optional | Production — secures `/api/cron/*` (daily refresh + model training) |

   Do **not** set `PYTHON_API_URL` — it's local-dev only.
3. **Redeploy after adding or changing env vars.** Existing deployments do not pick
   up new variables — push a commit, or use **Deployments → ⋯ → Redeploy**.
4. Vercel auto-detects Next.js for the frontend and builds the Python
   function from `api/main.py` (config in `vercel.json`, `requirements.txt`).

### Verify the deploy

```bash
curl -i https://<your-app>.vercel.app/api/issuers   # expect 200 + JSON
```

A 500 here almost always means a missing env var — check **Vercel → Deployment →
Logs** for the traceback.

```bash
# CLI alternative
npm i -g vercel
vercel          # preview deploy
vercel --prod   # production deploy
```

`vercel.json` highlights:

```json
{
  "functions": { "api/main.py": { "maxDuration": 60 } },
  "rewrites": [{ "source": "/api/:path*", "destination": "/api/main" }]
}
```
