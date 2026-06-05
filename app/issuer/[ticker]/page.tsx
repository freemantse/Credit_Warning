'use client'

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
  const { ticker } = useParams<{ ticker: string }>()
  const [data, setData] = useState<IssuerDetail | null>(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [openAudit, setOpenAudit] = useState<string | null>(null)

  useEffect(() => { load() }, [ticker])

  async function load() {
    setError('')
    try { setData(await fetchIssuer(ticker)) }
    catch (e: unknown) { setError(e instanceof Error ? e.message : 'Failed to load') }
  }

  async function handleRefresh() {
    setRefreshing(true); setError('')
    try { await trackIssuer(ticker); await load() }
    catch (e: unknown) { setError(e instanceof Error ? e.message : 'Refresh failed') }
    finally { setRefreshing(false) }
  }

  const chartData = data
    ? [...data.periods].reverse().map(p => ({ date: p.period_end, score: p.score }))
    : []

  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between">
        <div>
          <Link href="/" className="text-sm text-slate-400 hover:text-slate-600 mb-2 inline-block">← Portfolio</Link>
          <h1 className="text-2xl font-bold font-mono text-slate-900">{ticker}</h1>
          {data && <p className="text-sm text-slate-400 mt-1">{data.periods.length} annual periods tracked</p>}
        </div>
        <button onClick={handleRefresh} disabled={refreshing}
          className="mt-6 text-sm bg-slate-100 hover:bg-slate-200 text-slate-700 px-4 py-2 rounded-lg disabled:opacity-50 transition-colors">
          {refreshing ? 'Refreshing…' : 'Refresh from EDGAR'}
        </button>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-4 py-3">{error}</div>
      )}
      {!data && !error && <div className="py-16 text-center text-slate-400 text-sm">Loading…</div>}

      {data && (
        <>
          {/* Score trend chart */}
          <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm">
            <h2 className="font-semibold text-slate-800 mb-4">Stress Score Trend</h2>
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={chartData}>
                <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#94a3b8' }}
                  tickFormatter={d => d.slice(0, 7)} />
                <YAxis domain={[0, 100]} tick={{ fontSize: 11, fill: '#94a3b8' }} width={32} />
                <Tooltip formatter={(v: number) => [`${v}`, 'Score']}
                  labelFormatter={l => `Period: ${l}`} contentStyle={{ fontSize: 12 }} />
                <ReferenceLine y={50} stroke="#f97316" strokeDasharray="4 2"
                  label={{ value: 'Stress threshold', position: 'right', fontSize: 10, fill: '#f97316' }} />
                <Line type="monotone" dataKey="score" stroke="#1e293b" strokeWidth={2}
                  dot={{ r: 4, fill: '#1e293b' }} activeDot={{ r: 6 }} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Ratio history table */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-100">
              <h2 className="font-semibold text-slate-800">Ratio History</h2>
              <p className="text-xs text-slate-400 mt-0.5">Click a row to see source audit (XBRL tags + raw inputs).</p>
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
                      <tr key={p.period_end}
                        onClick={() => setOpenAudit(openAudit === p.period_end ? null : p.period_end)}
                        className="hover:bg-gray-50 cursor-pointer transition-colors">
                        <td className="px-6 py-3 font-mono text-slate-700 text-xs">{p.period_end}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.leverage?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.interest_coverage?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtFCF(p.ratios.free_cash_flow?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtPct(p.ratios.fcf_margin?.value)}</td>
                        <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtRatio(p.ratios.liquidity?.value)}</td>
                        <td className="px-4 py-3 text-center font-mono font-bold text-slate-800">{Math.round(p.score)}</td>
                        <td className="px-6 py-3 text-center">
                          <span className={`inline-block text-xs font-medium px-2.5 py-1 rounded-full ${scoreBg(p.score)}`}>
                            {scoreLabel(p.score)}
                          </span>
                        </td>
                      </tr>
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

          <FindingsSection periods={data.periods} />
        </>
      )}
    </div>
  )
}

function AuditPanel({ period }: { period: PeriodData }) {
  return (
    <div className="space-y-4">
      {period.alerts.length > 0 && (
        <div>
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Triggered Alerts</p>
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
      <div>
        <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Source Audit (XBRL inputs)</p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(period.ratios).map(([name, data]) => (
            <div key={name} className="bg-white rounded-lg border border-gray-200 p-3">
              <p className="text-xs font-semibold text-slate-700 mb-2 capitalize">{name.replace(/_/g, ' ')}</p>
              {Object.entries(data.inputs).map(([field, val]) => {
                const tag = data.source_tags[field] || '?'
                const fmtVal = typeof val === 'number'
                  ? Math.abs(val) >= 1e9 ? `$${(val / 1e9).toFixed(2)}B`
                  : Math.abs(val) >= 1e6 ? `$${(val / 1e6).toFixed(0)}M`
                  : val.toFixed(2)
                  : String(val)
                return (
                  <div key={field} className="text-xs text-slate-500 mt-1">
                    <span className="text-slate-400">{field}:</span>{' '}
                    <span className="font-mono text-slate-700">{fmtVal}</span>
                    <div className="text-slate-300 text-[10px] truncate" title={tag}>{tag}</div>
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

function FindingsSection({ periods }: { periods: PeriodData[] }) {
  const all = periods.flatMap(p => p.findings.map(f => ({ ...f, period: p.period_end })))
  if (all.length === 0) return null
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-100">
        <h2 className="font-semibold text-slate-800">Qualitative Findings</h2>
        <p className="text-xs text-slate-400 mt-0.5">LLM-identified signals from MD&A and footnotes. Each includes a verbatim quote.</p>
      </div>
      <div className="divide-y divide-gray-100">
        {all.map((f, i) => (
          <div key={i} className="px-6 py-4">
            <div className="flex items-start gap-3">
              <span className={`mt-1.5 w-2 h-2 rounded-full flex-shrink-0 ${severityDot(f.severity)}`} />
              <div className="flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-sm text-slate-800">{f.concern}</span>
                  <span className="text-xs text-slate-400 font-mono">{f.period}</span>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium capitalize
                    ${f.severity === 'high' ? 'bg-red-100 text-red-700'
                      : f.severity === 'medium' ? 'bg-yellow-100 text-yellow-700'
                      : 'bg-blue-100 text-blue-700'}`}>
                    {f.severity}
                  </span>
                </div>
                <blockquote className="mt-2 text-xs text-slate-500 italic border-l-2 border-slate-200 pl-3">
                  "{f.evidence_quote}"
                </blockquote>
                <p className="mt-1 text-xs text-slate-400">{f.source}</p>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
