// All URLs are relative (/api/...) — routing is handled by infrastructure:
//   Dev:  next.config.mjs proxies /api/* → Python at localhost:8000
//   Prod: vercel.json routes  /api/* → Python serverless function

export interface IssuerSummary {
  ticker: string
  latest_period: string | null
  period_count: number
  leverage: number | null
  interest_coverage: number | null
  free_cash_flow: number | null
  fcf_margin: number | null
  liquidity: number | null
  score: number
  alerts: string[]
}

export interface RatioData {
  value: number
  inputs: Record<string, number>
  source_tags: Record<string, string>
}

export interface Finding {
  concern: string
  severity: 'low' | 'medium' | 'high'
  evidence_quote: string
  source: string
}

export interface PeriodData {
  period_end: string
  ratios: Record<string, RatioData>
  score: number
  breakdown: Record<string, number>
  alerts: string[]
  findings: Finding[]
}

export interface IssuerDetail {
  ticker: string
  periods: PeriodData[]
}

export interface BacktestStatus {
  running: boolean
  result: BacktestResult | null
  error: string | null
}

export interface BacktestCase {
  ticker: string
  label: string
  event_date?: string
  caught?: boolean
  lead_months?: number
  fp_count?: number
  error: string | null
}

export interface BacktestResult {
  cases: BacktestCase[]
  summary: {
    catch_rate: number
    caught: number
    total_distressed: number
    median_lead_months: number
    fp_rate: number
  }
}

export async function fetchIssuers(): Promise<IssuerSummary[]> {
  const res = await fetch('/api/issuers')
  if (!res.ok) throw new Error('Failed to fetch issuers')
  return res.json()
}

export async function trackIssuer(ticker: string, noLlm = true): Promise<void> {
  const res = await fetch('/api/track', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticker, no_llm: noLlm, periods: 8 }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    throw new Error(err.detail || 'Failed to track issuer')
  }
}

export async function fetchIssuer(ticker: string): Promise<IssuerDetail> {
  const res = await fetch(`/api/issuer/${ticker}`)
  if (!res.ok) throw new Error(`Failed to fetch ${ticker}`)
  return res.json()
}

export async function deleteIssuer(ticker: string): Promise<void> {
  const res = await fetch(`/api/issuer/${ticker}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Failed to delete ${ticker}`)
}

export async function startBacktest(): Promise<void> {
  const res = await fetch('/api/backtest', { method: 'POST' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
    throw new Error(err.detail || 'Failed to start backtest')
  }
}

export async function fetchBacktestStatus(): Promise<BacktestStatus> {
  const res = await fetch('/api/backtest/status')
  if (!res.ok) throw new Error('Failed to fetch backtest status')
  return res.json()
}

// ── Formatting helpers ──────────────────────────────────────────────────────

export function fmtRatio(val: number | null | undefined, dp = 2): string {
  if (val == null) return '—'
  return val.toFixed(dp) + '×'
}

export function fmtFCF(val: number | null | undefined): string {
  if (val == null) return '—'
  const m = val / 1e6
  if (Math.abs(m) >= 1000) return `$${(m / 1000).toFixed(1)}B`
  return `$${m.toFixed(0)}M`
}

export function fmtPct(val: number | null | undefined): string {
  if (val == null) return '—'
  return (val * 100).toFixed(1) + '%'
}

export function scoreLabel(score: number): string {
  if (score >= 75) return 'High Risk'
  if (score >= 50) return 'Stressed'
  if (score >= 25) return 'Watch'
  return 'Healthy'
}

export function scoreBg(score: number): string {
  if (score >= 75) return 'bg-red-100 text-red-800 border border-red-200'
  if (score >= 50) return 'bg-orange-100 text-orange-800 border border-orange-200'
  if (score >= 25) return 'bg-yellow-100 text-yellow-800 border border-yellow-200'
  return 'bg-green-100 text-green-800 border border-green-200'
}

export function severityDot(severity: string): string {
  if (severity === 'high') return 'bg-red-500'
  if (severity === 'medium') return 'bg-yellow-500'
  return 'bg-blue-400'
}
