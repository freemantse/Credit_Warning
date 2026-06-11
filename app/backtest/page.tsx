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
import type { FormEvent, ReactNode } from 'react'
import {
  LineChart, Line, YAxis, Tooltip, ReferenceLine,
} from 'recharts'
import {
  BacktestStatus, BacktestCase, TrajectoryPoint,
  CaseLibrary, BacktestCaseInfo, AddCasePayload, ScoreConfig,
  startBacktest, fetchBacktestStatus, fetchBacktestCases,
  addCase, deleteCase, fetchScoreConfig, saveScoreConfig,
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

  // History depth: point-in-time snapshots per case (~90 days apart). Default 40
  // (~10 years). The server clamps this to a safe range.
  const [steps, setSteps] = useState(40)

  // UI-level error (e.g. the 409 "already running" response). Separate from
  // status.error (which is a server-side task error) so both can be shown.
  const [error, setError] = useState('')

  // ── Case library (which companies the backtest uses) ───────────────────────
  const [library, setLibrary] = useState<CaseLibrary | null>(null)

  // ── Scoring parameters ──────────────────────────────────────────────────────
  // `scoreCfg` is the editable DRAFT; `scoreDefaults` are the built-in defaults
  // (for "Reset"). Running the backtest TESTS the draft (transient); "Apply to
  // portfolio" persists it as the active config the live dashboard uses.
  const [scoreCfg, setScoreCfg] = useState<ScoreConfig | null>(null)
  const [scoreDefaults, setScoreDefaults] = useState<ScoreConfig | null>(null)
  const [applying, setApplying] = useState(false)
  const [applyMsg, setApplyMsg] = useState('')

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
    // Fetch the case roster and the scoring parameters once alongside the status.
    fetchBacktestCases().then(setLibrary).catch(() => {})
    fetchScoreConfig().then(r => { setScoreCfg(r.active); setScoreDefaults(r.defaults) }).catch(() => {})
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  /** Re-fetch the case library after an add/delete so the list reflects the change. */
  const reloadLibrary = () => fetchBacktestCases().then(setLibrary).catch(() => {})

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
      // Send the current draft config so the run TESTS these parameters without
      // touching the live portfolio. When unedited, the draft equals the applied
      // config, so the backtest matches the dashboard.
      await startBacktest(steps, scoreCfg ?? undefined)
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
   * Apply the draft scoring parameters to the live portfolio (persist as active).
   * This immediately changes the dashboard/detail scores — they're recomputed on
   * the fly, no re-track needed.
   */
  async function handleApply() {
    if (!scoreCfg) return
    setApplying(true)
    setApplyMsg('')
    try {
      await saveScoreConfig(scoreCfg)
      setApplyMsg('Applied — the portfolio now scores with these parameters.')
    } catch (e: unknown) {
      setApplyMsg(e instanceof Error ? e.message : 'Failed to apply parameters')
    } finally {
      setApplying(false)
    }
  }

  // Destructure for cleaner JSX below.
  const { result, error: runError } = status
  const summary = result?.summary
  const threshold = result?.threshold ?? summary?.threshold ?? 50
  const earlyMonths = result?.early_months ?? summary?.early_months ?? 6
  // Snapshots used by the displayed run (fall back to the selected value before
  // any run). Drives the methodology copy so it matches the actual window.
  const stepsUsed = result?.steps ?? steps
  const windowYears = (stepsUsed * 90 / 365.25).toFixed(1)
  const oldestMonths = Math.round((stepsUsed - 1) * 90 / 30.44)


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

      {/* ── How the backtest works (methodology) ──
          Shown first so the scorecard and case table below read in context. */}
      <div className="bg-slate-50 rounded-xl border border-slate-200 p-5 text-sm text-slate-600 space-y-2">
        <p className="font-medium text-slate-700">How the backtest works</p>
        <ul className="list-disc ml-4 space-y-1 text-xs">
          <li>Each distressed issuer is scored quarterly working backward from its <strong>credit-event date</strong> — the day distress crystallised, e.g. the Chapter&nbsp;11 bankruptcy filing (T-{oldestMonths} → T-0, ~{windowYears} years) — using only filings available at that time.</li>
          <li>A score ≥ {threshold} counts as a stress flag. Lead time = months from the <em>first</em> flag to that event; ≥ {earlyMonths} months counts as an early warning.</li>
          <li>Healthy controls are scored across ~{windowYears} years from a pinned anchor date; any stressed quarter is a false positive.</li>
          <li>Cases where no filings existed in the whole window are reported as <em>data gaps</em>, not misses.</li>
          <li>No look-ahead: only filings with <code className="bg-slate-100 px-1 rounded">filed ≤ eval_date</code> are used.</li>
        </ul>
      </div>

      {/* ── Scoring parameters (test in backtest vs. apply to portfolio) ── */}
      {scoreCfg && scoreDefaults && (
        <ScoreParamsCard
          cfg={scoreCfg}
          defaults={scoreDefaults}
          onChange={setScoreCfg}
          onApply={handleApply}
          applying={applying}
          applyMsg={applyMsg}
          running={status.running || starting}
        />
      )}

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

        {/* History depth control: snapshots per case (~90 days apart). A slider
            with the window in years as an inline readout — that's what the user
            actually reasons about. Disabled mid-run. */}
        <div className="flex flex-col gap-1">
          <label htmlFor="history-depth" className="text-xs font-medium text-slate-500 uppercase tracking-wide">
            History depth
          </label>
          <div className="flex items-center gap-3">
            <input
              id="history-depth"
              type="range"
              min={4}
              max={60}
              step={1}
              value={steps}
              onChange={e => setSteps(Number(e.target.value))}
              disabled={status.running || starting}
              className="w-40 accent-slate-700 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            />
            <span className="text-xs text-slate-400 tabular-nums whitespace-nowrap">
              {steps} snapshots · ~{(steps * 90 / 365.25).toFixed(1)} yrs
            </span>
          </div>
        </div>

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

        <p className="basis-full text-xs text-slate-400">
          Runs with the scoring parameters above — testing only. Use <strong>Apply to portfolio</strong> to change live dashboard scores.
        </p>
      </div>

      {/* ── Case library: which companies the backtest uses ── */}
      {library && <CaseLibraryCard library={library} onChange={reloadLibrary} />}

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
                  <th
                    className="px-4 py-3 text-left"
                    title="Date of the credit event — e.g. the Chapter 11 bankruptcy filing. Healthy controls have none."
                  >
                    Credit Event
                  </th>
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
// Shows how many and which companies the backtest evaluates (from the Supabase
// `cases` table) — visible before any run. Collapsed by default; the header
// always answers "how many" while the expanded grid answers "which". The
// expanded panel also lets the user add a new case or delete an existing one;
// `onChange` re-fetches the library after either mutation.

function CaseLibraryCard({ library, onChange }: { library: CaseLibrary; onChange: () => void }) {
  const [open, setOpen] = useState(false)

  // Add-case form state.
  const [identifier, setIdentifier] = useState('')
  const [label, setLabel] = useState<'distressed' | 'healthy'>('distressed')
  const [eventDate, setEventDate] = useState('')
  const [notes, setNotes] = useState('')
  const [adding, setAdding] = useState(false)
  const [formError, setFormError] = useState('')

  // Custom delete-confirmation modal state. `pendingDelete` holds the case whose
  // × the user clicked; the modal is shown while it's non-null. `deleting` guards
  // the modal buttons (and the backdrop dismiss) while the DELETE is in flight.
  const [pendingDelete, setPendingDelete] = useState<BacktestCaseInfo | null>(null)
  const [deleting, setDeleting] = useState(false)

  async function handleAdd(e: FormEvent) {
    e.preventDefault()
    setAdding(true)
    setFormError('')
    try {
      const payload: AddCasePayload = { identifier: identifier.trim(), label }
      // event_date is required for distressed; optional for healthy (backend pins a default).
      if (eventDate) payload.event_date = eventDate
      if (notes.trim()) payload.notes = notes.trim()
      await addCase(payload)
      setIdentifier(''); setEventDate(''); setNotes('')
      onChange()
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : 'Failed to add case')
    } finally {
      setAdding(false)
    }
  }

  // Performs the deletion the modal is confirming. Triggered by the modal's
  // "Remove" button — the × button only opens the modal (sets pendingDelete).
  async function confirmDelete() {
    const c = pendingDelete
    if (!c) return
    setDeleting(true)
    setFormError('')
    try {
      await deleteCase(c.case_id)
      setPendingDelete(null)  // close the modal on success
      onChange()              // re-fetch the library to drop the removed row
    } catch (err: unknown) {
      setPendingDelete(null)  // close the modal; the error banner explains the failure
      setFormError(err instanceof Error ? err.message : 'Failed to delete case')
    } finally {
      setDeleting(false)
    }
  }

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
        <div className="px-6 pb-5 pt-3 border-t border-gray-100 space-y-4">
          {/* Add-case form. The backend resolves the ticker/CIK to a CIK + name. */}
          <form onSubmit={handleAdd} className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-slate-500">Ticker or CIK</label>
              <input
                value={identifier}
                onChange={e => setIdentifier(e.target.value)}
                placeholder="e.g. BTU or 1064728"
                className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm w-40 focus:outline-none focus:ring-2 focus:ring-slate-300"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-slate-500">Label</label>
              <select
                value={label}
                onChange={e => setLabel(e.target.value as 'distressed' | 'healthy')}
                className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-slate-300"
              >
                <option value="distressed">Distressed</option>
                <option value="healthy">Healthy control</option>
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-slate-500">
                {label === 'distressed' ? 'Event date (required)' : 'Anchor date (optional)'}
              </label>
              <input
                type="date"
                value={eventDate}
                onChange={e => setEventDate(e.target.value)}
                className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-300"
              />
            </div>
            <div className="flex flex-col gap-1 flex-1 min-w-[8rem]">
              <label className="text-xs font-medium text-slate-500">Notes (optional)</label>
              <input
                value={notes}
                onChange={e => setNotes(e.target.value)}
                placeholder="e.g. Chapter 11 petition; coal downturn"
                className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm w-full focus:outline-none focus:ring-2 focus:ring-slate-300"
              />
            </div>
            <button
              type="submit"
              disabled={adding || !identifier.trim() || (label === 'distressed' && !eventDate)}
              className="bg-slate-800 text-white px-4 py-1.5 rounded-lg text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {adding ? 'Adding…' : 'Add case'}
            </button>
          </form>
          {formError && <p className="text-sm text-red-600">{formError}</p>}

          {/* The roster, with a delete control per row. */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-2">
            {library.cases.map(c => (
              <div key={c.case_id || c.ticker} className="flex items-center gap-2 text-sm py-1 group">
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
                  <span
                    className="ml-auto font-mono text-xs text-slate-400 shrink-0"
                    title="Credit-event date — e.g. the Chapter 11 bankruptcy filing"
                  >
                    {c.event_date}
                  </span>
                )}
                <button
                  onClick={() => { setFormError(''); setPendingDelete(c) }}
                  title="Remove case"
                  className={`shrink-0 text-slate-300 hover:text-red-600 transition-colors px-1 leading-none ${
                    c.label === 'distressed' && c.event_date ? '' : 'ml-auto'
                  }`}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Remove-case confirmation modal ──
          Rendered only when a case is pending deletion. The backdrop click and
          the Cancel button both dismiss it; Remove calls confirmDelete(). */}
      {pendingDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          // Click on the backdrop (but not the dialog itself) cancels.
          onClick={() => { if (!deleting) setPendingDelete(null) }}
        >
          <div
            className="bg-white rounded-xl shadow-xl border border-gray-200 w-full max-w-md p-6"
            // Stop clicks inside the dialog from bubbling up to the backdrop handler.
            onClick={e => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
          >
            <h3 className="text-base font-semibold text-slate-900">Remove case?</h3>
            <p className="text-sm text-slate-600 mt-2">
              This removes{' '}
              <span className="font-semibold text-slate-800">
                {pendingDelete.company_name || pendingDelete.ticker || pendingDelete.case_id}
              </span>
              {pendingDelete.company_name && pendingDelete.ticker && (
                <span className="font-mono text-slate-500"> ({pendingDelete.ticker})</span>
              )}
              {' '}from the case library, so future backtests won&apos;t evaluate it.
            </p>
            <div className="text-xs text-slate-400 mt-2 flex items-center gap-2">
              <span className={`px-2 py-0.5 rounded-full font-medium ${
                pendingDelete.label === 'distressed'
                  ? 'bg-red-100 text-red-700'
                  : 'bg-green-100 text-green-700'
              }`}>
                {pendingDelete.label === 'distressed' ? 'Distressed' : 'Healthy control'}
              </span>
              {pendingDelete.event_date && (
                <span className="font-mono">{pendingDelete.event_date}</span>
              )}
            </div>

            <div className="flex justify-end gap-3 mt-6">
              <button
                onClick={() => setPendingDelete(null)}
                disabled={deleting}
                className="px-4 py-2 rounded-lg text-sm font-medium text-slate-600 hover:bg-gray-100 transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={confirmDelete}
                disabled={deleting}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 text-white hover:bg-red-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {deleting ? 'Removing…' : 'Remove'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}


// ── ScoreParamsCard component ─────────────────────────────────────────────────
//
// The editable scoring-parameter panel. Edits a DRAFT config (lifted to the page
// via onChange). The page's "Run Backtest" tests the draft transiently; this
// card's "Apply to portfolio" persists it as the active config the live
// dashboard scores with. "Reset to defaults" restores the built-in parameters.

// The 9 quantitative rules, in scorecard order, with friendly labels and a
// one-line description of what each metric measures. Keys match the breakdown
// keys in src/score.py. Weight = the rule's max points; the ramp awards 0 pts at
// `healthy`, rising to `weight` at `severe`.
const RULE_META: { key: string; label: string; desc: string }[] = [
  { key: 'profitability',         label: 'Profitability (EBITDA margin)', desc: 'EBITDA ÷ revenue. Lower is worse; operating losses score highest.' },
  { key: 'leverage>5x',           label: 'Leverage (net debt / EBITDA)',  desc: 'Net debt ÷ EBITDA — years of earnings to repay debt. Higher is worse.' },
  { key: 'coverage<2x',           label: 'Interest coverage',             desc: 'EBITDA ÷ interest expense. Lower is worse; <1× can’t cover interest.' },
  { key: 'cash_flow_to_debt<30%', label: 'Cash flow / debt (FFO proxy)',  desc: 'Operating cash flow ÷ gross debt. Lower is worse (key distress signal).' },
  { key: 'fcf_negative',          label: 'FCF margin',                    desc: 'Free cash flow ÷ revenue. Lower is worse; sustained negative burns cash.' },
  { key: 'liquidity<1x',          label: 'Liquidity (cash / ST debt)',    desc: 'Cash ÷ short-term debt. Lower is worse; <1× can’t cover near-term debt.' },
  { key: 'current_ratio<1.5x',    label: 'Current ratio',                 desc: 'Current assets ÷ current liabilities — working-capital cushion.' },
  { key: 'debt_to_assets>40%',    label: 'Debt / assets',                 desc: 'Gross debt ÷ total assets (gearing). Higher is worse.' },
  { key: 'maturity_wall',         label: 'Maturity wall (≤3y due)',       desc: 'Share of debt maturing within 3 years. Higher is worse (refinancing risk).' },
]

// A fixed-width number input with the built-in default shown directly BELOW it
// (not beside it) so the input boxes stay vertically aligned across rows.
function NumInput({ value, onChange, step = 'any', width = 'w-24', def }: {
  value: number; onChange: (v: number) => void; step?: string; width?: string; def?: number
}) {
  return (
    <span className="inline-flex flex-col items-end">
      <input
        type="number"
        step={step}
        value={value}
        onChange={e => { const v = parseFloat(e.target.value); onChange(Number.isNaN(v) ? 0 : v) }}
        className={`${width} border border-gray-300 rounded px-2 py-1 text-xs font-mono text-right focus:outline-none focus:ring-2 focus:ring-slate-300`}
      />
      {def != null && (
        <span className="text-[10px] text-slate-400 leading-none mt-1" title="Built-in default value">
          default: {def}
        </span>
      )}
    </span>
  )
}

// One parameter row: a label + short description on the left, the input(s) on the
// right. Keeps every control aligned down a single right-hand column.
function ParamRow({ label, desc, children }: { label: string; desc?: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2 border-t border-slate-100 first:border-t-0">
      <div className="min-w-0">
        <div className="text-slate-600">{label}</div>
        {desc && <div className="text-[11px] text-slate-400">{desc}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

function ScoreParamsCard({
  cfg, defaults, onChange, onApply, applying, applyMsg, running,
}: {
  cfg: ScoreConfig
  defaults: ScoreConfig
  onChange: (c: ScoreConfig) => void
  onApply: () => void
  applying: boolean
  applyMsg: string
  running: boolean
}) {
  const [open, setOpen] = useState(false)
  const [advanced, setAdvanced] = useState(false)

  const setRule = (key: string, field: 'weight' | 'healthy' | 'severe', v: number) =>
    onChange({ ...cfg, rules: { ...cfg.rules, [key]: { ...cfg.rules[key], [field]: v } } })
  const setEsc = (field: 'min_severe' | 'severe_frac' | 'floor', v: number) =>
    onChange({ ...cfg, escalation: { ...cfg.escalation, [field]: v } })
  const setLlm = (field: keyof ScoreConfig['llm'], v: number) =>
    onChange({ ...cfg, llm: { ...cfg.llm, [field]: v } })
  const setOverride = (key: string, v: number) =>
    onChange({ ...cfg, ebitda_override: { ...cfg.ebitda_override, [key]: v } })

  const totalWeight = RULE_META.reduce((s, r) => s + (cfg.rules[r.key]?.weight ?? 0), 0)

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full px-6 py-4 flex items-baseline justify-between gap-3 flex-wrap text-left hover:bg-slate-50 transition-colors"
      >
        <span className="font-semibold text-slate-800">
          <span className="inline-block w-4 text-slate-400 text-xs">{open ? '▾' : '▸'}</span>
          Scoring parameters
        </span>
        <span className="text-sm text-slate-500">
          Tune the weights & thresholds — total core weight {totalWeight.toFixed(0)} pts
        </span>
      </button>

      {open && (
        <div className="px-6 pb-5 pt-3 border-t border-gray-100 space-y-5">
          {/* How the parameters work — orient the user before they edit anything. */}
          <div className="text-xs text-slate-600 bg-slate-50 rounded-lg p-3 border border-slate-200 space-y-1.5">
            <p>
              The stress score (0–{cfg.score_cap.toFixed(0)}) is the sum of the rule points below. Each rule scores
              <strong> 0</strong> while its metric is at or healthier than <strong>Healthy</strong>, then ramps
              linearly up to its full <strong>Weight</strong> at <strong>Severe</strong> (and beyond). An issuer is
              flagged <strong>stressed</strong> at or above the <strong>Threshold</strong>; if at least the escalation
              count of rules are individually severe, the score is floored.
            </p>
            <p className="text-slate-500">
              <strong>Weight</strong> = the most points a rule can add ·
              <strong> Healthy</strong> = metric value that scores 0 ·
              <strong> Severe</strong> = metric value that scores the full weight.
              Raising a weight makes that signal matter more; moving Healthy/Severe changes how early it starts to bite.
            </p>
            <p className="text-slate-500">
              The small grey <span className="text-slate-400">default: N</span> under each field is that
              parameter&rsquo;s built-in default — what <strong>Reset to defaults</strong> restores.
            </p>
          </div>

          {/* Rule weights & ramp endpoints. */}
          <div>
            <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Rule weights &amp; ramps</p>
            <div className="overflow-x-auto">
              <table className="text-xs">
                <thead>
                  <tr className="text-slate-400">
                    <th className="text-left pr-6 py-1 font-medium">Rule</th>
                    <th className="text-right px-3 py-1 font-medium" title="Max points this rule contributes">Weight</th>
                    <th className="text-right px-3 py-1 font-medium" title="Metric value at/above which the rule scores 0">Healthy</th>
                    <th className="text-right px-3 py-1 font-medium" title="Metric value at/beyond which the rule scores its full weight">Severe</th>
                  </tr>
                </thead>
                <tbody>
                  {RULE_META.map(({ key, label, desc }) => (
                    <tr key={key} className="border-t border-slate-100 align-top">
                      <td className="pr-6 py-1.5 text-slate-600">
                        <div>{label}</div>
                        <div className="text-[11px] text-slate-400 font-normal">{desc}</div>
                      </td>
                      <td className="px-3 py-1.5 text-right"><NumInput value={cfg.rules[key].weight} onChange={v => setRule(key, 'weight', v)} def={defaults.rules[key]?.weight} /></td>
                      <td className="px-3 py-1.5 text-right"><NumInput value={cfg.rules[key].healthy} onChange={v => setRule(key, 'healthy', v)} def={defaults.rules[key]?.healthy} /></td>
                      <td className="px-3 py-1.5 text-right"><NumInput value={cfg.rules[key].severe} onChange={v => setRule(key, 'severe', v)} def={defaults.rules[key]?.severe} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Threshold, score cap, and the distress-escalation floor. */}
          <div>
            <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Thresholds &amp; escalation</p>
            <p className="text-xs text-slate-400 mb-1">
              When a company counts as &ldquo;stressed&rdquo;, the score ceiling, and a safety net that floors the
              score for issuers failing many rules at once (so an offsetting metric can&rsquo;t mask broad distress).
            </p>
            <div className="text-xs max-w-2xl">
              <ParamRow label="Stress threshold" desc="Score at or above which a company is flagged stressed (and counts as a backtest catch).">
                <NumInput value={cfg.threshold} onChange={v => onChange({ ...cfg, threshold: v })} step="1" def={defaults.threshold} />
              </ParamRow>
              <ParamRow label="Score cap" desc="Highest possible score — the summed rule points are capped here.">
                <NumInput value={cfg.score_cap} onChange={v => onChange({ ...cfg, score_cap: v })} step="1" def={defaults.score_cap} />
              </ParamRow>
              <ParamRow label="Escalation trigger" desc="Number of individually-severe rules needed to trigger the floor.">
                <NumInput value={cfg.escalation.min_severe} onChange={v => setEsc('min_severe', v)} step="1" def={defaults.escalation.min_severe} />
              </ParamRow>
              <ParamRow label="Severe cutoff" desc="Fraction (0–1) of its weight at which a rule counts as “severe”.">
                <NumInput value={cfg.escalation.severe_frac} onChange={v => setEsc('severe_frac', v)} step="0.05" def={defaults.escalation.severe_frac} />
              </ParamRow>
              <ParamRow label="Escalation floor" desc="The score is raised to at least this value when the trigger fires.">
                <NumInput value={cfg.escalation.floor} onChange={v => setEsc('floor', v)} step="1" def={defaults.escalation.floor} />
              </ParamRow>
            </div>
          </div>

          {/* Advanced: LLM qualitative signals + EBITDA≤0 override. These do NOT
              affect the backtest (it scores XBRL-only) — only the live dashboard. */}
          <div>
            <button
              onClick={() => setAdvanced(a => !a)}
              className="inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-700 hover:underline transition-colors"
            >
              <span className="text-slate-400">{advanced ? '▾' : '▸'}</span>
              Advanced parameters
              <span className="text-slate-400">— qualitative (LLM) signals &amp; negative-EBITDA override</span>
            </button>
            {advanced && (
              <div className="mt-3 text-xs max-w-2xl">
                <p className="text-slate-400 mb-1">
                  These come from the LLM review of filing text and affect <strong>only the live portfolio dashboard</strong> —
                  the backtest is scored from XBRL financials alone. Each signal adds <em>points per item</em> up to its own
                  <em> cap</em>; all qualitative signals together are also capped (Combined cap) so filing language can never
                  push a company over the threshold on its own.
                </p>
                <ParamRow label="High-severity finding" desc="Points per high-severity qualitative concern, capped per row.">
                  <span className="inline-flex items-center gap-1 text-slate-500">points <NumInput value={cfg.llm.high_severity_per} onChange={v => setLlm('high_severity_per', v)} width="w-16" def={defaults.llm.high_severity_per} /> cap <NumInput value={cfg.llm.high_severity_cap} onChange={v => setLlm('high_severity_cap', v)} width="w-16" def={defaults.llm.high_severity_cap} /></span>
                </ParamRow>
                <ParamRow label="Covenant proximity" desc="Points per covenant the filing flags as near its limit.">
                  <span className="inline-flex items-center gap-1 text-slate-500">points <NumInput value={cfg.llm.covenant_per} onChange={v => setLlm('covenant_per', v)} width="w-16" def={defaults.llm.covenant_per} /> cap <NumInput value={cfg.llm.covenant_cap} onChange={v => setLlm('covenant_cap', v)} width="w-16" def={defaults.llm.covenant_cap} /></span>
                </ParamRow>
                <ParamRow label="Loss provision" desc="Points per material litigation / contingency provision disclosed.">
                  <span className="inline-flex items-center gap-1 text-slate-500">points <NumInput value={cfg.llm.provision_per} onChange={v => setLlm('provision_per', v)} width="w-16" def={defaults.llm.provision_per} /> cap <NumInput value={cfg.llm.provision_cap} onChange={v => setLlm('provision_cap', v)} width="w-16" def={defaults.llm.provision_cap} /></span>
                </ParamRow>
                <ParamRow label="Combined LLM cap" desc="Hard ceiling on all qualitative signals added together.">
                  <NumInput value={cfg.llm.combined_cap} onChange={v => setLlm('combined_cap', v)} def={defaults.llm.combined_cap} />
                </ParamRow>
                <ParamRow label="Negative-EBITDA override" desc="When EBITDA ≤ 0 the leverage & coverage ratios flip sign; instead of the ramp, force these points.">
                  <span className="inline-flex items-center gap-1 text-slate-500">leverage <NumInput value={cfg.ebitda_override['leverage>5x']} onChange={v => setOverride('leverage>5x', v)} width="w-16" def={defaults.ebitda_override['leverage>5x']} /> coverage <NumInput value={cfg.ebitda_override['coverage<2x']} onChange={v => setOverride('coverage<2x', v)} width="w-16" def={defaults.ebitda_override['coverage<2x']} /></span>
                </ParamRow>
              </div>
            )}
          </div>

          {/* Actions. Apply changes live scores immediately; Run Backtest (above)
              only tests. Kept as subtle gray controls. */}
          <div className="flex items-center gap-3 flex-wrap pt-3 border-t border-slate-100">
            <button
              onClick={onApply}
              disabled={applying || running}
              className="text-sm font-medium text-white bg-slate-800 px-4 py-1.5 rounded-lg hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {applying ? 'Applying…' : 'Apply to portfolio'}
            </button>
            <button
              onClick={() => onChange(structuredClone(defaults))}
              disabled={applying}
              title="Reset the editor to the built-in default parameters (draft only — click Apply to persist)"
              className="text-sm text-slate-600 px-4 py-1.5 rounded-lg border border-slate-200 bg-slate-50 hover:bg-slate-100 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Reset to defaults
            </button>
            <span className="text-xs text-slate-400">
              Applying recomputes the portfolio &amp; detail scores immediately (no re-track).
            </span>
            {applyMsg && <span className="basis-full text-xs text-slate-600">{applyMsg}</span>}
          </div>
        </div>
      )}
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
// The trajectory holds ~40 quarterly snapshots (~10 years), but the underlying
// data is annual 10-Ks — several consecutive snapshots score against the same
// fiscal year. Deduping on period_end yields one column per available fiscal
// year, which is the view an analyst wants: each metric tracked across years.

// Row definitions for the metrics-by-year table. `fmt` reuses the exact same
// formatters as the issuer detail page so values read identically everywhere.
const METRICS: { key: string; label: string; fmt: (v: number | null) => string }[] = [
  { key: 'leverage',               label: 'Leverage (net debt / EBITDA)', fmt: v => fmtRatio(v) },
  { key: 'interest_coverage',      label: 'Interest Coverage',            fmt: v => fmtRatio(v) },
  { key: 'ebitda_margin',          label: 'EBITDA Margin',                fmt: v => fmtPct(v) },
  { key: 'fcf_margin',             label: 'FCF Margin',                   fmt: v => fmtPct(v) },
  { key: 'free_cash_flow',         label: 'Free Cash Flow',               fmt: v => fmtFCF(v) },
  { key: 'liquidity',              label: 'Liquidity (cash / ST debt)',   fmt: v => fmtRatio(v) },
  { key: 'cash_flow_to_debt',      label: 'Cash Flow / Debt (FFO proxy)', fmt: v => fmtPct(v) },
  { key: 'current_ratio',          label: 'Current Ratio',                fmt: v => fmtRatio(v) },
  { key: 'debt_to_assets',         label: 'Debt / Assets',                fmt: v => fmtPct(v) },
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
