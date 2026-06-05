/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    // In development, proxy /api/* to the local Python server.
    // On Vercel, vercel.json routes /api/* to the Python function directly.
    if (process.env.NODE_ENV === 'development') {
      const pythonUrl = process.env.PYTHON_API_URL || 'http://localhost:8000'
      return [
        {
          source: '/api/:path*',
          destination: `${pythonUrl}/api/:path*`,
        },
      ]
    }
    return []
  },
}

export default nextConfig
