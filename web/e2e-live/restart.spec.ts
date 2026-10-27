import { expect, test, type Page } from '@playwright/test'
import { spawn, type ChildProcess } from 'node:child_process'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { TaskSnapshot } from '../src/types'

const projectRoot = fileURLToPath(new URL('../../', import.meta.url))
const python = join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const origin = 'http://127.0.0.1:8877'
let server: ChildProcess | null = null
let stateRoot = ''
let serverLogs = ''

async function launch(flags: string[] = []) {
  let logs = ''
  server = spawn(python, ['-m', 'tests.web.restart_server', '--root', stateRoot, ...flags], {
    cwd: projectRoot, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  })
  const capture = (data: Buffer) => {
    logs = (logs + data.toString()).slice(-8000)
    serverLogs = (serverLogs + data.toString()).slice(-24000)
  }
  server.stdout?.on('data', capture)
  server.stderr?.on('data', capture)
  await expect.poll(async () => {
    if (server?.exitCode !== null) throw new Error(logs)
    return fetch(`${origin}/api/bootstrap`).then((response) => response.ok).catch(() => false)
  }, { timeout: 15_000 }).toBe(true)
}

async function stop() {
  const process = server
  server = null
  if (!process || process.exitCode !== null) return
  const exited = new Promise<void>((resolve) => process.once('exit', () => resolve()))
  process.kill('SIGKILL')
  await exited
}

test.beforeEach(async () => {
  serverLogs = ''
  const parent = join(projectRoot, 'web', 'test-results', 'fixtures')
  await mkdir(parent, { recursive: true })
  stateRoot = await mkdtemp(join(parent, 'restart-'))
})
test.afterEach(async ({ browserName }, info) => {
  await stop()
  if (info.status !== info.expectedStatus) {
    await info.attach('test-server.log', { body: `${browserName}\n${serverLogs}`, contentType: 'text/plain' })
  }
})

async function start(page: Page) {
  await page.goto(origin)
  await page.getByRole('button', { name: /Workspace/ }).click()
  const picker = page.getByRole('dialog', { name: '选择工作目录' })
  await picker.locator('.directory-list').getByRole('button', { name: 'demo demo', exact: true }).click()
  await picker.getByRole('button', { name: '选择此目录' }).click()
  await page.getByLabel('任务目标').fill('Fix value and verify with durable runtime')
  const created = page.waitForResponse((response) => response.url().endsWith('/api/tasks') && response.request().method() === 'POST')
  await page.getByRole('button', { name: /开始任务/ }).click()
  const response = await created
  expect(response.status()).toBe(201)
  const task = await response.json() as TaskSnapshot
  await expect(page.getByRole('dialog', { name: '此操作需要你的确认' })).toBeVisible()
  return task.session_id
}

async function snapshot(page: Page, id: string): Promise<TaskSnapshot> {
  return page.evaluate(async (id) => (await fetch(`/api/tasks/${id}`)).json(), id)
}

async function crashVerification(page: Page, flags: string[] = []) {
  await launch(['--replay-safe', '--crash-after-verification-process', ...flags])
  const id = await start(page)
  await page.getByRole('button', { name: '允许一次', exact: true }).click()
  await expect.poll(() => server?.exitCode).toBe(88)
  return id
}

