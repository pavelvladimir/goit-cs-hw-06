import { defineConfig, devices } from '@playwright/test'

const externalURL = process.env.FRONTEND_BASE_URL

export default defineConfig({
  testDir: './tests',
  testMatch: externalURL ? ['**/integration.spec.ts', '**/docs.spec.ts'] : ['**/web.spec.ts', '**/docs.spec.ts'],
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 3,
  timeout: 30_000,
  expect: { timeout: 5000 },
  outputDir: externalURL ? 'test-results/integration' : 'test-results/web',
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: externalURL ? 'playwright-report/integration' : 'playwright-report/web' }],
  ],
  use: {
    baseURL: externalURL ?? 'http://127.0.0.1:4176',
    viewport: { width: 1280, height: 720 },
    locale: 'uk-UA',
    timezoneId: 'Europe/Prague',
    colorScheme: 'light',
    reducedMotion: 'reduce',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
  webServer: externalURL
    ? undefined
    : {
        command: 'uv run --locked python -m scripts.preview_frontend',
        cwd: '..',
        url: 'http://127.0.0.1:4176/message',
        reuseExistingServer: false,
        timeout: 30_000,
      },
})
