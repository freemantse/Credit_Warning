// ─────────────────────────────────────────────────────────────────────────────
// lib/api.ts — Frontend API client and shared utilities
//
// All fetch calls use relative URLs (/api/...) so the same code works in:
//   Dev:  Next.js rewrites /api/* → Python at localhost:8000 (next.config.mjs)
//   Prod: Vercel routes /api/* → Python serverless function (vercel.json)
//
// This file has two responsibilities:
//   1. TypeScript interfaces that mirror the Python API response shapes.
//   2. Async fetch wrappers and display helpers used by all page components.
// ─────────────────────────────────────────────────────────────────────────────


// ── Type definitions ──────────────────────────────────────────────────────────
// These mirror the dict shapes returned by the Python FastAPI routes.
// Keeping them in one file means a change to the API only needs to be updated here.

/**
 * One row in the portfolio dashboard table.
 * Returned by GET /api/issuers — one entry per tracked ticker.
 */
export interface IssuerSummary {
  cik: string                      // canonical 10-digit EDGAR id, e.g. "0000320193"
  ticker: string
  name: string                     // company display name, e.g. "Apple Inc."
  latest_period: string | null    // most recent fiscal year-end, e.g. "2023-09-30"
  period_count: number            // how many annual periods are stored for this issuer
  ebitda_margin: number | null    // EBITDA / revenue, decimal fraction (negative = operating loss)
  leverage: number | null         // net_debt / EBITDA (null = XBRL data missing)
  interest_coverage: number | null
  free_cash_flow: number | null   // raw dollars — use fmtFCF() to display
  fcf_margin: number | null       // decimal fraction, e.g. 0.12 = 12%
  liquidity: number | null        // cash / short_term_debt
  cash_flow_to_debt: number | null      // operating_cashflow / gross_debt (FFO/Debt proxy), decimal fraction
  debt_to_assets: number | null         // gross_debt / total_assets (gearing), decimal fraction
  score: number | null             // 0–100 stress score; null when no ratios are stored yet
  alerts: string[]                // human-readable triggered threshold messages
  implied_rating?: string | null  // S&P-style implied rating letter (e.g. "BBB-"); null when uncomputable
  rating_index?: number | null    // its position in RATING_SCALE (0 = AAA) — used for sorting
  rating_note?: string | null     // why there's no rating (e.g. financial-sector issuer); null otherwise
  outlook?: string | null         // Rating Outlook: "Positive" | "Stable" | "Negative" | null
  prediction?: RatingChangePrediction | null  // directional rating-change signal + a short "why"
}

/**
 * Rating Outlook — a directional signal (where the rating is headed) from the
 * trend of the ratio-derived score plus the implied-vs-agency gap (Stage 0).
 * Returned at the issuer level by GET /api/issuer/{ticker}.
 */
export interface RatingOutlook {
  outlook: 'Positive' | 'Stable' | 'Negative'
  trend_pressure: number          // -1 improving, 0 flat, +1 worsening (from the score/rating trend)
  gap_pressure: number            // -1, 0, +1 (from implied − agency gap; 0 until agency data exists)
  gap: number | null              // implied − agency rating_index (+ = implied worse); null when no agency rating
  rating_change: number | null    // implied rating_index change over the window (+ = worse)
  score_change: number | null     // stress-score change over the window (+ = worse)
  reasons: string[]               // human-readable "why" lines (the auditable explanation)
  periods_used: number
}

/**
 * S&P-style implied credit rating for one fiscal period (from src/rating.py).
 * Derived deterministically from the period's ratios — orthogonal to the stress score.
 */
export interface ImpliedRating {
  implied_rating: string                  // rating letter, e.g. "BBB-"
  rating_index: number                    // position in RATING_SCALE (0 = AAA)
  financial_risk_profile: string          // e.g. "Intermediate"
  financial_risk_index: number            // 1..6 (1 = Minimal)
  business_risk_index: number             // 1..6 (1 = Excellent); default until supplied
  // Per-sub-factor audit: { ffo_to_debt: { value, profile, profile_name, source_ratio, overridden }, ... }
  subscores: Record<string, {
    value: number | null
    profile: number | null
    profile_name: string | null
    source_ratio: string
    overridden: boolean
  }>
  notes: string[]                         // human-readable explanation lines (proxy caveat, overrides, …)
}

/**
 * Full ratio data for one ratio in one fiscal period.
 * Includes the XBRL audit trail so the detail page can show source tags.
 */
