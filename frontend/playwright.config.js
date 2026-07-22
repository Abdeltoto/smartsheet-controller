import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  use: {
    baseURL: process.env.PW_BASE_URL || 'http://127.0.0.1:8100',
    headless: true,
  },
  webServer: process.env.PW_SKIP_SERVER
    ? undefined
    : {
        command: 'cd .. && python -m uvicorn backend.app:app --port 8100',
        url: 'http://127.0.0.1:8100/health',
        reuseExistingServer: true,
        timeout: 120_000,
      },
});
