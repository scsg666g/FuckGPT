import { translate, type Language } from '@/lib/i18n'
import { apiFetch } from '@/lib/utils'

export type WorkflowDefinition = {
  id: number
  key: string
  version: number
  name: string
  description: string
  enabled: boolean
  definition: {
    steps?: WorkflowDefinitionStep[]
    sample_input?: Record<string, unknown>
    input_schema?: Record<string, unknown>
    ui_schema?: WorkflowUiSchema
    [key: string]: unknown
  }
  created_at?: string
  updated_at?: string
}

export type WorkflowUiOption = {
  label: string
  value: string | number | boolean
}

export type WorkflowUiField = {
  path: string
  label: string
  type?: 'text' | 'number' | 'select' | 'boolean'
  advanced?: boolean
  options?: WorkflowUiOption[]
  placeholder?: string
  helper?: string
  min?: number
  max?: number
}

export type WorkflowUiSection = {
  title: string
  description?: string
  fields?: WorkflowUiField[]
}

export type WorkflowUiSchema = {
  sections?: WorkflowUiSection[]
}

export type WorkflowLaunchMode = 'single' | 'batch'

export type WorkflowInputPreset = {
  id: number
  definition_key: string
  definition_version: number
  current_definition_version: number
  version_mismatch: boolean
  name: string
  is_default: boolean
  is_last_used: boolean
  input: Record<string, unknown>
  launch_mode: WorkflowLaunchMode
  batch_concurrency: number
  batch_count: number
  created_at?: string
  updated_at?: string
}

export type WorkflowInputPresetCollection = {
  definition_key: string
  definition_version: number
  template_input: Record<string, unknown>
  items: WorkflowInputPreset[]
  last_used: WorkflowInputPreset | null
  default_id: number | null
}

export type WorkflowInputPresetPayload = {
  name?: string
  definition_version: number
  input: Record<string, unknown>
  launch_mode: WorkflowLaunchMode
  batch_concurrency: number
  batch_count: number
  is_default?: boolean
}

export type WorkflowAdapter = {
  key: string
}

export type WorkflowDefinitionStep = {
  id: string
  name?: string
  uses: string
  needs?: string[]
  timeout?: string
  max_attempts?: number
}

export type WorkflowStepRun = {
  id: string
  workflow_run_id: string
  step_id: string
  name: string
  adapter_key: string
  status: string
  attempt: number
  max_attempts: number
  input: Record<string, unknown>
  output: Record<string, unknown>
  error: Record<string, unknown>
  external_ref: string
  idempotency_key: string
  next_run_at?: string
  timeout_at?: string
  started_at?: string
  finished_at?: string
  duration_seconds?: number
  created_at?: string
  updated_at?: string
}

export type WorkflowRun = {
  id: string
  definition_key: string
  definition_version: number
  name: string
  status: string
  terminal: boolean
  input: Record<string, unknown>
  context?: Record<string, unknown>
  output?: Record<string, unknown>
  metadata?: Record<string, unknown>
  definition?: Record<string, unknown>
  batch_id?: string
  batch_item_index?: number
  current_step_id: string
  error: string
  cancellation_requested_at?: string
  started_at?: string
  finished_at?: string
  created_at?: string
  updated_at?: string
  steps?: WorkflowStepRun[]
}

export type WorkflowStatusSummary = {
  total: number
  pending: number
  running: number
  waiting_external: number
  retry_scheduled: number
  needs_attention: number
  cancel_requested: number
  succeeded: number
  failed: number
  cancelled: number
  terminal: number
  active: number
}

export type WorkflowObservability = {
  duration_seconds_avg: number
  duration_seconds_max: number
  stuck: number
}

export type WorkflowBatch = {
  id: string
  definition_key: string
  definition_version: number
  name: string
  status: string
  terminal: boolean
  total: number
  concurrency: number
  input: Record<string, unknown>
  summary: WorkflowStatusSummary
  observability?: WorkflowObservability
  runs?: WorkflowRun[] | null
  created_at?: string
  updated_at?: string
}

export type WorkflowStepSummary = {
  step_id: string
  name: string
  status: string
  adapter_key: string
  attempt: number
  max_attempts: number
  error_code: string
  error_message: string
  error_category: string
  operator_hint: string
  external_ref: string
  duration_seconds: number
  stuck: boolean
  stuck_reason: string
}

