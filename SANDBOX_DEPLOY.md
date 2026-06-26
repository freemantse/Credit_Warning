# SANDBOX_DEPLOY.md — Stand up a personal sandbox (your own Vercel + Supabase)

A step-by-step runbook for deploying an **isolated** copy of the Credit Warning
System to test LLM changes safely, and a post-deploy checklist for discovering
Vercel's real limits empirically.

**Core design constraint (read first):** the LLM extraction (`review_filing`)
takes **minutes** per company (≈10 min for 3 periods on a low Anthropic rate
tier). Vercel serverless caps a function at **60 s** (`vercel.json` →
`maxDuration: 60`) and freezes the instance after the HTTP response returns. So
**LLM extraction is populated OFFLINE** (locally, writing to your sandbox
Supabase); the **deployed Vercel app only reads/displays** that data and serves
fast ratio endpoints. This is not a workaround — it's the intended split.

---

## Architecture recap

| Component | Tech | Vercel role |
|-----------|------|-------------|
| Backend   | FastAPI (`api/main.py`) | one serverless Python function, 60 s cap |
| Frontend  | Next.js 14 (`app/`)     | static + SSR; fetches relative `/api/*` |
| Database  | Supabase (Postgres)     | hosted; backend reads/writes via service-role key |

The frontend never talks to Supabase directly — it only calls `/api/*`, which
`vercel.json` rewrites to the Python function. So **only the backend needs DB
credentials**, and pointing the backend at *your* Supabase fully isolates the
sandbox.

---

## TASK 2 — Runbook

### Step 1 — Create your sandbox Supabase project

1. Go to <https://app.supabase.com> → **New project**. Pick a name (e.g.
   `credit-warning-sandbox`), set a DB password, choose a region.
2. Once provisioned, open **SQL Editor** → **New query**, paste the **entire
   contents of `supabase/schema.sql`**, and **Run** it once.
   - The script is **idempotent** — every statement is `CREATE TABLE IF NOT
     EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, or
     `DROP POLICY IF EXISTS` + `CREATE POLICY`. Safe to re-run if you tweak it.
   - It creates **all** tables in one shot:
     `companies`, `ratios`, `llm_findings`, `debt_maturities`,
     **`covenants`** (including the 2c-i `ALTER` block — `covenant_subtype`,
     `ratio_name`, `unit`, `testing_frequency`, `is_springing`,
     `springing_trigger`, `step_down`, `is_maintenance`, `cushion`,
     `cushion_pct`, `section_confidence`, `null_reason` — **and** the 2c-iii
     columns `near_limit_reason`, `near_limit_evidence_quote`,
     `near_limit_section`), `loss_provisions`, `cases`, `score_config`,
     **`going_concern`**.
   - It also enables **Row-Level Security** and adds a public `SELECT` policy on
     every table. Writes go through the **service-role key** (which bypasses
     RLS), so no insert/update policies are needed.
   - **Verify:** Table Editor should now list all 9 tables. Or run
     `SELECT table_name FROM information_schema.tables WHERE table_schema='public';`
3. Grab credentials from **Settings → API**:
   - **`SUPABASE_URL`** = "Project URL" (e.g. `https://abcd1234.supabase.co`).
   - **`SUPABASE_SERVICE_ROLE_KEY`** = "service_role" secret key (under
     *Project API keys* — **not** the `anon` key). Keep it secret; backend only.

### Step 2 — Deploy to your own Vercel project

1. Fork the repo (or push your branch to your own GitHub remote).
2. <https://vercel.com> → **Add New → Project** → import your fork/branch.
   Vercel auto-detects Next.js for the frontend and builds the Python function
   from `api/main.py` (per `vercel.json` + `requirements.txt`).
3. **Settings → Environment Variables** — add these, scoped to **Production**
   *and* **Preview**:

   | Variable | Set it? | Value / note |
   |----------|---------|--------------|
   | `SUPABASE_URL` | **yes** | your sandbox Project URL |
   | `SUPABASE_SERVICE_ROLE_KEY` | **yes** | your sandbox service_role key (secret; **no** `NEXT_PUBLIC_` prefix) |
   | `ANTHROPIC_API_KEY` | optional | only if you'll trigger LLM *on the server* — **not recommended** (see "What works"); leave unset if LLM is populated offline |
   | `ANTHROPIC_BASE_URL` | **NO** | you're on direct Anthropic — do **not** set this (it's only for the APIYI relay) |
   | `PYTHON_API_URL` | **NO** | local-dev only; setting it in prod breaks routing |

