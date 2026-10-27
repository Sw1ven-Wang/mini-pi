import { expect, test, type Page } from '@playwright/test'

const now = '2026-09-01T08:00:00Z'

const bootstrap = {
  csrf_token: 'e2e-token',
  roots: [{ id: 'projects', label: 'Projects' }],
  providers: [{
    id: 'fake', label: 'Fake Provider', models: ['fake-model'],
    default_model: 'fake-model', configured: true,
  }],
  verify_command: 'uv run python -m pytest -q',
  active_session_id: null,
}

function snapshot(status: 'awaiting_approval' | 'completed') {
  const completed = status === 'completed'
  return {
    session_id: 'session-e2e',
    goal: '修复失败测试并保持公开 API',
    root_id: 'projects',
    workspace_path: 'demo',
    provider_id: 'fake',
    model_id: 'fake-model',
    status,
    current_phase: completed ? 'final' : 'mutate',
    permission_mode: 'default',
    plan: {
      objective: '修复失败测试', constraints: [], revision: 1,
      acceptance_criteria: ['测试通过'],
      steps: [{ id: 'step-1', objective: '修正计算逻辑', target_files: ['src/math.py'], status: completed ? 'completed' : 'pending' }],
    },
    metrics: { model_calls: 2, tool_calls: 2, files_read: 1, replans: 0, policy_asks: 1, duration_ms: completed ? 1820 : 0 },
    result: completed ? {
      passed: true,
      changed_files: ['src/math.py'],
      trace: ['plan', 'gather_evidence', 'edit', 'mutate', 'verify', 'final'],
      metrics: { task_success: true },
      decision: { passed: true, failed_criteria: [], diagnostics: [], should_replan: false, reason: 'All checks passed.' },
    } : null,
    pending_approval: completed ? null : {
      approval_id: 'approval-e2e', tool_name: 'workspace_mutation',
      reason: 'workspace mutation requires approval', rule_id: 'default_mutation_ask',
      matched_rule_ids: ['default_mutation_ask'], risk: 'write',
      normalized_targets: ['src/math.py'], summary: 'Edit src/math.py', preview_id: 'preview-e2e',
    },
    error: null,
    created_at: now,
    updated_at: now,
  }
}

async function mockApi(page: Page) {
  let completed = false
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/bootstrap') {
      await route.fulfill({ json: bootstrap })
    } else if (url.pathname === '/api/sessions') {
      await route.fulfill({ json: [] })
    } else if (url.pathname === '/api/directories') {
      await route.fulfill({ json: { root_id: 'projects', relative_path: url.searchParams.get('path') ?? '', parent_path: null, directories: [{ name: 'demo', relative_path: 'demo' }] } })
    } else if (url.pathname === '/api/tasks' && request.method() === 'POST') {
      await route.fulfill({ status: 201, json: snapshot('awaiting_approval') })
    } else if (url.pathname.endsWith('/events')) {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `id: 1\nevent: agent\ndata: ${JSON.stringify({ event_id: 'event-1', type: 'approval.requested', timestamp: now, payload: {} })}\n\n`,
      })
    } else if (url.pathname.endsWith('/changes')) {
      await route.fulfill({ json: { preview: { preview_id: 'preview-e2e', truncated: false, operations: [{ kind: 'edit_file', path: 'src/math.py', source: null, destination: null, diff: '--- a/src/math.py\n+++ b/src/math.py\n@@ -1 +1 @@\n-return a - b\n+return a + b', truncated: false }] } } })
    } else if (url.pathname.includes('/approvals/')) {
      completed = true
      await route.fulfill({ json: snapshot('completed') })
    } else if (url.pathname === '/api/tasks/session-e2e') {
      await route.fulfill({ json: snapshot(completed ? 'completed' : 'awaiting_approval') })
    } else {
      await route.fulfill({ status: 404, json: { detail: 'not mocked' } })
    }
  })
}

test('Fake Provider task closes the approval, diff, and verification loop', async ({ page }) => {
  await mockApi(page)
  await page.goto('/')
  await page.getByLabel('任务目标').fill('修复失败测试并保持公开 API')
  await page.getByRole('button', { name: /开始任务/ }).click()
  await expect(page.getByRole('dialog', { name: '此操作需要你的确认' })).toBeVisible()
  await expect(page.getByText('+return a + b')).toBeVisible()
  await page.getByRole('button', { name: '允许一次' }).click()
  await expect(page.getByText('任务已通过验证')).toBeVisible()
  await expect(page.getByText('已完成')).toBeVisible()
})

test('empty workbench remains clean at desktop and mobile widths', async ({ page }) => {
  await mockApi(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('从目标到验证，保持每一步可见。')).toBeVisible()
  // The native Windows textarea grip changes between Edge versions. Exclude
  // that OS-drawn decoration while retaining exact app layout comparisons.
  const screenshot = { animations: 'disabled' as const, style: 'textarea { resize: none !important; }' }
  const checkVisual = async (name: string) => {
    if (process.platform === 'win32') {
      await expect(page).toHaveScreenshot(name, screenshot)
    } else {
      // Native font rasterization is platform-dependent. Linux has layout gates
      // and explicit review artifacts, not invented/auto-accepted pixel baselines.
      await test.info().attach(name, { body: await page.screenshot(screenshot), contentType: 'image/png' })
    }
  }
  await checkVisual('empty-desktop.png')
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.setViewportSize({ width: 1024, height: 768 })
  await checkVisual('empty-tablet.png')
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.setViewportSize({ width: 390, height: 844 })
  await checkVisual('empty-mobile.png')
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
