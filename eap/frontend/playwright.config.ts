import { defineConfig } from '@playwright/test'

// E2E：需要本地后端（127.0.0.1:8300）+ 前端 dev（3001）先起好；
// CI 未起服务时由 webServer 自动拉起前端 dev（后端需另行就绪）。
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:3001',
    trace: 'retain-on-failure',
  },
  webServer: process.env.E2E_NO_WEB_SERVER ? undefined : {
    command: 'pnpm dev -p 3001',
    url: 'http://localhost:3001',
    reuseExistingServer: true,
    timeout: 60_000,
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
})
