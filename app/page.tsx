'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import {
  IssuerSummary, fetchIssuers, trackIssuer, deleteIssuer,
  fmtRatio, fmtFCF, scoreLabel, scoreBg,
} from '@/lib/api'

export default function Dashboard() {
  const [issuers, setIssuers] = useState<IssuerSummary[]>([])
  const [ticker, setTicker] = useState('')
  const [tracking, setTracking] = useState(false)
  const [deletingTicker, setDeletingTicker] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [initialLoad, setInitialLoad] = useState(true)

  useEffect(() => { load() }, [])

  async function load() {
    setInitialLoad(true)
    try {
      setIssuers(await fetchIssuers())
    } catch {
      setError('Cannot reach API. Make sure the Python server is running: python3 -m uvicorn api.main:app --reload --port 8000')
    } finally {
      setInitialLoad(false)
    }
  }

  async function handleTrack() {
    const t = ticker.trim().toUpperCase()
    if (!t) return
    setTracking(true); setError(''); setSuccess('')
    try {
      await trackIssuer(t)
      setTicker('')
      setSuccess(`${t} added successfully`)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to track ticker')
    } finally {
      setTracking(false)
    }
  }

  async function handleDelete(t: string) {
    setDeletingTicker(t); setError('')
    try {
      await deleteIssuer(t)
      await load()
    } catch {
      setError(`Failed to remove ${t}`)
    } finally {
      setDeletingTicker(null)
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Portfolio Monitor</h1>
        <p className="text-slate-500 mt-1 text-sm">
          Track credit ratios for your corporate bond issuers — updated from SEC EDGAR.
        </p>
      </div>

      {/* Add issuer */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm">
        <h2 className="text-sm font-semibold text-slate-700 mb-3">Add Issuer</h2>
        <div className="flex gap-3">
          <input
            type="text"
            value={ticker}
            onChange={e => setTicker(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !tracking && handleTrack()}
            placeholder="Ticker (e.g. AAPL)"
            className="border border-gray-300 rounded-lg px-4 py-2 text-sm w-48 focus:outline-none focus:ring-2 focus:ring-slate-400 font-mono uppercase"
            disabled={tracking}
          />
          <button
            onClick={handleTrack}
            disabled={tracking || !ticker.trim()}
            className="bg-slate-800 text-white px-5 py-2 rounded-lg text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {tracking ? 'Fetching EDGAR data…' : 'Track'}
          </button>
        </div>
        {tracking && (
          <p className="text-xs text-slate-400 mt-2">
            Fetching SEC filings — takes 10–30 s on first run (cached after).
          </p>
        )}
        {error && <p className="text-sm text-red-600 mt-2">{error}</p>}
        {success && <p className="text-sm text-green-600 mt-2">{success}</p>}
      </div>

      {/* Portfolio table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 className="font-semibold text-slate-800">
            Portfolio
            {issuers.length > 0 && (
              <span className="ml-2 text-slate-400 font-normal text-sm">
                ({issuers.length} issuers)
              </span>
            )}
          </h2>
          <button onClick={load} className="text-xs text-slate-400 hover:text-slate-600 transition-colors">
            Refresh
          </button>
        </div>

        {initialLoad ? (
          <div className="py-16 text-center text-slate-400 text-sm">Loading…</div>
        ) : issuers.length === 0 ? (
          <div className="py-16 text-center text-slate-400 text-sm">
            No issuers tracked yet. Add a ticker above to get started.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 text-xs font-medium text-slate-500 uppercase tracking-wide">
                  <th className="px-6 py-3 text-left">Ticker</th>
                  <th className="px-4 py-3 text-left">Latest Period</th>
                  <th className="px-4 py-3 text-right">Leverage</th>
                  <th className="px-4 py-3 text-right">Coverage</th>
                  <th className="px-4 py-3 text-right">FCF</th>
                  <th className="px-4 py-3 text-right">Liquidity</th>
                  <th className="px-4 py-3 text-center">Score</th>
                  <th className="px-4 py-3 text-center">Status</th>
                  <th className="px-6 py-3"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {issuers.map(iss => (
                  <tr key={iss.ticker} className="hover:bg-gray-50 transition-colors">
                    <td className="px-6 py-4">
                      <Link href={`/issuer/${iss.ticker}`}
                        className="font-bold text-slate-800 hover:text-blue-600 font-mono">
                        {iss.ticker}
                      </Link>
                      <div className="text-xs text-slate-400 mt-0.5">{iss.period_count} periods</div>
                    </td>
                    <td className="px-4 py-4 text-slate-500 font-mono text-xs">{iss.latest_period ?? '—'}</td>
                    <td className="px-4 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.leverage)}</td>
                    <td className="px-4 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.interest_coverage)}</td>
                    <td className="px-4 py-4 text-right font-mono text-slate-700">{fmtFCF(iss.free_cash_flow)}</td>
                    <td className="px-4 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.liquidity)}</td>
                    <td className="px-4 py-4 text-center font-mono font-bold text-slate-800">
                      {Math.round(iss.score)}
                    </td>
                    <td className="px-4 py-4 text-center">
                      <span className={`inline-block text-xs font-medium px-2.5 py-1 rounded-full ${scoreBg(iss.score)}`}>
                        {scoreLabel(iss.score)}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-right">
                      <button
                        onClick={() => handleDelete(iss.ticker)}
                        disabled={deletingTicker === iss.ticker}
                        className="text-xs text-slate-300 hover:text-red-400 transition-colors disabled:opacity-50"
                      >
                        {deletingTicker === iss.ticker ? '…' : 'Remove'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="flex gap-4 text-xs text-slate-400">
        <span>Score:</span>
        <span className="text-green-600">0–24 Healthy</span>
        <span className="text-yellow-600">25–49 Watch</span>
        <span className="text-orange-600">50–74 Stressed</span>
        <span className="text-red-600">75–100 High Risk</span>
      </div>
    </div>
  )
}
