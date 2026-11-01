import type { NextConfig } from 'next'
import { PHASE_DEVELOPMENT_SERVER } from 'next/constants'

export default function config(phase: string): NextConfig {
  const shared: NextConfig = {
    turbopack: { root: process.cwd() },
    images: { unoptimized: true },
  }
  if (phase === PHASE_DEVELOPMENT_SERVER) {
    return {
      ...shared,
      async rewrites() {
        return [
          { source: '/api/message', destination: 'http://127.0.0.1:3000/message' },
          { source: '/openapi.json', destination: 'http://127.0.0.1:3000/openapi.json' },
          { source: '/health', destination: 'http://127.0.0.1:3000/health' },
          { source: '/docs', destination: '/docs.html' },
        ]
      },
    }
  }
  return { ...shared, output: 'export' }
}
