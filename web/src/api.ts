import type {
  Bootstrap,
  DirectoryListing,
  MutationPreview,
  SessionSummary,
  TaskSnapshot,
} from './types'

let csrfToken = ''

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (init.body) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers.set('X-Mini-Pi-CSRF', csrfToken)
  }
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: 'same-origin',
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(body.detail ?? `请求失败 (${response.status})`)
  }
  return response.json() as Promise<T>
}

export async function getBootstrap(): Promise<Bootstrap> {
  const data = await requestJson<Bootstrap>('/api/bootstrap')
  csrfToken = data.csrf_token
  return data
}

export function getDirectories(rootId: string, path = ''): Promise<DirectoryListing> {
  const query = new URLSearchParams({ root_id: rootId, path })
  return requestJson(`/api/directories?${query}`)
}

export function getSessions(rootId: string, workspacePath: string): Promise<SessionSummary[]> {
  const query = new URLSearchParams({ root_id: rootId, workspace_path: workspacePath })
  return requestJson(`/api/sessions?${query}`)
}

export function createTask(payload: {
  root_id: string
  workspace_path: string
  goal: string
  provider_id: string
  model_id: string
}): Promise<TaskSnapshot> {
  return requestJson('/api/tasks', { method: 'POST', body: JSON.stringify(payload) })
}

export function getTask(sessionId: string): Promise<TaskSnapshot> {
  return requestJson(`/api/tasks/${encodeURIComponent(sessionId)}`)
}

export function getChanges(sessionId: string, previewId?: string | null): Promise<MutationPreview | null> {
  const query = previewId ? `?${new URLSearchParams({ preview_id: previewId })}` : ''
  return requestJson<{ preview: MutationPreview | null }>(
    `/api/tasks/${encodeURIComponent(sessionId)}/changes${query}`,
  ).then((value) => value.preview)
}

export function resolveApproval(
  sessionId: string,
  approvalId: string,
  decision: 'approve_once' | 'approve_session' | 'deny',
): Promise<TaskSnapshot> {
  return requestJson(
    `/api/tasks/${encodeURIComponent(sessionId)}/approvals/${encodeURIComponent(approvalId)}`,
    { method: 'POST', body: JSON.stringify({ decision }) },
  )
}

export function cancelTask(sessionId: string): Promise<TaskSnapshot> {
  return requestJson(`/api/tasks/${encodeURIComponent(sessionId)}/cancel`, { method: 'POST' })
}

export function reconcileTask(sessionId: string, revision: number,
  action: 'retry_verification' | 'end_preserving_files'): Promise<TaskSnapshot> {
  return requestJson(`/api/tasks/${encodeURIComponent(sessionId)}/reconcile`, {
    method: 'POST', body: JSON.stringify({ action, expected_revision: revision }),
  })
}

export function confirmAcceptance(sessionId: string, requirementId: string, revision: number): Promise<TaskSnapshot> {
  return requestJson(`/api/tasks/${encodeURIComponent(sessionId)}/acceptance/${encodeURIComponent(requirementId)}`, {
    method: 'POST', body: JSON.stringify({ expected_revision: revision }),
  })
}

export function resumeSession(
  sessionId: string,
  rootId: string,
  workspacePath: string,
): Promise<TaskSnapshot> {
  return requestJson(`/api/sessions/${encodeURIComponent(sessionId)}/resume`, {
    method: 'POST',
    body: JSON.stringify({ root_id: rootId, workspace_path: workspacePath }),
  })
}
