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
  fmtRatio, fmtFCF, fmtPct, scoreLabel, scoreBg,
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

  // Stores the CIK currently being deleted so only that row's button shows "…".
  // Keyed on CIK (not ticker) because delisted issuers have an empty ticker,
  // which would otherwise collide across rows. null = no delete in progress.
  const [deletingCik, setDeletingCik] = useState<string | null>(null)

  // The issuer the user has clicked "Remove" on, awaiting confirmation in the
  // modal. null = the confirm modal is closed.
  const [pendingDelete, setPendingDelete] = useState<IssuerSummary | null>(null)

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
      const added = await trackIssuer(identifier)
      setTicker('')  // clear both inputs on success
      setCik('')
      // Name the company and its CIK in the confirmation so the user can verify
      // the right issuer was resolved (especially for CIK-only / delisted inputs).
      const label = added.name
        ? `${added.name} (CIK ${added.cik})`
        : `CIK ${added.cik}`
      setSuccess(`${label} added successfully`)
      await load()   // reload the table to show the newly tracked issuer
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to track issuer')
    } finally {
      setTracking(false)
    }
  }

  /**
   * Perform the actual deletion for the issuer awaiting confirmation in the
   * modal. Triggered by the modal's "Remove" button (handleDelete only opens
   * the modal). Sets deletingCik so the modal button shows an in-flight state.
   *
   * Deletes by CIK (the permanent identifier) rather than ticker — delisted
   * issuers (e.g. WeWork) have no ticker, and DELETE /api/issuer/{id} accepts
   * either form.
   */
  async function confirmDelete() {
    const iss = pendingDelete
    if (!iss) return
    const label = iss.name || iss.ticker || `CIK ${iss.cik}`
    setDeletingCik(iss.cik)
    setError('')
    try {
      // Prefer the ticker for a friendly URL, but fall back to the CIK when the
      // issuer has no ticker so the delete still resolves.
      await deleteIssuer(iss.ticker || iss.cik)
      setPendingDelete(null)  // close the modal on success
      await load()            // reload the table to remove the deleted row
    } catch {
      setError(`Failed to remove ${label}`)
      setPendingDelete(null)  // close the modal; the error banner explains the failure
    } finally {
      setDeletingCik(null)
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
          <button onClick={load} className="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-slate-600 transition-colors">
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M21 12a9 9 0 1 1-2.64-6.36" />
              <path d="M21 3v6h-6" />
            </svg>
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
            {/* w-full keeps the overview within the viewport (no horizontal scroll).
                The many ratio columns use tight px-2 padding so all of them fit;
                overflow-x-auto remains only as a safety net on very narrow screens. */}
            <table className="w-full table-fixed text-sm">
              {/* Fixed column widths keep the table balanced: the 8 ratio columns are
                  equal-width regardless of how long each heading is, instead of each
                  column sizing itself to its content. */}
              <colgroup>
                <col className="w-[10%]" />  {/* Ticker */}
                <col className="w-[7%]" />   {/* Latest Period */}
                <col className="w-[8.25%]" />{/* EBITDA Margin */}
                <col className="w-[8.25%]" />{/* Leverage */}
                <col className="w-[8.25%]" />{/* Interest Coverage */}
                <col className="w-[8.25%]" />{/* FCF */}
                <col className="w-[8.25%]" />{/* Liquidity */}
                <col className="w-[8.25%]" />{/* Cash Flow / Debt */}
                <col className="w-[8.25%]" />{/* Current Ratio */}
                <col className="w-[8.25%]" />{/* Debt / Assets */}
                <col className="w-[4%]" />   {/* Score */}
                <col className="w-[7%]" />   {/* Status */}
                <col className="w-[6%]" />   {/* Remove */}
              </colgroup>
              <thead>
                {/* Bottom-align headers so single-line and wrapped (two-line) headings
                    share the same baseline — keeps the header row visually even. */}
                <tr className="bg-gray-50 text-xs font-medium text-slate-500 uppercase tracking-wide [&>th]:align-bottom">
                  <th className="px-4 py-3 text-left">Ticker</th>
                  <th className="px-2 py-3 text-left">Latest Period</th>
                  <th className="px-2 py-3 text-right">EBITDA Margin</th>
                  <th className="px-2 py-3 text-right">Leverage</th>
                  <th className="px-2 py-3 text-right">Interest Coverage</th>
                  <th className="px-2 py-3 text-right">FCF</th>
                  <th className="px-2 py-3 text-right">Liquidity</th>
                  <th className="px-2 py-3 text-right">Cash Flow / Debt</th>
                  <th className="px-2 py-3 text-right">Current Ratio</th>
                  <th className="px-2 py-3 text-right">Debt / Assets</th>
                  <th className="px-2 py-3 text-center">Score</th>
                  <th className="px-2 py-3 text-center">Status</th>
                  {/* Remove button column, no header */}
                  <th className="px-4 py-3"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {issuers.map(iss => {
                  // Delisted issuers (e.g. WeWork) have no current ticker. The detail
                  // route accepts a CIK too, so fall back to it for the link target.
                  const href = `/issuer/${iss.ticker || iss.cik}`
                  return (
                  <tr key={iss.cik} className="hover:bg-gray-50 transition-colors">

                    {/* Ticker cell: links to the full issuer detail page + shows period count.
                        Ticker and name share a `group` wrapper so hovering either one
                        turns both of them blue together. */}
                    <td className="px-4 py-4">
                      <div className="group">
                        {/* Ticker — or the CIK as a fallback for delisted issuers
                            that have no current ticker. */}
                        <Link
                          href={href}
                          className="font-bold text-slate-800 group-hover:text-blue-600 transition-colors font-mono"
                        >
                          {iss.ticker || iss.cik}
                        </Link>
                        {/* Company name beneath — also a link, so issuers without a
                            ticker (delisted) remain reachable via their name. */}
                        {iss.name && (
                          <Link
                            href={href}
                            className="block text-xs text-slate-600 group-hover:text-blue-600 transition-colors mt-0.5 truncate"
                            title={iss.name}
                          >
                            {iss.name}
                          </Link>
                        )}
                      </div>
                      <div className="text-xs text-slate-400 mt-0.5">{iss.period_count} periods</div>
                    </td>

                    {/* Most recent fiscal year-end date in monospace for alignment. */}
                    <td className="px-2 py-4 text-slate-500 font-mono text-xs whitespace-nowrap">
                      {iss.latest_period ?? '—'}
                    </td>

                    {/* Ratio cells use fmtRatio() which adds the × suffix and handles nulls. */}
                    <td className="px-2 py-4 text-right font-mono text-slate-700">{fmtPct(iss.ebitda_margin)}</td>
                    <td className="px-2 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.leverage)}</td>
                    <td className="px-2 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.interest_coverage)}</td>

                    {/* FCF is in raw dollars from EDGAR — fmtFCF converts to $M/$B. */}
                    <td className="px-2 py-4 text-right font-mono text-slate-700">{fmtFCF(iss.free_cash_flow)}</td>
                    <td className="px-2 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.liquidity)}</td>
                    <td className="px-2 py-4 text-right font-mono text-slate-700">{fmtPct(iss.cash_flow_to_debt)}</td>
                    <td className="px-2 py-4 text-right font-mono text-slate-700">{fmtRatio(iss.current_ratio)}</td>
                    <td className="px-2 py-4 text-right font-mono text-slate-700">{fmtPct(iss.debt_to_assets)}</td>

                    {/* Score as a rounded integer — the exact value is shown in the detail page. */}
                    <td className="px-2 py-4 text-center font-mono font-bold text-slate-800">
                      {Math.round(iss.score)}
                    </td>

                    {/* Colour-coded status badge. scoreBg() returns Tailwind classes. */}
                    <td className="px-2 py-4 text-center">
                      <span className={`inline-block text-xs font-medium px-2.5 py-1 rounded-full whitespace-nowrap ${scoreBg(iss.score)}`}>
                        {scoreLabel(iss.score)}
                      </span>
                    </td>

                    {/* Remove button. Shows "…" while the delete for THIS row is in-flight. */}
                    <td className="px-4 py-4 text-right">
                      <button
                        onClick={() => { setError(''); setPendingDelete(iss) }}
                        disabled={deletingCik === iss.cik}
                        className="inline-flex items-center gap-1 text-xs text-slate-300 hover:text-red-400 transition-colors disabled:opacity-50 whitespace-nowrap"
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                          <path d="M3 6h18" />
                          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                          <line x1="10" y1="11" x2="10" y2="17" />
                          <line x1="14" y1="11" x2="14" y2="17" />
                        </svg>
                        {deletingCik === iss.cik ? '…' : 'Remove'}
                      </button>
                    </td>
                  </tr>
                  )
                })}
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

      {/* ── Remove-confirmation modal ──
          Rendered only when an issuer is pending deletion. The backdrop click and
          the Cancel button both dismiss it; Remove calls confirmDelete(). */}
      {pendingDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          // Click on the backdrop (but not the dialog itself) cancels.
          onClick={() => { if (!deletingCik) setPendingDelete(null) }}
        >
          <div
            className="bg-white rounded-xl shadow-xl border border-gray-200 w-full max-w-md p-6"
            // Stop clicks inside the dialog from bubbling up to the backdrop handler.
            onClick={e => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
          >
            <h3 className="text-base font-semibold text-slate-900">Remove issuer?</h3>
            <p className="text-sm text-slate-600 mt-2">
              This permanently deletes all stored data for{' '}
              <span className="font-semibold text-slate-800">
                {pendingDelete.name || pendingDelete.ticker || `CIK ${pendingDelete.cik}`}
              </span>
              {pendingDelete.name && pendingDelete.ticker && (
                <span className="font-mono text-slate-500"> ({pendingDelete.ticker})</span>
              )}
              {' '}from your portfolio. This can&apos;t be undone.
            </p>
            <div className="text-xs text-slate-400 mt-2 font-mono">CIK {pendingDelete.cik}</div>

            <div className="flex justify-end gap-3 mt-6">
              <button
                onClick={() => setPendingDelete(null)}
                disabled={!!deletingCik}
                className="px-4 py-2 rounded-lg text-sm font-medium text-slate-600 hover:bg-gray-100 transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={confirmDelete}
                disabled={!!deletingCik}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 text-white hover:bg-red-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {deletingCik ? 'Removing…' : 'Remove'}
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  )
}
