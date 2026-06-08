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

## Deploy to Vercel

1. Push the repo to GitHub and import it in Vercel (or use the `vercel` CLI).
2. In **Vercel → Project → Settings → Environment Variables**, add:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `ANTHROPIC_API_KEY` (if used)
   - Do **not** set `PYTHON_API_URL` — it's local-dev only.
3. Deploy. Vercel auto-detects Next.js for the frontend and builds the Python
   function from `api/main.py` (config in `vercel.json`, `requirements.txt`).

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
