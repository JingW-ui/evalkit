import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 独立开发时：/api 与 /events 代理到 eval_server（8090）
// 生产：vite build 后由 eval_server 直接服务 dist/
export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8090', changeOrigin: true },
      '/events': { target: 'http://127.0.0.1:8090', changeOrigin: true },
    },
  },
  build: { outDir: 'dist' },
})
