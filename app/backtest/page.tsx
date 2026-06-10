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
//
// On first load the server may return the persisted results of a PREVIOUS run
// (status.saved === true) so the scorecard is visible without re-running.
// ─────────────────────────────────────────────────────────────────────────────

import { Fragment, useEffect, useRef, useState } from 'react'
import {
  LineChart, Line, YAxis, Tooltip, ReferenceLine,
} from 'recharts'
import {
  BacktestStatus, BacktestCase, TrajectoryPoint,
  CaseLibrary, SnapshotResult,
  startBacktest, fetchBacktestStatus, fetchBacktestCases, fetchSnapshot,
  fmtRatio, fmtPct, fmtFCF,
} from '@/lib/api'

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

  // ── Case library (which companies the backtest uses) ───────────────────────
  const [library, setLibrary] = useState<CaseLibrary | null>(null)

  // ── Point-in-time snapshot tool state ───────────────────────────────────────
  // asOf is the filed-before cutoff the user picks; default to today.
  const [asOf, setAsOf] = useState(() => new Date().toISOString().slice(0, 10))
  const [snapshot, setSnapshot] = useState<SnapshotResult | null>(null)
  const [snapLoading, setSnapLoading] = useState(false)
  const [snapError, setSnapError] = useState('')

  // ── Polling interval ref ───────────────────────────────────────────────────
  // We use useRef instead of useState to store the interval ID.
  // Reason: calling clearInterval() in a state setter would trigger a re-render,
  // which would re-run the useEffect that sets the interval — creating a loop.
  // A ref holds the ID without causing re-renders when it changes.
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)


  // ── Effects ─────────────────────────────────────────────────────────────────

  // On mount: fetch the current status so we can restore the UI if a backtest
  // was already running (e.g. user refreshed the page mid-run), or show the
  // last persisted run's scorecard (status.saved).
  // Cleanup function clears the poll interval when the component unmounts.
  useEffect(() => {
    fetchBacktestStatus().then(setStatus).catch(() => {})
    // The case library is static CSV data — fetch once alongside the status.
    fetchBacktestCases().then(setLibrary).catch(() => {})
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

  /**
   * Handle "Score as of date": POST the chosen filed-before cutoff and show
   * every backtest company's point-in-time score for that day.
   */
  async function handleSnapshot() {
    setSnapLoading(true)
    setSnapError('')
    try {
      setSnapshot(await fetchSnapshot(asOf))
    } catch (e: unknown) {
      setSnapError(e instanceof Error ? e.message : 'Failed to compute snapshot')
    } finally {
      setSnapLoading(false)
    }
  }

  // Destructure for cleaner JSX below.
  const { result, error: runError } = status
  const summary = result?.summary
  const threshold = result?.threshold ?? summary?.threshold ?? 50
  const earlyMonths = result?.early_months ?? summary?.early_months ?? 6


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

        {/* Provenance: which run the scorecard below comes from. */}
        {!status.running && result && (
          <p className="text-xs text-slate-400">
            {status.saved ? 'Showing saved results' : 'Results'}
            {result.run_at ? ` from ${result.run_at.replace('T', ' ')}` : ''}
          </p>
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

      {/* ── Case library: which companies the backtest uses ── */}
      {library && <CaseLibraryCard library={library} />}

      {/* ── Summary stat cards ──
          Only rendered when a result is available (live or saved).
          The `good` prop controls whether the value displays in green (good) or red (bad). */}
      {summary && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
          {/* Catch rate: ≥ 70% is considered good. */}
          <StatCard
            label="Catch Rate"
            value={`${summary.catch_rate.toFixed(0)}%`}
            sub={`${summary.caught} of ${summary.total_distressed} flagged`}
            good={summary.catch_rate >= 70}
          />
          {/* Early-warning rate: caught with enough lead time to act on. */}
          {summary.early_warning_rate != null && (
            <StatCard
              label={`Early Warning (≥${earlyMonths} mo)`}
              value={`${summary.early_warning_rate.toFixed(0)}%`}
              sub={`${summary.early_warning_caught} of ${summary.total_distressed} in time to act`}
              good={summary.early_warning_rate >= 50}
            />
          )}
          {/* Median lead: ≥ 3 months advance warning is considered good. */}
          <StatCard
            label="Median Lead"
            value={`${summary.median_lead_months.toFixed(0)} mo`}
            sub={summary.mean_lead_months != null
              ? `mean ${summary.mean_lead_months.toFixed(0)} mo`
              : 'before event'}
            good={summary.median_lead_months >= 3}
          />
          {/* False-positive rate: ≤ 10% is considered good (low false alarm rate). */}
          <StatCard
            label="False Positive Rate"
            value={`${summary.fp_rate.toFixed(1)}%`}
            sub={summary.healthy_periods_evaluated != null
              ? `${summary.fp_periods} of ${summary.healthy_periods_evaluated} healthy periods`
              : 'healthy controls stressed'}
            good={summary.fp_rate <= 10}
          />
          {/* Threshold is a run parameter — no good/bad colouring. Data quality
              issues (gaps/errors) surface here so a "clean" scorecard can't hide them. */}
          <StatCard
            label="Stress Threshold"
            value={`${threshold}`}
            sub={summary.data_gaps != null
              ? `${summary.data_gaps} data gaps · ${summary.errors} errors`
              : 'score to flag'}
          />
        </div>
      )}

      {/* ── Per-case results table ──
          Only rendered when a result with at least one case is available. */}
      {result && result.cases.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <div className="px-6 py-4 border-b border-gray-100 flex items-baseline justify-between flex-wrap gap-2">
            <h2 className="font-semibold text-slate-800">Case Results</h2>
            <p className="text-xs text-slate-400">Click a row to see each metric across its fiscal years</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 text-xs font-medium text-slate-500 uppercase tracking-wide">
                  <th className="px-6 py-3 text-left">Case</th>
                  <th className="px-4 py-3 text-left">Label</th>
                  <th className="px-4 py-3 text-left">Event Date</th>
                  <th className="px-4 py-3 text-center">Result</th>
                  <th className="px-4 py-3 text-center">Score Trajectory</th>
                  <th className="px-6 py-3 text-right">Detail</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {/*
                  CaseRow handles the distinct layouts:
                  1. Error row      — EDGAR or scoring failed for this case
                  2. Distressed row — caught/missed/data-gap, lead time, trajectory
                  3. Healthy row    — false-positive count, trajectory
                */}
                {result.cases.map((c, i) => (
                  <CaseRow key={c.case_id ?? i} c={c} threshold={threshold} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Point-in-time snapshot tool ──
          Enter a filed-before date and score all backtest companies as of
          that day — the UI entry point for the backtest's no-look-ahead rule. */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-slate-800">Point-in-Time Snapshot</h2>
          <p className="text-xs text-slate-400 mt-1">
            Pick a date to see what the system would have said that day — only filings
            with <code className="bg-slate-100 px-1 rounded">filed ≤ date</code> are used.
          </p>
        </div>
        <div className="px-6 py-4 flex items-center gap-3 flex-wrap">
          <input
            type="date"
            value={asOf}
            onChange={e => setAsOf(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-slate-700"
          />
          <button
            onClick={handleSnapshot}
            disabled={snapLoading || !asOf}
            className="bg-slate-800 text-white px-4 py-1.5 rounded-lg text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {snapLoading ? 'Scoring…' : 'Score as of date'}
          </button>
          {snapLoading && (
            <span className="text-sm text-slate-500 flex items-center gap-2">
              <span className="w-3 h-3 rounded-full bg-orange-400 animate-pulse inline-block" />
              Scoring {library?.total ?? 'all'} companies point-in-time…
            </span>
          )}
          {snapError && <p className="text-sm text-red-600">{snapError}</p>}
        </div>
        {snapshot && <SnapshotTable snapshot={snapshot} />}
      </div>

      {/* ── Methodology explainer ── */}
      <div className="bg-slate-50 rounded-xl border border-slate-200 p-5 text-sm text-slate-600 space-y-2">
        <p className="font-medium text-slate-700">How the backtest works</p>
        <ul className="list-disc ml-4 space-y-1 text-xs">
          <li>Each distressed issuer is scored quarterly back from its event date (T-36 → T-0) using only filings available at that time.</li>
          <li>A score ≥ {threshold} counts as a stress flag. Lead time = months from <em>first</em> flag to the event; ≥ {earlyMonths} months counts as an early warning.</li>
          <li>Healthy controls are scored across 3 years from a pinned anchor date; any stressed quarter is a false positive.</li>
          <li>Cases where no filings existed in the whole window are reported as <em>data gaps</em>, not misses.</li>
          <li>No look-ahead: only filings with <code className="bg-slate-100 px-1 rounded">filed ≤ eval_date</code> are used.</li>
        </ul>
      </div>

    </div>
  )
}


// ── StatCard component ────────────────────────────────────────────────────────
//
// Small KPI card used in the summary grid.
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


// ── CaseLibraryCard component ─────────────────────────────────────────────────
//
// Shows how many and which companies the backtest evaluates, straight from
// data/cases.csv — visible before any run. Collapsed by default; the header
// always answers "how many" while the expanded grid answers "which".

function CaseLibraryCard({ library }: { library: CaseLibrary }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full px-6 py-4 flex items-baseline justify-between gap-3 flex-wrap text-left hover:bg-slate-50 transition-colors"
      >
        <span className="font-semibold text-slate-800">
          <span className="inline-block w-4 text-slate-400 text-xs">{open ? '▾' : '▸'}</span>
          Case Library
        </span>
        <span className="text-sm text-slate-500">
          {library.total} companies — {library.distressed} distressed, {library.healthy} healthy controls
        </span>
      </button>
      {open && (
        <div className="px-6 pb-5 pt-1 border-t border-gray-100 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-2">
          {library.cases.map(c => (
            <div key={c.case_id || c.ticker} className="flex items-center gap-2 text-sm py-1">
              <span className={`text-xs px-2 py-0.5 rounded-full font-medium shrink-0 ${
                c.label === 'distressed'
                  ? 'bg-red-100 text-red-700'
                  : 'bg-green-100 text-green-700'
              }`}>
                {c.label === 'distressed' ? 'D' : 'H'}
              </span>
              <span className="font-mono font-bold text-slate-700 shrink-0">{c.ticker}</span>
              <span className="text-slate-500 truncate">{c.company_name}</span>
              {/* Event date only matters for distressed names (bankruptcy date). */}
              {c.label === 'distressed' && c.event_date && (
                <span className="ml-auto font-mono text-xs text-slate-400 shrink-0">{c.event_date}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}


// ── SnapshotTable component ───────────────────────────────────────────────────
//
// Results of scoring every backtest company as of one user-chosen date.
// Sorted by score descending so the most stressed names lead. The fiscal year
// column makes the point-in-time mechanics visible: it's the latest 10-K that
// had been FILED by the as-of date, which can lag the calendar by a year.

function SnapshotTable({ snapshot }: { snapshot: SnapshotResult }) {
  const rows = [...snapshot.rows].sort((a, b) => (b.score ?? -1) - (a.score ?? -1))

  return (
    <div className="border-t border-gray-100">
      <p className="px-6 pt-3 text-xs text-slate-400">
        As of <span className="font-mono">{snapshot.as_of}</span> — exactly what an investor
        could have known that day. Stress threshold: {snapshot.threshold}.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs font-medium text-slate-500 uppercase tracking-wide">
              <th className="px-6 py-3 text-left">Company</th>
              <th className="px-4 py-3 text-left">Label</th>
              <th className="px-4 py-3 text-left">FY Scored</th>
              <th className="px-4 py-3 text-right">Score</th>
              <th className="px-4 py-3 text-center">Status</th>
              <th className="px-4 py-3 text-right">Leverage</th>
              <th className="px-4 py-3 text-right">Coverage</th>
              <th className="px-6 py-3 text-right">Liquidity</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map(r => (
              <tr key={r.case_id} className={r.error ? 'bg-red-50' : ''}>
                <td className="px-6 py-2.5 whitespace-nowrap">
                  <span className="font-mono font-bold text-slate-700">{r.ticker}</span>
                  {r.company_name && (
                    <span className="block text-xs text-slate-400">{r.company_name}</span>
                  )}
                </td>
                <td className="px-4 py-2.5">
                  <span className={`text-xs px-2 py-1 rounded-full font-medium ${
                    r.label === 'distressed'
                      ? 'bg-red-100 text-red-700'
                      : 'bg-green-100 text-green-700'
                  }`}>
                    {r.label === 'distressed' ? 'Distressed' : 'Healthy'}
                  </span>
                </td>
                {r.error ? (
                  <td colSpan={6} className="px-4 py-2.5 text-xs text-red-500">{r.error}</td>
                ) : !r.has_data ? (
                  <td colSpan={6} className="px-4 py-2.5 text-xs text-slate-400">
                    No filings available yet at this date
                  </td>
                ) : (
                  <Fragment>
                    <td className="px-4 py-2.5 font-mono text-xs text-slate-500">
                      FY {r.period_end?.slice(0, 4)}
                    </td>
                    <td className={`px-4 py-2.5 text-right font-mono font-bold ${
                      (r.score ?? 0) >= snapshot.threshold ? 'text-red-600' : 'text-slate-700'
                    }`}>
                      {r.score?.toFixed(0)}
                    </td>
                    <td className="px-4 py-2.5 text-center">
                      {r.stressed
                        ? <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-medium">⚠ Stressed</span>
                        : <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">OK</span>
                      }
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-xs text-slate-600">
                      {fmtRatio(r.ratios?.leverage ?? null)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-xs text-slate-600">
                      {fmtRatio(r.ratios?.interest_coverage ?? null)}
                    </td>
                    <td className="px-6 py-2.5 text-right font-mono text-xs text-slate-600">
                      {fmtRatio(r.ratios?.liquidity ?? null)}
                    </td>
                  </Fragment>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}


// ── Sparkline component ───────────────────────────────────────────────────────
//
// Tiny inline chart of a case's point-in-time score trajectory (oldest → newest,
// i.e. T-36 on the left, the event date on the right). The dashed reference line
// is the stress threshold, so "did the line cross before the end?" is readable
// at a glance. Snapshots with no available filings render as gaps in the line.

function Sparkline({ c, threshold }: { c: BacktestCase; threshold: number }) {
  if (!c.trajectory || c.trajectory.length === 0) {
    return <span className="text-xs text-slate-300">—</span>
  }

  // Trajectory arrives newest-first; plot left-to-right in time order.
  const data = [...c.trajectory].reverse().map(t => ({
    t: `T-${t.months_before_event.toFixed(0)}`,
    // null breaks the line where no filings existed yet (recharts skips nulls).
    score: t.has_data ? t.score : null,
  }))

  return (
    <LineChart width={150} height={40} data={data}
               margin={{ top: 4, right: 4, bottom: 2, left: 4 }}>
      {/* Fixed 0–100 domain so all rows are visually comparable. */}
      <YAxis domain={[0, 100]} hide />
      <ReferenceLine y={threshold} stroke="#f97316" strokeDasharray="3 3" strokeWidth={1} />
      <Line
        type="monotone"
        dataKey="score"
        stroke={c.label === 'healthy' ? '#16a34a' : '#475569'}
        strokeWidth={1.5}
        dot={false}
        isAnimationActive={false}
      />
      <Tooltip
        formatter={(v: number) => [`score ${v.toFixed(1)}`, '']}
        labelFormatter={(l: string) => `${l} months`}
        separator=""
        contentStyle={{ fontSize: 11, padding: '2px 6px' }}
      />
    </LineChart>
  )
}


// ── Metric history (expanded row) ─────────────────────────────────────────────
//
// The trajectory holds ~13 quarterly snapshots, but the underlying data is
// annual 10-Ks — several consecutive snapshots score against the same fiscal
// year. Deduping on period_end yields one column per available fiscal year,
// which is the view an analyst wants: each metric tracked across years.

// Row definitions for the metrics-by-year table. `fmt` reuses the exact same
// formatters as the issuer detail page so values read identically everywhere.
const METRICS: { key: string; label: string; fmt: (v: number | null) => string }[] = [
  { key: 'leverage',               label: 'Leverage (net debt / EBITDA)', fmt: v => fmtRatio(v) },
  { key: 'interest_coverage',      label: 'Interest Coverage',            fmt: v => fmtRatio(v) },
  { key: 'ebitda_margin',          label: 'EBITDA Margin',                fmt: v => fmtPct(v) },
  { key: 'fcf_margin',             label: 'FCF Margin',                   fmt: v => fmtPct(v) },
  { key: 'free_cash_flow',         label: 'Free Cash Flow',               fmt: v => fmtFCF(v) },
  { key: 'liquidity',              label: 'Liquidity (cash / ST debt)',   fmt: v => fmtRatio(v) },
  { key: 'maturity_near_term_pct', label: 'Maturity Wall (≤3y due)',      fmt: v => fmtPct(v) },
]

/**
 * Collapse the trajectory to one snapshot per fiscal year.
 * Trajectory is newest-first; the FIRST snapshot seen for a period_end is the
 * most recently evaluated one (fullest point-in-time data for that year).
 * Returns fiscal years ascending so time reads left → right.
 */
function periodsByYear(trajectory: TrajectoryPoint[]): TrajectoryPoint[] {
  const byPeriod = new Map<string, TrajectoryPoint>()
  for (const t of trajectory) {
    if (t.has_data && t.period_end && !byPeriod.has(t.period_end)) {
      byPeriod.set(t.period_end, t)
    }
  }
  return Array.from(byPeriod.values()).sort((a, b) => a.period_end!.localeCompare(b.period_end!))
}

function MetricHistory({ c, threshold }: { c: BacktestCase; threshold: number }) {
  const periods = periodsByYear(c.trajectory ?? [])
  if (periods.length === 0) {
    return <p className="text-xs text-slate-400 px-6 py-4">No fiscal periods had filings available in the backtest window.</p>
  }

  return (
    <div className="px-6 py-4 overflow-x-auto">
      <p className="text-xs font-medium text-slate-500 mb-2">
        Point-in-time metrics by fiscal year — values as they were knowable at each backtest snapshot
        {c.event_date ? ` (event: ${c.event_date})` : ''}
      </p>
      <table className="text-xs">
        <thead>
          <tr className="text-slate-400">
            <th className="text-left pr-6 py-1 font-medium">Metric</th>
            {periods.map(p => (
              <th key={p.period_end} className="text-right px-3 py-1 font-mono font-medium">
                FY {p.period_end!.slice(0, 4)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {/* Stress score first — the composite the metrics below feed into. */}
          <tr className="border-t border-slate-100">
            <td className="pr-6 py-1.5 font-medium text-slate-600">Stress Score</td>
            {periods.map(p => (
              <td key={p.period_end}
                  className={`text-right px-3 py-1.5 font-mono font-bold ${
                    p.score >= threshold ? 'text-red-600' : 'text-slate-700'
                  }`}>
                {p.score.toFixed(0)}
              </td>
            ))}
          </tr>
          {METRICS.map(m => (
            <tr key={m.key} className="border-t border-slate-100">
              <td className="pr-6 py-1.5 text-slate-500">{m.label}</td>
              {periods.map(p => (
                <td key={p.period_end} className="text-right px-3 py-1.5 font-mono text-slate-700">
                  {m.fmt(p.ratios?.[m.key] ?? null)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}


// ── CaseRow component ─────────────────────────────────────────────────────────
//
// Renders one row in the case results table. The row layout varies by case type:
//
//   Error row     → red background, error message in the Detail column
//   Distressed    → event date + caught/missed/data-gap badge + lead time
//   Healthy       → FP count over evaluated periods (control issuer)
//
// Clicking a (non-error) row expands a per-fiscal-year metric history table —
// the drill-down that shows WHICH ratio drove (or failed to drive) the flag.

function CaseRow({ c, threshold }: { c: BacktestCase; threshold: number }) {
  const [expanded, setExpanded] = useState(false)
  const expandable = !c.error && (c.trajectory?.length ?? 0) > 0

  // Company + ticker cell shared by all row types. The chevron signals the
  // row is expandable and flips when open.
  const nameCell = (
    <td className="px-6 py-3 whitespace-nowrap">
      {expandable && (
        <span className="inline-block w-4 text-slate-400 text-xs">{expanded ? '▾' : '▸'}</span>
      )}
      <span className="font-mono font-bold text-slate-700">{c.ticker}</span>
      {c.company_name && (
        <span className="block text-xs text-slate-400 pl-4">{c.company_name}</span>
      )}
    </td>
  )

  // Expanded detail row spanning the full table width.
  const detailRow = expanded && expandable && (
    <tr className="bg-slate-50">
      <td colSpan={6} className="border-t border-slate-100">
        <MetricHistory c={c} threshold={threshold} />
      </td>
    </tr>
  )

  const rowProps = {
    onClick: () => expandable && setExpanded(e => !e),
    className: expandable ? 'cursor-pointer hover:bg-slate-50' : '',
  }

  // ── Error row: EDGAR fetch or scoring failed (not expandable) ──
  if (c.error) return (
    <tr className="bg-red-50">
      {nameCell}
      <td className="px-4 py-3 capitalize text-slate-500">{c.label}</td>
      <td className="px-4 py-3 text-slate-400">—</td>
      <td className="px-4 py-3 text-center">
        <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full">Error</span>
      </td>
      <td className="px-4 py-3" />
      <td className="px-6 py-3 text-right text-xs text-red-500">{c.error}</td>
    </tr>
  )

  // ── Distressed row: show whether the model caught the stress early ──
  if (c.label === 'distressed') {
    // Default caught to false if undefined (shouldn't happen, but safe).
    const caught = c.caught ?? false
    const dataGap = c.status === 'data_gap'
    return (
      <Fragment>
        {/* Highlight the entire row red only for a genuine miss — a data gap is
            a coverage problem, not a scoring failure, so it gets amber instead. */}
        <tr {...rowProps}
            className={`${rowProps.className} ${caught ? '' : dataGap ? 'bg-amber-50' : 'bg-red-50'}`}>
          {nameCell}
          <td className="px-4 py-3">
            <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-medium">
              Distressed
            </span>
          </td>
          <td className="px-4 py-3 font-mono text-xs text-slate-500">{c.event_date ?? '—'}</td>
          <td className="px-4 py-3 text-center">
            {caught
              ? <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">
                  ✓ Caught{c.early_warning ? ' early' : ''}
                </span>
              : dataGap
              ? <span className="text-xs bg-amber-100 text-amber-700 px-2 py-1 rounded-full font-medium">No data</span>
              : <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-medium">✗ Missed</span>
            }
          </td>
          <td className="px-4 py-3"><Sparkline c={c} threshold={threshold} /></td>
          {/* Lead time: only shown for caught cases. early_warning = in time to act. */}
          <td className="px-6 py-3 text-right font-mono text-sm text-slate-700">
            {caught ? `${c.lead_months?.toFixed(1)} mo early` : '—'}
          </td>
        </tr>
        {detailRow}
      </Fragment>
    )
  }

  // ── Healthy row: show false-positive count ──
  return (
    <Fragment>
      <tr {...rowProps}>
        {nameCell}
        <td className="px-4 py-3">
          <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">
            Healthy
          </span>
        </td>
        <td className="px-4 py-3 text-slate-400 text-xs">Control</td>
        <td className="px-4 py-3 text-center">
          {/* Clean = 0 false positives. FP = at least one quarter wrongly flagged. */}
          {c.status === 'data_gap'
            ? <span className="text-xs bg-amber-100 text-amber-700 px-2 py-1 rounded-full font-medium">No data</span>
            : (c.fp_count ?? 0) === 0
            ? <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">✓ Clean</span>
            : <span className="text-xs bg-orange-100 text-orange-700 px-2 py-1 rounded-full font-medium">⚠ FP</span>
          }
        </td>
        <td className="px-4 py-3"><Sparkline c={c} threshold={threshold} /></td>
        <td className="px-6 py-3 text-right font-mono text-sm text-slate-700">
          {c.fp_count} FP
          {c.periods_evaluated != null ? ` / ${c.periods_evaluated} periods` : ' periods'}
        </td>
      </tr>
      {detailRow}
    </Fragment>
  )
}