export interface RatioData {
  value: number | null                 // null when the ratio couldn't be computed
  inputs: Record<string, number>      // raw dollar inputs (subset that resolved, if missing)
  source_tags: Record<string, string> // XBRL tag per resolved input, e.g. { total_debt: "us-gaap/LongTermDebt" }
  // Present only for a missing ratio: which raw inputs are absent and the tags tried.
  missing_inputs?: { field: string; tags_tried: string[] }[]
  reason?: string                      // why it's missing (e.g. guard "EBITDA is zero")
  not_applicable?: boolean             // ratio is structurally N/A for this issuer
                                       // (e.g. current ratio on an unclassified balance sheet)
}

/**
 * One qualitative finding from the LLM review of a 10-K filing.
 * Findings are only present if no_llm=false was used during tracking.
 */
export interface Finding {
  concern: string          // qualitative label, e.g. "Going-concern language"
  severity: 'low' | 'medium' | 'high'
  evidence_quote: string   // verbatim excerpt from the filing text
  source: string           // e.g. "10-K 2023-12-31, MD&A"
  source_url?: string      // EDGAR doc URL; optional — absent on older findings
}

/**
 * Deterministic long-term-debt maturity schedule for one period (from XBRL).
 * Drives the "maturity wall" block on the detail page.
 */
export interface MaturitySchedule {
  buckets: Record<string, number>      // { y1, y2, ..., y5, thereafter } principal due
  source_tags: Record<string, string>  // winning XBRL tag per bucket (audit trail)
  total_scheduled: number
  near_term_pct: number | null         // (y1 + y2 + y3) / total — the concentration metric
  wall_year: string | null             // bucket with the most principal due
}

/**
 * One maintenance covenant extracted from the debt footnote (LLM, hybrid).
 * Numeric fields are null unless the figure appears verbatim in evidence_quote.
 */
export interface Covenant {
  covenant_type: string                // max_leverage | min_coverage | min_net_worth | other
  threshold: number | null             // the limit, if reliably parsed
  direction: 'max' | 'min'
  reported_actual: number | null       // current level, if disclosed
  near_limit: boolean                  // sits close to / at risk of breaching the limit
  evidence_quote: string               // verbatim quote
  source: string
}

/**
 * One litigation/contingency provision from the commitments footnote (LLM, hybrid).
 */
export interface LossProvision {
  matter: string                       // short label of the matter
  provision_amount: number | null      // accrued amount, if reliably parsed
  is_material: boolean
  qualitative_flag: string             // e.g. "reasonably possible loss, not accrued"
  evidence_quote: string               // verbatim quote
  source: string
}

/**
 * One debt instrument enumerated from the debt footnote (LLM, hybrid), with its
 * seniority — the basis for issue-level notching and the senior-secured screen.
 */
export interface BondInstrument {
  instrument_name: string
  seniority: 'senior_secured' | 'senior_unsecured' | 'subordinated' | 'other'
  principal_amount: number | null
  coupon: number | null
  maturity_year: number | null
  evidence_quote: string
  source: string
}

/**
 * One signed driver behind a migration prediction (baseline-difference attribution).
 * `contribution` is in probability points (+ raised downgrade risk, − lowered it).
 */
export interface MigrationDriver {
  feature: string
  value: number | null
  baseline: number
  contribution: number
  direction: 'raises' | 'lowers'
  // Year-over-year % move of the underlying ratio (now vs the prior period),
  // server-computed so it matches the "why" reason text. Null when the feature
  // isn't a tracked ratio or the prior value is missing/zero.
  pct_change?: number | null
}

/**
 * Calibrated rating-migration prediction for one period (Stage 3 model). Present
 * only after the model has been trained and predictions written; null otherwise.
 */
export interface MigrationPrediction {
  horizon_months: number
  p_downgrade: number | null
  p_upgrade: number | null
  p_distress: number | null             // P(transition into CCC+/default within horizon)
  drivers_json: MigrationDriver[]
  model_version: string
  reason?: string                       // plain-language "why" (server-built); present on issuer detail
  direction?: 'down' | 'stable' | 'up'
  source?: 'model' | 'outlook'
}

/**
 * Unified directional "rating change" signal for an issuer (GET /api/issuers).
 * `source` is 'model' once the migration model is trained, else 'outlook'.
 */
