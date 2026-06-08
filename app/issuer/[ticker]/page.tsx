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

import { useEffect, useState } from 'react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer,
} from 'recharts'
import {
  IssuerDetail, PeriodData, Finding,
  fetchIssuer, trackIssuer,
  fmtRatio, fmtFCF, fmtPct, scoreBg, scoreLabel, severityDot,
} from '@/lib/api'

export default function IssuerPage() {

  // ── State ───────────────────────────────────────────────────────────────────
  const { ticker } = useParams<{ ticker: string }>()  // from the dynamic route

  const [data, setData] = useState<IssuerDetail | null>(null)
  const [error, setError] = useState('')

  // True while POST /api/track (re-fetch from EDGAR) is running.
  const [refreshing, setRefreshing] = useState(false)

  // The period_end string of the row currently showing the inline audit panel.
  // null = no row is expanded.
  const [openAudit, setOpenAudit] = useState<string | null>(null)


  // ── Data loading ────────────────────────────────────────────────────────────

  // Re-fetch whenever the ticker changes (handles browser back/forward navigation).
  useEffect(() => { load() }, [ticker])

  /** Fetch the issuer's full period history from the API. */
  async function load() {
    setError('')
    try {
      setData(await fetchIssuer(ticker))
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

  // ── Chart data preparation ─────────────────────────────────────────────────
  // The API returns periods newest-first, but the Recharts line chart plots
  // left-to-right chronologically — so we reverse before mapping.
  // [...data.periods] creates a shallow copy so we don't mutate the state array.
  const chartData = data
    ? [...data.periods].reverse().map(p => ({ date: p.period_end, score: p.score }))
    : []


  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-8">

      {/* ── Header: back link, ticker, period count, refresh button ── */}
      <div className="flex items-start justify-between">
        <div>
          <Link href="/" className="text-sm text-slate-400 hover:text-slate-600 mb-2 inline-block">
            ← Portfolio
          </Link>
          <h1 className="text-2xl font-bold font-mono text-slate-900">{ticker}</h1>
          {data && (
            <p className="text-sm text-slate-400 mt-1">
              {data.periods.length} annual periods tracked
            </p>
          )}
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="mt-6 text-sm bg-slate-100 hover:bg-slate-200 text-slate-700 px-4 py-2 rounded-lg disabled:opacity-50 transition-colors"
        >
          {refreshing ? 'Refreshing…' : 'Refresh from EDGAR'}
        </button>
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
            <h2 className="font-semibold text-slate-800 mb-4">Stress Score Trend</h2>
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={chartData}>
                {/* Trim dates to "YYYY-MM" to save horizontal space on the axis. */}
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 11, fill: '#94a3b8' }}
                  tickFormatter={d => d.slice(0, 7)}
                />
                <YAxis
                  domain={[0, 100]}  // fixed scale so changes are visually comparable
                  tick={{ fontSize: 11, fill: '#94a3b8' }}
                  width={32}
                />
                <Tooltip
                  formatter={(v: number) => [`${v}`, 'Score']}
                  labelFormatter={l => `Period: ${l}`}
                  contentStyle={{ fontSize: 12 }}
                />
                {/* Dashed orange reference line at the stress threshold. */}
                <ReferenceLine
                  y={50}
                  stroke="#f97316"
                  strokeDasharray="4 2"
                  label={{ value: 'Stress threshold', position: 'right', fontSize: 10, fill: '#f97316' }}
                />
                <Line
                  type="monotone"
                  dataKey="score"
                  stroke="#1e293b"
                  strokeWidth={2}
                  dot={{ r: 4, fill: '#1e293b' }}
                  activeDot={{ r: 6 }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>

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
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 text-xs font-medium text-slate-500 uppercase tracking-wide">
                    <th className="px-6 py-3 text-left">Period</th>
                    <th className="px-4 py-3 text-right">Leverage</th>
                    <th className="px-4 py-3 text-right">Coverage</th>
                    <th className="px-4 py-3 text-right">FCF</th>
                    <th className="px-4 py-3 text-right">FCF Margin</th>
                    <th className="px-4 py-3 text-right">Liquidity</th>
                    <th className="px-4 py-3 text-center">Score</th>
                    <th className="px-6 py-3 text-center">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {data.periods.map(p => (
                    <>
                      {/*
                        Data row — clicking toggles the inline audit panel.
                        openAudit holds the period_end of the currently-open row.
                        Clicking the open row again collapses it (toggles to null).
                      */}
                      <tr
                        key={p.period_end}
                        onClick={() => setOpenAudit(openAudit === p.period_end ? null : p.period_end)}
                        className="hover:bg-gray-50 cursor-pointer transition-colors"
                      >
                        <td className="px-6 py-3 font-mono text-slate-700 text-xs">{p.period_end}</td>

                        {/* Optional chaining (?.) handles periods where a ratio is missing. */}
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.leverage?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.interest_coverage?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtFCF(p.ratios.free_cash_flow?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtPct(p.ratios.fcf_margin?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.liquidity?.value)}</td>

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
                        Uses colSpan={8} to span the full table width.
                        The AuditPanel component handles the detailed display.
                      */}
                      {openAudit === p.period_end && (
                        <tr key={`${p.period_end}-audit`}>
                          <td colSpan={8} className="bg-slate-50 px-6 py-4 border-t border-slate-100">
                            <AuditPanel period={p} />
                          </td>
                        </tr>
                      )}
                    </>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

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
        <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">
          Source Audit (XBRL inputs)
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(period.ratios).map(([name, data]) => (
            <div key={name} className="bg-white rounded-lg border border-gray-200 p-3">
              {/* Ratio name: underscores replaced with spaces for readability. */}
              <p className="text-xs font-semibold text-slate-700 mb-2 capitalize">
                {name.replace(/_/g, ' ')}
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
          LLM-identified signals from MD&amp;A and footnotes. Each includes a verbatim quote.
        </p>
      </div>
      <div className="divide-y divide-gray-100">
        {all.map((f, i) => (
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

                {/* Source label (e.g. "10-K 2023-12-31, MD&A"). */}
                <p className="mt-1 text-xs text-slate-400">{f.source}</p>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
