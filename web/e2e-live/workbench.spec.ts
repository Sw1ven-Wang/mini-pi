import { expect, test, type Page } from '@playwright/test'
import type { TaskSnapshot } from '../src/types'

// No API mocks: the built SPA talks to the real FastAPI workbench.
async function start(page: Page, workspace: string): Promise<string> {
  await page.goto('/')
  await page.getByRole('button', { name: /Workspace/ }).click()
  const picker = page.getByRole('dialog', { name: '选择工作目录' })
  await picker.locator('.root-tabs').getByRole('button', { name: 'Projects', exact: true }).click()
  await picker.locator('.directory-list').getByRole('button', { name: `${workspace} ${workspace}`, exact: true }).click()
  await picker.getByRole('button', { name: '选择此目录' }).click()
  await page.getByRole('combobox', { name: 'Provider', exact: true }).selectOption('active')
  await page.getByLabel('任务目标').fill(`Fix ${workspace} with real verification`)
  const created = page.waitForResponse((response) =>
    response.url().endsWith('/api/tasks') && response.request().method() === 'POST')
  await page.getByRole('button', { name: /开始任务/ }).click()
  const response = await created
  expect(response.status()).toBe(201)
  return ((await response.json()) as TaskSnapshot).session_id
}

async function snapshot(page: Page, sessionId: string): Promise<TaskSnapshot> {
  return page.evaluate(async (id) => (await fetch(`/api/tasks/${id}`)).json(), sessionId)
}

async function approve(page: Page, label = '允许一次') {
  const dialog = page.getByRole('dialog', { name: '此操作需要你的确认' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('app.py', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: label, exact: true }).click()
}

async function mutate(page: Page, path: string, body = {}) {
  return page.evaluate(async ({ path, body }) => {
    const bootstrap = await (await fetch('/api/bootstrap')).json()
    const response = await fetch(path, { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Mini-Pi-CSRF': bootstrap.csrf_token },
      body: JSON.stringify(body) })
    return { status: response.status, body: await response.json() }
  }, { path, body })
}

test('real create → SSE approval → diff → isolated verification success', async ({ page }) => {
  const id = await start(page, 'success')
  await expect(page.getByRole('dialog', { name: '此操作需要你的确认' })).toBeVisible()
  await expect(page.getByText('+    return 2', { exact: true })).toBeVisible()
  const pending = await snapshot(page, id)
  expect(pending.status).toBe('awaiting_approval')
  expect(pending.pending_approval?.matched_rule_ids).toContain('edit_file_ask')
  await approve(page)
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
  const final = await snapshot(page, id)
  expect(final.result?.passed).toBe(true)
  expect(final.result?.changed_files).toEqual(['app.py'])
  expect(final.result?.metrics.verification_attempts).toBe(1)
  expect(final.result?.trace).toContain('verify')
  // The fixture's actual pytest test also checks the provider key is absent.
  expect(await page.content()).not.toContain('sk-test-only-server-sentinel')
})

test('deny fails safely; persisted Session resumes through the UI', async ({ page }) => {
  const id = await start(page, 'denied')
  await approve(page, '拒绝')
  await expect(page.locator('.result-banner')).toContainText('任务未完成')
  expect((await snapshot(page, id)).status).toBe('failed')
  await expect(page.locator('.session-list button').filter({ hasText: 'Fix denied' })).toBeEnabled()
  await page.locator('.session-list button').filter({ hasText: 'Fix denied' }).click()
  await approve(page, '本 Session 允许')
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
  expect((await snapshot(page, id)).result?.passed).toBe(true)
})

test('cancellation denies pending approval and frees the active slot', async ({ page }) => {
  const id = await start(page, 'cancelled')
  await expect(page.getByRole('dialog', { name: '此操作需要你的确认' })).toBeVisible()
  const pending = (await snapshot(page, id)).pending_approval!
  expect((await mutate(page, `/api/tasks/${id}/cancel`)).status).toBe(200)
  await expect(page.locator('.result-banner')).toContainText('任务已取消')
  const cancelled = await snapshot(page, id)
  expect(cancelled.pending_approval).toBeNull()
  expect((await mutate(page, `/api/tasks/${id}/approvals/${pending.approval_id}`, { decision: 'approve_once' })).status).toBe(409)
  const resumed = await mutate(page, `/api/sessions/${id}/resume`, { root_id: cancelled.root_id, workspace_path: 'cancelled' })
  expect(resumed.status).toBe(200)
  await page.reload()
  await approve(page)
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
})

test('reload restores workspace, profile and model for the next task', async ({ page }) => {
  const id = await start(page, 'refresh')
  await expect(page.getByRole('dialog', { name: '此操作需要你的确认' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: /Workspace/ })).toContainText('Projects / refresh')
  await approve(page)
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
  await expect(page.getByRole('combobox', { name: 'Provider', exact: true })).toHaveValue('active')
  await expect(page.getByRole('combobox', { name: 'Model', exact: true })).toHaveValue('active-model')
  await page.getByRole('button', { name: '新建任务', exact: true }).click()
  await expect(page.getByRole('button', { name: /Workspace/ })).toContainText('Projects / refresh')
  expect((await snapshot(page, id)).result?.passed).toBe(true)
})

test('deterministic DENY never offers approval', async ({ page }) => {
  const id = await start(page, 'hard-denied')
  await expect(page.locator('.result-banner')).toContainText('任务未完成')
  await expect(page.getByRole('dialog', { name: '此操作需要你的确认' })).toHaveCount(0)
  const final = await snapshot(page, id)
  expect(final.status).toBe('failed')
  expect(final.pending_approval).toBeNull()
})

test('missing L2 engine/image fails without host fallback', async ({ page }) => {
  const id = await start(page, 'l2-unavailable')
  await approve(page)
  await expect(page.locator('.result-banner')).toContainText('任务未完成')
  const final = await snapshot(page, id)
  expect(final.status).toBe('failed')
  expect(final.result).toBeNull()
  expect(final.error).toMatch(/Docker|Podman|镜像|容器/)
})

test('SSE reconnect reconciles completion while the browser was offline', async ({ page, context }) => {
  const id = await start(page, 'offline')
  await expect(page.getByRole('dialog', { name: '此操作需要你的确认' })).toBeVisible()
  const pending = (await snapshot(page, id)).pending_approval!
  const bootstrap = await (await context.request.get('/api/bootstrap')).json()
  await context.setOffline(true)
  const response = await context.request.post(`/api/tasks/${id}/approvals/${pending.approval_id}`, {
    headers: { 'X-Mini-Pi-CSRF': bootstrap.csrf_token }, data: { decision: 'approve_once' },
  })
  expect(response.status()).toBe(200)
  await expect.poll(async () => (await (await context.request.get(`/api/tasks/${id}`)).json()).status).toBe('completed')
  await context.setOffline(false)
  await expect(page.getByText('任务已通过验证', { exact: true })).toBeVisible()
})