export interface RatingChangePrediction {
  direction: 'down' | 'stable' | 'up'
  p_downgrade: number | null
  p_upgrade: number | null
  source: 'model' | 'outlook'
  reason: string
}

/**
 * All data for one fiscal year of one issuer.
 * Returned as an array in the IssuerDetail response, newest period first.
 */
export interface PeriodData {
  period_end: string
  ratios: Record<string, RatioData>   // keyed by ratio name: "leverage", "liquidity", etc.
  score: number
  breakdown: Record<string, number>   // per-component points, e.g. { "leverage>5x": 25.0 }
  alerts: string[]
  findings: Finding[]                 // empty if no LLM review was run for this period
  maturities?: MaturitySchedule | null // XBRL maturity schedule (always present after track)
  covenants?: Covenant[]              // LLM-extracted covenants (empty if no LLM review)
  loss_provisions?: LossProvision[]   // LLM-extracted provisions (empty if no LLM review)
  source_url?: string | null          // public SEC EDGAR URL of the 10-K these ratios came from
  implied_rating?: ImpliedRating | null  // S&P-style implied rating (null when uncomputable)
  bond_instruments?: BondInstrument[]  // LLM-extracted debt instruments + seniority (empty if no LLM review)
  migration?: MigrationPrediction | null  // calibrated migration prediction (null until the model runs)
  // Primary agency's actual rating as-of this period_end (forward-filled, absorbing
  // at withdrawal/default) — overlaid on the implied-rating chart. Null pre-ingest.
  agency_rating_index?: number | null
  agency_rating?: string | null          // …as a notation letter (AAA…D)
}

/**
 * One dated agency rating action (from the real Moody's/Fitch/Egan-Jones history).
 * rating_index is null for non-notch statuses (withdrawn / not_rated).
 */
export interface AgencyRatingEvent {
  effective_date: string
  rating_index: number | null
  rating_raw: string | null              // raw notation as pulled (e.g. "Baa3", "BB+")
  rating_status: 'rated' | 'withdrawn' | 'not_rated' | 'default'
  rating_action: string | null           // new | upgrade | downgrade | affirm | withdrawn | default
}

/**
 * Full issuer detail response from GET /api/issuer/{ticker}.
 * periods are ordered newest-first — the frontend chart reverses them for display.
 */
export interface IssuerDetail {
  ticker: string
  name: string                     // company display name, e.g. "Apple Inc."
  periods: PeriodData[]
  outlook?: RatingOutlook | null   // directional Rating Outlook (null when no usable history)
  // Headline directional rating-change signal (model once trained, else outlook) —
  // shown as the banner atop the trend chart. Null when neither is available.
  prediction?: RatingChangePrediction | null
  // Real agency rating actions, keyed by agency code (MDY | FTC | SPI | EJR).
  agency_ratings?: Record<string, AgencyRatingEvent[]>
  primary_agency?: string | null   // the agency overlaid on the implied-rating chart
  rating_note?: string | null      // why there's no implied rating (e.g. financial-sector); null otherwise
}

/**
 * Backtest task status polled from GET /api/backtest/status.
 * The frontend polls this every 3 seconds while running === true.
 */
export interface BacktestStatus {
  running: boolean
  result: BacktestResult | null  // populated when the task completes successfully
  error: string | null           // populated if the task threw an exception
  saved?: boolean                // true when result was loaded from a previous run on disk
}

/**
 * One point-in-time scoring snapshot in a case's backtest trajectory.
 * Snapshots are ordered newest-first (T-0 first, then back in ~90-day steps).
 */
export interface TrajectoryPoint {
  eval_date: string            // the simulated "today" the score was computed at
  months_before_event: number  // distance from the event/anchor date, e.g. 35.5
  score: number                // 0–100 stress score at this snapshot
  stressed: boolean            // score ≥ threshold at this snapshot
  has_data: boolean            // false = no filings existed yet at this date
  period_end: string | null    // fiscal period the score was computed against
  missing_ratios: number       // core ratios that couldn't be computed
  // Per-metric values as seen at this snapshot (null = not computable then).
  // Keys: leverage, interest_coverage, free_cash_flow, fcf_margin,
  // ebitda_margin, liquidity, maturity_near_term_pct.
  ratios?: Record<string, number | null>
}

/**
 * One row in the backtest results table.
 * The label field determines which columns are relevant:
 *   "distressed" → event_date, status, caught, lead_months, early_warning
 *   "healthy"    → fp_count, periods_evaluated
 */