4. **The frontend needs NO environment variables** — no `NEXT_PUBLIC_*`, no
   Supabase JS client. It builds and runs with none.
5. **Redeploy after setting env vars** — existing builds don't pick up new
   variables. Push a commit or **Deployments → ⋯ → Redeploy**.

> ⚠️ If `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` are missing, the build still
> shows **Ready**, but every API call returns **500**
> (`RuntimeError: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set`) and the
> UI shows "Cannot reach API."

### Step 3 — Populate data OFFLINE (locally, into your sandbox Supabase)

The LLM passes run on a **long-lived local process** (no 60 s cap), writing
straight to your sandbox DB. Point your local env at the sandbox first:

```bash
# In the repo root, put SANDBOX creds in .env.local (gitignored, never committed):
#   SUPABASE_URL=https://<your-sandbox>.supabase.co
#   SUPABASE_SERVICE_ROLE_KEY=<your sandbox service_role key>
#   ANTHROPIC_API_KEY=sk-ant-...        # direct Anthropic
#   # do NOT set ANTHROPIC_BASE_URL (direct Anthropic, not the relay)
# (store.py and the Anthropic SDK read these from .env.local / the environment.)

python3 -m pip install -r requirements.txt     # one time
```

1. **Seed the backtest `cases` table** (needed for the backtest page):
   ```bash
   python3 -m scripts.seed_cases
   ```
   Idempotent (upserts on `case_id`) — re-run anytime after editing
   `data/cases.csv`.

2. **Populate a few demo companies** (ratios over full history + LLM over the
   latest 3 periods). One healthy, two distressed makes a good demo:
   ```bash
   python3 -m src.track AAPL     # healthy control
   python3 -m src.track RAD      # Rite Aid — distressed
   python3 -m src.track BBBY     # Bed Bath & Beyond — distressed, disclosed waivers
   ```
   - Ratios + maturities are computed for the **full** XBRL history (free,
     deterministic — they feed the migration detector's trajectory).
   - The LLM passes are **capped to the latest 3 periods** by default
     (`LLM_DEFAULT_PERIODS = 3`). Override per run with `--llm-periods N`, or
     skip the LLM entirely with `--no-llm`.
   - **Cost:** the latest-3 cap keeps each company to **≈ $0.33** in Anthropic
     spend. **Time:** on a low rate tier (10k input tokens/min) expect **several
     minutes per company** — the SDK auto-retries 429s (`max_retries=8` in
     `review_filing`) so a throttled run *completes* rather than erroring.
   - This is the **offline-population approach** — you are deliberately **not**
     running the LLM on Vercel.

3. Confirm the rows landed: in Supabase **Table Editor**, check `ratios`,
   `llm_findings`, `covenants`, `going_concern` have rows for your tickers.

### Step 4 — View it

Open your Vercel URL. The dashboard reads from your sandbox Supabase via the fast
API endpoints. Newly-tracked tickers should appear with scores, ratios, and
(for the latest 3 periods) LLM findings/covenants/going-concern.

---

## What works vs. doesn't on Vercel

**The deployed app = read/display + fast ratio endpoints only.** Anything that
runs `review_filing` (the LLM) on the server will hit the 60 s `maxDuration` and
the serverless instance-freeze, and the in-memory job-status dict
(`_llm_review_status`) won't survive across invocations/cold starts.

### ✅ Safe to call on Vercel (fast — Supabase reads / light writes, seconds)
- `GET /api/health` — liveness
- `GET /api/issuers` — dashboard list
- `GET /api/issuer/{ticker}` — issuer detail (ratios, findings, covenants, GC)
- `DELETE /api/issuer/{ticker}` — remove an issuer
- `GET /api/issuer/{ticker}/llm-review/status` — reads the status dict (note:
  meaningful only on a long-lived server; see caveat below)
- `GET /api/backtest/cases`, `POST /api/cases`, `DELETE /api/cases/{case_id}`
- `GET /api/score-config`, `PUT /api/score-config`
- `GET /api/backtest/status`
- `POST /api/track` **with the default `no_llm: true`** — fetches EDGAR + computes
  ratios only. Usually within 60 s for one company on a warm cache, but it does a
  live EDGAR fetch, so treat it as "fast, occasionally slow on cold cache." This
  is how the deployed app can populate **ratios** on demand.

### ⛔ Avoid on Vercel (LLM / long-running — will time out or be killed)
- `POST /api/track` with **`no_llm: false`** — runs `review_filing`
  **synchronously** in-request → **60 s timeout** for multi-period LLM.
- `POST /api/issuer/{ticker}/llm-review` — kicks off a FastAPI `BackgroundTask`;
  on serverless the instance is frozen/reclaimed after the response and capped at
  60 s, so a minutes-long job **won't finish**, and the in-memory status dict
  won't be visible to the polling `GET .../status` (different instance / cold
  start). Use the **offline** `python -m src.track` path instead.
