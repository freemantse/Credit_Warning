'use client'
// ─────────────────────────────────────────────────────────────────────────────
// app/issuer/[ticker]/page.tsx — Issuer Detail Page (route "/issuer/AAPL")
//
// This page shows the full multi-year credit history for one issuer:
//   1. A line chart of the stress score over time.
//   2. A ratio history table — each row can be expanded to show an XBRL source
//      audit (which tags were used and what their raw dollar values were).
//   3. A qualitative findings section (LLM results, if available).
//
// The ticker comes from the dynamic route segment: useParams<{ ticker: string }>().
// ─────────────────────────────────────────────────────────────────────────────

import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import {
  LineChart, Line, BarChart, Bar, Cell,
  XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer,
} from 'recharts'
import {
  IssuerDetail, PeriodData, Finding, Covenant, LossProvision, RatingOutlook, BondInstrument,
  fetchIssuer, trackIssuer, startLlmReview, fetchLlmReviewStatus,
  fmtRatio, fmtFCF, fmtPct, scoreBg, scoreLabel, severityDot,
  ratingBg, ratingFromIndex, RATING_SCALE, outlookBadge, seniorityBadge,
} from '@/lib/api'

// How many period rows to show per page in the Ratio History table.
const PAGE_SIZE = 10

// Metrics selectable in the trend chart's tab bar: the stress score plus each
// of the 6 financial ratios. `accessor` pulls the value out of a period,
// `fmt` formats it for the Y-axis ticks and tooltip, `domain` fixes the Y-axis
// scale (undefined → auto), and `threshold` (when set) draws a reference line.
const TREND_METRICS: {
  key: string
  label: string
  accessor: (p: PeriodData) => number | null | undefined
  fmt: (v: number) => string
  domain?: [number, number]
  threshold?: number
  reversed?: boolean   // invert the Y-axis (used by Implied Rating so AAA sits at top)
}[] = [
  { key: 'score',             label: 'Stress Score',      accessor: p => p.score,                       fmt: v => `${Math.round(v)}`, domain: [0, 100], threshold: 50 },
  { key: 'implied_rating',    label: 'Implied Rating',    accessor: p => p.implied_rating?.rating_index, fmt: v => ratingFromIndex(v), domain: [0, RATING_SCALE.length - 1], reversed: true },
  { key: 'ebitda_margin',     label: 'EBITDA Margin',     accessor: p => p.ratios.ebitda_margin?.value,     fmt: fmtPct },
  { key: 'leverage',          label: 'Leverage',          accessor: p => p.ratios.leverage?.value,          fmt: fmtRatio },
  { key: 'interest_coverage', label: 'Interest Coverage', accessor: p => p.ratios.interest_coverage?.value, fmt: fmtRatio },
  { key: 'free_cash_flow',    label: 'FCF',               accessor: p => p.ratios.free_cash_flow?.value,    fmt: fmtFCF },
  { key: 'fcf_margin',        label: 'FCF Margin',        accessor: p => p.ratios.fcf_margin?.value,        fmt: fmtPct },
  { key: 'liquidity',         label: 'Liquidity',         accessor: p => p.ratios.liquidity?.value,         fmt: fmtRatio },
  { key: 'cash_flow_to_debt',     label: 'Cash Flow / Debt',   accessor: p => p.ratios.cash_flow_to_debt?.value,     fmt: fmtPct },
  { key: 'current_ratio',         label: 'Current Ratio',      accessor: p => p.ratios.current_ratio?.value,         fmt: fmtRatio },
  { key: 'debt_to_assets',        label: 'Debt / Assets',      accessor: p => p.ratios.debt_to_assets?.value,        fmt: fmtPct },
]