export interface BacktestCase {
  ticker: string
  label: string                // "distressed" or "healthy"
  case_id?: string             // stable slug, e.g. "hertz-2020"
  company_name?: string        // display name, e.g. "Hertz Global Holdings"
  cik?: string | null
  // "caught" | "missed" | "data_gap" | "clean" | "false_positive" | "error"
  status?: string
  event_date?: string          // ISO date of the credit event (distressed only)
  caught?: boolean             // true if the model flagged it before the event
  lead_months?: number         // months from first flag to event (distressed + caught)
  earliest_flag_date?: string | null  // first snapshot that crossed the threshold
  early_warning?: boolean      // caught with lead_months ≥ early_months
  fp_count?: number            // count of false-positive quarters (healthy only)
  periods_evaluated?: number   // healthy snapshots that actually had data
  trajectory?: TrajectoryPoint[]  // full point-in-time score history
  error: string | null         // non-null if EDGAR fetch or scoring failed
}

export interface BacktestResult {
  run_at?: string               // ISO timestamp of the run
  threshold?: number            // stress threshold the run used
  early_months?: number         // early-warning cutoff the run used
  steps?: number                // point-in-time snapshots per case (~90 days apart)
  config?: ScoreConfig          // scoring parameters this run was scored with (provenance)
  cases: BacktestCase[]
  summary: {
    catch_rate: number          // percentage of distressed cases that were caught
    caught: number              // absolute count of caught cases
    total_distressed: number    // caught + missed (data gaps / errors excluded)
    median_lead_months: number  // median months of advance warning for caught cases
    fp_rate: number             // false-positive rate across all healthy control periods
    // Additive fields (present from the scorecard rework onward).
    missed?: number
    data_gaps?: number          // cases with no filings in the whole window
    errors?: number             // cases that failed to resolve/fetch
    mean_lead_months?: number
    early_warning_caught?: number  // caught with lead ≥ early_months
    early_warning_rate?: number    // … as % of total_distressed
    early_months?: number
    fp_periods?: number
    healthy_periods_evaluated?: number
    threshold?: number
  }
}


/**
 * One row of the backtest case library (Supabase `cases` table) — identity only,
 * no run results. Served by GET /api/backtest/cases and returned by POST /api/cases.
 */
export interface BacktestCaseInfo {
  case_id: string
  company_name: string
  ticker: string
  cik: string
  label: string                // "distressed" or "healthy" (legacy axis)
  event_type?: string          // downgrade | upgrade | default | control
  agency?: string              // MDY|FTC|SPI for a rating-migration event, else ""
  event_date: string           // the rating-event / Ch.11 date (or pinned anchor for controls)
  notes: string
}

export interface CaseLibrary {
  total: number
  distressed: number
  healthy: number
  cases: BacktestCaseInfo[]
}

/** Payload for POST /api/cases — add a case to the backtest library. */
export interface AddCasePayload {
  identifier: string            // ticker (e.g. "BTU") or CIK
  event_type: 'downgrade' | 'upgrade' | 'default' | 'control'
  agency?: string               // MDY|FTC|SPI for a rating-migration event (optional)
  event_date?: string           // "YYYY-MM-DD"; required for non-control events
  notes?: string
  case_id?: string              // optional explicit slug; auto-generated when omitted
}

/**
 * One quantitative scoring rule: max points (weight) plus the ramp endpoints.
 * The ramp awards 0 pts at `healthy`, rising linearly to `weight` at `severe`.
 */
export interface ScoreRule {
  weight: number
  healthy: number
  severe: number
}

/**
 * The full set of tunable stress-score parameters. Mirrors src/score.py's
 * DEFAULT_CONFIG. `rules` is keyed by the breakdown keys (e.g. "leverage>5x").
 */
export interface ScoreConfig {
  rules: Record<string, ScoreRule>
  ebitda_override: Record<string, number>   // points forced when EBITDA ≤ 0
  llm: {
    high_severity_per: number; high_severity_cap: number
    covenant_per: number; covenant_cap: number
    provision_per: number; provision_cap: number
    combined_cap: number
  }
  score_cap: number
  escalation: { min_severe: number; severe_frac: number; floor: number }
  threshold: number
}

// ── API fetch functions ───────────────────────────────────────────────────────
// Each function throws an Error on non-OK responses so calling code can catch
// and display the error message to the user.