- `POST /api/backtest` — long-running BackgroundTask over many cases + network;
  same serverless limitations. Run the backtest **locally** (`python -m
  src.backtest`) instead.

> **Why offline beats "just raise maxDuration":** even Vercel's higher duration
> tiers won't hold a 10-min throttled job, and the in-memory status/`BackgroundTasks`
> model assumes one long-lived process. If you later want in-cloud LLM runs, host
> the FastAPI backend on a long-lived server (Render / Railway / Fly / a VM)
> rather than a Vercel function.

---

## TASK 4 — What to probe after deploy (click / observe / expect)

Run these against your live sandbox URL (`https://<your-app>.vercel.app`) to
confirm the deployment and **empirically** verify the 60 s LLM-timeout
prediction.

### A. Python function cold-starts cleanly
- **Do:** `curl -i https://<your-app>.vercel.app/api/health`
- **Expect:** `200` with a small JSON body within a few seconds (first call may
  be a slow cold start; a second call is fast). A `500` here = almost always a
  missing/typo'd env var → check **Vercel → Deployment → Logs** for
  `RuntimeError: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set`.

### B. Frontend reaches `/api/*`
- **Do:** open `https://<your-app>.vercel.app` in a browser; open DevTools →
  Network.
- **Expect:** the page issues a request to `/api/issuers` returning `200` JSON.
  No "Cannot reach API" banner. (If the page loads but is empty with that banner,
  the rewrite or env vars are wrong.)

### C. Supabase connects from the function
- **Do:** `curl -s https://<your-app>.vercel.app/api/issuers`
- **Expect:** a JSON array of the tickers you populated offline (AAPL, RAD,
  BBBY). An empty `[]` means the function reached Supabase but you haven't
  populated yet (or it's pointed at the wrong project). A `500` means it can't
  reach Supabase — re-check `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`.

### D. Fast read endpoints return populated data
- **Do:** `curl -s https://<your-app>.vercel.app/api/issuer/AAPL` and click into
  the AAPL issuer page in the UI.
- **Expect:** ratios for the **full history**, a stress score, and — for the
  **latest 3 periods only** — LLM findings/covenants/going-concern. Older periods
  show ratio-only data (no LLM), which is the intended latest-3 cap.

### E. Confirm the LLM-endpoint 60 s timeout (the empirical limit test)
- **Do:** trigger a server-side LLM run and time it:
  ```bash
  time curl -i -X POST https://<your-app>.vercel.app/api/issuer/AAPL/llm-review \
       -H 'Content-Type: application/json' -d '{"periods": 3}'
  ```
  Then immediately poll status:
  ```bash
  curl -s https://<your-app>.vercel.app/api/issuer/AAPL/llm-review/status
  ```
- **Expect (the prediction):** the POST may return quickly (`{"status":"started"}`)
  because the work is deferred to a BackgroundTask — **but the LLM work does not
  actually complete**: the status poll will show `running` that never reaches
  done (or a reset/empty status from a different instance), and **no new
  `llm_findings` rows appear in Supabase** for periods that needed an LLM call.
  Requires `ANTHROPIC_API_KEY` set on Vercel to even attempt it.
- **Also test the synchronous path** (to see the hard timeout directly):
  ```bash
  time curl -i -X POST https://<your-app>.vercel.app/api/track \
       -H 'Content-Type: application/json' -d '{"ticker":"AAPL","no_llm":false}'
  ```
- **Expect:** the request runs ~60 s then returns a Vercel **`504`
  FUNCTION_INVOCATION_TIMEOUT** (or `500`) — the function is killed at the
  `maxDuration` boundary mid-LLM. This is the concrete confirmation that LLM must
  be populated offline. **Compare:** the same `{"ticker":"AAPL","no_llm":true}`
  (or omitting `no_llm`) returns `200` in seconds.

### F. (Optional) Backtest endpoint behavior
- **Do:** `POST /api/backtest` then poll `GET /api/backtest/status`.
- **Expect:** same serverless limitation — a long backtest won't complete in the
  function; run it locally instead. Confirms the same "long jobs belong offline"
  conclusion.

---

*This runbook intentionally changes no application code and applies no SQL. It
documents the existing offline-populate / Vercel-read split and a checklist to
verify (and probe the limits of) a fresh sandbox deploy.*