test('reconciliation retries only replay-safe verification and survives a second hard crash', async ({ page }) => {
  const id = await crashVerification(page)
  const path = join(stateRoot, 'projects', 'demo', 'app.py')
  const source = await readFile(path, 'utf8')
  await launch(['--replay-safe', '--crash-after-verification-process'])
  await page.reload()
  await expect(page.getByRole('button', { name: '安全重试验证', exact: true })).toBeEnabled()
  const before = await snapshot(page, id)
  const stale = await page.evaluate(async ({ id, revision }) => {
    const boot = await (await fetch('/api/bootstrap')).json()
    return (await fetch(`/api/tasks/${id}/reconcile`, { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Mini-Pi-CSRF': boot.csrf_token },
      body: JSON.stringify({ action: 'retry_verification', expected_revision: revision - 1 }),
    })).status
  }, { id, revision: before.revision ?? 0 })
  expect(stale).toBe(409)
  await page.getByRole('button', { name: '安全重试验证', exact: true }).click()
  await expect.poll(() => server?.exitCode).toBe(88)
  await launch(['--replay-safe'])
  await page.reload()
  await expect(page.getByRole('button', { name: '安全重试验证', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '安全重试验证', exact: true }).click()
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
  const after = await snapshot(page, id)
  expect(after.run_id).toBe(before.run_id)
  expect(after.acceptance?.passed).toBe(true)
  expect(await readFile(path, 'utf8')).toBe(source)
})

test('external edits block verification replay but can be preserved when ending the run', async ({ page }) => {
  const id = await crashVerification(page)
  const path = join(stateRoot, 'projects', 'demo', 'app.py')
  await writeFile(path, 'human work must survive\n')
  await launch(['--replay-safe'])
  await page.reload()
  await expect(page.getByRole('button', { name: '安全重试验证', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '保留修改并结束任务', exact: true })).toBeEnabled()
  const before = await snapshot(page, id)
  expect(before.recovery?.blockers).toContain('workspace_changed')
  await page.getByRole('button', { name: '保留修改并结束任务', exact: true }).click()
  await expect.poll(async () => (await snapshot(page, id)).runtime_status).toBe('cancelled')
  expect(await readFile(path, 'utf8')).toBe('human work must survive\n')
  await page.reload()
  expect(await readFile(path, 'utf8')).toBe('human work must survive\n')
  await expect(page.getByRole('button', { name: '新建任务', exact: true })).toBeEnabled()
})

test('non replay-safe checks remain blocked after a real verification crash', async ({ page }) => {
  await launch(['--crash-after-verification-process'])
  const id = await start(page)
  await page.getByRole('button', { name: '允许一次', exact: true }).click()
  await expect.poll(() => server?.exitCode).toBe(88)
  await launch()
  await page.reload()
  await expect(page.getByRole('button', { name: '安全重试验证', exact: true })).toBeDisabled()
  expect((await snapshot(page, id)).recovery?.blockers).toContain('check_not_replay_safe')
  await page.getByRole('button', { name: '保留修改并结束任务', exact: true }).click()
  await expect.poll(async () => (await snapshot(page, id)).runtime_status).toBe('cancelled')
})

test('owned process crash uses OS job proof on Windows and stays blocked on POSIX', async ({ page }) => {
  await launch(['--replay-safe', '--crash-during-verification'])
  const id = await start(page)
  await page.getByRole('button', { name: '允许一次', exact: true }).click()
  await expect.poll(() => server?.exitCode).toBe(89)
  await launch(['--replay-safe'])
  await page.reload()
  if (process.platform === 'win32') {
    await expect(page.getByRole('button', { name: '安全重试验证', exact: true })).toBeEnabled()
    await page.getByRole('button', { name: '安全重试验证', exact: true }).click()
    await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
    expect((await snapshot(page, id)).acceptance?.passed).toBe(true)
  } else {
    await expect(page.getByRole('button', { name: '安全重试验证', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '保留修改并结束任务', exact: true })).toBeDisabled()
    expect((await snapshot(page, id)).recovery?.blockers).toContain('resource_lifetime_unknown')
  }
})

test('pending approval survives actual server termination and a new CSRF session', async ({ page }) => {
  await launch()
  const id = await start(page)
  const before = await snapshot(page, id)
  const oldToken = await page.evaluate(async () => (await (await fetch('/api/bootstrap')).json()).csrf_token as string)
  await stop()
  await launch()
  await page.reload()
  await expect(page.getByRole('dialog', { name: '此操作需要你的确认' })).toBeVisible()
  await expect(page.getByText('+    return 2', { exact: true })).toBeVisible()
  const restored = await snapshot(page, id)
  expect(restored.run_id).toBe(before.run_id)
  expect(restored.pending_approval?.approval_id).toBe(before.pending_approval?.approval_id)
  expect(restored.pending_approval?.preview_id).not.toBe(before.pending_approval?.preview_id)
  const stale = await page.evaluate(async ({ id, oldToken }) => (await fetch(`/api/tasks/${id}/cancel`, {
    method: 'POST', headers: { 'X-Mini-Pi-CSRF': oldToken },
  })).status, { id, oldToken })
  expect(stale).toBe(403)
  await page.getByRole('button', { name: '允许一次', exact: true }).click()
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
  expect((await snapshot(page, id)).acceptance?.passed).toBe(true)
})

test('crash after file effect resumes verification without replaying the edit', async ({ page }) => {
  await launch(['--crash-after-effect'])
  const id = await start(page)
  const before = await snapshot(page, id)
  await page.getByRole('button', { name: '允许一次', exact: true }).click()
  await expect.poll(() => server?.exitCode).toBe(86)
  const path = join(stateRoot, 'projects', 'demo', 'app.py')
  const bytes = await readFile(path, 'utf8')
  expect(bytes).toContain('return 2')
  await launch()
  await page.reload()
  await expect(page.getByRole('region', { name: '执行恢复' })).toBeVisible()
  await page.getByRole('button', { name: '检查并继续运行', exact: true }).click()
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
  expect((await snapshot(page, id)).run_id).toBe(before.run_id)
  expect(await readFile(path, 'utf8')).toBe(bytes)
})

test('external edit after restart invalidates the pending approval', async ({ page }) => {
  await launch()
  const id = await start(page)
  await stop()
  const path = join(stateRoot, 'projects', 'demo', 'app.py')
  await writeFile(path, 'human work\n')
  await launch()
  await page.reload()
  await expect(page.getByRole('region', { name: '执行恢复' })).toBeVisible()
  await expect(page.getByRole('dialog', { name: '此操作需要你的确认' })).toHaveCount(0)
  expect((await snapshot(page, id)).runtime_status).toBe('needs_reconciliation')
  expect(await readFile(path, 'utf8')).toBe('human work\n')
})

test('test success waits for manual acceptance across a server restart', async ({ page }) => {
  await launch(['--manual'])
  const id = await start(page)
  await page.getByRole('button', { name: '允许一次', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认此项', exact: true })).toBeVisible()
  expect((await snapshot(page, id)).runtime_status).toBe('waiting_acceptance')
  await stop()
  await launch(['--manual'])
  await page.reload()
  await expect(page.getByRole('button', { name: '确认此项', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '确认此项', exact: true }).click()
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
  expect((await snapshot(page, id)).acceptance?.passed).toBe(true)
})

test('an open browser re-bootstraps after server restart without reloading the page', async ({ page }) => {
  await launch()
  const id = await start(page)
  const before = await snapshot(page, id)
  await stop()
  await launch()
  await expect.poll(async () => {
    const current = await snapshot(page, id)
    return Boolean(current.pending_approval?.preview_id
      && current.pending_approval.preview_id !== before.pending_approval?.preview_id)
  }, { timeout: 15_000 }).toBe(true)
  await page.getByRole('button', { name: '允许一次', exact: true }).click()
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
})
