import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base 对应 FastAPI 托管路径（/sdk StaticFiles 挂载 static 目录）
export default defineConfig({
  plugins: [react()],
  base: '/sdk/console/',
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8300',
      '/v1': 'http://localhost:8300',
      '/health': 'http://localhost:8300',
    },
  },
  build: {
    outDir: '../src/eap/static/console',
    emptyOutDir: true,
  },
})
