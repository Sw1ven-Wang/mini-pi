export type TaskStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type Phase =
  | 'receive_task'
  | 'plan'
  | 'gather_evidence'
  | 'edit'
  | 'mutate'
  | 'verify'
  | 'replan'
  | 'final'

export interface WorkspaceRoot {
  id: string
  label: string
}

export interface ProviderProfile {
  id: string
  label: string
  models: string[]
  default_model: string
  configured: boolean
}

export interface Bootstrap {
  csrf_token: string
  roots: WorkspaceRoot[]
  providers: ProviderProfile[]
  verify_command: string
  active_session_id: string | null
}

export interface DirectoryEntry {
  name: string
  relative_path: string
}

export interface DirectoryListing {
  root_id: string
  relative_path: string
  parent_path: string | null
  directories: DirectoryEntry[]
}

export interface PlanStep {
  id: string
  objective: string
  target_files: string[]
  status: 'pending' | 'in_progress' | 'completed' | 'blocked' | 'failed'
}

export interface TaskPlan {
  objective: string
  constraints: string[]
  steps: PlanStep[]
  acceptance_criteria: string[]
  revision: number
}

export interface Approval {
  approval_id: string
  tool_name: string
  reason: string
  rule_id: string
  matched_rule_ids: string[]
  risk: string | null
  normalized_targets: string[]
  summary: string | null
  preview_id: string | null
}

export interface RunMetrics {
  model_calls: number
  tool_calls: number
  files_read: number
  replans: number
  policy_asks: number
  duration_ms: number
  [key: string]: number | boolean
}

export interface Diagnostic {
  path?: string | null
  line?: number | null
  severity?: string
  message: string
  source?: string | null
}

export interface TaskResult {
  passed: boolean
  decision: {
    passed: boolean
    failed_criteria: string[]
    diagnostics: Diagnostic[]
    should_replan: boolean
    reason: string
  }
  changed_files: string[]
  trace: Phase[]
  metrics: Record<string, number | boolean>
}

export interface TaskSnapshot {
  session_id: string
  goal: string
  root_id: string
  workspace_path: string
  provider_id: string
  model_id: string
  status: TaskStatus
  current_phase: Phase | null
  permission_mode: string
  plan: TaskPlan | null
  metrics: RunMetrics
  result: TaskResult | null
  pending_approval: Approval | null
  error: string | null
  created_at: string
  updated_at: string
  run_id?: string | null
  runtime_status?: string | null
  revision?: number
  event_sequence?: number
  recovery_required?: boolean
  recovery?: {
    can_retry_verification?: boolean
    can_end_preserving_files?: boolean
    blockers?: string[]
    cursor: string
    reason: string | null
    operations: { kind: string; status: string; targets: string[] }[]
    external_resources: { engine: string; state: string; identities: string[] }[]
  } | null
  acceptance?: {
    passed: boolean
    coverage: string
    requirements: Record<string, 'passed' | 'failed' | 'missing'>
    pending_manual: string[]
  } | null
  acceptance_contract?: {
    contract_id: string
    revision: number
    requirements: { requirement_id: string; description: string; required: boolean; manual_confirmation: boolean }[]
  } | null
}

export interface SessionSummary {
  session_id: string
  goal: string
  status: string
  updated_at: string
  plan_revision: number
  provider_id: string | null
  model_id: string | null
}

export interface PreviewOperation {
  kind: 'create_file' | 'edit_file' | 'delete_file' | 'move_file'
  path: string | null
  source: string | null
  destination: string | null
  diff: string
  truncated: boolean
}

export interface MutationPreview {
  preview_id: string
  operations: PreviewOperation[]
  truncated: boolean
}

export interface AgentEvent {
  event_id: string
  type: string
  timestamp: string
  payload: Record<string, unknown>
}
