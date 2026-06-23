'use client'
// ─────────────────────────────────────────────────────────────────────────────
// app/screen/page.tsx — Senior-Secured Bond Screen (route "/screen")
//
// The system's headline deliverable: senior-secured bond instruments of
// credit-healthy, not-deteriorating issuers, ranked best-first. It joins the
// implied rating (health), the Rating Outlook (forward direction), and the
// LLM-extracted bond instruments + seniority, notching each instrument off its
// issuer's rating.
// ─────────────────────────────────────────────────────────────────────────────

import { useEffect, useState } from 'react'
import Link from 'next/link'
import {
  ScreenRow, ScreenResponse, fetchScreen,
  ratingBg, outlookBadge, seniorityBadge, fmtFCF,
} from '@/lib/api'

// Issuer-health floors offered in the control bar (issuer must rate at least this).
const RATING_FLOORS = ['A-', 'BBB', 'BBB-', 'BB+', 'B-']

export default function ScreenPage() {
  const [data, setData] = useState<ScreenResponse | null>(null)
  const [minRating, setMinRating] = useState('BBB-')
  const [excludeNegative, setExcludeNegative] = useState(true)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => { load() }, [minRating, excludeNegative])

  async function load() {
    setLoading(true)
    setError('')
    try {
      setData(await fetchScreen(minRating, excludeNegative))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load screen')
    } finally {
      setLoading(false)
    }
  }

  const rows = data?.rows ?? []

  return (
    <div className="space-y-8">

      {/* ── Header ── */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Senior-Secured Bond Screen</h1>
        <p className="text-slate-500 mt-1 text-sm max-w-3xl">
          Senior-secured instruments of credit-healthy issuers whose rating isn&apos;t
          deteriorating — ranked by their seniority-notched rating. Health comes from the
          implied rating, direction from the Rating Outlook, and the instruments from the
          10-K debt footnote.
        </p>
      </div>

      {/* ── Controls ── */}
      <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm flex flex-wrap items-end gap-6">
        <div>
          <label className="block text-xs font-medium text-slate-500 mb-1">Minimum issuer rating</label>
          <select
            value={minRating}
            onChange={e => setMinRating(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-slate-400 bg-white"
          >
            {RATING_FLOORS.map(r => <option key={r} value={r}>{r} and better</option>)}
          </select>
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-600 pb-2">
          <input
            type="checkbox"
            checked={excludeNegative}
            onChange={e => setExcludeNegative(e.target.checked)}
            className="rounded border-gray-300"
          />
          Exclude Negative outlook
        </label>
        <button
          onClick={load}
          className="ml-auto inline-flex items-center gap-1 text-xs text-slate-400 hover:text-slate-600 transition-colors pb-2"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" />
          </svg>
          Refresh
        </button>
      </div>

      {/* ── Meta line ── */}
      {data && (
        <p className="text-xs text-slate-400">
          {data.meta.matches} senior-secured match{data.meta.matches === 1 ? '' : 'es'} across{' '}
          {data.meta.issuers_with_instruments} issuer(s) with extracted instruments. Forward filter:{' '}
          <span className="font-mono">{data.meta.downgrade_signal}</span>
          {data.meta.downgrade_signal === 'rating_outlook' && ' (calibrated downgrade probability arrives with the migration model).'}
        </p>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-4 py-3">{error}</div>
      )}

      {/* ── Results table ── */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {loading ? (
          <div className="py-16 text-center text-slate-400 text-sm">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="py-16 px-6 text-center text-slate-400 text-sm">
            No senior-secured instruments match.{' '}
            {data && data.meta.issuers_with_instruments === 0 ? (
              <>No bond instruments have been extracted yet — they come from the LLM pass.
              Open an issuer and click <span className="font-medium text-slate-500">Run LLM analysis</span> to populate seniority data.</>
            ) : (
              <>Try lowering the minimum rating or allowing Negative outlooks.</>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-max text-sm whitespace-nowrap">
              <thead>
                <tr className="bg-gray-50 text-xs font-medium text-slate-500 uppercase tracking-wide">
                  <th className="px-6 py-3 text-left">Issuer</th>
                  <th className="px-4 py-3 text-left">Instrument</th>
                  <th className="px-4 py-3 text-center">Seniority</th>
                  <th className="px-4 py-3 text-center">Issuer Rating</th>
                  <th className="px-4 py-3 text-center">Outlook</th>
                  <th className="px-4 py-3 text-right">12m Downgrade</th>
                  <th className="px-4 py-3 text-center">Notched Rating</th>
                  <th className="px-4 py-3 text-right">Coupon</th>
                  <th className="px-4 py-3 text-right">Maturity</th>
                  <th className="px-4 py-3 text-right">Principal</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {rows.map((r, i) => (
                  <ScreenRowView key={`${r.cik}-${i}`} row={r} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Legend ── */}
      <div className="text-xs text-slate-400 max-w-3xl">
        <span className="font-medium">Notched rating</span> = the issuer&apos;s implied rating adjusted
        for the instrument&apos;s seniority (senior secured is notched up one for better recovery).
        This is a deterministic, ratio-derived view — not an agency rating.
      </div>
    </div>
  )
}

function ScreenRowView({ row }: { row: ScreenRow }) {
  const ob = outlookBadge(row.outlook)
  const sb = seniorityBadge(row.seniority)
  const href = `/issuer/${row.ticker || row.cik}`
  return (
    <tr className="hover:bg-gray-50 transition-colors">
      <td className="px-6 py-3">
        <Link href={href} className="font-bold text-slate-800 hover:text-blue-600 transition-colors font-mono">
          {row.ticker || row.cik}
        </Link>
        {row.name && <div className="text-xs text-slate-500 truncate max-w-[16rem]" title={row.name}>{row.name}</div>}
      </td>
      <td className="px-4 py-3 text-slate-700">{row.instrument_name || '—'}</td>
      <td className="px-4 py-3 text-center">
        <span className={`inline-block text-xs font-medium px-2.5 py-1 rounded-full ${sb.cls}`}>{sb.label}</span>
      </td>
      <td className="px-4 py-3 text-center">
        <span className={`inline-block text-xs font-mono font-semibold px-2.5 py-1 rounded-full ${ratingBg(row.issuer_implied_rating)}`}>
          {row.issuer_implied_rating}
        </span>
      </td>
      <td className="px-4 py-3 text-center">
        {ob ? (
          <span className={`inline-block text-xs font-bold px-1.5 py-1 rounded-full ${ob.cls}`} title={`Outlook: ${ob.label}`}>{ob.arrow}</span>
        ) : <span className="text-slate-300">—</span>}
      </td>
      <td className="px-4 py-3 text-right font-mono text-slate-700">
        {row.p_downgrade != null ? `${(row.p_downgrade * 100).toFixed(0)}%` : <span className="text-slate-300">—</span>}
      </td>
      <td className="px-4 py-3 text-center">
        {row.instrument_notched_rating ? (
          <span className={`inline-block text-xs font-mono font-semibold px-2.5 py-1 rounded-full ${ratingBg(row.instrument_notched_rating)}`}>
            {row.instrument_notched_rating}
          </span>
        ) : <span className="text-slate-300">—</span>}
      </td>
      <td className="px-4 py-3 text-right font-mono text-slate-700">{row.coupon != null ? `${row.coupon.toFixed(2)}%` : '—'}</td>
      <td className="px-4 py-3 text-right font-mono text-slate-700">{row.maturity_year ?? '—'}</td>
      <td className="px-4 py-3 text-right font-mono text-slate-700">{fmtFCF(row.principal_amount)}</td>
    </tr>
  )
}
