import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const upstreamUiSource = decodeURIComponent(
  new URL('../vendor/deepseek-ui/packages/client/web/src', import.meta.url).pathname,
).replace(/^\/(\w:)/, '$1')

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Provenance alias for the pinned upstream source. The adapter imports
    // only the cc-harness surface today, but keeping the alias explicit makes
    // future component-level migrations reviewable without a second build.
    alias: {
      '@deepseek-ui/source': upstreamUiSource,
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:3080',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