export default function IssuerPage() {

  // ── State ───────────────────────────────────────────────────────────────────
  const { ticker } = useParams<{ ticker: string }>()  // from the dynamic route

  const [data, setData] = useState<IssuerDetail | null>(null)
  const [error, setError] = useState('')

  // True while POST /api/track (re-fetch from EDGAR) is running.
  const [refreshing, setRefreshing] = useState(false)

  // On-demand LLM review state. `llmRunning` drives the poll effect below;
  // `llmProgress` tracks periods_done/periods_total for the progress label.
  const [llmRunning, setLlmRunning] = useState(false)
  const [llmProgress, setLlmProgress] = useState<{ done: number; total: number } | null>(null)
  const [llmError, setLlmError] = useState('')

  // Holds the status poll interval ID without triggering re-renders (see the
  // backtest page for the same rationale — a ref avoids a clear/re-arm loop).
  const llmPollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Used to scroll the viewport to the qualitative-analysis section when the
  // user triggers the LLM review from the top-right button.
  const llmSectionRef = useRef<HTMLDivElement>(null)

  // The period_end string of the row currently showing the inline audit panel.
  // null = no row is expanded.
  const [openAudit, setOpenAudit] = useState<string | null>(null)

  // Zero-based page index for the Ratio History table (full history can be ~15 rows).
  const [page, setPage] = useState(0)

  // Which metric the trend chart is currently plotting (a key from TREND_METRICS).
  // Defaults to the stress score so the chart looks unchanged on load.
  const [selectedMetric, setSelectedMetric] = useState('score')


  // ── Data loading ────────────────────────────────────────────────────────────

  // Re-fetch whenever the ticker changes (handles browser back/forward navigation).
  useEffect(() => { load() }, [ticker])

  // On mount / ticker change, restore the LLM-review UI if a review is already
  // running for this issuer (e.g. the user navigated away and back mid-run).
  // The cleanup clears any poll interval on unmount or ticker change.
  useEffect(() => {
    fetchLlmReviewStatus(ticker).then(s => {
      if (s.running) {
        setLlmRunning(true)
        setLlmProgress({ done: s.periods_done, total: s.periods_total })
      }
    }).catch(() => {})
    return () => { if (llmPollRef.current) clearInterval(llmPollRef.current) }
  }, [ticker])

  // Scroll to the qualitative-analysis section when a review starts so the
  // user sees progress without having to manually scroll down.
  useEffect(() => {
    if (llmRunning) {
      llmSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [llmRunning])

  // While a review is running, poll its status every 3 s. When it finishes,
  // stop polling, reload the issuer data to show the new findings, and surface
  // any error the run reported.
  useEffect(() => {
    if (llmRunning) {
      llmPollRef.current = setInterval(async () => {
        const s = await fetchLlmReviewStatus(ticker).catch(() => null)
        if (!s) return
        setLlmProgress({ done: s.periods_done, total: s.periods_total })
        if (!s.running) {
          if (llmPollRef.current) clearInterval(llmPollRef.current)
          setLlmRunning(false)
          if (s.error) setLlmError(s.error)
          await load()  // reload so the LLM sections populate with the new data
        }
      }, 3000)
    }
    return () => { if (llmPollRef.current) clearInterval(llmPollRef.current) }
  }, [llmRunning, ticker])

  /** Fetch the issuer's full period history from the API. */
  async function load() {
    setError('')
    try {
      setData(await fetchIssuer(ticker))
      setPage(0)  // jump back to the newest periods whenever data (re)loads
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    }
  }

  /**
   * Re-run the EDGAR fetch for this ticker (equivalent to track() in the CLI).
   * Used when the user clicks "Refresh from EDGAR" to pick up newly filed periods.
   */
  async function handleRefresh() {
    setRefreshing(true)
    setError('')
    try {
      await trackIssuer(ticker)  // re-track: fetches latest EDGAR data + saves to DB
      await load()                // reload the UI with the updated data
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Refresh failed')
    } finally {
      setRefreshing(false)
    }
  }

  /**
   * Start the on-demand LLM qualitative review for the latest filings. Tracking
   * skips this pass for speed, so this is how the user populates the findings,
   * covenants, and loss-provisions sections. The poll effect picks up
   * llmRunning=true and reloads the data when the background task completes.
   */
  async function handleRunLlm() {
    setLlmError('')
    setLlmProgress(null)
    setLlmRunning(true)
    try {
      await startLlmReview(ticker)
      // Fetch status once immediately so the progress label appears without
      // waiting for the first poll tick.
      const s = await fetchLlmReviewStatus(ticker)
      setLlmProgress({ done: s.periods_done, total: s.periods_total })
    } catch (e: unknown) {
      setLlmError(e instanceof Error ? e.message : 'Failed to start LLM analysis')
      setLlmRunning(false)
    }
  }

  // ── Chart data preparation ─────────────────────────────────────────────────
  // The API returns periods newest-first, but the Recharts line chart plots
  // left-to-right chronologically — so we reverse before mapping.
  // [...data.periods] creates a shallow copy so we don't mutate the state array.
  // The metric the chart is plotting, and its values per period (chronological).
  // A missing ratio in a period maps to null; connectNulls bridges the gap.
  const metric = TREND_METRICS.find(m => m.key === selectedMetric) ?? TREND_METRICS[0]
  const chartData = data
    ? [...data.periods].reverse().map(p => ({ date: p.period_end, value: metric.accessor(p) ?? null }))
    : []

  // ── Ratio-history pagination ──────────────────────────────────────────────
  // The chart above shows the full history; the table is paged at PAGE_SIZE rows.
  const allPeriods = data?.periods ?? []
  const totalPages = Math.max(1, Math.ceil(allPeriods.length / PAGE_SIZE))
  const pagedPeriods = allPeriods.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE)

  // Whether any LLM-derived data exists across all periods. Drives the empty-state
  // hint: the three LLM sections each render null when empty, so without this the
  // page would be silent about why there's nothing to show.
  const hasLlmData = allPeriods.some(
    p => p.findings.length > 0 || (p.covenants?.length ?? 0) > 0 || (p.loss_provisions?.length ?? 0) > 0
  )


  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-8">

      {/* ── Header: back link, ticker, period count, refresh button ── */}
      <div className="flex items-start justify-between">
        <div>
          <Link href="/" className="text-sm text-slate-400 hover:text-slate-600 mb-2 inline-block">
            ← Portfolio
          </Link>
          {/* Company name as the primary heading; ticker shown as a monospace sub-label.
              Falls back to the route ticker until data loads. */}
          <h1 className="text-2xl font-bold text-slate-900">{data?.name || ticker}</h1>
          {data && (
            <p className="text-sm text-slate-400 mt-1">
              <span className="font-mono text-slate-500">{data.ticker}</span>
              {' · '}
              {data.periods.length} annual periods tracked
            </p>
          )}
        </div>
        <div className="mt-6 flex items-center gap-2">
          {/* Run the LLM qualitative pass on demand (tracking skips it for speed). */}
          <button
            onClick={handleRunLlm}
            disabled={llmRunning || refreshing}
            className="inline-flex items-center gap-2 text-sm bg-slate-800 hover:bg-slate-700 text-white px-4 py-2 rounded-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={llmRunning ? 'animate-pulse' : ''}>
              <path d="M12 3a6 6 0 0 0-6 6c0 2 1 3 1.5 4 .5.8.5 1.5.5 2h8c0-.5 0-1.2.5-2 .5-1 1.5-2 1.5-4a6 6 0 0 0-6-6Z" />
              <path d="M9 21h6" />
            </svg>
            {llmRunning
              ? llmProgress && llmProgress.total > 0
                ? `Analysing… ${llmProgress.done}/${llmProgress.total}`
                : 'Analysing…'
              : 'Run LLM analysis'}
          </button>
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className="inline-flex items-center gap-2 text-sm bg-slate-100 hover:bg-slate-200 text-slate-700 px-4 py-2 rounded-lg disabled:opacity-50 transition-colors"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={refreshing ? 'animate-spin' : ''}>
              <path d="M21 12a9 9 0 1 1-2.64-6.36" />
              <path d="M21 3v6h-6" />
            </svg>
            {refreshing ? 'Refreshing…' : 'Refresh from EDGAR'}
          </button>
        </div>
      </div>

      {/* ── Error banner ── */}
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-4 py-3">
          {error}
        </div>
      )}

      {/* Loading placeholder — shown until the first fetch completes. */}
      {!data && !error && (
        <div className="py-16 text-center text-slate-400 text-sm">Loading…</div>
      )}

      {/* ── Main content — only rendered after data is available ── */}
      {data && (
        <>
          {/* ── Score trend chart ──────────────────────────────────────── */}
          {/* Shows the stress score on the Y-axis (0–100) over time on the X-axis. */}
          {/* The orange dashed line at Y=50 marks the STRESS_THRESHOLD. */}
          <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm">
            <div className="mb-4">
              <h2 className="font-semibold text-slate-800">{metric.label} Trend</h2>
              {metric.key === 'score' ? (
                <p className="text-xs text-slate-400 mt-0.5 leading-relaxed">
                  The stress score (0–100) gauges an issuer's credit risk from its financial
                  ratios — higher means more financial stress. The orange dashed line at 50 is
                  the stress threshold: scores below it are considered healthy, while scores at
                  or above it signal elevated credit risk.
                </p>
              ) : (
                <p className="text-xs text-slate-400 mt-0.5 leading-relaxed">
                  {metric.label} over time — same history as the Stress Score, plotted for this ratio.
                </p>
              )}
            </div>

            {/* Tab bar — switches which metric the chart below plots. */}
            <div className="flex flex-wrap gap-1.5 mb-4">
              {TREND_METRICS.map(m => (
                <button
                  key={m.key}
                  onClick={() => setSelectedMetric(m.key)}
                  className={`text-xs px-3 py-1.5 rounded-full transition-colors ${
                    m.key === selectedMetric
                      ? 'bg-slate-800 text-white'
                      : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                  }`}
                >
                  {m.label}
                </button>
              ))}
            </div>

            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={chartData}>
                {/* Trim dates to "YYYY-MM" to save horizontal space on the axis. */}
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 11, fill: '#94a3b8' }}
                  tickFormatter={d => d.slice(0, 7)}
                  interval={0}          // show a label for every period (Recharts otherwise auto-skips overlapping ones)
                  angle={-45}           // rotate so adjacent YYYY-MM labels don't collide
                  textAnchor="end"
                  height={50}           // reserve vertical room for the rotated labels
                />
                <YAxis
                  domain={metric.domain ?? ['auto', 'auto']}  // score uses a fixed 0–100; ratios auto-scale
                  reversed={metric.reversed}                   // Implied Rating: AAA (index 0) at the top
                  allowDecimals={!metric.reversed}             // rating ticks are integer indices → letters
                  tick={{ fontSize: 11, fill: '#94a3b8' }}
                  tickFormatter={(v: number) => metric.fmt(v)}
                  width={48}
                />
                <Tooltip
                  formatter={(v: number) => [metric.fmt(v), metric.label]}
                  labelFormatter={l => `Period: ${l}`}
                  contentStyle={{ fontSize: 12 }}
                />
                {/* Dashed orange reference line at the stress threshold (score tab only). */}
                {metric.threshold != null && (
                  <ReferenceLine
                    y={metric.threshold}
                    stroke="#f97316"
                    strokeDasharray="4 2"
                    label={{ value: 'Stress threshold (healthy below)', position: 'right', fontSize: 10, fill: '#f97316' }}
                  />
                )}
                <Line
                  type="monotone"
                  dataKey="value"
                  connectNulls            // bridge periods where this ratio is missing
                  stroke="#1e293b"
                  strokeWidth={2}
                  dot={{ r: 4, fill: '#1e293b' }}
                  activeDot={{ r: 6 }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* ── Implied credit rating ──────────────────────────────────── */}
          {/* S&P-style rating for the latest period, with its sub-factor breakdown
              and the directional Rating Outlook. */}
          <RatingProfileSection periods={data.periods} outlook={data.outlook} />

          {/* ── Ratio history table ────────────────────────────────────── */}
          {/* Each row shows one fiscal year's ratios and score. */}
          {/* Clicking a row toggles an inline XBRL source audit panel below it. */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-100">
              <h2 className="font-semibold text-slate-800">Ratio History</h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Click a row to see source audit (XBRL tags + raw inputs).
              </p>
            </div>
            <div className="overflow-x-auto">
              {/* min-w-max + whitespace-nowrap: this detailed table carries 12 columns
                  (6 original ratios + 3 new ones + period/score/status), so let it grow
                  to its natural width and scroll horizontally rather than cram. The
                  portfolio overview (app/page.tsx) instead fits within the viewport. */}
              <table className="w-full min-w-max text-sm whitespace-nowrap">
                <thead>
                  <tr className="bg-gray-50 text-xs font-medium text-slate-500 uppercase tracking-wide">
                    <th className="px-6 py-3 text-left">Period</th>
                    <th className="px-4 py-3 text-right">EBITDA Margin</th>
                    <th className="px-4 py-3 text-right">Leverage</th>
                    <th className="px-4 py-3 text-right">Interest Coverage</th>
                    <th className="px-4 py-3 text-right">FCF</th>
                    <th className="px-4 py-3 text-right">FCF Margin</th>
                    <th className="px-4 py-3 text-right">Liquidity</th>
                    <th className="px-4 py-3 text-right">Cash Flow / Debt</th>
                    <th className="px-4 py-3 text-right">Current Ratio</th>
                    <th className="px-4 py-3 text-right">Debt / Assets</th>
                    <th className="px-4 py-3 text-center">Score</th>
                    <th className="px-6 py-3 text-center">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {pagedPeriods.map(p => (
                    // Keyed Fragment: the map's list item needs the key, not the
                    // inner <tr> — a shorthand <> fragment can't carry one.
                    <Fragment key={p.period_end}>
                      {/*
                        Data row — clicking toggles the inline audit panel.
                        openAudit holds the period_end of the currently-open row.
                        Clicking the open row again collapses it (toggles to null).
                      */}
                      <tr
                        onClick={() => setOpenAudit(openAudit === p.period_end ? null : p.period_end)}
                        className="hover:bg-gray-50 cursor-pointer transition-colors"
                      >
                        <td className="px-6 py-3 font-mono text-slate-700 text-xs">{p.period_end}</td>

                        {/* Optional chaining (?.) handles periods where a ratio is missing. */}
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtPct(p.ratios.ebitda_margin?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.leverage?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.interest_coverage?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtFCF(p.ratios.free_cash_flow?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtPct(p.ratios.fcf_margin?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.liquidity?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtPct(p.ratios.cash_flow_to_debt?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.current_ratio?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtPct(p.ratios.debt_to_assets?.value)}</td>

                        <td className="px-4 py-3 text-center font-mono font-bold text-slate-800">
                          {Math.round(p.score)}
                        </td>
                        <td className="px-6 py-3 text-center">
                          <span className={`inline-block text-xs font-medium px-2.5 py-1 rounded-full ${scoreBg(p.score)}`}>
                            {scoreLabel(p.score)}
                          </span>
                        </td>
                      </tr>

                      {/*
                        Audit panel row — rendered only when this period's row is open.
                        Uses colSpan={12} to span the full table width.
                        The AuditPanel component handles the detailed display.
                      */}
                      {openAudit === p.period_end && (
                        <tr>
                          <td colSpan={12} className="bg-slate-50 px-6 py-4 border-t border-slate-100">
                            <AuditPanel period={p} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pager — only shown when history exceeds one page. */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between px-6 py-3 border-t border-gray-100 text-sm">
                <span className="text-xs text-slate-400">
                  Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, allPeriods.length)} of {allPeriods.length} periods
                </span>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setPage(p => Math.max(0, p - 1))}
                    disabled={page === 0}
                    className="px-3 py-1 rounded-md text-slate-600 bg-slate-100 hover:bg-slate-200 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    ← Prev
                  </button>
                  <span className="text-xs text-slate-500 font-mono">
                    {page + 1} / {totalPages}
                  </span>
                  <button
                    onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
                    disabled={page >= totalPages - 1}
                    className="px-3 py-1 rounded-md text-slate-600 bg-slate-100 hover:bg-slate-200 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    Next →
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* Debt maturity wall — XBRL-derived, shown for the latest period. */}
          <MaturityWallSection periods={data.periods} />

          {/* ── Qualitative analysis status / empty state ──────────────────
              The three LLM sections below each render null when empty, so this
              card explains the absence and offers the trigger. It's shown while
              a review runs, on error, or when no LLM data exists yet. Once data
              is present (and no run is active), it disappears. */}
          {(llmRunning || llmError || !hasLlmData) && (
            <div ref={llmSectionRef} className="bg-white rounded-xl border border-gray-200 shadow-sm px-6 py-5">
              <h2 className="font-semibold text-slate-800">Qualitative Analysis</h2>
              {llmRunning ? (
                <div className="mt-2 flex items-center gap-2 text-sm text-slate-500">
                  <span className="w-3 h-3 rounded-full bg-orange-400 animate-pulse inline-block" />
                  Reading the latest 10-K filings with the LLM
                  {llmProgress && llmProgress.total > 0 ? ` — ${llmProgress.done}/${llmProgress.total} done` : ''}
                  {' '}(~30 s per filing).
                </div>
              ) : (
                <>
                  <p className="mt-1 text-sm text-slate-500">
                    {hasLlmData
                      ? 'Re-run to refresh findings, covenants, and loss provisions from the latest filings.'
                      : 'No LLM findings yet — tracking skips this pass for speed. Run it to scan the latest 10-K MD&A and footnotes for qualitative credit signals.'}
                  </p>
                  <button
                    onClick={handleRunLlm}
                    disabled={refreshing}
                    className="mt-3 inline-flex items-center gap-2 text-sm bg-slate-800 hover:bg-slate-700 text-white px-4 py-2 rounded-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    Run LLM analysis
                  </button>
                  {llmError && (
                    <p className="mt-3 text-sm text-red-600">{llmError}</p>
                  )}
                </>
              )}
            </div>
          )}

          {/* Anchor for the scroll-on-LLM-start when hasLlmData is already true
              (the status card above is hidden in that case, so we need a target
              that is always in the DOM once data is loaded). */}
          {hasLlmData && <div ref={llmSectionRef} />}

          {/* Maintenance covenants — LLM-extracted from the debt footnote. */}
          <CovenantsSection periods={data.periods} />

          {/* Bond instruments + seniority — LLM-extracted from the debt footnote. */}
          <BondInstrumentsSection periods={data.periods} />

          {/* Loss provisions — LLM-extracted from the contingencies footnote. */}
          <LossProvisionsSection periods={data.periods} />

          {/* Qualitative findings section — only rendered if findings exist. */}
          <FindingsSection periods={data.periods} />
        </>
      )}
    </div>
  )
}


// ── AuditPanel component ──────────────────────────────────────────────────────
//
// Shown in the expanded inline row below a period's data row.
// Displays two sections:
//   1. Triggered Alerts — the human-readable alert messages from compute_score()
//   2. Source Audit — per-ratio cards showing each XBRL input name, its raw value,
//      and the winning XBRL tag path that supplied it.
//
// This lets an analyst trace any ratio value back to the exact SEC filing tag.

function AuditPanel({ period }: { period: PeriodData }) {
  return (
    <div className="space-y-4">

      {/* Triggered alerts — only shown if at least one rule fired. */}
      {period.alerts.length > 0 && (
        <div>
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">
            Triggered Alerts
          </p>
          <ul className="space-y-1">
            {period.alerts.map((alert, i) => (
              <li key={i} className="flex items-center gap-2 text-sm text-orange-700">
                <span className="w-1.5 h-1.5 rounded-full bg-orange-400 flex-shrink-0" />
                {alert}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* XBRL source audit grid — one card per ratio. */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
            Source Audit (XBRL inputs)
          </p>
          {/* Deep link to the SEC 10-K these ratios were extracted from, so an
              analyst can trace any figure back to the filing. Omitted when the
              backend couldn't match a filing to this period. */}
          {period.source_url && (
            <a
              href={period.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-xs font-semibold text-white bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-md shadow-sm transition-colors"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M15 3h6v6" />
                <path d="M10 14 21 3" />
                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
              </svg>
              View source 10-K on SEC EDGAR
            </a>
          )}
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(period.ratios).map(([name, data]) => (
            <div key={name} className="bg-white rounded-lg border border-gray-200 p-3">
              {/* Ratio name: underscores replaced with spaces for readability. A
                  missing ratio (value === null) gets a red "missing" badge. */}
              <p className="text-xs font-semibold text-slate-700 mb-2 capitalize flex items-center gap-2">
                <span>{name.replace(/_/g, ' ')}</span>
                {/* N/A (structurally not applicable, e.g. current ratio on an
                    unclassified balance sheet) reads as neutral, not an error.
                    A genuine data gap stays red. */}
                {data.not_applicable ? (
                  <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-500 bg-slate-100 rounded px-1.5 py-0.5">
                    N/A
                  </span>
                ) : data.value === null && (
                  <span className="text-[10px] font-semibold uppercase tracking-wide text-red-600 bg-red-50 rounded px-1.5 py-0.5">
                    missing
                  </span>
                )}
              </p>
              {/* Each input (e.g. "total_debt") with its value and the XBRL tag. */}
              {Object.entries(data.inputs).map(([field, val]) => {
                const tag = data.source_tags[field] || '?'

                // Auto-scale the displayed value:
                //   ≥ 1B → show in billions (e.g. "$5.23B")
                //   ≥ 1M → show in millions (e.g. "$250M")
                //   else → show as a decimal (e.g. "3.20" for a ratio)
                const fmtVal = typeof val === 'number'
                  ? Math.abs(val) >= 1e9 ? `$${(val / 1e9).toFixed(2)}B`
                  : Math.abs(val) >= 1e6 ? `$${(val / 1e6).toFixed(0)}M`
                  : val.toFixed(2)
                  : String(val)

                return (
                  <div key={field} className="text-xs text-slate-500 mt-1">
                    <span className="text-slate-400">{field}:</span>{' '}
                    <span className="font-mono text-slate-700">{fmtVal}</span>
                    {/*
                      The XBRL tag path (e.g. "us-gaap/LongTermDebt") is shown in
                      a very small muted font. truncate+title shows the full path
                      in a browser tooltip on hover if it's clipped.
                    */}
                    <div className="text-slate-300 text-[10px] truncate" title={tag}>
                      {tag}
                    </div>
                  </div>
                )
              })}

              {/* Missing inputs: each absent raw datum, flagged red, with the XBRL
                  tags that were searched so the analyst sees exactly what's missing.
                  Tried tags wrap as chips (whitespace-normal overrides the history
                  table's whitespace-nowrap), packing 2-3 per line when the card has
                  room instead of overflowing on a single clipped line. */}
              {data.missing_inputs?.map(({ field, tags_tried }) => (
                <div key={field} className="text-xs mt-1">
                  <span className="text-red-400">{field}:</span>{' '}
                  <span className="font-mono text-red-600 font-semibold">missing</span>
                  <div className="text-red-300 text-[10px] mt-0.5">
                    tried:
                    {tags_tried.length ? (
                      // max-w caps the wrap width — without it the history table's
                      // min-w-max lets this row grow to fit every chip on one line.
                      <div className="mt-0.5 flex flex-wrap gap-1 font-mono max-w-[20rem]">
                        {tags_tried.map(tag => (
                          <span
                            key={tag}
                            className="whitespace-normal break-words rounded bg-red-50 px-1 py-0.5 text-red-400"
                          >
                            {tag}
                          </span>
                        ))}
                      </div>
                    ) : ' —'}
                  </div>
                </div>
              ))}

              {/* Not applicable (e.g. current ratio on an unclassified balance
                  sheet): neutral slate note, never red. */}
              {data.not_applicable && data.reason && (
                <div className="text-xs text-slate-500 mt-1">{data.reason}</div>
              )}

              {/* Guard failure (all inputs resolved but ratio undefined, e.g. zero
                  EBITDA): no missing inputs, so show the reason instead. */}
              {!data.not_applicable && data.value === null && !data.missing_inputs?.length && data.reason && (
                <div className="text-xs text-red-600 mt-1">{data.reason}</div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}


// ── FindingsSection component ─────────────────────────────────────────────────
//
// Aggregates all LLM qualitative findings from all periods into one flat list.
// Renders nothing at all if there are no findings — this is normal when the
// LLM review was skipped (no_llm=true, which is the default).
//
// Each finding shows:
//   - A coloured severity dot (red/yellow/blue)
//   - The concern label, period date, and severity badge
//   - The verbatim evidence quote from the filing
//   - The source label (e.g. "10-K 2023-12-31, MD&A")

// Builds a deep link to the source filing that scrolls to and highlights the
// evidence quote using a browser text fragment (https://...#:~:text=...).
//
// Returns the bare source_url if no usable quote is available, and null if there
// is no source_url at all (older findings predate the field — see lib/api.ts).
//
// Text fragments must match the rendered text exactly, and long single matches
// are brittle across HTML element boundaries. So for longer quotes we emit a
// `text=start,end` range (first/last few words) which the browser matches by
// anchoring on both ends — far more robust than one long string.
function findingSourceLink(sourceUrl: string | undefined, quote: string): string | null {
  if (!sourceUrl) return null

  // Normalize: drop surrounding quotes / leading-or-trailing ellipses, collapse
  // whitespace. Mirrors the spirit of llm_review._normalize_for_match.
  const clean = quote
    .replace(/^[\s"“”'']+|[\s"“”'']+$/g, '')
    .replace(/^[.…]+|[.…]+$/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!clean) return sourceUrl

  const words = clean.split(' ')
  if (words.length <= 12) {
    return `${sourceUrl}#:~:text=${encodeURIComponent(clean)}`
  }
  // Range match: anchor on the first 6 and last 6 words.
  const start = encodeURIComponent(words.slice(0, 6).join(' '))
  const end = encodeURIComponent(words.slice(-6).join(' '))
  return `${sourceUrl}#:~:text=${start},${end}`
}

// Source label that links to the filing on SEC EDGAR (scrolling to the quote)
// when a source_url is present; falls back to plain text when there's no URL
// (older records stored before the field existed, or periods with no matched
// filing). Shared by the Findings, Loss Provisions, and Covenants sections.
function SourceLink({ sourceUrl, quote, label }: { sourceUrl: string | null | undefined; quote: string; label: string }) {
  const href = findingSourceLink(sourceUrl ?? undefined, quote)
  if (!href) {
    return (
      <p className="mt-1.5 text-xs text-slate-400">
        {label} <span className="text-slate-300">· source link unavailable</span>
      </p>
    )
  }
  return (
    <p className="mt-1.5 flex items-center gap-1.5 text-xs">
      {/* Gray source label first, then the blue link on the right. */}
      <span className="text-slate-400">{label} ·</span>
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="group inline-flex items-center gap-1.5 font-medium text-blue-600 hover:text-blue-700 transition-colors"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M15 3h6v6" />
          <path d="M10 14 21 3" />
          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
        </svg>
        <span className="group-hover:underline underline-offset-2">View source on SEC EDGAR</span>
      </a>
    </p>
  )
}

// ── Year grouping for the LLM qualitative sections ────────────────────────────
//
// The three LLM sections (findings, covenants, loss provisions) each aggregate
// items across every tracked period. With a long history that flat list runs
// very long, so once a section exceeds GROUP_THRESHOLD items we collapse it into
// one disclosure per calendar year — newest year expanded, older years collapsed
// — keeping the page short while any year's detail stays one click away.

const GROUP_THRESHOLD = 6

// Group items (each carrying a `period` field like "2023-12-31") by calendar
// year, newest year first.
function groupByYear<T extends { period: string }>(items: T[]): { year: string; items: T[] }[] {
  const map = new Map<string, T[]>()
  for (const it of items) {
    const year = it.period.slice(0, 4)
    const bucket = map.get(year)
    if (bucket) bucket.push(it)
    else map.set(year, [it])
  }
  return Array.from(map.entries())
    .sort((a, b) => b[0].localeCompare(a[0]))   // newest year first
    .map(([year, items]) => ({ year, items }))
}

// Renders a list of period-tagged items. Short lists render flat (unchanged);
// long ones (> GROUP_THRESHOLD across ≥ 2 years) collapse into per-year
// disclosures with the newest year open by default and an expand/collapse-all
// control. `itemNoun` labels the per-year counts (e.g. "finding" → "3 findings").
function YearGroupedList<T extends { period: string }>({
  items,
  itemNoun,
  renderItem,
}: {
  items: T[]
  itemNoun: string
  renderItem: (item: T, i: number) => ReactNode
}) {
  const groups = groupByYear(items)

  // Newest year expanded by default; older years start collapsed.
  const [openYears, setOpenYears] = useState<Set<string>>(
    () => new Set(groups.slice(0, 1).map(g => g.year))
  )

  // Short lists aren't worth the collapse chrome — render them flat, as before.
  if (items.length <= GROUP_THRESHOLD || groups.length <= 1) {
    return <div className="divide-y divide-gray-100">{items.map(renderItem)}</div>
  }

  const plural = (n: number) => `${n} ${itemNoun}${n === 1 ? '' : 's'}`
  const allOpen = groups.every(g => openYears.has(g.year))
  const toggleYear = (year: string) =>
    setOpenYears(prev => {
      const next = new Set(prev)
      if (next.has(year)) next.delete(year)
      else next.add(year)
      return next
    })
  const setAll = (open: boolean) =>
    setOpenYears(open ? new Set(groups.map(g => g.year)) : new Set())

  return (
    <div>
      {/* Expand/collapse-all toolbar — only shown in grouped mode. */}
      <div className="flex items-center justify-end gap-3 px-6 py-2 border-b border-gray-100 bg-gray-50/60">
        <span className="text-xs text-slate-400">
          {groups.length} years · {plural(items.length)}
        </span>
        <button
          onClick={() => setAll(!allOpen)}
          className="text-xs font-medium text-slate-500 hover:text-slate-700 transition-colors"
        >
          {allOpen ? 'Collapse all' : 'Expand all'}
        </button>
      </div>

      <div className="divide-y divide-gray-100">
        {groups.map(g => {
          const isOpen = openYears.has(g.year)
          return (
            <div key={g.year}>
              {/* Year header — click to toggle this year's items. */}
              <button
                onClick={() => toggleYear(g.year)}
                aria-expanded={isOpen}
                className="w-full flex items-center gap-2 px-6 py-3 text-left hover:bg-gray-50 transition-colors"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24"
                  fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                  aria-hidden="true"
                  className={`text-slate-400 transition-transform ${isOpen ? 'rotate-90' : ''}`}
                >
                  <path d="m9 18 6-6-6-6" />
                </svg>
                <span className="font-mono font-semibold text-sm text-slate-700">{g.year}</span>
                <span className="text-xs text-slate-400">{plural(g.items.length)}</span>
              </button>
              {isOpen && (
                <div className="divide-y divide-gray-100 border-t border-gray-100 bg-slate-50/40">
                  {g.items.map(renderItem)}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function FindingsSection({ periods }: { periods: PeriodData[] }) {
  // Flatten findings from all periods into one array.
  // The spread {…f, period: p.period_end} adds the period_end date to each finding
  // so we can display which year each finding came from.
  const all = periods.flatMap(p => p.findings.map(f => ({ ...f, period: p.period_end })))

  // Render nothing if no LLM review was run or no findings were flagged.
  if (all.length === 0) return null

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-100">
        <h2 className="font-semibold text-slate-800">Qualitative Findings</h2>
        <p className="text-xs text-slate-400 mt-0.5">
          LLM-identified signals from MD&amp;A and footnotes. Each includes a verbatim quote;
          click the source label to open the filing on SEC EDGAR and jump to the quote.
        </p>

        {/* Severity legend — explains what the high/medium/low badges mean. The
            dot colours mirror severityDot() so the legend matches each finding. */}
        <dl className="mt-3 flex flex-col gap-1.5 sm:flex-row sm:flex-wrap sm:gap-x-5 sm:gap-y-1">
          <div className="flex items-start gap-1.5">
            <span className="mt-1 w-2 h-2 rounded-full flex-shrink-0 bg-red-500" />
            <span className="text-xs text-slate-500">
              <dt className="inline font-medium text-slate-700">High</dt>
              {' '}— serious credit-risk language (e.g. going-concern doubt, covenant
              breach or waiver); adds up to 10 points to the stress score.
            </span>
          </div>
          <div className="flex items-start gap-1.5">
            <span className="mt-1 w-2 h-2 rounded-full flex-shrink-0 bg-yellow-500" />
            <span className="text-xs text-slate-500">
              <dt className="inline font-medium text-slate-700">Medium</dt>
              {' '}— a notable concern worth monitoring.
            </span>
          </div>
          <div className="flex items-start gap-1.5">
            <span className="mt-1 w-2 h-2 rounded-full flex-shrink-0 bg-blue-400" />
            <span className="text-xs text-slate-500">
              <dt className="inline font-medium text-slate-700">Low</dt>
              {' '}— a minor or contextual signal.
            </span>
          </div>
        </dl>
      </div>
      <YearGroupedList
        items={all}
        itemNoun="finding"
        renderItem={(f, i) => (
          <div key={i} className="px-6 py-4">
            <div className="flex items-start gap-3">

              {/* Coloured severity dot: red=high, yellow=medium, blue=low. */}
              <span className={`mt-1.5 w-2 h-2 rounded-full flex-shrink-0 ${severityDot(f.severity)}`} />

              <div className="flex-1">
                {/* First line: concern label + period date + severity badge. */}
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-sm text-slate-800">{f.concern}</span>
                  <span className="text-xs text-slate-400 font-mono">{f.period}</span>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium capitalize
                    ${f.severity === 'high'   ? 'bg-red-100 text-red-700'
                    : f.severity === 'medium' ? 'bg-yellow-100 text-yellow-700'
                    :                           'bg-blue-100 text-blue-700'}`}>
                    {f.severity}
                  </span>
                </div>

                {/* Verbatim quote from the filing, styled as a blockquote. */}
                <blockquote className="mt-2 text-xs text-slate-500 italic border-l-2 border-slate-200 pl-3">
                  "{f.evidence_quote}"
                </blockquote>

                {/* Source label (e.g. "10-K 2023-12-31, MD&A") linking to the
                    filing on SEC EDGAR and scrolling to the quote. */}
                <SourceLink sourceUrl={f.source_url} quote={f.evidence_quote} label={f.source} />
              </div>
            </div>
          </div>
        )}
      />
    </div>
  )
}


// ── Money formatting ──────────────────────────────────────────────────────────
// Compact $ display for maturity bars and provision amounts.
// e.g. 5000000000 → "$5.0B", 250000000 → "$250M".
function fmtMoney(val: number | null | undefined): string {
  if (val == null) return '—'
  const m = val / 1e6
  if (Math.abs(m) >= 1000) return `$${(m / 1000).toFixed(1)}B`
  if (Math.abs(m) >= 1) return `$${m.toFixed(0)}M`
  return `$${val.toFixed(0)}`
}


// ── MaturityWallSection component ─────────────────────────────────────────────
//
// Renders the debt maturity schedule for the most recent period as a bar chart.
// The buckets (y1…thereafter) are XBRL-derived, so this is fully auditable. The
// "wall" year (the bucket with the most principal) is highlighted, and the
// near-term concentration (% due within 3 years) is shown — the metric that
// drives the maturity-wall stress rule.

const _BUCKET_ORDER = ['y1', 'y2', 'y3', 'y4', 'y5', 'thereafter']
const _BUCKET_LABEL: Record<string, string> = {
  y1: 'Yr 1', y2: 'Yr 2', y3: 'Yr 3', y4: 'Yr 4', y5: 'Yr 5', thereafter: '5+ yrs',
}

function MaturityWallSection({ periods }: { periods: PeriodData[] }) {
  // Use the most recent period that actually has a maturity schedule with buckets.
  const period = periods.find(p => p.maturities && Object.keys(p.maturities.buckets).length > 0)
  const sched = period?.maturities
  if (!period || !sched) return null

  // Build the chart series in canonical bucket order (skip buckets the filer omitted).
  const chartData = _BUCKET_ORDER
    .filter(b => b in sched.buckets)
    .map(b => ({ bucket: _BUCKET_LABEL[b] ?? b, key: b, value: sched.buckets[b] }))

  const pct = sched.near_term_pct
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-100 flex items-baseline justify-between">
        <div>
          <h2 className="font-semibold text-slate-800">Debt Maturity Wall</h2>
          <p className="text-xs text-slate-400 mt-0.5">
            Long-term debt principal due by year (XBRL-sourced) · {period.period_end}
          </p>
        </div>
        <div className="text-right">
          <span className="text-xs text-slate-400">Total scheduled</span>
          <p className="font-mono font-semibold text-slate-700">{fmtMoney(sched.total_scheduled)}</p>
        </div>
      </div>

      <div className="p-6">
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={chartData}>
            <XAxis dataKey="bucket" tick={{ fontSize: 11, fill: '#94a3b8' }} />
            <YAxis
              tick={{ fontSize: 11, fill: '#94a3b8' }}
              width={48}
              tickFormatter={(v: number) => fmtMoney(v)}
            />
            <Tooltip
              formatter={(v: number) => [fmtMoney(v), 'Due']}
              contentStyle={{ fontSize: 12 }}
            />
            <Bar dataKey="value" radius={[4, 4, 0, 0]}>
              {/* Highlight the wall year (most principal due) in orange. */}
              {chartData.map(d => (
                <Cell key={d.key} fill={d.key === sched.wall_year ? '#f97316' : '#1e293b'} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>

        {pct != null && (
          <p className="mt-3 text-sm text-slate-600">
            <span className={pct > 0.4 ? 'text-orange-700 font-semibold' : 'text-slate-700 font-semibold'}>
              {(pct * 100).toFixed(0)}%
            </span>{' '}
            of scheduled principal is due within 3 years
            {pct > 0.4 && ' — elevated refinancing risk'}.
          </p>
        )}
      </div>
    </div>
  )
}


// ── RatingProfileSection component ────────────────────────────────────────────
//
// Shows the S&P-style implied credit rating for the most recent period that has
// one, plus the three sub-factors that drove it (FFO/Debt, Debt/EBITDA, interest
// coverage) and which financial-risk band each landed in. This is the rating
// analogue of the AuditPanel — it makes the letter fully traceable to the ratios.

// Friendly labels for the three rating sub-factors.
const _SUBFACTOR_LABEL: Record<string, string> = {
  ffo_to_debt: 'FFO / Debt',
  debt_to_ebitda: 'Debt / EBITDA',
  ebitda_to_interest: 'EBITDA / Interest',
}

// Format a sub-factor's raw value for display: FFO/Debt is a fraction (→ %),
// the other two are multiples (→ ×).
function fmtSubfactor(sub: string, value: number | null): string {
  if (value == null) return '—'
  if (sub === 'ffo_to_debt') return fmtPct(value)
  return fmtRatio(value)
}

function RatingProfileSection({ periods, outlook }: { periods: PeriodData[]; outlook?: RatingOutlook | null }) {
  // Use the most recent period that actually carries an implied rating.
  const period = periods.find(p => p.implied_rating)
  const rating = period?.implied_rating
  if (!period || !rating) return null

  const ob = outlookBadge(outlook?.outlook)

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-100 flex items-baseline justify-between gap-4">
        <div>
          <h2 className="font-semibold text-slate-800">Implied Credit Rating</h2>
          <p className="text-xs text-slate-400 mt-0.5">
            S&amp;P-style rating derived from the period&apos;s ratios (not an agency rating) · {period.period_end}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {ob && (
            <span className={`inline-flex items-center gap-1 text-xs font-semibold px-2.5 py-1 rounded-lg whitespace-nowrap ${ob.cls}`} title="Rating Outlook">
              {ob.arrow} {ob.label}
            </span>
          )}
          <span className={`inline-block text-base font-mono font-bold px-3 py-1 rounded-lg whitespace-nowrap ${ratingBg(rating.implied_rating)}`}>
            {rating.implied_rating}
          </span>
        </div>
      </div>

      <div className="p-6 space-y-4">

        {/* Directional Rating Outlook — trend + (when available) the implied-vs-agency
            gap. The reasons are the auditable "why" behind the arrow. */}
        {outlook && ob && (
          <div className="rounded-lg border border-gray-200 bg-slate-50 p-3">
            <p className="text-sm">
              <span className="font-semibold text-slate-700">Outlook:</span>{' '}
              <span className={`font-semibold ${outlook.outlook === 'Negative' ? 'text-red-700' : outlook.outlook === 'Positive' ? 'text-green-700' : 'text-slate-600'}`}>
                {ob.arrow} {outlook.outlook}
              </span>
              <span className="text-slate-400"> — where the rating looks headed, from the score trend{outlook.gap != null ? ' and the implied-vs-agency gap' : ''}.</span>
            </p>
            {outlook.reasons.length > 0 && (
              <ul className="mt-2 space-y-1">
                {outlook.reasons.map((r, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs text-slate-500">
                    <span className="mt-1 w-1 h-1 rounded-full bg-slate-300 flex-shrink-0" />
                    {r}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* Calibrated migration prediction (Stage 3 model) — present once trained. */}
        <MigrationPredictionBlock periods={periods} />
        {/* Profile summary line. */}
        <p className="text-sm text-slate-600">
          Financial risk profile:{' '}
          <span className="font-semibold text-slate-800">{rating.financial_risk_profile}</span>
          {' '}· business risk:{' '}
          <span className="font-semibold text-slate-800">{BUSINESS_RISK_LABEL[rating.business_risk_index] ?? rating.business_risk_index}</span>
        </p>

        {/* Sub-factor breakdown — one card per sub-factor, showing its value and band. */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {Object.entries(rating.subscores).map(([sub, s]) => (
            <div key={sub} className="bg-slate-50 rounded-lg border border-gray-200 p-3">
              <p className="text-xs font-semibold text-slate-700">{_SUBFACTOR_LABEL[sub] ?? sub}</p>
              <p className="mt-1 font-mono text-lg text-slate-800">{fmtSubfactor(sub, s.value)}</p>
              <p className="mt-0.5 text-xs text-slate-500">
                {s.profile_name ?? 'no data'}
                {s.overridden && <span className="text-orange-600"> · forced (EBITDA ≤ 0)</span>}
              </p>
              <p className="mt-1 text-[10px] text-slate-300 font-mono truncate" title={s.source_ratio}>
                {s.source_ratio}
              </p>
            </div>
          ))}
        </div>

        {/* Methodology / honesty notes from the rating engine. */}
        {rating.notes.length > 0 && (
          <ul className="space-y-1 border-t border-gray-100 pt-3">
            {rating.notes.map((note, i) => (
              <li key={i} className="flex items-start gap-2 text-xs text-slate-500">
                <span className="mt-1 w-1 h-1 rounded-full bg-slate-300 flex-shrink-0" />
                {note}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

// Business-risk index → label (1 = Excellent … 6 = Vulnerable), mirroring
// src/rating.py's BUSINESS_RISK_PROFILES.
const BUSINESS_RISK_LABEL: Record<number, string> = {
  1: 'Excellent', 2: 'Strong', 3: 'Satisfactory', 4: 'Fair', 5: 'Weak', 6: 'Vulnerable',
}

// Friendly labels for the model's feature drivers (mirrors the feature columns).
function driverLabel(feature: string): string {
  return feature
    .replace(/_yoy$/, ' (Δ)')
    .replace(/_/g, ' ')
    .replace(/\bpct\b/, '%')
}

// Calibrated migration prediction for the latest period that has one. Renders
// nothing until the Stage 3 model has been trained and predictions written.
function MigrationPredictionBlock({ periods }: { periods: PeriodData[] }) {
  const period = periods.find(p => p.migration)
  const m = period?.migration
  if (!period || !m) return null

  const pct = (v: number | null) => (v == null ? '—' : `${(v * 100).toFixed(0)}%`)
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-3">
      <div className="flex items-center gap-4 flex-wrap">
        <span className="text-sm font-semibold text-slate-700">
          Predicted {m.horizon_months}-month migration
        </span>
        <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-red-50 text-red-700 border border-red-100">
          Downgrade {pct(m.p_downgrade)}
        </span>
        <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-green-50 text-green-700 border border-green-100">
          Upgrade {pct(m.p_upgrade)}
        </span>
        {m.p_default != null && (
          <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-slate-100 text-slate-600 border border-slate-200">
            Default {pct(m.p_default)}
          </span>
        )}
        <span className="text-[10px] text-slate-300 font-mono ml-auto">{m.model_version}</span>
      </div>
      {m.drivers_json.length > 0 && (
        <div className="mt-2">
          <p className="text-xs text-slate-400 mb-1">Top drivers of downgrade risk:</p>
          <ul className="space-y-1">
            {m.drivers_json.map((d, i) => (
              <li key={i} className="flex items-center gap-2 text-xs">
                <span className={d.contribution > 0 ? 'text-red-600' : 'text-green-600'}>
                  {d.contribution > 0 ? '▲' : '▼'}
                </span>
                <span className="text-slate-600">{driverLabel(d.feature)}</span>
                <span className="text-slate-400 font-mono">
                  {d.value == null ? 'n/a' : d.value}
                </span>
                <span className="text-slate-400 ml-auto font-mono">
                  {d.contribution > 0 ? '+' : ''}{(d.contribution * 100).toFixed(1)} pts
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}


// ── CovenantsSection component ────────────────────────────────────────────────
//
// Aggregates LLM-extracted maintenance covenants across all periods into a table.
// Numeric fields may be null (only kept when quote-backed), so they render "—".
// Rows where the company sits near its limit get a red "Near limit" badge. Each
// row carries the verbatim quote, mirroring the FindingsSection styling.

function CovenantsSection({ periods }: { periods: PeriodData[] }) {
  const all = periods.flatMap(p =>
    (p.covenants ?? []).map(c => ({ ...c, period: p.period_end, source_url: p.source_url }))
  )
  if (all.length === 0) return null

  const label: Record<string, string> = {
    max_leverage: 'Max leverage',
    min_coverage: 'Min coverage',
    min_net_worth: 'Min net worth',
    other: 'Other',
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-100">
        <h2 className="font-semibold text-slate-800">Maintenance Covenants</h2>
        <p className="text-xs text-slate-400 mt-0.5">
          Extracted from the debt footnote. Figures shown only when quoted verbatim.
        </p>
      </div>
      <YearGroupedList
        items={all}
        itemNoun="covenant"
        renderItem={(c: Covenant & { period: string; source_url?: string | null }, i) => (
          <div key={i} className="px-6 py-4">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-medium text-sm text-slate-800">
                {label[c.covenant_type] ?? c.covenant_type}
              </span>
              <span className="text-xs text-slate-400 font-mono">{c.period}</span>
              <span className="text-xs text-slate-500 font-mono">
                {c.direction === 'max' ? '≤' : '≥'} {c.threshold ?? '—'}
              </span>
              {c.reported_actual != null && (
                <span className="text-xs text-slate-500 font-mono">
                  actual {c.reported_actual}
                </span>
              )}
              {c.near_limit && (
                <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-red-100 text-red-700">
                  Near limit
                </span>
              )}
            </div>
            <blockquote className="mt-2 text-xs text-slate-500 italic border-l-2 border-slate-200 pl-3">
              "{c.evidence_quote}"
            </blockquote>
            <SourceLink sourceUrl={c.source_url} quote={c.evidence_quote} label={c.source} />
          </div>
        )}
      />
    </div>
  )
}


// ── BondInstrumentsSection component ──────────────────────────────────────────
//
// LLM-extracted debt instruments + seniority across all periods. Mirrors
// CovenantsSection. Each row shows the instrument, a seniority badge, coupon /
// maturity / principal (where quote-backed), and the verbatim quote.

function BondInstrumentsSection({ periods }: { periods: PeriodData[] }) {
  const all = periods.flatMap(p =>
    (p.bond_instruments ?? []).map(b => ({ ...b, period: p.period_end, source_url: p.source_url }))
  )
  if (all.length === 0) return null

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-100">
        <h2 className="font-semibold text-slate-800">Debt Instruments &amp; Seniority</h2>
        <p className="text-xs text-slate-400 mt-0.5">
          Extracted from the debt footnote. Seniority drives the senior-secured screen and
          issue-level notching. Figures shown only when quoted verbatim.
        </p>
      </div>
      <YearGroupedList
        items={all}
        itemNoun="instrument"
        renderItem={(b: BondInstrument & { period: string; source_url?: string | null }, i) => {
          const sb = seniorityBadge(b.seniority)
          return (
            <div key={i} className="px-6 py-4">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium text-sm text-slate-800">{b.instrument_name}</span>
                <span className="text-xs text-slate-400 font-mono">{b.period}</span>
                <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${sb.cls}`}>{sb.label}</span>
                {b.coupon != null && <span className="text-xs text-slate-500 font-mono">{b.coupon.toFixed(2)}%</span>}
                {b.maturity_year != null && <span className="text-xs text-slate-500 font-mono">due {b.maturity_year}</span>}
                {b.principal_amount != null && <span className="text-xs text-slate-500 font-mono">{fmtMoney(b.principal_amount)}</span>}
              </div>
              <blockquote className="mt-2 text-xs text-slate-500 italic border-l-2 border-slate-200 pl-3">
                "{b.evidence_quote}"
              </blockquote>
              <SourceLink sourceUrl={b.source_url} quote={b.evidence_quote} label={b.source} />
            </div>
          )
        }}
      />
    </div>
  )
}


// ── LossProvisionsSection component ───────────────────────────────────────────
//
// Aggregates LLM-extracted litigation/contingency provisions across all periods.
// Mirrors FindingsSection: matter label, period, optional amount, a "Material"
// badge, the qualitative flag, and the verbatim quote.

function LossProvisionsSection({ periods }: { periods: PeriodData[] }) {
  const all = periods.flatMap(p =>
    (p.loss_provisions ?? []).map(lp => ({ ...lp, period: p.period_end, source_url: p.source_url }))
  )
  if (all.length === 0) return null

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-100">
        <h2 className="font-semibold text-slate-800">Loss Provisions &amp; Contingencies</h2>
        <p className="text-xs text-slate-400 mt-0.5">
          Litigation and contingency exposures from the footnotes. Amounts shown only when quoted verbatim.
        </p>
      </div>
      <YearGroupedList
        items={all}
        itemNoun="provision"
        renderItem={(lp: LossProvision & { period: string; source_url?: string | null }, i) => (
          <div key={i} className="px-6 py-4">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-medium text-sm text-slate-800">{lp.matter}</span>
              <span className="text-xs text-slate-400 font-mono">{lp.period}</span>
              {lp.provision_amount != null && (
                <span className="text-xs text-slate-500 font-mono">{fmtMoney(lp.provision_amount)}</span>
              )}
              {lp.is_material && (
                <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-red-100 text-red-700">
                  Material
                </span>
              )}
            </div>
            {lp.qualitative_flag && (
              <p className="mt-1 text-xs text-slate-500">{lp.qualitative_flag}</p>
            )}
            <blockquote className="mt-2 text-xs text-slate-500 italic border-l-2 border-slate-200 pl-3">
              "{lp.evidence_quote}"
            </blockquote>
            <SourceLink sourceUrl={lp.source_url} quote={lp.evidence_quote} label={lp.source} />
          </div>
        )}
      />
    </div>
  )
}