/** Fetch all tracked issuers with their latest-period ratios and scores. */
export async function fetchIssuers(): Promise<IssuerSummary[]> {
  const res = await fetch('/api/issuers')
  if (!res.ok) throw new Error('Failed to fetch issuers')
  return res.json()
}

/**
 * Start tracking a new issuer (or refresh an existing one).
 *
 * `identifier` may be either a ticker (e.g. "AAPL") or a CIK (e.g. "320193" or
 * "0000320193"); the backend resolves both to the canonical CIK.
 *
 * noLlm defaults to true — the LLM qualitative pass takes ~30 s per filing
 * and is skipped by default in the UI to keep the "Track" button responsive.
 * The user can enable it by setting noLlm=false, but the UI doesn't expose this yet.
 */
export async function trackIssuer(identifier: string, noLlm = true): Promise<TrackResult> {
  const res = await fetch('/api/track', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // The API field is still named `ticker` but accepts a ticker or a CIK.
    // Omitting `periods` lets the backend fetch the full available history
    // (~15 years — XBRL data only goes back to ~2009).
    body: JSON.stringify({ ticker: identifier, no_llm: noLlm }),
  })
  if (!res.ok) {
    // Parse the FastAPI error detail. It is either a plain string or a structured
    // object {code, message} (e.g. NO_XBRL_DATA for an untrackable issuer); surface
    // the message and carry the code so the caller can show a tailored hint.
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    const detail = (err as { detail?: unknown }).detail
    const isObj = detail !== null && typeof detail === 'object'
    const message = typeof detail === 'string'
      ? detail
      : (isObj && (detail as { message?: string }).message) || 'Failed to track issuer'
    const error = new Error(message) as Error & { code?: string }
    if (isObj && (detail as { code?: string }).code) error.code = (detail as { code: string }).code
    throw error
  }
  // The backend echoes the resolved identity (cik, ticker, name) so the UI can
  // show a confirmation that names the company and its CIK.
  return res.json()
}

/**
 * Identity echoed back by POST /api/track after a successful track.
 * Used to confirm to the user exactly which company was resolved and added.
 */
export interface TrackResult {
  cik: string
  ticker: string                  // resolved current ticker, or the input if none
  name: string                    // company display name, e.g. "Apple Inc."
  periods_saved: number
  periods: string[]
}

/** Fetch full ratio history and scores for one issuer. */
export async function fetchIssuer(ticker: string): Promise<IssuerDetail> {
  const res = await fetch(`/api/issuer/${ticker}`)
  if (!res.ok) throw new Error(`Failed to fetch ${ticker}`)
  return res.json()
}

/** Remove all stored data for a ticker from Supabase. */
export async function deleteIssuer(ticker: string): Promise<void> {
  const res = await fetch(`/api/issuer/${ticker}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Failed to delete ${ticker}`)
}

/**
 * Progress of the on-demand LLM review for one issuer, polled from
 * GET /api/issuer/{ticker}/llm-review/status. The detail page polls this every
 * few seconds while running === true.
 */
export interface LlmReviewStatus {
  running: boolean
  error: string | null         // non-null if the run aborted (e.g. EDGAR fetch failed)
  periods_done: number         // filings reviewed so far
  periods_total: number        // filings this run will review (0 = no run yet)
}

/**
 * Kick off the LLM qualitative review for an already-tracked issuer as a
 * server-side background task. Returns immediately — poll fetchLlmReviewStatus()
 * to watch for completion, then re-fetch the issuer to show the findings.
 *
 * `periods` caps how many most-recent annual filings are reviewed (the pass is
 * ~30 s/filing). Defaults to the backend's cap (3) when omitted.
 * Throws if a review is already running for this issuer (409 Conflict).
 */
