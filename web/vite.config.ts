import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_')
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:3080',
      },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
    },
  }
})
