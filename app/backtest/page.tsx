'use client'

import { useEffect, useRef, useState } from 'react'
import { BacktestStatus, BacktestCase, startBacktest, fetchBacktestStatus } from '@/lib/api'

export default function BacktestPage() {
  const [status, setStatus] = useState<BacktestStatus>({ running: false, result: null, error: null })
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    fetchBacktestStatus().then(setStatus).catch(() => {})
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  useEffect(() => {
    if (status.running) {
      pollRef.current = setInterval(async () => {
        const s = await fetchBacktestStatus().catch(() => null)
        if (s) { setStatus(s); if (!s.running && pollRef.current) clearInterval(pollRef.current) }
      }, 3000)
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [status.running])

  async function handleRun() {
    setStarting(true); setError('')
    try {
      await startBacktest()
      setStatus(await fetchBacktestStatus())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to start backtest')
    } finally {
      setStarting(false)
    }
  }

  const { result, error: runError } = status
  const summary = result?.summary

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Backtest</h1>
        <p className="text-slate-500 mt-1 text-sm">
          Replay known distressed issuers point-in-time — no look-ahead from later restatements.
        </p>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm flex items-center gap-4 flex-wrap">
        <button onClick={handleRun} disabled={status.running || starting}
          className="bg-slate-800 text-white px-6 py-2 rounded-lg text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
          {status.running ? 'Running…' : 'Run Backtest'}
        </button>
        {status.running && (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <span className="w-3 h-3 rounded-full bg-orange-400 animate-pulse inline-block" />
            Fetching EDGAR data for each case — may take 1–2 minutes on first run.
          </div>
        )}
        {(error || runError) && (
          <p className="text-sm text-red-600">{error || `Error: ${runError}`}</p>
        )}
      </div>

      {summary && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <StatCard label="Catch Rate" value={`${summary.catch_rate.toFixed(0)}%`}
            sub={`${summary.caught} of ${summary.total_distressed} flagged`} good={summary.catch_rate >= 70} />
          <StatCard label="Median Lead" value={`${summary.median_lead_months.toFixed(0)} mo`}
            sub="before event" good={summary.median_lead_months >= 3} />
          <StatCard label="False Positive Rate" value={`${summary.fp_rate.toFixed(1)}%`}
            sub="healthy controls stressed" good={summary.fp_rate <= 10} />
          <StatCard label="Stress Threshold" value="50" sub="score to flag" />
        </div>
      )}

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
                {result.cases.map((c, i) => <CaseRow key={i} c={c} />)}
              </tbody>
            </table>
          </div>
        </div>
      )}

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

function StatCard({ label, value, sub, good }: { label: string; value: string; sub?: string; good?: boolean }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
      <p className="text-xs text-slate-400 uppercase tracking-wide font-medium">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${good === true ? 'text-green-600' : good === false ? 'text-red-600' : 'text-slate-800'}`}>
        {value}
      </p>
      {sub && <p className="text-xs text-slate-400 mt-1">{sub}</p>}
    </div>
  )
}

function CaseRow({ c }: { c: BacktestCase }) {
  if (c.error) return (
    <tr className="bg-red-50">
      <td className="px-6 py-3 font-mono font-bold text-slate-700">{c.ticker}</td>
      <td className="px-4 py-3 capitalize text-slate-500">{c.label}</td>
      <td className="px-4 py-3 text-slate-400">—</td>
      <td className="px-4 py-3 text-center"><span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full">Error</span></td>
      <td className="px-6 py-3 text-right text-xs text-red-500">{c.error}</td>
    </tr>
  )

  if (c.label === 'distressed') {
    const caught = c.caught ?? false
    return (
      <tr className={caught ? '' : 'bg-red-50'}>
        <td className="px-6 py-3 font-mono font-bold text-slate-700">{c.ticker}</td>
        <td className="px-4 py-3"><span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-medium">Distressed</span></td>
        <td className="px-4 py-3 font-mono text-xs text-slate-500">{c.event_date ?? '—'}</td>
        <td className="px-4 py-3 text-center">
          {caught
            ? <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">✓ Caught</span>
            : <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-medium">✗ Missed</span>}
        </td>
        <td className="px-6 py-3 text-right font-mono text-sm text-slate-700">
          {caught ? `${c.lead_months?.toFixed(0)} mo early` : '—'}
        </td>
      </tr>
    )
  }

  return (
    <tr>
      <td className="px-6 py-3 font-mono font-bold text-slate-700">{c.ticker}</td>
      <td className="px-4 py-3"><span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">Healthy</span></td>
      <td className="px-4 py-3 text-slate-400 text-xs">Control</td>
      <td className="px-4 py-3 text-center">
        {(c.fp_count ?? 0) === 0
          ? <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full font-medium">✓ Clean</span>
          : <span className="text-xs bg-orange-100 text-orange-700 px-2 py-1 rounded-full font-medium">⚠ FP</span>}
      </td>
      <td className="px-6 py-3 text-right font-mono text-sm text-slate-700">{c.fp_count} FP periods</td>
    </tr>
  )
}