export async function startLlmReview(ticker: string, periods?: number): Promise<void> {
  const res = await fetch(`/api/issuer/${ticker}/llm-review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(periods != null ? { periods } : {}),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    throw new Error(err.detail || 'Failed to start LLM review')
  }
}

/** Check the current state of the background LLM review for one issuer. */
export async function fetchLlmReviewStatus(ticker: string): Promise<LlmReviewStatus> {
  const res = await fetch(`/api/issuer/${ticker}/llm-review/status`)
  if (!res.ok) throw new Error('Failed to fetch LLM review status')
  return res.json()
}

/**
 * Kick off the backtest as a server-side background task.
 * Returns immediately — poll fetchBacktestStatus() to watch for completion.
 * Throws if a backtest is already running (409 Conflict).
 *
 * `steps` sets the point-in-time history depth (snapshots per case, ~90 days
 * apart). Omitted → the server's default. The server clamps it to a safe range.
 * The run uses the active stress-score config (model-learned weights when trained).
 */
export async function startBacktest(steps?: number): Promise<void> {
  const body: { steps?: number } = {}
  if (steps != null) body.steps = steps
  const res = await fetch('/api/backtest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    throw new Error(err.detail || 'Failed to start backtest')
  }
}

/**
 * Check the current state of the background backtest task.
 * The backtest page calls this every 3 seconds while status.running === true.
 */
export async function fetchBacktestStatus(): Promise<BacktestStatus> {
  const res = await fetch('/api/backtest/status')
  if (!res.ok) throw new Error('Failed to fetch backtest status')
  return res.json()
}

/** Fetch the backtest case library (which companies are tested, with counts). */
export async function fetchBacktestCases(): Promise<CaseLibrary> {
  const res = await fetch('/api/backtest/cases')
  if (!res.ok) throw new Error('Failed to fetch backtest case library')
  return res.json()
}

/**
 * Add a case to the backtest library. The backend resolves the ticker/CIK to a
 * canonical CIK + name via EDGAR, so the returned row carries the resolved
 * identity. Throws on a bad ticker (404), missing event_date for distressed
 * (400), or a duplicate case_id (409).
 */
export async function addCase(payload: AddCasePayload): Promise<BacktestCaseInfo> {
  const res = await fetch('/api/cases', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    throw new Error(err.detail || 'Failed to add case')
  }
  return res.json()
}

/** Remove a case from the backtest library by case_id. */
export async function deleteCase(caseId: string): Promise<void> {
  const res = await fetch(`/api/cases/${encodeURIComponent(caseId)}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    throw new Error(err.detail || 'Failed to delete case')
  }
}

// ── Rating-migration model: event backtest + walk-forward scorecard ───────────

/** One case row in the migration event backtest (per-issuer rating-event result). */
export interface MigrationCaseResult {
  case_id?: string
  ticker: string
  company_name?: string
  event_type: string                     // downgrade | upgrade | default | control
  event_date?: string | null
  status?: string                        // caught | missed | data_gap | clean | false_positive | error
  caught?: boolean
  early_warning?: boolean
  lead_months?: number | null
  fp_count?: number
  trajectory?: {
    eval_date: string
    months_before_event: number
    prob: number | null
    flagged: boolean
    score?: number | null                       // point-in-time stress score (0–100)
    ratios?: Record<string, number | null>      // ratio levels at that snapshot
  }[]
  error?: string | null
}

/** Per-event-type summary of the migration event backtest. */
export interface MigrationEventSummary {
  catch_rate?: number
  caught?: number
  total: number
  median_lead_months?: number
  early_warning_rate?: number
  false_positive?: number       // control rows: count of false positives
  fp_rate?: number
}

export interface MigrationBacktestStatus {
  running: boolean
  result: {
    run_at?: string
    threshold?: number
    by_event_type: Record<string, MigrationEventSummary>
    cases: MigrationCaseResult[]
    note?: string
  } | null
  error: string | null
  saved?: boolean
}

/** Aggregate walk-forward metrics per head (read-only scorecard). */
export interface MigrationHeadAgg {
  mean_pr_auc_model: number | null
  mean_pr_auc_baseline: number | null
  n_splits_scored: number
}

export interface MigrationBacktest {
  migration: {
    split_dates: string[]
    aggregate: Record<string, MigrationHeadAgg>
    confusion_by_bucket_final: Record<string, { n: number; tp?: number; fp?: number; tn?: number; fn?: number; actual_downgrade_rate?: number }>
  } | null
  model: { version: string; train_window?: Record<string, unknown>; metrics_json?: Record<string, unknown> } | null
}

/** Start the migration EVENT backtest (runs the trained model over the case library). */
export async function startMigrationBacktest(steps?: number): Promise<void> {
  const body: { steps?: number } = {}
  if (steps != null) body.steps = steps
  const res = await fetch('/api/migration/backtest', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    throw new Error(err.detail || 'Failed to start migration backtest')
  }
}

/** Poll the migration event-backtest status. */
export async function fetchMigrationBacktestStatus(): Promise<MigrationBacktestStatus> {
  const res = await fetch('/api/migration/backtest/status')
  if (!res.ok) throw new Error('Failed to fetch migration backtest status')
  return res.json()
}