export type WorkflowRunSummary = {
  run_id: string
  batch_id?: string
  batch_item_index?: number
  definition_key: string
  status: string
  terminal: boolean
  batch_paused?: boolean
  account_id: number
  email: string
  current_stage: string
  display_status: string
  operator_action: string
  risk: string
  duration_seconds: number
  stuck: boolean
  stuck_reason: string
  stuck_step_id: string
  steps: WorkflowStepSummary[]
}

export type WorkflowBatchSummary = {
  id: string
  definition_key: string
  definition_version: number
  name: string
  status: string
  terminal: boolean
  total: number
  concurrency: number
  summary: WorkflowStatusSummary
  observability?: WorkflowObservability
  runs: WorkflowRunSummary[]
  created_at?: string
  updated_at?: string
}

export type WorkflowEvent = {
  id: number
  workflow_run_id: string
  step_id: string
  type: string
  level: string
  message: string
  line: string
  detail: Record<string, unknown>
  created_at?: string
}

export const WORKFLOW_STATUS_VARIANTS: Record<string, 'default' | 'success' | 'warning' | 'danger' | 'secondary'> = {
  pending: 'secondary',
  ready: 'secondary',
  running: 'default',
  waiting_external: 'warning',
  retry_scheduled: 'warning',
  needs_attention: 'warning',
  cancel_requested: 'warning',
  paused: 'warning',
  succeeded: 'success',
  failed: 'danger',
  skipped: 'secondary',
  cancelled: 'warning',
}

export const CANCELLABLE_WORKFLOW_STATUSES = new Set([
  'pending',
  'running',
  'waiting_external',
  'retry_scheduled',
  'needs_attention',
  'cancel_requested',
])

export const RETRYABLE_STEP_STATUSES = new Set([
  'failed',
  'needs_attention',
  'cancelled',
])

export function getWorkflowStatusText(status: string, language?: Language) {
  switch (status) {
    case 'pending':
      return translate('workflowStatus.pending', language)
    case 'ready':
      return translate('workflowStatus.ready', language)
    case 'running':
      return translate('workflowStatus.running', language)
    case 'waiting_external':
      return translate('workflowStatus.waiting_external', language)
    case 'retry_scheduled':
      return translate('workflowStatus.retry_scheduled', language)
    case 'needs_attention':
      return translate('workflowStatus.needs_attention', language)
    case 'cancel_requested':
      return translate('workflowStatus.cancel_requested', language)
    case 'paused':
      return translate('workflowStatus.paused', language)
    case 'succeeded':
      return translate('workflowStatus.succeeded', language)
    case 'failed':
      return translate('workflowStatus.failed', language)
    case 'skipped':
      return translate('workflowStatus.skipped', language)
    case 'cancelled':
      return translate('workflowStatus.cancelled', language)
    default:
      return status || '-'
  }
}

export async function fetchWorkflowDefinitions() {
  const data = await apiFetch('/workflows/definitions')
  return (data.items || []) as WorkflowDefinition[]
}

export async function fetchWorkflowAdapters() {
  const data = await apiFetch('/workflows/adapters')
  return (data.items || []) as WorkflowAdapter[]
}

export async function saveWorkflowDefinition(definition: Record<string, unknown>) {
  return apiFetch('/workflows/definitions', {
    method: 'POST',
    body: JSON.stringify({ definition }),
  }) as Promise<WorkflowDefinition>
}

export async function fetchWorkflowInputPresets(definitionKey: string, version = 0) {
  const key = encodeURIComponent(definitionKey)
  return apiFetch(`/workflows/definitions/${key}/presets?version=${version}`) as Promise<WorkflowInputPresetCollection>
}

export async function createWorkflowInputPreset(
  definitionKey: string,
  payload: WorkflowInputPresetPayload & { name: string },
) {
  return apiFetch(`/workflows/definitions/${encodeURIComponent(definitionKey)}/presets`, {
    method: 'POST',
    body: JSON.stringify(payload),
  }) as Promise<WorkflowInputPreset>
}

