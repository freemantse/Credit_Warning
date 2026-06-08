'use client'
// ─────────────────────────────────────────────────────────────────────────────
// app/backtest/page.tsx — Backtest Page (route "/backtest")
//
// This page lets the user run a point-in-time backtest against known historical
// credit events. The backtest is a long-running server-side task (1–2 minutes).
//
// How the async backtest flow works:
//   1. User clicks "Run Backtest" → POST /api/backtest → server starts the task
//      in the background and immediately returns { status: "started" }.
//   2. The page starts polling GET /api/backtest/status every 3 seconds.
//   3. While status.running === true, the button is disabled and a spinner shows.
//   4. When status.running becomes false, the poll interval is cleared.
//   5. If status.result is set, the summary cards and case table are rendered.
//   6. If status.error is set, an error message is shown instead.
// ─────────────────────────────────────────────────────────────────────────────

import { useEffect, useRef, useState } from 'react'
import { BacktestStatus, BacktestCase, startBacktest, fetchBacktestStatus } from '@/lib/api'

export default function BacktestPage() {

  // ── State ───────────────────────────────────────────────────────────────────
  const [status, setStatus] = useState<BacktestStatus>({
    running: false,
    result: null,
    error: null,
  })

  // True only for the brief window between clicking "Run" and getting the first
  // status response. Prevents the button from being clickable twice in quick succession.
  const [starting, setStarting] = useState(false)

  // UI-level error (e.g. the 409 "already running" response). Separate from
  // status.error (which is a server-side task error) so both can be shown.
  const [error, setError] = useState('')

  // ── Polling interval ref ───────────────────────────────────────────────────
  // We use useRef instead of useState to store the interval ID.
  // Reason: calling clearInterval() in a state setter would trigger a re-render,
  // which would re-run the useEffect that sets the interval — creating a loop.
  // A ref holds the ID without causing re-renders when it changes.
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)


  // ── Effects ─────────────────────────────────────────────────────────────────

  // On mount: fetch the current status so we can restore the UI if a backtest
  // was already running (e.g. user refreshed the page mid-run).
  // Cleanup function clears the poll interval when the component unmounts.
  useEffect(() => {
    fetchBacktestStatus().then(setStatus).catch(() => {})
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  // When status.running transitions to true, start polling every 3 seconds.
  // When it transitions to false (task finished or errored), clear the interval.
  // The cleanup function at the end of the effect runs before the next effect
  // execution — ensuring we never have two intervals running simultaneously.
  useEffect(() => {
    if (status.running) {
      pollRef.current = setInterval(async () => {
        const s = await fetchBacktestStatus().catch(() => null)
        if (s) {
          setStatus(s)
          // Once the task finishes (running=false), stop polling immediately.
          if (!s.running && pollRef.current) clearInterval(pollRef.current)
        }
      }, 3000)  // poll every 3 seconds
    }
    // Cleanup: clear the interval when the effect re-runs or the component unmounts.
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [status.running])


  // ── Event handlers ──────────────────────────────────────────────────────────

  /**
   * Handle the "Run Backtest" button click.
   * Sends POST /api/backtest, then immediately fetches status to confirm it's running.
   * The useEffect above will pick up running=true and start polling automatically.
   */
  async function handleRun() {
    setStarting(true)
    setError('')
    try {
      await startBacktest()
      // Immediately fetch status so the UI shows "Running…" without waiting for the
      // first poll interval to fire.
      setStatus(await fetchBacktestStatus())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to start backtest')
    } finally {
      setStarting(false)
    }
  }

  // Destructure for cleaner JSX below.
  const { result, error: runError } = status
  const summary = result?.summary


  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-8">

      {/* ── Page header ── */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Backtest</h1>
        <p className="text-slate-500 mt-1 text-sm">
          Replay known distressed issuers point-in-time — no look-ahead from later restatements.
        </p>
      </div>

      {/* ── Run control card ── */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm flex items-center gap-4 flex-wrap">
        <button
          onClick={handleRun}
          // Disabled while the task is running OR while the POST is in-flight.
          disabled={status.running || starting}
          className="bg-slate-800 text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {status.running ? 'Running…' : 'Run Backtest'}
        </button>

        {/* Animated pulse dot shown only while the background task is running. */}
        {status.running && (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <span className="w-3 h-3 rounded-full bg-orange-400 animate-pulse inline-block" />
            Fetching EDGAR data for each case — may take 1–2 minutes on first run.
          </div>
        )}

        {/*
          Show whichever error is present:
          - `error`    → UI-level error (e.g. 409 already running)
          - `runError` → server-side task error (stored in status.error)
        */}
        {(error || runError) && (
          <p className="text-sm text-red-600">{error || `Error: ${runError}`}</p>
        )}
      </div>

      {/* ── Summary stat cards ──
          Only rendered after a successful run (summary is non-null).
          The `good` prop controls whether the value displays in green (good) or red (bad). */}
      {summary && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          {/* Catch rate: ≥ 70% is considered good. */}
          <StatCard
            label="Catch Rate"
            value={`${summary.catch_rate.toFixed(0)}%`}
            sub={`${summary.caught} of ${summary.total_distressed} flagged`}
            good={summary.catch_rate >= 70}
          />
          {/* Median lead: ≥ 3 months advance warning is considered good. */}
          <StatCard
            label="Median Lead"
            value={`${summary.median_lead_months.toFixed(0)} mo`}
            sub="before event"
            good={summary.median_lead_months >= 3}
          />
          {/* False-positive rate: ≤ 10% is considered good (low false alarm rate). */}
          <StatCard
            label="False Positive Rate"
            value={`${summary.fp_rate.toFixed(1)}%`}
            sub="healthy controls stressed"
            good={summary.fp_rate <= 10}
          />
          {/* Threshold is a fixed parameter — no good/bad colouring. */}
          <StatCard label="Stress Threshold" value="50" sub="score to flag" />
        </div>
      )}

      {/* ── Per-case results table ──
          Only rendered after a successful run with at least one case. */}
      {result && result.cases.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <div className="px-6 py-4 border-b border-gray-100">
            <h2 className="font-semibold text-slate-800">Case Results</h2>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 text-xs font-medium text-slate-500 uppercase tracking-wide">
                  <th className="px-6 py-3 text-left">Ticker</th>
                  <th className="px-4 py-3 text-left">Label</th>
                  <th className="px-4 py-3 text-left">Event Date</th>
                  <th className="px-4 py-3 text-center">Result</th>
                  <th className="px-6 py-3 text-right">Detail</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {/*
                  CaseRow handles three distinct layouts:
                  1. Error row — EDGAR or scoring failed for this ticker
                  2. Distressed row — shows caught/missed and lead time
                  3. Healthy row — shows false-positive count
                */}
                {result.cases.map((c, i) => <CaseRow key={i} c={c} />)}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Methodology explainer ── */}
      <div className="bg-slate-50 rounded-xl border border-slate-200 p-5 text-sm text-slate-600 space-y-2">
        <p className="font-medium text-slate-700">How the backtest works</p>
        <ul className="list-disc ml-4 space-y-1 text-xs">
          <li>Each distressed issuer is scored quarterly back from its event date using only filings available at that time.</li>
          <li>A score ≥ 50 counts as a stress flag. Lead time = months from first flag to the event.</li>
          <li>Healthy controls are scored across the last 3 years; any stressed quarter is a false positive.</li>
          <li>No look-ahead: only filings with <code className="bg-slate-100 px-1 rounded">filed ≤ eval_date</code> are used.</li>
        </ul>
      </div>

    </div>
  )
}


// ── StatCard component ────────────────────────────────────────────────────────
//
// Small KPI card used in the 2×2 summary grid.
// The `good` prop drives the value's text colour:
//   good=true  → green  (metric is in a healthy range)
//   good=false → red    (metric is in a concerning range)
//   good=undefined → default slate (no good/bad judgment)

function StatCard({
  label, value, sub, good,
}: {
  label: string
  value: string
  sub?: string
  good?: boolean
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
      <p className="text-xs text-slate-400 uppercase tracking-wide font-medium">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${
        good === true  ? 'text-green-600' :
        good === false ? 'text-red-600'   :
                         'text-slate-800'
      }`}>
        {value}
      </p>
      {sub && <p className="text-xs text-slate-400 mt-1">{sub}</p>}
    </div>
  )
}


// ── CaseRow component ─────────────────────────────────────────────────────────
//
// Renders one row in the case results table. The row layout varies by case type:
//
//   Error row     → red background, error message in the Detail column
//   Distressed    → shows the event date + caught/missed badge + lead time
//   Healthy       → shows FP count (no event date; it's a control issuer)

function CaseRow({ c }: { c: BacktestCase }) {

  // ── Error row: EDGAR fetch or scoring failed ──
  if (c.error) return (
    <tr className="bg-red-50">
      <td className="px-6 py-3 font-mono font-bold text-slate-700">{c.ticker}</td>
      <td className="px-4 py-3 capitalize text-slate-500">{c.label}</td>
      <td className="px-4 py-3 text-slate-400">—</td>
      <td className="px-4 py-3 text-center">
        <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full">Error</span>
      </td>
      <td className="px-6 py-3 text-right text-xs text-red-500">{c.error}</td>
    </tr>
  )

  // ── Distressed row: show whether the model caught the stress early ──
  if (c.label === 'distressed') {
    // Default caught to false if undefined (shouldn't happen, but safe).
    const caught = c.caught ?? false
    return (
      // Highlight the entire row red if the model missed this case.
      <tr className={caught ? '' : 'bg-red-50'}>
        <td className="px-6 py-3 font-mono font-bold text-slate-700">{c.ticker}</td>
        <td className="px-4 py-3">
          <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-medium">
            Distressed
          </span>
        </td>
        <td className="px-4 py-3 font-mono text-xs text-slate-500">{c.event_date ?? '—'}</td>
        <td className="px-4 py-3 text-center">
          {caught
            ? <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">✓ Caught</span>
            : <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-medium">✗ Missed</span>
          }
        </td>
        {/* Lead time: only shown for caught cases. */}
        <td className="px-6 py-3 text-right font-mono text-sm text-slate-700">
          {caught ? `${c.lead_months?.toFixed(0)} mo early` : '—'}
        </td>
      </tr>
    )
  }

  // ── Healthy row: show false-positive count ──
  return (
    <tr>
      <td className="px-6 py-3 font-mono font-bold text-slate-700">{c.ticker}</td>
      <td className="px-4 py-3">
        <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">
          Healthy
        </span>
      </td>
      <td className="px-4 py-3 text-slate-400 text-xs">Control</td>
      <td className="px-4 py-3 text-center">
        {/* Clean = 0 false positives. FP = at least one quarter wrongly flagged. */}
        {(c.fp_count ?? 0) === 0
          ? <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">✓ Clean</span>
          : <span className="text-xs bg-orange-100 text-orange-700 px-2 py-1 rounded-full font-medium">⚠ FP</span>
        }
      </td>
      <td className="px-6 py-3 text-right font-mono text-sm text-slate-700">
        {c.fp_count} FP periods
      </td>
    </tr>
  )
}
