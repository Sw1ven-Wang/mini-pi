import { useEffect, useState } from 'react'
import {
  ArrowLeft,
  Check,
  CheckCircle2,
  ChevronRight,
  FileCode2,
  Folder,
  FolderOpen,
  LoaderCircle,
  ShieldAlert,
  X,
  XCircle,
} from 'lucide-react'
import type {
  AgentEvent,
  Approval,
  DirectoryListing,
  MutationPreview,
  Phase,
  PreviewOperation,
  TaskPlan,
  TaskStatus,
  WorkspaceRoot,
} from './types'

const phaseItems: Array<{ id: Phase; label: string; short: string }> = [
  { id: 'plan', label: '规划任务', short: 'Plan' },
  { id: 'gather_evidence', label: '收集证据', short: 'Evidence' },
  { id: 'edit', label: '生成变更', short: 'Edit' },
  { id: 'mutate', label: '安全应用', short: 'Apply' },
  { id: 'verify', label: '运行验证', short: 'Verify' },
]

export function PhaseRail({ phase, status }: { phase: Phase | null; status: TaskStatus | null }) {
  const activePhase = phase === 'replan' ? 'plan' : phase
  const activeIndex = phaseItems.findIndex((item) => item.id === activePhase)
  const complete = status === 'completed'
  return (
    <ol className="phase-rail" aria-label="任务执行阶段">
      {phaseItems.map((item, index) => {
        const state = complete || index < activeIndex ? 'done' : index === activeIndex ? 'active' : 'upcoming'
        return (
          <li className={`phase-item ${state}`} key={item.id} aria-current={state === 'active' ? 'step' : undefined}>
            <span className="phase-dot">{state === 'done' ? <Check size={13} /> : index + 1}</span>
            <span><strong>{item.short}</strong><small>{item.label}</small></span>
          </li>
        )
      })}
    </ol>
  )
}