/** Fetch the read-only walk-forward scorecard + active-model provenance. */
export async function fetchMigrationScorecard(): Promise<MigrationBacktest> {
  const res = await fetch('/api/migration/scorecard')
  if (!res.ok) throw new Error('Failed to fetch migration scorecard')
  return res.json()
}


// ── Senior-secured screen ─────────────────────────────────────────────────────

/** One ranked row of the senior-secured screen (GET /api/screen/senior-secured). */
export interface ScreenRow {
  cik: string
  ticker: string
  name: string
  instrument_name: string | null
  seniority: string
  principal_amount: number | null
  coupon: number | null
  maturity_year: number | null
  issuer_implied_rating: string         // issuer's implied rating letter
  issuer_rating_index: number
  instrument_notched_rating: string | null  // issuer rating notched for seniority
  instrument_notched_index: number | null
  outlook: string | null                // Rating Outlook (fallback forward filter)
  p_downgrade: number | null            // calibrated P(downgrade) when the model has run, else null
  period_end: string
  evidence_quote: string | null
  source: string | null
}

export interface ScreenResponse {
  meta: {
    min_rating: string
    exclude_negative_outlook: boolean
    seniority: string
    issuers_with_instruments: number
    matches: number
    downgrade_signal: string            // "rating_outlook" until the ML model lands
  }
  rows: ScreenRow[]
}

/**
 * Fetch the senior-secured screen. `minRating` is the issuer health floor (default
 * "BBB-"); `excludeNegative` drops issuers with a Negative Rating Outlook.
 */
export async function fetchScreen(minRating = 'BBB-', excludeNegative = true): Promise<ScreenResponse> {
  const params = new URLSearchParams({ min_rating: minRating, exclude_negative: String(excludeNegative) })
  const res = await fetch(`/api/screen/senior-secured?${params}`)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    throw new Error(err.detail || 'Failed to fetch screen')
  }
  return res.json()
}


// ── Display formatting helpers ────────────────────────────────────────────────
// These are pure functions — no side effects, no API calls.
// Centralised here so every page uses the same formatting logic.

/**
 * Format a ratio (e.g. leverage) as "3.20×".
 * Returns "—" for null/undefined values (missing XBRL data).
 * dp controls decimal places (default 2).
 */
export function fmtRatio(val: number | null | undefined, dp = 2): string {
  if (val == null) return '—'
  return val.toFixed(dp) + '×'
}

/**
 * Format a raw FCF dollar value into a compact human-readable string.
 *
 * EDGAR stores FCF in raw dollars (e.g. 5000000000 = $5B).
 * We convert to millions first, then to billions if the absolute value ≥ $1B.
 *
 * Examples:
 *   5000000000  → "$5.0B"
 *   250000000   → "$250M"
 *   -80000000   → "$-80M"
 */
export function fmtFCF(val: number | null | undefined): string {
  if (val == null) return '—'
  const m = val / 1e6
  if (Math.abs(m) >= 1000) return `$${(m / 1000).toFixed(1)}B`  // ≥ $1B → billions
  return `$${m.toFixed(0)}M`
}

/**
 * Format a decimal fraction as a percentage string.
 * e.g. 0.1234 → "12.3%"
 * Returns "—" for null/undefined.
 */
export function fmtPct(val: number | null | undefined): string {
  if (val == null) return '—'
  return (val * 100).toFixed(1) + '%'
}

/**
 * Convert a numeric stress score into a human-readable risk label.
 *
 * Bands are aligned with STRESS_THRESHOLD = 50 from Python:
 *   0–24:   Healthy
 *   25–49:  Watch      (approaching stress territory)
 *   50–74:  Stressed   (≥ STRESS_THRESHOLD)
 *   75–100: High Risk
 */
export function scoreLabel(score: number): string {
  if (score >= 75) return 'High Risk'
  if (score >= 50) return 'Stressed'
  if (score >= 25) return 'Watch'
  return 'Healthy'
}

/**
 * Return a Tailwind CSS class string for the score badge background, text, and border.
 * Colours match the risk bands in scoreLabel() — same cut-points, different visual output.
 */
