// Root layout: wraps every page with the global nav bar and page container.
// Metadata here sets the browser tab title and meta description for all routes.

import type { Metadata } from 'next'
import Link from 'next/link'
import './globals.css'

export const metadata: Metadata = {
  title: 'Credit Warning System',
  description: 'Early-warning credit monitoring for corporate bond portfolios',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-gray-50 text-gray-900 min-h-screen">
        {/* Top navigation bar — shared across Portfolio and Backtest pages */}
        <header className="bg-slate-900 text-white shadow-md">
          <div className="max-w-[100rem] mx-auto px-6 py-4 flex items-center gap-8">
            <div>
              <span className="text-lg font-bold tracking-tight">Credit Warning</span>
              <span className="ml-2 text-slate-400 text-sm hidden sm:inline">
                Bond Portfolio Monitor
              </span>
            </div>
            <nav className="flex gap-6 text-sm font-medium">
              <Link href="/" className="text-slate-300 hover:text-white transition-colors">
                Portfolio
              </Link>
              <Link href="/backtest" className="text-slate-300 hover:text-white transition-colors">
                Backtest
              </Link>
            </nav>
          </div>
        </header>
        {/* Page content is constrained to max-w-7xl and padded consistently */}
        <main className="max-w-[100rem] mx-auto px-6 py-8">{children}</main>
      </body>
    </html>
  )
}
