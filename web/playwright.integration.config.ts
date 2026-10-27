import { defineConfig } from '@playwright/test'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const projectRoot = fileURLToPath(new URL('../', import.meta.url))
const python = join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')

export default defineConfig({
  testDir: './e2e-live',
  outputDir: './test-results/integration',
  workers: 1,
  fullyParallel: false,
  timeout: 45_000,
  expect: { timeout: 15_000 },
  webServer: {
    command: `"${python}" -m tests.web.live_server`,
    cwd: projectRoot,
    url: 'http://127.0.0.1:8876/api/bootstrap',
    reuseExistingServer: false,
    timeout: 45_000,
  },
  use: {
    baseURL: 'http://127.0.0.1:8876',
    channel: process.env.CI ? undefined : 'msedge',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
