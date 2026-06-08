'use client'
// ─────────────────────────────────────────────────────────────────────────────
// app/page.tsx — Portfolio Dashboard (the home page, route "/")
//
// This is the main landing page. It shows a table of all tracked issuers with
// their latest-period key ratios, stress scores, and status badges.
//
// User actions available on this page:
//   1. Add a new issuer: type a ticker or CIK → click Track → data is fetched from EDGAR
//   2. Click a ticker row → navigates to /issuer/[ticker] for full history
//   3. Remove an issuer: click the Remove button on any row
//   4. Refresh: reload the table from the database
// ─────────────────────────────────────────────────────────────────────────────

import { useEffect, useState } from 'react'
import Link from 'next/link'
import {
  IssuerSummary, fetchIssuers, trackIssuer, deleteIssuer,
  fmtRatio, fmtFCF, scoreLabel, scoreBg,
} from '@/lib/api'

export default function Dashboard() {

  // ── State ───────────────────────────────────────────────────────────────────
  const [issuers, setIssuers] = useState<IssuerSummary[]>([])

  // Controlled inputs for the "Add Issuer" card — one row per identifier type.
  // The user fills in EITHER field; handleTrack submits whichever is non-empty.
  const [ticker, setTicker] = useState('')
  const [cik, setCik] = useState('')

  // True while POST /api/track is in-flight. Disables the Track button
  // and shows a "Fetching EDGAR data…" label to prevent double-submits.
  const [tracking, setTracking] = useState(false)

  // Stores the ticker currently being deleted so only that row's button shows "…".
  // null = no delete in progress.
  const [deletingTicker, setDeletingTicker] = useState<string | null>(null)

  // Error and success banners shown below the Add Issuer input.
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  // Suppresses the table while the first data fetch is in progress.
  // Without this, an empty table flashes briefly before data arrives.
  const [initialLoad, setInitialLoad] = useState(true)


  // ── Data loading ────────────────────────────────────────────────────────────

  // Load the issuer list when the component first mounts.
  useEffect(() => { load() }, [])

  /**
   * Fetch all tracked issuers from the API and update the table.
   * Called on mount, after tracking a new issuer, and after deleting one.
   */
  async function load() {
    setInitialLoad(true)
    try {
      setIssuers(await fetchIssuers())
    } catch {
      // The most likely cause is the Python server not running.
      setError(
        'Cannot reach API. Make sure the Python server is running: ' +
        'python3 -m uvicorn api.main:app --reload --port 8000'
      )
    } finally {
      // Always clear the loading flag, even if the fetch failed.
      setInitialLoad(false)
    }
  }


  // ── Event handlers ──────────────────────────────────────────────────────────

  /**
   * Handle the "Track" button click (and Enter keypress in either input).
   * Submits whichever field is filled — a ticker (e.g. AAPL) or a CIK
   * (e.g. 320193 / 0000320193). The backend resolves both to the canonical CIK.
   * Uppercasing is safe for digits and normalises an optional "CIK" prefix.
   */
  async function handleTrack() {
    // Prefer the ticker field if both happen to be filled.
    const identifier = (ticker.trim() || cik.trim()).toUpperCase()
    if (!identifier) return  // ignore when both fields are empty

    setError('')
    setSuccess('')

    // Skip the EDGAR round-trip if this issuer is already in the portfolio.
    // Match against the ticker and the CIK (zero-padding so "320193" matches
    // the stored "0000320193") so either identifier form is caught.
    const existing = issuers.find(iss =>
      iss.ticker.toUpperCase() === identifier ||
      iss.cik === identifier.replace(/^CIK/i, '').padStart(10, '0')
    )
    if (existing) {
      setError(`${existing.ticker} is already in your portfolio.`)
      return
    }

    setTracking(true)

    try {
      await trackIssuer(identifier)
      setTicker('')  // clear both inputs on success
      setCik('')
      setSuccess(`${identifier} added successfully`)
      await load()   // reload the table to show the newly tracked issuer
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to track issuer')
    } finally {
      setTracking(false)
    }
  }

  /**
   * Handle the "Remove" button click for a specific issuer.
   * Sets deletingTicker so only that row's button shows "…" while in-flight.
   */
  async function handleDelete(t: string) {
    setDeletingTicker(t)
    setError('')
    try {
      await deleteIssuer(t)
      await load()   // reload the table to remove the deleted row
    } catch {
      setError(`Failed to remove ${t}`)
    } finally {
      setDeletingTicker(null)
    }
  }


  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-8">

      {/* ── Page header ── */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Portfolio Monitor</h1>
        <p className="text-slate-500 mt-1 text-sm">
          Track credit ratios for your corporate bond issuers — updated from SEC EDGAR.
        </p>
      </div>

      {/* ── Add Issuer card ── */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm">
        <h2 className="text-sm font-semibold text-slate-700 mb-1">Add Issuer</h2>
        <p className="text-xs text-slate-400 mb-4">Enter a ticker or a CIK — either one works.</p>

        <div className="space-y-3">

          {/* Row 1 — Ticker */}
          <div className="flex items-center gap-3">
            <label className="w-16 text-xs font-medium text-slate-500 shrink-0">Ticker</label>
            <input
              type="text"
              value={ticker}
              // Clear any stale error/success banner as soon as the user edits the input.
              onChange={e => { setTicker(e.target.value); setError(''); setSuccess('') }}
              // Submit on Enter so users don't have to reach for the Track button.
              // Guard with !tracking to prevent double-submits on fast keystrokes.
              onKeyDown={e => e.key === 'Enter' && !tracking && handleTrack()}
              placeholder="e.g. AAPL"
              className="border border-gray-300 rounded-lg px-4 py-2 text-sm w-56 focus:outline-none focus:ring-2 focus:ring-slate-400 font-mono disabled:bg-gray-50"
              // Disable while the other field is in use so it's clear only one applies.
              disabled={tracking || !!cik.trim()}
            />
          </div>

          {/* Row 2 — CIK */}
          <div className="flex items-center gap-3">
            <label className="w-16 text-xs font-medium text-slate-500 shrink-0">CIK</label>
            <input
              type="text"
              value={cik}
              onChange={e => { setCik(e.target.value); setError(''); setSuccess('') }}
              onKeyDown={e => e.key === 'Enter' && !tracking && handleTrack()}
              placeholder="e.g. 0000320193"
              className="border border-gray-300 rounded-lg px-4 py-2 text-sm w-56 focus:outline-none focus:ring-2 focus:ring-slate-400 font-mono disabled:bg-gray-50"
              disabled={tracking || !!ticker.trim()}
            />
          </div>

          {/* Track button on its own row, aligned under the inputs. */}
          <div className="flex items-center gap-3">
            <span className="w-16 shrink-0" aria-hidden="true" />
            <button
              onClick={handleTrack}
              // Disable while tracking OR when both fields are empty (nothing to submit).
              disabled={tracking || (!ticker.trim() && !cik.trim())}
              className="bg-slate-800 text-white px-5 py-2 rounded-lg text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {tracking ? 'Fetching EDGAR data…' : 'Track'}
            </button>
          </div>
        </div>

        {/* Hint while EDGAR fetch is running — first call can take 10–30 s (cached after). */}
        {tracking && (
          <p className="text-xs text-slate-400 mt-2">
            Fetching SEC filings — takes 10–30 s on first run (cached after).
          </p>
        )}

        {/* Error and success messages appear below the input, not in a separate toast. */}
        {error && <p className="text-sm text-red-600 mt-2">{error}</p>}
        {success && <p className="text-sm text-green-600 mt-2">{success}</p>}
      </div>

      {/* ── Portfolio table ── */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 className="font-semibold text-slate-800">
            Portfolio
            {/* Show the issuer count in a muted badge next to the heading. */}
            {issuers.length > 0 && (
              <span className="ml-2 text-slate-400 font-normal text-sm">
                ({issuers.length} issuers)
              </span>
            )}
          </h2>
          {/* Manual refresh button — useful after external data changes. */}
          <button onClick={load} className="text-xs text-slate-400 hover:text-slate-600 transition-colors">
            Refresh
          </button>
        </div>

        {/*
          Three possible states:
          1. initialLoad=true  → show a loading placeholder (prevents empty-table flash)
          2. issuers.length=0  → show an empty state prompt
          3. issuers.length>0  → render the data table
        */}
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
                  {/* Remove button column, no header */}
                  <th className="px-6 py-3"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {issuers.map(iss => (
                  <tr key={iss.ticker} className="hover:bg-gray-50 transition-colors">

                    {/* Ticker cell: links to the full issuer detail page + shows period count. */}
                    <td className="px-6 py-4">
                      <Link
                        href={`/issuer/${iss.ticker}`}
                        className="font-bold text-slate-800 hover:text-blue-600 font-mono"
                      >
                        {iss.ticker}
                      </Link>
                      <div className="text-xs text-slate-400 mt-0.5">{iss.period_count} periods</div>
                    </td>

                    {/* Most recent fiscal year-end date in monospace for alignment. */}
                    <td className="px-4 py-4 text-slate-500 font-mono text-xs">
                      {iss.latest_period ?? '—'}
                    </td>

                    {/* Ratio cells use fmtRatio() which adds the × suffix and handles nulls. */}
                    <td className="px-4 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.leverage)}</td>
                    <td className="px-4 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.interest_coverage)}</td>

                    {/* FCF is in raw dollars from EDGAR — fmtFCF converts to $M/$B. */}
                    <td className="px-4 py-4 text-right font-mono text-slate-700">{fmtFCF(iss.free_cash_flow)}</td>
                    <td className="px-4 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.liquidity)}</td>

                    {/* Score as a rounded integer — the exact value is shown in the detail page. */}
                    <td className="px-4 py-4 text-center font-mono font-bold text-slate-800">
                      {Math.round(iss.score)}
                    </td>

                    {/* Colour-coded status badge. scoreBg() returns Tailwind classes. */}
                    <td className="px-4 py-4 text-center">
                      <span className={`inline-block text-xs font-medium px-2.5 py-1 rounded-full ${scoreBg(iss.score)}`}>
                        {scoreLabel(iss.score)}
                      </span>
                    </td>

                    {/* Remove button. Shows "…" while the delete for THIS row is in-flight. */}
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

      {/* ── Score legend ──
          Explains the four colour bands shown in the Status column.
          These cut-points mirror the scoreLabel() and scoreBg() functions in lib/api.ts. */}
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
