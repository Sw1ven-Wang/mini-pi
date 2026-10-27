import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'

const bootstrap = {
  csrf_token: 'test-token',
  roots: [{ id: 'projects', label: 'Projects' }],
  providers: [{
    id: 'fake', label: 'Fake Provider', models: ['fake-model'],
    default_model: 'fake-model', configured: true,
  }],
  verify_command: 'uv run python -m pytest -q',
  active_session_id: null,
}

describe('App', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('keeps paused runtime events live and sends a versioned preserve-files action', async () => {
    const connected = vi.fn()
    class EventSourceStub {
      constructor() { connected() }
      addEventListener = vi.fn()
      removeEventListener = vi.fn()
      close = vi.fn()
    }
    vi.stubGlobal('EventSource', EventSourceStub)
    const task = {
      session_id: 'paused', root_id: 'projects', workspace_path: 'demo',
      provider_id: 'fake', model_id: 'fake-model', goal: 'Recover safely',
      status: 'failed', runtime_status: 'interrupted', revision: 8,
      current_phase: 'verify', permission_mode: 'default', metrics: {},
      recovery_required: true, recovery: { cursor: 'verification_started',
        operations: [], external_resources: [], blockers: ['check_not_replay_safe'],
        can_retry_verification: false, can_end_preserving_files: true },
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    }
    const submissions: RequestInit[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/reconcile')) {
        submissions.push(init ?? {})
        return new Response(JSON.stringify({ ...task, status: 'cancelled', runtime_status: 'cancelled',
          revision: 9, recovery_required: false, recovery: null }))
      }
      const body = url === '/api/bootstrap' ? { ...bootstrap, active_session_id: 'paused' }
        : url.endsWith('/changes') ? { preview: null } : url === '/api/tasks/paused' ? task : []
      return new Response(JSON.stringify(body))
    }))
    render(<App />)
    await waitFor(() => expect(screen.getByRole('button', { name: '安全重试验证' })).toBeDisabled())
    expect(connected).toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: '保留修改并结束任务' }))
    await waitFor(() => expect(submissions).toHaveLength(1))
    expect(JSON.parse(submissions[0].body as string)).toEqual({ action: 'end_preserving_files', expected_revision: 8 })
    expect(new Headers(submissions[0].headers).get('X-Mini-Pi-CSRF')).toBe('test-token')
    await waitFor(() => expect(screen.queryByRole('region', { name: '执行恢复' })).not.toBeInTheDocument())
  })

  it('boots into the calm empty workbench state', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const body = url.startsWith('/api/bootstrap') ? bootstrap : []
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }))
    render(<App />)
    expect(screen.getByText('正在启动 Mini-Pi Workbench')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('今天要让 Mini-Pi 完成什么？')).toBeInTheDocument())
    expect(screen.getByText('Fake Provider')).toBeInTheDocument()
    expect(screen.getByText('从目标到验证，保持每一步可见。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /开始任务/ })).toBeDisabled()
    await user.type(screen.getByLabelText('任务目标'), '修复失败测试')
    expect(screen.getByRole('button', { name: /开始任务/ })).toBeEnabled()
  })

  it('restores the active workspace and model rather than bootstrap defaults', async () => {
    class EventSourceStub {
      addEventListener = vi.fn()
      removeEventListener = vi.fn()
      close = vi.fn()
    }
    vi.stubGlobal('EventSource', EventSourceStub)
    const restored = {
      session_id: 'active-session', root_id: 'actual-root', workspace_path: 'demo',
      provider_id: 'actual', model_id: 'actual-model', goal: 'Restored goal',
      status: 'running', current_phase: 'plan', permission_mode: 'default',
      metrics: {}, plan: null, pending_approval: null, result: null, error: null,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    }
    const requests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      requests.push(url)
      const body = url === '/api/bootstrap' ? {
        ...bootstrap,
        active_session_id: restored.session_id,
        roots: [...bootstrap.roots, { id: 'actual-root', label: 'Actual' }],
        providers: [...bootstrap.providers, { id: 'actual', label: 'Actual Provider', configured: true,
          models: ['actual-model'], default_model: 'actual-model' }],
      } : url.startsWith('/api/tasks/') ? restored : []
      return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
    }))
    render(<App />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Workspace/ })).toHaveTextContent('Actual / demo'))
    await waitFor(() => expect(requests).toContain('/api/sessions?root_id=actual-root&workspace_path=demo'))
    restored.status = 'cancelled'
    await userEvent.click(screen.getByRole('button', { name: /停止任务/ }))
    await waitFor(() => expect(screen.getByLabelText('Provider')).toHaveValue('actual'))
    expect(screen.getByLabelText('Model')).toHaveValue('actual-model')
    expect(screen.getByLabelText('任务目标')).toHaveValue('Restored goal')
  })
})