export function scoreBg(score: number): string {
  if (score >= 75) return 'bg-red-100 text-red-800 border border-red-200'
  if (score >= 50) return 'bg-orange-100 text-orange-800 border border-orange-200'
  if (score >= 25) return 'bg-yellow-100 text-yellow-800 border border-yellow-200'
  return 'bg-green-100 text-green-800 border border-green-200'
}

/**
 * The ordered rating scale, best (index 0) to worst. Mirrors src/rating.py's
 * RATING_SCALE so the frontend can map a stored rating_index back to its letter
 * (e.g. for the rating trend chart's Y-axis ticks).
 */
export const RATING_SCALE = [
  'AAA',
  'AA+', 'AA', 'AA-',
  'A+', 'A', 'A-',
  'BBB+', 'BBB', 'BBB-',
  'BB+', 'BB', 'BB-',
  'B+', 'B', 'B-',
  'CCC+', 'CCC', 'CCC-',
] as const

/** Map a rating_index back to its letter (clamped). Returns '—' for null/undefined. */
export function ratingFromIndex(idx: number | null | undefined): string {
  if (idx == null) return '—'
  const i = Math.max(0, Math.min(RATING_SCALE.length - 1, Math.round(idx)))
  return RATING_SCALE[i]
}

/**
 * Return a Tailwind CSS class string for an implied-rating badge, banded by
 * broad credit grade (parallels scoreBg — but for the rating letter, not the
 * stress score):
 *   A- and above  → green   (high investment grade)
 *   BBB+ … BBB-   → yellow  (low investment grade — the IG floor / crossover)
 *   BB+ … B-      → orange  (speculative grade)
 *   CCC and below → red     (distressed)
 * Matching is by the letter prefix so every +/- notch maps to the right band.
 */
export function ratingBg(rating: string): string {
  if (rating.startsWith('CCC') || rating.startsWith('CC') || rating === 'C' || rating === 'D')
    return 'bg-red-100 text-red-800 border border-red-200'
  // BBB must be tested before BB/B since "BBB".startsWith("BB") is also true.
  if (rating.startsWith('BBB'))
    return 'bg-yellow-100 text-yellow-800 border border-yellow-200'
  if (rating.startsWith('BB') || rating.startsWith('B'))
    return 'bg-orange-100 text-orange-800 border border-orange-200'
  // AAA, AA*, A* → high investment grade.
  return 'bg-green-100 text-green-800 border border-green-200'
}

/**
 * Visual treatment for a Rating Outlook signal: an arrow + colour banded by
 * direction. Negative (downgrade pressure) is red with a down arrow, Positive
 * (upgrade) green with an up arrow, Stable slate with a right arrow.
 * Note the inversion vs. an equity view: a *downgrade* outlook is the risk, so
 * Negative is red (bad for a bondholder).
 */
export function outlookBadge(outlook: string | null | undefined): { arrow: string; label: string; cls: string } | null {
  if (!outlook) return null
  if (outlook === 'Negative') return { arrow: '↓', label: 'Negative', cls: 'bg-red-100 text-red-700 border border-red-200' }
  if (outlook === 'Positive') return { arrow: '↑', label: 'Positive', cls: 'bg-green-100 text-green-700 border border-green-200' }
  return { arrow: '→', label: 'Stable', cls: 'bg-slate-100 text-slate-600 border border-slate-200' }
}

/**
 * Friendly label + badge classes for a debt instrument's seniority. Senior secured
 * (best recovery) reads green; subordinated (worst) red; senior unsecured neutral.
 */
export function seniorityBadge(seniority: string): { label: string; cls: string } {
  switch (seniority) {
    case 'senior_secured':
      return { label: 'Senior Secured', cls: 'bg-green-100 text-green-800 border border-green-200' }
    case 'senior_unsecured':
      return { label: 'Senior Unsecured', cls: 'bg-slate-100 text-slate-700 border border-slate-200' }
    case 'subordinated':
      return { label: 'Subordinated', cls: 'bg-red-100 text-red-700 border border-red-200' }
    default:
      return { label: 'Other', cls: 'bg-gray-100 text-gray-600 border border-gray-200' }
  }
}

/**
 * Return a Tailwind dot colour class for a finding's severity level.
 * Used as the coloured circle to the left of each finding in the findings list.
 *   high   → red dot
 *   medium → yellow dot
 *   low    → blue dot
 */
export function severityDot(severity: string): string {
  if (severity === 'high') return 'bg-red-500'
  if (severity === 'medium') return 'bg-yellow-500'
  return 'bg-blue-400'
}
