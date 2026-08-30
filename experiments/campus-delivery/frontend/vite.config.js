import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 校园外卖前端 Vite 配置
// - dev server 端口 5173,/api 代理到后端 8080(见 docs/operations.md)
// - 生产构建产物输出 dist/,由 nginx 或 Spring Boot 静态资源托管
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_URL || 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    chunkSizeWarningLimit: 1500,
  },
});
