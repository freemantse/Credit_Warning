# Credit Warning System

A credit early-warning tool for a ~30-issuer corporate-bond portfolio. It watches each
issuer's SEC filings and flags credit stress *before* it becomes a problem — automating
the work a credit analyst does by hand.

This file is the source of truth for how the system is built. **Read the "Hard
invariants" section before writing any code, and re-read it before the backtest.**

The original one-week brief this is based on lives in [`intern_plan.md`](./intern_plan.md).

---

## The idea that defines the whole project

There is a strict division of labour between deterministic code and the LLM:

- **A parser computes all numbers.** Leverage, coverage, free cash flow, liquidity,
  maturities — every figure is produced by arithmetic on line items, never by a model.
  This is so each number is *auditable*: we can always show "net debt ÷ EBITDA = 4.2×
  from these exact inputs in this filing."
- **The LLM reads only the soft signals** that no parser can catch — management tone and
  hedging, covenant/litigation footnote concerns, worries raised on earnings calls. It
  returns **findings with evidence** (a short quote + its source), and **never numbers or
  scores.**
- Both feed a **stress score per issuer, tracked over time**, with alerts and an
  explainable breakdown.

If you ever find yourself asking the LLM to compute, estimate, or score a number — stop.
That is a design violation, not a shortcut.

---

## Architecture (data flows top to bottom)

```
SEC filings (EDGAR: 10-K, 10-Q, 8-K, XBRL company facts)
        │
   Ingestion           ticker → CIK, fetch filings + facts, rate-limit, cache
        │
        ├────────────────────────────┐
        ▼                             ▼
 Deterministic parser            LLM reader
 numbers, with audit trail       findings + evidence, NEVER numbers
        └────────────────────────────┘
        ▼
 Stress score per issuer         combined, tracked over time
        ▼
 Alerts + explainable breakdown
```

---

## Hard invariants (never violate these)

1. **The LLM never produces numbers or scores.** Its output is structured findings:
   `{concern, severity, evidence_quote, source}`. Numbers come only from the parser.
2. **Every computed number is auditable.** A ratio result must carry the raw inputs it
   used and the source filing/tag for each input. No black-box values.
3. **Missing data fails loud — never guess.** If an input concept is absent, the function
   raises (or returns an explicit "missing" result). It must never substitute a default,
   a zero, or a guessed value and silently produce a wrong ratio.
4. **The backtest is point-in-time. No look-ahead.** When scoring an issuer as of date D,
   use *only* filings whose filing date ≤ D. Today's `companyfacts` feed includes later
   restatements — using it naively makes the backtest look accurate and be worthless.
   This is the single most important correctness rule in the project.
5. **Respect SEC EDGAR etiquette.** Send a descriptive `User-Agent` with a real contact
   email, stay under ~10 requests/second, and cache every response to disk. Getting
   blocked by SEC stops all progress.

---

## Extraction realities (build for these from day one)

- **Companies tag the same concept differently.** Each number needs a *prioritized
  fallback list* of XBRL tags — try them in order, take the first that resolves, and
  record which tag won.
- **EBITDA has no standard tag.** Derive it (operating income + depreciation &
  amortization) and record the components.
- The key ratios, each tracked per period:
  - Leverage = net debt / EBITDA
  - Interest coverage = EBITDA / interest expense
  - Free cash flow = operating cash flow − capex (and FCF margin)
  - Liquidity = cash vs. near-term obligations
  - Near-term debt maturities / refinancing risk (from the debt footnote)
  - Covenant proximity (stretch — parsed/LLM-read from footnote text)

---

## Suggested repo layout

```
src/
  ingest.py        SEC fetch: ticker→CIK, filings, company facts; rate-limit + cache
  extract.py       deterministic ratios; one function per ratio, each returns value+inputs
  concepts.py      XBRL tag fallback lists per concept (the messy-tagging map)
  llm_review.py    qualitative pass; returns findings+evidence, NEVER numbers
  score.py         combine numbers + findings into a stress score per issuer per period
  store.py         persist results per (issuer, period) so trends form over time
  backtest.py      point-in-time harness: case library, lead-time + false-positive report
tests/
cache/             cached SEC responses (gitignore)
data/cases.csv     distressed issuers (+ event date) and healthy controls
```

---

## Commands

```bash
pip install -r requirements.txt
python -m src.ingest AAPL          # smoke-test ingestion for one ticker
python -m src.extract AAPL         # print ratios + their inputs for recent periods
python -m src.backtest             # run the backtest, print per-case pass/fail + lead time
pytest -q                          # tests
```

---

## Build order (1-week phased plan)

- **Phase 1 (Days 1–2) — Understand the analyst workflow.** Output is a *written spec*
  (`docs/spec.md`): for each ratio, the XBRL tags + fallbacks, the formula, and whether it
  is structurally extractable or must be read from footnote text. Do this part by hand /
  in discussion — it is the core learning of the week. Don't code-gen it away.
- **Phase 2 (Days 3–5) — MVP.** Ingestion → deterministic extraction → time-series store →
  a simple "add a company to track" flow. **Done when:** typing a real company shows its
  leverage / coverage / FCF / liquidity for the last several periods, each traceable to
  the source filing.
- **Phase 3 (Days 6–7) — Backtest.** Case library of known distressed issuers + healthy
  controls; point-in-time scoring (invariant #4); measure catch rate / lead time and false
  positives; emit a re-runnable pass/fail report. **Done when:** the backtest prints, per
  case, whether it was flagged early and by how many months.

Build and trust the deterministic numeric pipeline **first**. Add the LLM layer only after
the numbers track correctly for a few real issuers — it is an enhancement on an auditable
foundation, not a substitute for it.

---

## Conventions

- Python 3.11+. Prefer the standard library + `requests` + `pandas`; storage can start as
  SQLite or JSON keyed by (issuer, period).
- Write a test alongside each ratio using a small fixed `companyfacts` fixture (no live
  network in tests).
- Keep functions small and pure where possible: data in, value-with-provenance out.

## Out of scope (don't add unless asked)

- No trading signals, position sizing, or buy/sell recommendations.
- No paid data vendors — SEC EDGAR is free and is the only required source for the MVP.
- No LLM-generated numbers or scores of any kind (see invariant #1).