export async function updateWorkflowInputPreset(
  definitionKey: string,
  presetId: number,
  payload: WorkflowInputPresetPayload & { name: string },
) {
  return apiFetch(`/workflows/definitions/${encodeURIComponent(definitionKey)}/presets/${presetId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  }) as Promise<WorkflowInputPreset>
}

export async function saveLastUsedWorkflowInput(
  definitionKey: string,
  payload: WorkflowInputPresetPayload,
) {
  return apiFetch(`/workflows/definitions/${encodeURIComponent(definitionKey)}/presets/last-used`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  }) as Promise<WorkflowInputPreset>
}

export async function deleteWorkflowInputPreset(definitionKey: string, presetId: number) {
  return apiFetch(`/workflows/definitions/${encodeURIComponent(definitionKey)}/presets/${presetId}`, {
    method: 'DELETE',
  }) as Promise<{ ok: boolean }>
}

export async function fetchWorkflowRuns(params: {
  limit: number
  offset: number
  status?: string
  definition_key?: string
  batch_id?: string
}) {
  const search = new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
  })
  if (params.status) search.set('status', params.status)
  if (params.definition_key) search.set('definition_key', params.definition_key)
  if (params.batch_id) search.set('batch_id', params.batch_id)
  return apiFetch(`/workflows/runs?${search.toString()}`) as Promise<{
    items: WorkflowRun[]
    total: number
    running: number
    limit: number
    offset: number
  }>
}

export async function fetchWorkflowBatches(params: {
  limit: number
  offset: number
  status?: string
  definition_key?: string
}) {
  const search = new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
  })
  if (params.status) search.set('status', params.status)
  if (params.definition_key) search.set('definition_key', params.definition_key)
  return apiFetch(`/workflows/batches?${search.toString()}`) as Promise<{
    items: WorkflowBatch[]
    total: number
    limit: number
    offset: number
  }>
}

export async function createWorkflowRun(payload: {
  definition_key: string
  version?: number
  name?: string
  input: Record<string, unknown>
}) {
  return apiFetch('/workflows/runs', {
    method: 'POST',
    body: JSON.stringify(payload),
  }) as Promise<WorkflowRun>
}

export async function createWorkflowBatch(payload: {
  definition_key: string
  version?: number
  name?: string
  concurrency?: number
  items: Array<{
    name?: string
    input: Record<string, unknown>
    metadata?: Record<string, unknown>
  }>
}) {
  return apiFetch('/workflows/runs/batch', {
    method: 'POST',
    body: JSON.stringify(payload),
  }) as Promise<WorkflowBatch>
}

export async function fetchWorkflowRun(runId: string) {
  return apiFetch(`/workflows/runs/${runId}`) as Promise<WorkflowRun>
}

export async function fetchWorkflowBatch(batchId: string) {
  return apiFetch(`/workflows/batches/${batchId}`) as Promise<WorkflowBatch>
}

export async function fetchWorkflowRunSummary(runId: string) {
  return apiFetch(`/workflows/runs/${runId}/summary`) as Promise<WorkflowRunSummary>
}

export async function fetchWorkflowBatchSummary(batchId: string) {
  return apiFetch(`/workflows/batches/${batchId}/summary`) as Promise<WorkflowBatchSummary>
}

export async function pauseWorkflowBatch(batchId: string) {
  return apiFetch(`/workflows/batches/${batchId}/pause`, { method: 'POST' }) as Promise<WorkflowBatch>
}

export async function resumeWorkflowBatch(batchId: string) {
  return apiFetch(`/workflows/batches/${batchId}/resume`, { method: 'POST' }) as Promise<WorkflowBatch>
}

export async function cancelWorkflowBatch(batchId: string) {
  return apiFetch(`/workflows/batches/${batchId}/cancel`, { method: 'POST' }) as Promise<WorkflowBatchSummary>
}

export async function retryFailedWorkflowBatch(batchId: string) {
  return apiFetch(`/workflows/batches/${batchId}/retry-failed`, { method: 'POST' }) as Promise<WorkflowBatchSummary & { retried?: number }>
}

export async function fetchWorkflowEvents(runId: string, since = 0) {
  const search = new URLSearchParams({ since: String(since), limit: '200' })
  const data = await apiFetch(`/workflows/runs/${runId}/events?${search.toString()}`)
  return data as { items: WorkflowEvent[]; cursor: number }
}

export async function cancelWorkflowRun(runId: string) {
  return apiFetch(`/workflows/runs/${runId}/cancel`, { method: 'POST' }) as Promise<WorkflowRun>
}

export async function retryWorkflowStep(runId: string, stepId: string) {
  return apiFetch(`/workflows/runs/${runId}/steps/${stepId}/retry`, { method: 'POST' }) as Promise<WorkflowRun>
}

export async function updateWorkflowStepInput(
  runId: string,
  stepId: string,
  input: Record<string, unknown>,
) {
  return apiFetch(`/workflows/runs/${runId}/steps/${stepId}/input`, {
    method: 'PATCH',
    body: JSON.stringify({ input }),
  }) as Promise<WorkflowRun>
}
