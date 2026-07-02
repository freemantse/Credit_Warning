'use client'
// ─────────────────────────────────────────────────────────────────────────────
// app/backtest/page.tsx — Backtest Page (route "/backtest")
//
// ONE comprehensive rating-EVENT backtest: does the trained migration model flag
// each issuer's upgrade / downgrade / default EARLY, point-in-time?
//
//   • For every case (issuer + event_type + event_date) the model is replayed
//     quarterly backward from the event, each snapshot scored by a VINTAGE trained
//     strictly before that date (no look-ahead — see src/model/train.train_vintages).
//   • The head matching the event fires (downgrade→P(downgrade), upgrade→P(upgrade),
//     default→P(default)); P ≥ threshold is a flag. The earliest flag before the
//     event is the catch; months from it to the event is the lead time.
//   • Healthy controls have no event — any flag is a false positive.
//
// The run is a background task: POST /api/migration/backtest starts it, then the
// page polls GET /api/migration/backtest/status every 3 s until it finishes. The
// read-only scorecard (/api/migration/scorecard) is still loaded alongside for the
// active-model provenance and the "model trained?" gate — but the walk-forward
// accuracy breakdown (PR-AUC vs. a logistic baseline) is CLI-only: it prints from
// `python -m src.model.evaluate` for modelers, not on this page.
//
// Inert until the model is trained: run `python -m scripts.seed_demo` (ingests
// agency ratings → builds labels → trains the model + walk-forward vintages).
// ─────────────────────────────────────────────────────────────────────────────

import { Fragment, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  LineChart, Line, YAxis, Tooltip, ReferenceLine,
} from 'recharts'
import {
  MigrationBacktestStatus, MigrationBacktest, MigrationCaseResult,
  CaseLibrary, BacktestCaseInfo, AddCasePayload,
  startMigrationBacktest, fetchMigrationBacktestStatus, fetchMigrationScorecard,
  fetchBacktestCases, addCase, deleteCase,
  fmtPct, fmtRatio, fmtFCF,
} from '@/lib/api'

// ── InfoTip ───────────────────────────────────────────────────────────────────
// A small ⓘ that explains a metric on hover. Uses the native `title` tooltip the
// rest of the app relies on (no popover lib), so it works everywhere with no deps.
function InfoTip({ text }: { text: string }) {
  return (
    <span
      title={text}
      className="ml-1 inline-flex items-center justify-center w-3.5 h-3.5 rounded-full border border-slate-300 text-slate-400 text-[9px] font-bold leading-none cursor-help align-middle select-none"
      aria-label={text}
    >i</span>
  )
}