export function PlanCard({ plan }: { plan: TaskPlan | null }) {
  if (!plan) {
    return <div className="empty-card"><LoaderCircle size={18} /> 等待 Planner 生成结构化计划</div>
  }
  return (
    <section className="panel plan-panel" aria-labelledby="plan-title">
      <div className="section-heading">
        <div><span className="eyebrow">PLAN · REV {plan.revision}</span><h2 id="plan-title">{plan.objective}</h2></div>
        <span className="soft-badge">{plan.steps.length} steps</span>
      </div>
      <ol className="plan-list">
        {plan.steps.map((step, index) => (
          <li key={step.id} className={`plan-step ${step.status}`}>
            <span className="step-number">{String(index + 1).padStart(2, '0')}</span>
            <div><strong>{step.objective}</strong>{step.target_files.length > 0 && <small>{step.target_files.join(' · ')}</small>}</div>
            <span className="step-status">{step.status.replace('_', ' ')}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}

function operationLabel(operation: PreviewOperation) {
  if (operation.kind === 'move_file') return `${operation.source} → ${operation.destination}`
  return operation.path ?? '未知文件'
}

function operationKind(kind: PreviewOperation['kind']) {
  return { create_file: 'CREATE', edit_file: 'EDIT', delete_file: 'DELETE', move_file: 'MOVE' }[kind]
}

export function DiffViewer({ preview }: { preview: MutationPreview | null }) {
  const [selected, setSelected] = useState(0)
  useEffect(() => setSelected(0), [preview?.preview_id])
  if (!preview || preview.operations.length === 0) {
    return (
      <section className="panel diff-panel">
        <div className="section-heading"><div><span className="eyebrow">CHANGES</span><h2>变更预览</h2></div></div>
        <div className="empty-diff"><FileCode2 size={22} /><p>Editor 生成变更后，安全 Diff 会显示在这里。</p></div>
      </section>
    )
  }
  const operation = preview.operations[Math.min(selected, preview.operations.length - 1)]
  const lines = operation.diff ? operation.diff.split('\n') : []
  return (
    <section className="panel diff-panel" aria-labelledby="diff-title">
      <div className="section-heading">
        <div><span className="eyebrow">CHANGES</span><h2 id="diff-title">变更预览</h2></div>
        <span className="soft-badge">{preview.operations.length} files</span>
      </div>
      <div className="file-tabs" role="tablist" aria-label="变更文件">
        {preview.operations.map((item, index) => (
          <button className={index === selected ? 'selected' : ''} key={`${item.kind}-${operationLabel(item)}`} onClick={() => setSelected(index)} role="tab" aria-selected={index === selected}>
            <span className={`change-kind ${item.kind}`}>{operationKind(item.kind)}</span>{operationLabel(item)}
          </button>
        ))}
      </div>
      {operation.kind === 'move_file' ? (
        <div className="move-preview"><FolderOpen size={22} /><span>{operation.source}</span><ChevronRight size={16} /><strong>{operation.destination}</strong></div>
      ) : (
        <div className="code-diff" role="region" aria-label={operationLabel(operation)} tabIndex={0}>
          {lines.map((line, index) => {
            const type = line.startsWith('+') && !line.startsWith('+++') ? 'addition' : line.startsWith('-') && !line.startsWith('---') ? 'deletion' : line.startsWith('@@') ? 'hunk' : 'context'
            return <div className={`diff-line ${type}`} key={`${index}-${line}`}><span>{index + 1}</span><code>{line || ' '}</code></div>
          })}
          {operation.truncated && <div className="truncated-note">预览过长，已安全截断</div>}
        </div>
      )}
    </section>
  )
}

const eventLabels: Record<string, string> = {
  'task.started': '任务已启动',
  'phase.changed': '执行阶段更新',
  'plan.created': '计划已生成',
  'plan.revised': '计划已修订',
  'model.request.started': '模型正在工作',
  'model.request.completed': '模型响应完成',
  'mutation.planned': '变更已规划',
  'mutation.applied': '变更已应用',
  'verification.started': '开始验证',
  'verification.completed': '验证完成',
  'policy.evaluated': '安全策略已评估',
  'approval.requested': '等待人工审批',
  'approval.resolved': '审批已处理',
  'task.completed': '任务完成',
  'task.failed': '任务失败',
  'task.cancelled': '任务已取消',
}

export function ActivityPanel({ events }: { events: AgentEvent[] }) {
  return (
    <aside className="activity-panel" aria-label="实时活动">
      <div className="activity-heading"><div><span className="eyebrow">LIVE TRACE</span><h2>实时活动</h2></div><span className="event-count">{events.length}</span></div>
      <div className="event-list">
        {events.length === 0 ? <div className="empty-activity">任务开始后，这里会显示脱敏事件。</div> : events.slice().reverse().map((event) => (
          <article className="event-row" key={event.event_id}>
            <span className={`event-mark ${event.type.includes('failed') || event.type.includes('denied') ? 'danger' : event.type.includes('approval') ? 'warning' : ''}`} />
            <div><strong>{eventLabels[event.type] ?? event.type}</strong><small>{new Date(event.timestamp).toLocaleTimeString('zh-CN', { hour12: false })}</small></div>
          </article>
        ))}
      </div>
    </aside>
  )
}

export function ApprovalDialog({
  approval,
  preview,
  busy,
  onDecision,
}: {
  approval: Approval
  preview: MutationPreview | null
  busy: boolean
  onDecision: (decision: 'approve_once' | 'approve_session' | 'deny') => void
}) {
  const targets = approval.normalized_targets.length ? approval.normalized_targets : ['未提供目标']
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="approval-dialog" role="dialog" aria-modal="true" aria-labelledby="approval-title">
        <div className="approval-icon"><ShieldAlert size={22} /></div>
        <div className="approval-copy"><span className="eyebrow warning-text">PERMISSION REQUIRED</span><h2 id="approval-title">此操作需要你的确认</h2><p>{approval.summary ?? approval.reason}</p></div>
        <dl className="approval-facts">
          <div><dt>风险</dt><dd>{approval.risk ?? 'unknown'}</dd></div>
          <div><dt>工具</dt><dd>{approval.tool_name}</dd></div>
          <div><dt>规则</dt><dd>{(approval.matched_rule_ids.length ? approval.matched_rule_ids : [approval.rule_id]).join(', ')}</dd></div>
        </dl>
        <div className="approval-targets"><span>目标</span>{targets.map((target) => <code key={target}>{target}</code>)}</div>
        {preview && <div className="approval-preview-note"><FileCode2 size={16} /> 已生成 {preview.operations.length} 个文件变更预览，可在主视图检查。</div>}
        <div className="approval-actions">
          <button className="button ghost danger-button" disabled={busy} onClick={() => onDecision('deny')}>拒绝</button>
          <div><button className="button secondary" disabled={busy} onClick={() => onDecision('approve_session')}>本 Session 允许</button><button autoFocus className="button primary" disabled={busy} onClick={() => onDecision('approve_once')}>{busy ? '处理中…' : '允许一次'}</button></div>
        </div>
      </section>
    </div>
  )
}

export function DirectoryDialog({
  roots,
  initialRoot,
  initialPath,
  listing,
  loading,
  onBrowse,
  onSelect,
  onClose,
}: {
  roots: WorkspaceRoot[]
  initialRoot: string
  initialPath: string
  listing: DirectoryListing | null
  loading: boolean
  onBrowse: (root: string, path: string) => void
  onSelect: (root: string, path: string) => void
  onClose: () => void
}) {
  const rootId = listing?.root_id ?? initialRoot
  const path = listing?.relative_path ?? initialPath
  const crumbs = path ? path.split('/') : []
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="directory-dialog" role="dialog" aria-modal="true" aria-labelledby="directory-title">
        <div className="dialog-title"><div><span className="eyebrow">WORKSPACE</span><h2 id="directory-title">选择工作目录</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18} /></button></div>
        <div className="root-tabs">{roots.map((root) => <button className={root.id === rootId ? 'selected' : ''} key={root.id} onClick={() => onBrowse(root.id, '')}>{root.label}</button>)}</div>
        <nav className="breadcrumbs" aria-label="目录路径"><button onClick={() => onBrowse(rootId, '')}>{roots.find((root) => root.id === rootId)?.label}</button>{crumbs.map((crumb, index) => <span key={`${crumb}-${index}`}><ChevronRight size={13} /><button onClick={() => onBrowse(rootId, crumbs.slice(0, index + 1).join('/'))}>{crumb}</button></span>)}</nav>
        <div className="directory-list">
          {listing?.parent_path !== null && listing && <button onClick={() => onBrowse(rootId, listing.parent_path ?? '')}><ArrowLeft size={17} /><span><strong>返回上一级</strong></span></button>}
          {loading ? <div className="directory-loading"><LoaderCircle className="spin" size={19} />读取目录…</div> : listing?.directories.map((directory) => <button key={directory.relative_path} onDoubleClick={() => onBrowse(rootId, directory.relative_path)} onClick={() => onBrowse(rootId, directory.relative_path)}><Folder size={18} /><span><strong>{directory.name}</strong><small>{directory.relative_path}</small></span><ChevronRight size={16} /></button>)}
          {!loading && listing?.directories.length === 0 && <div className="directory-loading">此目录没有子目录</div>}
        </div>
        <div className="directory-footer"><span>当前：<strong>{path || roots.find((root) => root.id === rootId)?.label}</strong></span><div><button className="button ghost" onClick={onClose}>取消</button><button className="button primary" onClick={() => onSelect(rootId, path)}>选择此目录</button></div></div>
      </section>
    </div>
  )
}

export function ResultBanner({ status, error, passed }: { status: TaskStatus; error: string | null; passed?: boolean }) {
  if (!['completed', 'failed', 'cancelled'].includes(status)) return null
  const success = status === 'completed' && passed
  return <section className={`result-banner ${success ? 'success' : status === 'cancelled' ? 'neutral' : 'failure'}`}>{success ? <CheckCircle2 size={22} /> : <XCircle size={22} />}<div><strong>{success ? '任务已通过验证' : status === 'cancelled' ? '任务已取消' : '任务未完成'}</strong><p>{error ?? (success ? '所有验证检查均已通过，变更已安全应用。' : '请检查诊断和实时事件后重试。')}</p></div></section>
}

export function statusLabel(status: TaskStatus | null) {
  return status ? { queued: '排队中', running: '执行中', awaiting_approval: '等待审批', completed: '已完成', failed: '失败', cancelled: '已取消' }[status] : '就绪'
}
