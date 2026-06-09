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
  score: number                   // 0–100 stress score from compute_score()
  alerts: string[]                // human-readable triggered threshold messages
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
}

/**
 * Deterministic long-term-debt maturity schedule for one period (from XBRL).
 * Drives the "maturity wall" block on the detail page.
 */
export interface MaturitySchedule {
  buckets: Record<string, number>      // { y1, y2, ..., y5, thereafter } principal due
  source_tags: Record<string, string>  // winning XBRL tag per bucket (audit trail)
  total_scheduled: number
  near_term_pct: number | null         // (y1 + y2) / total — the concentration metric
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
}

/**
 * Full issuer detail response from GET /api/issuer/{ticker}.
 * periods are ordered newest-first — the frontend chart reverses them for display.
 */
export interface IssuerDetail {
  ticker: string
  name: string                     // company display name, e.g. "Apple Inc."
  periods: PeriodData[]
}

/**
 * Backtest task status polled from GET /api/backtest/status.
 * The frontend polls this every 3 seconds while running === true.
 */
export interface BacktestStatus {
  running: boolean
  result: BacktestResult | null  // populated when the task completes successfully
  error: string | null           // populated if the task threw an exception
}

/**
 * One row in the backtest results table.
 * The label field determines which columns are relevant:
 *   "distressed" → event_date, caught, lead_months
 *   "healthy"    → fp_count
 */
export interface BacktestCase {
  ticker: string
  label: string                // "distressed" or "healthy"
  event_date?: string          // ISO date of the credit event (distressed only)
  caught?: boolean             // true if the model flagged it before the event
  lead_months?: number         // months from first flag to event (distressed + caught)
  fp_count?: number            // count of false-positive quarters (healthy only)
  error: string | null         // non-null if EDGAR fetch or scoring failed
}

export interface BacktestResult {
  cases: BacktestCase[]
  summary: {
    catch_rate: number          // percentage of distressed cases that were caught
    caught: number              // absolute count of caught cases
    total_distressed: number    // total distressed cases evaluated
    median_lead_months: number  // median months of advance warning for caught cases
    fp_rate: number             // false-positive rate across all healthy control periods
  }
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
    // Try to parse the FastAPI error detail; fall back to a generic message.
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    throw new Error(err.detail || 'Failed to track issuer')
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
 * Kick off the backtest as a server-side background task.
 * Returns immediately — poll fetchBacktestStatus() to watch for completion.
 * Throws if a backtest is already running (409 Conflict).
 */
export async function startBacktest(): Promise<void> {
  const res = await fetch('/api/backtest', { method: 'POST' })
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