export default function BacktestPage() {

  // ── State ───────────────────────────────────────────────────────────────────
  const [status, setStatus] = useState<MigrationBacktestStatus>({
    running: false, result: null, error: null,
  })
  // Read-only walk-forward scorecard (PR-AUC per head) + active-model provenance.
  const [scorecard, setScorecard] = useState<MigrationBacktest | null>(null)

  // True only between clicking "Run" and the first status response (anti double-click).
  const [starting, setStarting] = useState(false)

  // History depth: how many point-in-time snapshots per case to walk back. Capped at 8
  // — the ~2-year (max-lead) window holds about that many quarterly snapshots, and both
  // event cases and controls are scored over the SAME window, so catch and false-positive
  // rates stay comparable. More snapshots just add flag chances (inflating both).
  const [steps, setSteps] = useState(8)

  // UI-level error (e.g. 409 already running), separate from a server task error.
  const [error, setError] = useState('')

  // Case library (which companies the backtest evaluates).
  const [library, setLibrary] = useState<CaseLibrary | null>(null)

  // Poll interval id held in a ref so clearing it never triggers a re-render loop.
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)


  // ── Effects ─────────────────────────────────────────────────────────────────

  // On mount: restore any in-flight run, load the read-only scorecard + the roster.
  useEffect(() => {
    fetchMigrationBacktestStatus().then(setStatus).catch(() => {})
    fetchMigrationScorecard().then(setScorecard).catch(() => {})
    fetchBacktestCases().then(setLibrary).catch(() => {})
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  /** Re-fetch the case library after an add/delete so the list reflects the change. */
  const reloadLibrary = () => fetchBacktestCases().then(setLibrary).catch(() => {})

  // Poll every 3 s while a run is active; stop the moment it finishes.
  useEffect(() => {
    if (status.running) {
      pollRef.current = setInterval(async () => {
        const s = await fetchMigrationBacktestStatus().catch(() => null)
        if (s) {
          setStatus(s)
          if (!s.running && pollRef.current) clearInterval(pollRef.current)
        }
      }, 3000)
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [status.running])


  // ── Event handlers ──────────────────────────────────────────────────────────

  async function handleRun() {
    setStarting(true)
    setError('')
    try {
      await startMigrationBacktest(steps)
      // Fetch status immediately so "Running…" shows without waiting for the first poll.
      setStatus(await fetchMigrationBacktestStatus())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to start backtest')
    } finally {
      setStarting(false)
    }
  }

  const result = status.result
  const runError = status.error
  const byType = result?.by_event_type
  const threshold = result?.threshold ?? 0.5
  const thresholds = result?.thresholds ?? {}
  const maxLead = result?.max_lead_months ?? 24
  const agg = scorecard?.migration?.aggregate
  const model = scorecard?.model

  // The flag cutoff for an event-type's head, as a "≥ NN%" string (UI heads:
  // downgrade/upgrade map 1:1; "default" is scored by the model's distress head).
  // These are the backend-tuned thresholds (data/migration_eval.json) the run scored at.
  const headKey = (et: string) => (et === 'default' ? 'distress' : et)
  const thrPct = (et: string) => {
    const v = thresholds[headKey(et)] ?? threshold
    return `${(v * 100).toFixed(0)}%`
  }

  // Overall median lead across all caught events (one headline number).
  const allLeads = (result?.cases ?? [])
    .filter(c => c.caught && c.lead_months != null)
    .map(c => c.lead_months as number)
  const medianLead = allLeads.length
    ? [...allLeads].sort((a, b) => a - b)[Math.floor((allLeads.length - 1) / 2)]
    : null


  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-8">

      {/* ── Page header ── */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Backtest</h1>
        <p className="text-slate-500 mt-1 text-sm">
          Replay each rating event point-in-time — does the model flag upgrades, downgrades,
          and defaults early, with no look-ahead?
        </p>
      </div>

      {/* ── Methodology ── */}
      <div className="bg-slate-50 rounded-xl border border-slate-200 p-5 text-sm text-slate-600 space-y-2">
        <p className="font-medium text-slate-700">How the backtest works</p>
        <ul className="list-disc ml-4 space-y-1 text-xs">
          <li>Each case is an issuer with a known rating event — an <strong>upgrade</strong> (e.g. a BBB issuer rising), a <strong>downgrade</strong> (e.g. an A/AA/AAA issuer slipping), or a <strong>default</strong> — on a specific date. Healthy controls have no event.</li>
          <li>The model is replayed backward from the event, but only over the <strong>{maxLead} months before it</strong> — a flag years out isn&apos;t a useful early warning. Each snapshot is scored by a <strong>vintage trained strictly before that date</strong>, so the model never sees the future.</li>
          <li>A flag is the event&apos;s head probability clearing its <strong>tuned cutoff</strong> — P(upgrade) ≥ {thrPct('upgrade')}, P(downgrade) ≥ {thrPct('downgrade')}, or P(default) ≥ {thrPct('default')}. <em>P(default)</em> reads the model&apos;s <strong>distress</strong> head — a default is a transition into the CCC+/default tail within 12 months. Cutoffs are tuned per head (calibrated probabilities sit well below 50%). <strong>Lead time</strong> = months from the first flag to the event; ≥ 6 months is an early warning.</li>
          <li>A control that gets flagged is a <strong>false positive</strong>. A case with no usable point-in-time data (or no vintage that predates it) is a <em>data gap</em>, not a miss.</li>
        </ul>
      </div>

      {/* ── Run control ── */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm flex items-center gap-4 flex-wrap">
        <button
          onClick={handleRun}
          disabled={status.running || starting}
          className="bg-slate-800 text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {status.running ? 'Running…' : 'Run Backtest'}
        </button>

        {/* History depth: how many annual snapshots per case to walk back. */}
        <div className="flex flex-col gap-1">
          <label htmlFor="history-depth" className="text-xs font-medium text-slate-500 uppercase tracking-wide">
            History depth
          </label>
          <div className="flex items-center gap-3">
            <input
              id="history-depth"
              type="range"
              min={4}
              max={8}
              step={1}
              value={steps}
              onChange={e => setSteps(Number(e.target.value))}
              disabled={status.running || starting}
              className="w-40 accent-slate-700 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            />
            <span className="text-xs text-slate-400 tabular-nums whitespace-nowrap">
              up to {steps} snapshots/case · same {maxLead}-mo window for events & controls
            </span>
          </div>
        </div>

        {status.running && (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <span className="w-3 h-3 rounded-full bg-orange-400 animate-pulse inline-block" />
            Replaying the model over each case — may take a minute.
          </div>
        )}

        {(error || runError) && (
          <p className="text-sm text-red-600">{error || `Error: ${runError}`}</p>
        )}

        {/* The model-not-trained note (server-provided) or a hint to train it. */}
        {result?.note
          ? <p className="basis-full text-xs text-amber-600">{result.note}</p>
          : !agg && (
            <p className="basis-full text-xs text-slate-400">
              No trained model yet — run <code className="bg-slate-100 px-1 rounded">python -m scripts.seed_demo</code> to
              ingest agency ratings, build labels, and train the model + walk-forward vintages.
            </p>
          )}
        {model?.version && (
          <p className="basis-full text-xs text-slate-400">Active model <span className="font-mono">{model.version}</span></p>
        )}
      </div>

      {/* ── Case library ── */}
      {library && <CaseLibraryCard library={library} onChange={reloadLibrary} />}

      {/* ── Catch rate / lead time / false-positive (at the backend-tuned cutoffs) ── */}
      {byType && Object.keys(byType).length > 0 && (
        <div className="space-y-3">
          <div>
            <h2 className="font-semibold text-slate-800">How early the model catches real rating events</h2>
            <p className="text-xs text-slate-400">
              Out-of-time: the share of real events flagged <em>before</em> they happened, the typical
              months of lead time, and how often healthy control issuers were wrongly flagged — at the
              backend-tuned per-head cutoffs.
            </p>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
            {(['downgrade', 'upgrade', 'default'] as const).map(et => {
              const s = byType[et]
              if (!s) return null
              const label = et[0].toUpperCase() + et.slice(1)
              const rate = s.catch_rate ?? 0
              return (
                <StatCard
                  key={et}
                  label={`${label} caught`}
                  value={`${rate.toFixed(0)}%`}
                  sub={`${s.caught ?? 0} of ${s.total} · ${(s.median_lead_months ?? 0).toFixed(0)} mo lead · flags ≥ ${thrPct(et)}`}
                  good={rate >= 70 ? true : rate >= 40 ? undefined : false}
                  tip={`Share of ${et} cases flagged before the event (flag = P(${et === 'default' ? 'distress' : et}) ≥ ${thrPct(et)}, the backend-tuned cutoff). Distress/default is the hardest, rarest head.`}
                />
              )
            })}
            {medianLead != null && (
              <StatCard
                label="Median Lead"
                value={`${medianLead.toFixed(0)} mo`}
                sub="across all caught events"
                good={medianLead >= 3}
                tip={`Typical months between the first flag and the event. Capped at ${maxLead} months — earlier flags don't count as catches.`}
              />
            )}
            {byType.control && (
              <StatCard
                label="Control FP Rate"
                value={`${(byType.control.fp_rate ?? 0).toFixed(0)}%`}
                sub={`${byType.control.false_positive ?? 0} of ${byType.control.total} controls flagged`}
                good={(byType.control.fp_rate ?? 0) <= 25 ? true : (byType.control.fp_rate ?? 0) <= 45 ? undefined : false}
                tip="Share of healthy control issuers wrongly flagged (scored against the downgrade head). For rating-migration this is a DIAL, not a bug: ~25–40% is the honest, expected level at a useful catch rate. Raise the downgrade cutoff (backend) to lower it — at the cost of catch."
              />
            )}
          </div>

          <p className="text-xs text-slate-500 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2 leading-relaxed">
            <strong>Catch rate and false positives trade off.</strong> Catching more real
            downgrades/upgrades means flagging at a lower probability cutoff, which also flags more
            healthy issuers (higher false-positive rate). No single setting makes both perfect; an
            early-warning tool leans toward catching more and accepts some false alarms. A ~25–40%
            control false-positive rate at a strong catch rate is normal for this problem. Tune the
            per-head cutoffs in the backend and re-run to move along this trade-off.
          </p>
        </div>
      )}

      {/* Walk-forward accuracy (PR-AUC per head, model vs. logistic baseline) is a
          modeler-facing diagnostic — it lives on the CLI only now, printed by
          `python -m src.model.evaluate`. The scorecard is still fetched above for
          the active-model provenance and the "model trained?" gate. */}

      {/* ── Per-case results ── */}
      {result && result.cases.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <div className="px-6 py-4 border-b border-gray-100 flex items-baseline justify-between flex-wrap gap-2">
            <h2 className="font-semibold text-slate-800">Case Results</h2>
            <p className="text-xs text-slate-400">Click a row to see the model&apos;s probability at each snapshot</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 text-xs font-medium text-slate-500 uppercase tracking-wide">
                  <th className="px-6 py-3 text-left">Case</th>
                  <th className="px-4 py-3 text-left">Event</th>
                  <th className="px-4 py-3 text-left" title="Date of the rating event the model must flag ahead of time. Controls have none.">Event Date</th>
                  <th className="px-4 py-3 text-center">Result</th>
                  <th className="px-4 py-3 text-center" title="The model's event probability at each snapshot (oldest left → event right). The dashed line is this head's flag cutoff.">P(event) Trajectory</th>
                  <th className="px-6 py-3 text-right" title="Months from the first flag to the event (the early warning). Capped at the lead window.">Lead</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {result.cases.map((c, i) => (
                  <MigrationCaseRow key={c.case_id ?? i} c={c} threshold={threshold} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!result && !status.running && (
        <p className="text-sm text-slate-400">
          Click <strong>Run Backtest</strong> to replay the trained model over the case library.
        </p>
      )}

    </div>
  )
}


// ── StatCard component ────────────────────────────────────────────────────────
// Small KPI card. `good` drives the value colour: green (healthy), red (concerning),
// or default slate (no judgment).

function StatCard({
  label, value, sub, good, tip,
}: {
  label: string
  value: string
  sub?: string
  good?: boolean
  tip?: string
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
      <p className="text-xs text-slate-400 uppercase tracking-wide font-medium">
        {label}{tip && <InfoTip text={tip} />}
      </p>
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


// ── Event-type styling ────────────────────────────────────────────────────────

// `glyph` is the compact roster marker (avoids the Downgrade/Default "D" collision):
// ↓ downgrade, ↑ upgrade, ✕ default/distress, • control. `label` is the full word for
// the wider results-table and modal badges.
function eventBadge(eventType: string): { label: string; glyph: string; cls: string } {
  switch (eventType) {
    case 'downgrade': return { label: 'Downgrade', glyph: '↓', cls: 'bg-red-100 text-red-700' }
    case 'upgrade':   return { label: 'Upgrade',   glyph: '↑', cls: 'bg-green-100 text-green-700' }
    case 'default':   return { label: 'Default',   glyph: '✕', cls: 'bg-rose-200 text-rose-800' }
    default:          return { label: 'Control',   glyph: '•', cls: 'bg-slate-100 text-slate-600' }
  }
}


// ── ProbSparkline ─────────────────────────────────────────────────────────────
// Tiny chart of the model's event-probability over time (oldest → newest / event
// on the right). Dashed line is the flag threshold, so "did P cross before the
// event?" reads at a glance. Snapshots with no model (no vintage) render as gaps.

function ProbSparkline({ c, threshold }: { c: MigrationCaseResult; threshold: number }) {
  const traj = c.trajectory ?? []
  if (traj.length === 0) return <span className="text-xs text-slate-300">—</span>

  // Trajectory arrives newest-first; plot left→right in time order.
  const data = [...traj].reverse().map(t => ({
    t: t.months_before_event != null ? `T-${t.months_before_event.toFixed(0)}` : '',
    prob: t.prob,  // null breaks the line where no vintage could score
  }))
  const isControl = c.event_type === 'control'

  return (
    <LineChart width={150} height={40} data={data}
               margin={{ top: 4, right: 4, bottom: 2, left: 4 }}>
      <YAxis domain={[0, 1]} hide />
      <ReferenceLine y={threshold} stroke="#f97316" strokeDasharray="3 3" strokeWidth={1} />
      <Line
        type="monotone"
        dataKey="prob"
        stroke={isControl ? '#16a34a' : '#475569'}
        strokeWidth={1.5}
        dot={false}
        isAnimationActive={false}
      />
      <Tooltip
        formatter={(v: number) => [`P ${(v * 100).toFixed(0)}%`, '']}
        labelFormatter={(l: string) => `${l} months`}
        separator=""
        contentStyle={{ fontSize: 11, padding: '2px 6px' }}
      />
    </LineChart>
  )
}


// ── Probability trajectory (expanded row) ─────────────────────────────────────
// The per-snapshot detail behind the sparkline: each evaluation date, how far it
// was before the event, the model's probability + whether it flagged, and — below
// that — the point-in-time stress score and ratios the model saw at each snapshot.

// Stress-score threshold: a score at/above this is "Stressed" — flagged red.
// Mirrors scoreLabel()'s cut-point in lib/api.ts (and src/score.py's DEFAULT_CONFIG).
const STRESS_THRESHOLD = 50

// The ratio rows shown under the probability, mirroring the issuer detail page's
// TREND_METRICS: each ratio's feature key, label, and formatter.
const TRAJ_RATIOS: { key: string; label: string; fmt: (v: number) => string }[] = [
  { key: 'ebitda_margin',     label: 'EBITDA Margin',     fmt: fmtPct },
  { key: 'leverage',          label: 'Leverage',          fmt: fmtRatio },
  { key: 'interest_coverage', label: 'Interest Coverage', fmt: fmtRatio },
  { key: 'free_cash_flow',    label: 'FCF',               fmt: fmtFCF },
  { key: 'fcf_margin',        label: 'FCF Margin',        fmt: fmtPct },
  { key: 'liquidity',         label: 'Liquidity',         fmt: fmtRatio },
  { key: 'cash_flow_to_debt', label: 'Cash Flow / Debt',  fmt: fmtPct },
  { key: 'debt_to_assets',    label: 'Debt / Assets',     fmt: fmtPct },
]

function ProbHistory({ c }: { c: MigrationCaseResult }) {
  const traj = [...(c.trajectory ?? [])].reverse()  // oldest → newest
  if (traj.length === 0) {
    return <p className="text-xs text-slate-400 px-6 py-4">No point-in-time snapshots were scorable for this case.</p>
  }
  return (
    <div className="px-6 py-4 overflow-x-auto">
      <p className="text-xs font-medium text-slate-500 mb-2">
        Model probability, real agency rating, stress score &amp; ratios at each snapshot{c.event_date ? ` (event: ${c.event_date})` : ''}
      </p>
      <table className="text-xs">
        <thead>
          <tr className="text-slate-400">
            <th className="text-left pr-6 py-1 font-medium">Snapshot</th>
            {traj.map((t, i) => (
              <th key={i} className="text-right px-3 py-1 font-mono font-medium">
                {t.months_before_event != null ? `T-${t.months_before_event.toFixed(0)}` : '—'}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr className="border-t border-slate-100">
            <td className="pr-6 py-1.5 text-slate-500">Date</td>
            {traj.map((t, i) => (
              <td key={i} className="text-right px-3 py-1.5 font-mono text-slate-500">{t.eval_date.slice(0, 7)}</td>
            ))}
          </tr>
          {/* Real agency rating in effect at each snapshot (point-in-time, forward-filled). */}
          <tr className="border-t border-slate-100">
            <td className="pr-6 py-1.5 text-slate-500">Rating</td>
            {traj.map((t, i) => (
              <td key={i} className="text-right px-3 py-1.5 font-mono text-slate-600">{t.rating ?? '—'}</td>
            ))}
          </tr>
          <tr className="border-t border-slate-100">
            <td className="pr-6 py-1.5 font-medium text-slate-600">P(event)</td>
            {traj.map((t, i) => (
              <td key={i}
                  className={`text-right px-3 py-1.5 font-mono font-bold ${t.flagged ? 'text-red-600' : 'text-slate-700'}`}>
                {t.prob != null ? `${(t.prob * 100).toFixed(0)}%` : '—'}
              </td>
            ))}
          </tr>
          {/* Stress score — red when at/above the stress threshold (Stressed). */}
          <tr className="border-t border-slate-200">
            <td className="pr-6 py-1.5 font-medium text-slate-600">Score</td>
            {traj.map((t, i) => {
              const stressed = t.score != null && t.score >= STRESS_THRESHOLD
              return (
                <td key={i}
                    className={`text-right px-3 py-1.5 font-mono font-bold ${stressed ? 'text-red-600' : 'text-slate-700'}`}>
                  {t.score != null ? Math.round(t.score) : '—'}
                </td>
              )
            })}
          </tr>
          {/* Ratio levels the model saw at each snapshot. */}
          {TRAJ_RATIOS.map(({ key, label, fmt }) => (
            <tr key={key} className="border-t border-slate-100">
              <td className="pr-6 py-1.5 text-slate-500">{label}</td>
              {traj.map((t, i) => {
                const v = t.ratios?.[key]
                return (
                  <td key={i} className="text-right px-3 py-1.5 font-mono text-slate-600">
                    {v != null ? fmt(v) : '—'}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}


// ── MigrationCaseRow ──────────────────────────────────────────────────────────
// One row per case. Layout varies by outcome: error / event (caught/missed/data
// gap) / control (clean/false-positive). Click to expand the probability history.

function MigrationCaseRow({ c, threshold }: { c: MigrationCaseResult; threshold: number }) {
  const [expanded, setExpanded] = useState(false)
  const expandable = !c.error && (c.trajectory?.length ?? 0) > 0
  const isControl = c.event_type === 'control'
  const eb = eventBadge(c.event_type)

  const nameCell = (
    <td className="px-6 py-3 whitespace-nowrap">
      {expandable && (
        <span className="inline-block w-4 text-slate-400 text-xs">{expanded ? '▾' : '▸'}</span>
      )}
      <span className="font-mono font-bold text-slate-700">{c.ticker}</span>
      {c.company_name && <span className="block text-xs text-slate-400 pl-4">{c.company_name}</span>}
    </td>
  )

  const detailRow = expanded && expandable && (
    <tr className="bg-slate-50">
      <td colSpan={6} className="border-t border-slate-100"><ProbHistory c={c} /></td>
    </tr>
  )

  const rowProps = {
    onClick: () => expandable && setExpanded(e => !e),
    className: expandable ? 'cursor-pointer hover:bg-slate-50' : '',
  }

  // Error row.
  if (c.error) return (
    <tr className="bg-red-50">
      {nameCell}
      <td className="px-4 py-3"><span className={`text-xs px-2 py-1 rounded-full font-medium ${eb.cls}`}>{eb.label}</span></td>
      <td className="px-4 py-3 text-slate-400">—</td>
      <td className="px-4 py-3 text-center"><span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full">Error</span></td>
      <td className="px-4 py-3" />
      <td className="px-6 py-3 text-right text-xs text-red-500">{c.error}</td>
    </tr>
  )

  // Result badge.
  const dataGap = c.status === 'data_gap'
  let resultBadge
  if (dataGap) {
    resultBadge = <span className="text-xs bg-amber-100 text-amber-700 px-2 py-1 rounded-full font-medium">No data</span>
  } else if (isControl) {
    resultBadge = (c.fp_count ?? 0) === 0
      ? <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">✓ Clean</span>
      : <span className="text-xs bg-orange-100 text-orange-700 px-2 py-1 rounded-full font-medium">⚠ FP</span>
  } else if (c.caught) {
    resultBadge = <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">✓ Caught{c.early_warning ? ' early' : ''}</span>
  } else {
    resultBadge = <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-medium">✗ Missed</span>
  }

  // Row tint: green-clean controls and caught events stay white; misses red; gaps amber.
  const tint = dataGap ? 'bg-amber-50'
    : (!isControl && !c.caught) ? 'bg-red-50'
    : (isControl && (c.fp_count ?? 0) > 0) ? 'bg-orange-50'
    : ''

  return (
    <Fragment>
      <tr {...rowProps} className={`${rowProps.className} ${tint}`}>
        {nameCell}
        <td className="px-4 py-3"><span className={`text-xs px-2 py-1 rounded-full font-medium ${eb.cls}`}>{eb.label}</span></td>
        <td className="px-4 py-3 font-mono text-xs text-slate-500">{c.event_date ?? '—'}</td>
        <td className="px-4 py-3 text-center">{resultBadge}</td>
        <td className="px-4 py-3"><ProbSparkline c={c} threshold={c.flag_threshold ?? threshold} /></td>
        <td className="px-6 py-3 text-right font-mono text-sm text-slate-700">
          {isControl
            ? `${c.fp_count ?? 0} FP`
            : c.caught
            ? `${c.lead_months?.toFixed(1)} mo early`
            : '—'}
        </td>
      </tr>
      {detailRow}
    </Fragment>
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
  const [eventType, setEventType] = useState<'downgrade' | 'upgrade' | 'default' | 'control'>('downgrade')
  const [eventDate, setEventDate] = useState('')
  const [notes, setNotes] = useState('')
  const [adding, setAdding] = useState(false)
  const [formError, setFormError] = useState('')

  // Custom delete-confirmation modal state.
  const [pendingDelete, setPendingDelete] = useState<BacktestCaseInfo | null>(null)
  const [deleting, setDeleting] = useState(false)

  async function handleAdd(e: FormEvent) {
    e.preventDefault()
    setAdding(true)
    setFormError('')
    try {
      const payload: AddCasePayload = { identifier: identifier.trim(), event_type: eventType }
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

  async function confirmDelete() {
    const c = pendingDelete
    if (!c) return
    setDeleting(true)
    setFormError('')
    try {
      await deleteCase(c.case_id)
      setPendingDelete(null)
      onChange()
    } catch (err: unknown) {
      setPendingDelete(null)
      setFormError(err instanceof Error ? err.message : 'Failed to delete case')
    } finally {
      setDeleting(false)
    }
  }

  // Roster breakdown by event type (counted client-side from the case list) so the
  // header shows the actual mix rather than the coarse distressed/healthy buckets.
  const counts = library.cases.reduce((acc, c) => {
    const et = c.event_type || (c.label === 'healthy' ? 'control' : 'downgrade')
    acc[et] = (acc[et] ?? 0) + 1
    return acc
  }, {} as Record<string, number>)

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
          {library.total} companies — {counts.upgrade ?? 0} upgrades, {counts.downgrade ?? 0} downgrades, {counts.default ?? 0} defaults, {counts.control ?? 0} controls
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
              <label className="text-xs font-medium text-slate-500">Event type</label>
              <select
                value={eventType}
                onChange={e => setEventType(e.target.value as 'downgrade' | 'upgrade' | 'default' | 'control')}
                className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-slate-300"
              >
                <option value="downgrade">Downgrade</option>
                <option value="upgrade">Upgrade</option>
                <option value="default">Default</option>
                <option value="control">Control (no event)</option>
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-slate-500">
                {eventType === 'control' ? 'Anchor date (optional)' : 'Event date (required)'}
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
                placeholder="e.g. fallen angel; COVID demand collapse"
                className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm w-full focus:outline-none focus:ring-2 focus:ring-slate-300"
              />
            </div>
            <button
              type="submit"
              disabled={adding || !identifier.trim() || (eventType !== 'control' && !eventDate)}
              className="bg-slate-800 text-white px-4 py-1.5 rounded-lg text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {adding ? 'Adding…' : 'Add case'}
            </button>
          </form>
          {formError && <p className="text-sm text-red-600">{formError}</p>}

          {/* The roster, with a delete control per row. */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-2">
            {library.cases.map(c => {
              const eb = eventBadge(c.event_type || (c.label === 'healthy' ? 'control' : 'downgrade'))
              return (
                <div key={c.case_id || c.ticker} className="flex items-center gap-2 text-sm py-1 group">
                  <span className={`inline-flex items-center justify-center w-4 h-4 text-[10px] rounded-full font-medium shrink-0 ${eb.cls}`} title={eb.label}>
                    {eb.glyph}
                  </span>
                  <span className="font-mono font-bold text-slate-700 shrink-0">{c.ticker}</span>
                  <span className="text-slate-500 truncate">{c.company_name}</span>
                  {c.event_type !== 'control' && c.event_date && (
                    <span className="ml-auto font-mono text-xs text-slate-400 shrink-0" title="Rating-event date">
                      {c.event_date}
                    </span>
                  )}
                  <button
                    onClick={() => { setFormError(''); setPendingDelete(c) }}
                    title="Remove case"
                    className={`shrink-0 text-slate-300 hover:text-red-600 transition-colors px-1 leading-none ${
                      c.event_type !== 'control' && c.event_date ? '' : 'ml-auto'
                    }`}
                  >
                    ×
                  </button>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* ── Remove-case confirmation modal ── */}
      {pendingDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => { if (!deleting) setPendingDelete(null) }}
        >
          <div
            className="bg-white rounded-xl shadow-xl border border-gray-200 w-full max-w-md p-6"
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
              <span className={`px-2 py-0.5 rounded-full font-medium ${eventBadge(pendingDelete.event_type || 'control').cls}`}>
                {eventBadge(pendingDelete.event_type || (pendingDelete.label === 'healthy' ? 'control' : 'downgrade')).label}
              </span>
              {pendingDelete.event_date && <span className="font-mono">{pendingDelete.event_date}</span>}
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
