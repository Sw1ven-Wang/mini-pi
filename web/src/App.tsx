import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity,
  Bot,
  ChevronRight,
  CircleStop,
  Clock3,
  FolderOpen,
  GitPullRequestArrow,
  LoaderCircle,
  PanelRight,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react'
import {
  cancelTask,
  confirmAcceptance,
  createTask,
  getBootstrap,
  getChanges,
  getDirectories,
  getSessions,
  getTask,
  resolveApproval,
  resumeSession,
  reconcileTask,
} from './api'
import {
  ActivityPanel,
  ApprovalDialog,
  DiffViewer,
  DirectoryDialog,
  PhaseRail,
  PlanCard,
  ResultBanner,
  statusLabel,
} from './components'
import type {
  AgentEvent,
  Bootstrap,
  DirectoryListing,
  MutationPreview,
  SessionSummary,
  TaskSnapshot,
} from './types'
import { useTaskEvents } from './useTaskEvents'

const terminalStatuses = new Set(['completed', 'failed', 'cancelled'])

export function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null)
  const [rootId, setRootId] = useState('')
  const [workspacePath, setWorkspacePath] = useState('')
  const [providerId, setProviderId] = useState('')
  const [modelId, setModelId] = useState('')
  const [goal, setGoal] = useState('')
  const [task, setTask] = useState<TaskSnapshot | null>(null)
  const [events, setEvents] = useState<AgentEvent[]>([])
  const [preview, setPreview] = useState<MutationPreview | null>(null)
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [directoryOpen, setDirectoryOpen] = useState(false)
  const [directoryListing, setDirectoryListing] = useState<DirectoryListing | null>(null)
  const [directoryLoading, setDirectoryLoading] = useState(false)
  const [activityOpen, setActivityOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [clock, setClock] = useState(() => Date.now())
  const refreshGeneration = useRef(0)

  const active = Boolean(task && (task.runtime_status
    ? !['succeeded', 'failed', 'cancelled'].includes(task.runtime_status)
    : !terminalStatuses.has(task.status)))
  const selectedProvider = bootstrap?.providers.find((item) => item.id === providerId)
  const selectedRoot = bootstrap?.roots.find((item) => item.id === rootId)
  const runtimeLabel = ({ waiting_acceptance: '等待验收', interrupted: '已中断', needs_reconciliation: '需要对账' } as Record<string, string>)[task?.runtime_status ?? '']

  const refreshTask = useCallback(async (sessionId: string) => {
    const generation = ++refreshGeneration.current
    const next = await getTask(sessionId)
    if (generation !== refreshGeneration.current) return next
    setTask((current) => {
      if (current?.session_id === next.session_id && current.run_id === next.run_id
        && (current.revision ?? 0) > (next.revision ?? 0)) return current
      return next
    })
    if (next.pending_approval?.preview_id) {
      setPreview(await getChanges(sessionId, next.pending_approval.preview_id))
    } else if (next.current_phase === 'mutate' || terminalStatuses.has(next.status)) {
      setPreview(await getChanges(sessionId))
    }
    return next
  }, [])

  const onAgentEvent = useCallback((event: AgentEvent) => {
    setEvents((current) => current.some((item) => item.event_id === event.event_id)
      ? current : [...current.slice(-99), event])
    if (task?.session_id) {
      void refreshTask(task.session_id).catch((reason) => setError(messageOf(reason)))
    }
  }, [refreshTask, task?.session_id])

  const syncTask = useCallback(() => {
    if (task?.session_id) {
      // A server restart rotates CSRF cookies. Re-bootstrap before resyncing.
      void getBootstrap().then((data) => { setBootstrap(data); return refreshTask(task.session_id) })
        .catch((reason) => setError(messageOf(reason)))
    }
  }, [refreshTask, task?.session_id])

  // Compatibility status can be "failed" for a durable pause. Keep listening
  // while the authoritative Run is nonterminal, including reconciliation retries.
  const eventStatus = task?.runtime_status && !['succeeded', 'failed', 'cancelled'].includes(task.runtime_status)
    ? 'running' : task?.status ?? null
  const connection = useTaskEvents(task?.session_id ?? null, eventStatus, onAgentEvent, syncTask)

  useEffect(() => {
    void getBootstrap()
      .then(async (data) => {
        setBootstrap(data)
        const root = data.roots[0]
        const provider = data.providers.find((item) => item.configured) ?? data.providers[0]
        setRootId(root?.id ?? '')
        setProviderId(provider?.id ?? '')
        setModelId(provider?.default_model ?? '')
        if (data.active_session_id) {
          const restored = await refreshTask(data.active_session_id)
          setRootId(restored.root_id)
          setWorkspacePath(restored.workspace_path)
          setProviderId(restored.provider_id)
          setModelId(restored.model_id)
          setGoal(restored.goal)
        }
      })
      .catch((reason) => setError(messageOf(reason)))
  }, [refreshTask])

  useEffect(() => {
    if (!rootId) return
    void getSessions(rootId, workspacePath)
      .then(setSessions)
      .catch((reason) => setError(messageOf(reason)))
  }, [rootId, workspacePath, task?.status])

  useEffect(() => {
    if (!selectedProvider) return
    if (!selectedProvider.models.includes(modelId)) {
      setModelId(selectedProvider.default_model)
    }
  }, [modelId, selectedProvider])

  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])

  async function submitTask(event: FormEvent) {
    event.preventDefault()
    if (!goal.trim() || !rootId || !providerId || !modelId) return
    setBusy(true)
    setError(null)
    setEvents([])
    setPreview(null)
    try {
      const created = await createTask({
        root_id: rootId,
        workspace_path: workspacePath,
        goal: goal.trim(),
        provider_id: providerId,
        model_id: modelId,
      })
      setTask(created)
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function browseDirectory(nextRoot: string, path: string) {
    setDirectoryLoading(true)
    setError(null)
    try {
      setDirectoryListing(await getDirectories(nextRoot, path))
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setDirectoryLoading(false)
    }
  }

  function openDirectoryPicker() {
    setDirectoryOpen(true)
    void browseDirectory(rootId, workspacePath)
  }

  function selectDirectory(nextRoot: string, path: string) {
    setRootId(nextRoot)
    setWorkspacePath(path)
    setDirectoryOpen(false)
  }

  async function decide(decision: 'approve_once' | 'approve_session' | 'deny') {
    if (!task?.pending_approval) return
    setBusy(true)
    setError(null)
    try {
      await resolveApproval(task.session_id, task.pending_approval.approval_id, decision)
      await refreshTask(task.session_id)
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function cancel() {
    if (!task) return
    setBusy(true)
    try {
      setTask(await cancelTask(task.session_id))
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function resume(session: SessionSummary) {
    setBusy(true)
    setError(null)
    setEvents([])
    setPreview(null)
    try {
      const restored = await resumeSession(session.session_id, rootId, workspacePath)
      setTask(restored)
      setRootId(restored.root_id)
      setWorkspacePath(restored.workspace_path)
      setProviderId(restored.provider_id)
      setModelId(restored.model_id)
      setGoal(session.goal)
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function resumeCurrent() {
    if (!task) return
    setBusy(true)
    setError(null)
    try {
      setTask(await resumeSession(task.session_id, task.root_id, task.workspace_path))
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function reconcile(action: 'retry_verification' | 'end_preserving_files') {
    if (!task) return
    setBusy(true)
    setError(null)
    try {
      setTask(await reconcileTask(task.session_id, task.revision ?? 0, action))
    } catch (reason) {
      setError(messageOf(reason))
      await refreshTask(task.session_id)
    } finally {
      setBusy(false)
    }
  }

  async function confirmRequirement(requirementId: string) {
    if (!task) return
    setBusy(true)
    setError(null)
    try {
      setTask(await confirmAcceptance(task.session_id, requirementId, task.revision ?? 0))
    } catch (reason) {
      setError(messageOf(reason))
      await refreshTask(task.session_id)
    } finally {
      setBusy(false)
    }
  }

  const elapsed = (() => {
    if (!task) return '—'
    const milliseconds = task.metrics.duration_ms || clock - new Date(task.created_at).getTime()
    return milliseconds < 1000 ? `${milliseconds}ms` : `${(milliseconds / 1000).toFixed(1)}s`
  })()

  if (!bootstrap) {
    return <main className="boot-screen"><div className="brand-mark"><Sparkles size={24} /></div><LoaderCircle className="spin" size={22} /><p>正在启动 Mini-Pi Workbench</p>{error && <span>{error}</span>}</main>
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark"><Sparkles size={18} /></span><div><strong>mini-pi</strong><small>Agent Workbench</small></div></div>
        <button className="workspace-pill" disabled={Boolean(active)} onClick={openDirectoryPicker}><FolderOpen size={16} /><span><small>Workspace</small><strong>{selectedRoot?.label}{workspacePath ? ` / ${workspacePath}` : ''}</strong></span><ChevronRight size={15} /></button>
        <div className="topbar-right">
          <span className="mode-pill"><ShieldCheck size={14} />{task?.permission_mode ?? 'default'}</span>
          <span className={`connection ${connection}`}><i />{connectionLabel(connection)}</span>
          <button className="icon-button activity-toggle" onClick={() => setActivityOpen((value) => !value)} aria-label="切换实时活动"><PanelRight size={18} /></button>
        </div>
      </header>

      {error && <div className="global-error" role="alert"><span>{error}</span><button onClick={() => setError(null)} aria-label="关闭错误"><X size={16} /></button></div>}

      <div className="workspace-grid">
        <aside className="left-sidebar">
          <div className="sidebar-title"><span className="eyebrow">SESSIONS</span><button className="icon-button" onClick={() => rootId && getSessions(rootId, workspacePath).then(setSessions)} aria-label="刷新 Session"><RefreshCw size={15} /></button></div>
          <button className="new-task-button" disabled={Boolean(active)} onClick={() => { setTask(null); setGoal(''); setEvents([]); setPreview(null) }}><Bot size={17} />新建任务</button>
          <div className="session-list">
            {sessions.length === 0 && <div className="empty-sessions">当前工作区还没有 Web Session。</div>}
            {sessions.map((session) => (
              <article className={task?.session_id === session.session_id ? 'session selected' : 'session'} key={session.session_id}>
                <button onClick={() => !active && session.status !== 'completed' && void resume(session)} disabled={Boolean(active) || session.status === 'completed' || !session.provider_id}>
                  <span className={`session-status ${session.status}`} />
                  <span><strong>{session.goal}</strong><small>{formatRelativeTime(session.updated_at)} · rev {session.plan_revision}</small></span>
                  {session.status !== 'completed' && <RotateCcw size={14} />}
                </button>
              </article>
            ))}
          </div>
          <div className="sidebar-footer"><ShieldCheck size={15} /><span>本地安全模式<strong>127.0.0.1 only</strong></span></div>
        </aside>

        <main className="main-workbench">
          <section className="task-composer">
            <div className="composer-heading"><div><span className="eyebrow">TASK</span><h1>{task ? task.goal : '今天要让 Mini-Pi 完成什么？'}</h1></div>{task && <span className={`status-badge ${task.status}`}><i />{runtimeLabel ?? statusLabel(task.status)}</span>}</div>
            {!task || !active ? (
              <form onSubmit={submitTask}>
                <textarea value={goal} onChange={(event) => setGoal(event.target.value)} onKeyDown={(event) => { if (event.ctrlKey && event.key === 'Enter') event.currentTarget.form?.requestSubmit() }} placeholder="例如：修复失败测试，并保持现有公开 API 不变…" rows={3} disabled={busy} aria-label="任务目标" />
                <div className="composer-controls">
                  <div className="model-controls">
                    <label><span>Provider</span><select value={providerId} onChange={(event) => setProviderId(event.target.value)}>{bootstrap.providers.map((provider) => <option value={provider.id} key={provider.id}>{provider.label}{provider.configured ? '' : ' · 未配置'}</option>)}</select></label>
                    <label><span>Model</span><select value={modelId} onChange={(event) => setModelId(event.target.value)}>{selectedProvider?.models.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
                  </div>
                  <button className="button primary run-button" disabled={busy || !goal.trim() || !selectedProvider?.configured}><Play size={16} fill="currentColor" />{busy ? '启动中…' : '开始任务'}<kbd>Ctrl ↵</kbd></button>
                </div>
                <div className="verify-command"><GitPullRequestArrow size={14} /><span>验证命令</span><code>{bootstrap.verify_command}</code></div>
              </form>
            ) : (
              <div className="active-controls"><div><Activity size={16} /><span>Mini-Pi 正在执行，工作区已锁定。</span></div><button className="button ghost danger-button" onClick={() => void cancel()} disabled={busy}><CircleStop size={15} />停止任务</button></div>
            )}
          </section>

          <PhaseRail phase={task?.current_phase ?? null} status={task?.status ?? null} />

          {task?.recovery_required && <section className="panel" aria-label="执行恢复">
            <div className="section-heading"><h2>{task.runtime_status === 'needs_reconciliation' ? '执行需要对账' : '执行已中断'}</h2></div>
            <p>恢复前会核对文件版本、操作日志及当前安全策略。未知副作用不会自动重放。</p>
            {task.recovery?.operations.map((operation, index) => <p key={`${operation.kind}-${index}`}>
              <code>{operation.kind} · {operation.status}</code> {operation.targets.join(', ')}
            </p>)}
            {task.recovery?.external_resources.map((resource, index) => <p key={`${resource.engine}-${index}`}>
              <code>{resource.engine} · {resource.state}</code> {resource.identities.join(', ')}
            </p>)}
            {Boolean(task.recovery?.blockers?.length) && <p>对账检查：{task.recovery?.blockers?.join(' · ')}</p>}
            {task.recovery?.cursor === 'verification_started'
              ? <button className="button ghost" disabled={busy || !task.recovery.can_retry_verification} onClick={() => void reconcile('retry_verification')}><RotateCcw size={15} />安全重试验证</button>
              : <button className="button ghost" disabled={busy} onClick={() => void resumeCurrent()}><RotateCcw size={15} />检查并继续运行</button>}
            <button className="button ghost danger-button" disabled={busy || !task.recovery?.can_end_preserving_files} onClick={() => void reconcile('end_preserving_files')}>保留修改并结束任务</button>
          </section>}

          {task?.acceptance && task.acceptance_contract && <section className="panel" aria-label="验收契约">
            <div className="section-heading"><div><span className="eyebrow">ACCEPTANCE · REV {task.acceptance_contract.revision}</span><h2>验收契约</h2></div>
              <span className="soft-badge">{task.acceptance.passed ? '契约通过' : task.runtime_status === 'waiting_acceptance' ? '等待验收' : '待验证'}</span></div>
            {task.acceptance_contract.requirements.map((requirement) => <div className="diagnostic" key={requirement.requirement_id}>
              <ShieldCheck size={16} /><div><strong>{requirement.description}</strong><small>{task.acceptance?.requirements[requirement.requirement_id] === 'passed' ? '证据有效' : '尚无有效通过证据'}</small></div>
              {task.runtime_status === 'waiting_acceptance' && task.acceptance?.pending_manual.includes(requirement.requirement_id)
                && <button className="button ghost" disabled={busy} onClick={() => void confirmRequirement(requirement.requirement_id)}>确认此项</button>}
            </div>)}
            {task.acceptance.coverage === 'registered_checks_only' && <p>仅证明已注册检查通过，不自动代表全部自然语言需求已完成。</p>}
          </section>}

          {task && <div className="metrics-strip"><span><Clock3 size={15} /><small>耗时</small><strong>{elapsed}</strong></span><span><small>模型调用</small><strong>{task.metrics.model_calls}</strong></span><span><small>工具调用</small><strong>{task.metrics.tool_calls}</strong></span><span><small>读取文件</small><strong>{task.metrics.files_read}</strong></span><span><small>Replan</small><strong>{task.metrics.replans}</strong></span></div>}

          {task ? <><ResultBanner status={task.status} error={task.error} passed={task.result?.passed} /><PlanCard plan={task.plan} /><DiffViewer preview={preview} />{task.result && task.result.decision.diagnostics.length > 0 && <section className="panel diagnostics-panel"><div className="section-heading"><div><span className="eyebrow">DIAGNOSTICS</span><h2>验证诊断</h2></div><span className="soft-badge">{task.result.decision.diagnostics.length}</span></div>{task.result.decision.diagnostics.map((diagnostic, index) => <div className="diagnostic" key={`${diagnostic.path}-${index}`}><span className={diagnostic.severity === 'error' ? 'error-dot' : 'warning-dot'} /><div><strong>{diagnostic.message}</strong><small>{diagnostic.path ?? 'workspace'}{diagnostic.line ? `:${diagnostic.line}` : ''}</small></div></div>)}</section>}</> : <section className="welcome-panel"><span className="welcome-orbit"><Sparkles size={25} /></span><span className="eyebrow">MINIMAL · SAFE · OBSERVABLE</span><h2>从目标到验证，保持每一步可见。</h2><p>选择受信任的工作目录，描述任务。Mini-Pi 会规划、收集证据、生成变更并在应用前执行统一安全策略。</p><div className="welcome-features"><span><ShieldCheck size={16} />统一权限审批</span><span><GitPullRequestArrow size={16} />版本绑定 Diff</span><span><Activity size={16} />实时脱敏事件</span></div></section>}
        </main>

        <div className={`activity-drawer ${activityOpen ? 'open' : ''}`}><button className="drawer-close icon-button" onClick={() => setActivityOpen(false)} aria-label="关闭活动"><X size={18} /></button><ActivityPanel events={events} /></div>
      </div>

      {directoryOpen && <DirectoryDialog roots={bootstrap.roots} initialRoot={rootId} initialPath={workspacePath} listing={directoryListing} loading={directoryLoading} onBrowse={(nextRoot, path) => void browseDirectory(nextRoot, path)} onSelect={selectDirectory} onClose={() => setDirectoryOpen(false)} />}
      {task?.pending_approval && <ApprovalDialog approval={task.pending_approval} preview={preview} busy={busy} onDecision={(decision) => void decide(decision)} />}
    </div>
  )
}

function connectionLabel(connection: string) {
  return { idle: '就绪', connecting: '连接中', live: '实时', reconnecting: '重连中', closed: '已同步' }[connection]
}

function messageOf(reason: unknown) {
  return reason instanceof Error ? reason.message : '发生未知错误'
}

function formatRelativeTime(value: string) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000))
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}
