import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Download,
  ExternalLink,
  GitBranch,
  Layers3,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Square,
  XCircle,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { formatDateTime, type Language, type TranslationKey } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { cn } from '@/lib/utils'
import {
  CANCELLABLE_WORKFLOW_STATUSES,
  RETRYABLE_STEP_STATUSES,
  WORKFLOW_STATUS_VARIANTS,
  cancelWorkflowBatch,
  cancelWorkflowRun,
  createWorkflowInputPreset,
  createWorkflowBatch,
  createWorkflowRun,
  deleteWorkflowInputPreset,
  fetchWorkflowAdapters,
  fetchWorkflowBatchSummary,
  fetchWorkflowBatches,
  fetchWorkflowDefinitions,
  fetchWorkflowEvents,
  fetchWorkflowInputPresets,
  fetchWorkflowRun,
  fetchWorkflowRunSummary,
  fetchWorkflowRuns,
  getWorkflowStatusText,
  pauseWorkflowBatch,
  resumeWorkflowBatch,
  retryFailedWorkflowBatch,
  retryWorkflowStep,
  saveLastUsedWorkflowInput,
  saveWorkflowDefinition,
  updateWorkflowInputPreset,
  updateWorkflowStepInput,
  type WorkflowAdapter,
  type WorkflowBatch,
  type WorkflowBatchSummary,
  type WorkflowDefinition,
  type WorkflowEvent,
  type WorkflowInputPreset,
  type WorkflowInputPresetCollection,
  type WorkflowInputPresetPayload,
  type WorkflowRun,
  type WorkflowRunSummary,
  type WorkflowStepRun,
  type WorkflowUiField,
  type WorkflowUiSection,
} from '@/lib/workflows'

const PAGE_SIZE = 20
const BATCH_PAGE_SIZE = 10

const STATUS_OPTIONS = [
  '',
  'pending',
  'running',
  'waiting_external',
  'retry_scheduled',
  'needs_attention',
  'succeeded',
  'failed',
  'cancelled',
]

const DEFAULT_REGISTER_CODEX_PUSH_INPUT = {
  registration: {
    count: 1,
    concurrency: 1,
    executor_type: 'browser',
    platform_proxy_mode: 'direct',
    platform_proxy_value: '',
    extra: {
      identity_provider: 'mailbox',
      browser_visible: false,
    },
  },
  codex: {
    oauth_mode: 'browser',
    browser_mode: 'headless',
    keep_browser_open: 'false',
    platform_proxy_mode: 'direct',
    platform_proxy_value: '',
  },
  push: {
    target_key: 'nvtokens',
    payload_format: 'codex',
  },
}

const DEFAULT_REGISTER_KAKAO_CODEX_PUSH_INPUT = {
  registration: {
    count: 1,
    concurrency: 1,
    executor_type: 'browser',
    platform_proxy_mode: 'direct',
    platform_proxy_value: '',
    extra: {
      identity_provider: 'mailbox',
      browser_visible: false,
    },
  },
  kakao: {
    payment_method: 'kakao_pay',
    supplier_setting_id: null,
    scanner_setting_id: null,
    scanner_kind: '',
    auto_submit_scanner: true,
  },
  codex: {
    oauth_mode: 'browser',
    browser_mode: 'headless',
    keep_browser_open: 'false',
    platform_proxy_mode: 'direct',
    platform_proxy_value: '',
  },
  push: {
    target_key: 'nvtokens',
    payload_format: 'codex',
  },
}

type LaunchMode = 'single' | 'batch'

function formatMaybeDate(value: string | undefined, language: Language) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '-'
  return formatDateTime(date, language, {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatJson(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2)
}

function formatDuration(seconds: number | undefined) {
  const total = Math.max(Number(seconds || 0), 0)
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const minuteRest = minutes % 60
  return minuteRest ? `${hours}h ${minuteRest}m` : `${hours}h`
}

function parseJsonObject(text: string) {
  const parsed = JSON.parse(text || '{}')
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error('JSON must be an object')
  }
  return parsed as Record<string, unknown>
}

function parseJsonArray(text: string) {
  const parsed = JSON.parse(text || '[]')
  if (!Array.isArray(parsed)) {
    throw new Error('JSON must be an array')
  }
  return parsed
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function cloneRecord(value: Record<string, unknown>) {
  return JSON.parse(JSON.stringify(value || {})) as Record<string, unknown>
}

function getPathValue(record: Record<string, unknown>, path: string) {
  return path.split('.').reduce<unknown>((current, part) => {
    if (!isPlainObject(current)) return undefined
    return current[part]
  }, record)
}

function setPathValue(record: Record<string, unknown>, path: string, value: unknown) {
  const next = cloneRecord(record)
  const parts = path.split('.').filter(Boolean)
  let cursor: Record<string, unknown> = next
  parts.slice(0, -1).forEach((part) => {
    if (!isPlainObject(cursor[part])) {
      cursor[part] = {}
    }
    cursor = cursor[part] as Record<string, unknown>
  })
  const leaf = parts[parts.length - 1]
  if (leaf) {
    cursor[leaf] = value
  }
  return next
}

function workflowInputForDefinition(definition?: WorkflowDefinition) {
  const sampleInput = definition?.definition?.sample_input
  if (isPlainObject(sampleInput)) {
    return cloneRecord(sampleInput)
  }
  if (definition?.key === 'register_kakao_codex_push') {
    return cloneRecord(DEFAULT_REGISTER_KAKAO_CODEX_PUSH_INPUT)
  }
  if (definition?.key === 'register_codex_push') {
    return cloneRecord(DEFAULT_REGISTER_CODEX_PUSH_INPUT)
  }
  return {}
}

function normalizeUiSections(definition?: WorkflowDefinition) {
  const sections = definition?.definition?.ui_schema?.sections
  if (!Array.isArray(sections)) return []
  return sections
    .map((section) => ({
      ...section,
      fields: Array.isArray(section.fields) ? section.fields.filter((field) => field.path) : [],
    }))
    .filter((section) => section.fields.length > 0)
}

function boolFromValue(value: unknown) {
  if (typeof value === 'boolean') return value
  return String(value || '').toLowerCase() === 'true'
}

function selectValueForField(field: WorkflowUiField, rawValue: string) {
  const option = (field.options || []).find((item) => String(item.value) === rawValue)
  return option ? option.value : rawValue
}

function stepIcon(status: string) {
  if (status === 'succeeded') return CheckCircle2
  if (status === 'failed') return XCircle
  if (status === 'needs_attention') return AlertTriangle
  if (status === 'running') return Loader2
  return Clock3
}

function shortError(error: Record<string, unknown>) {
  return String(error?.message || error?.code || '')
}

function errorCategory(error: Record<string, unknown>) {
  return String(error?.category || '')
}

function operatorHint(error: Record<string, unknown>) {
  return String(error?.operator_hint || '')
}

function externalTaskUrl(step: WorkflowStepRun) {
  const taskId = step.external_ref || String(step.output?.task_id || '')
  return taskId ? '/tasks' : ''
}

function summaryCount(summary: WorkflowBatchSummary['summary'] | WorkflowBatch['summary'] | undefined, key: string) {
  if (!summary) return 0
  return Number((summary as Record<string, number>)[key] || 0)
}

function WorkflowPanel({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={cn(
        'overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-[var(--shadow-soft)]',
        className,
      )}
    >
      {children}
    </section>
  )
}

function WorkflowPanelHeader({
  title,
  description,
  icon,
  action,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  icon?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex items-start justify-between gap-3 border-b border-[var(--border)] px-4 py-3',
        className,
      )}
    >
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          {icon}
          <h2 className="truncate text-sm font-semibold text-[var(--text-primary)]">
            {title}
          </h2>
        </div>
        {description && (
          <p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--text-muted)]">
            {description}
          </p>
        )}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}

function WorkflowStatChip({
  label,
  value,
  tone = 'muted',
  mono = false,
}: {
  label: string
  value: ReactNode
  tone?: 'muted' | 'accent' | 'warning'
  mono?: boolean
}) {
  return (
    <div className="inline-flex min-w-0 items-center gap-2 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/50 px-2.5 py-1.5 text-xs">
      <span className="text-[var(--text-muted)]">{label}</span>
      <span
        className={cn(
          'min-w-0 truncate font-medium text-[var(--text-primary)]',
          mono && 'font-mono',
          tone === 'accent' && 'text-[var(--accent-strong)]',
          tone === 'warning' && 'text-amber-400',
        )}
      >
        {value}
      </span>
    </div>
  )
}

function WorkflowInputFields({
  sections,
  value,
  onChange,
}: {
  sections: WorkflowUiSection[]
  value: Record<string, unknown>
  onChange: (path: string, nextValue: unknown) => void
}) {
  if (sections.length === 0) return null

  const renderField = (field: WorkflowUiField) => {
    const currentValue = getPathValue(value, field.path)
    if (field.type === 'boolean') {
      return (
        <label
          key={field.path}
          className="flex min-h-9 items-start gap-2 rounded-lg border border-[var(--border-soft)] bg-[var(--bg-input)] px-3 py-2 text-sm text-[var(--text-secondary)] transition-colors hover:border-[var(--accent-edge)] hover:text-[var(--text-primary)]"
        >
          <input
            className="checkbox-accent mt-0.5"
            type="checkbox"
            checked={boolFromValue(currentValue)}
            onChange={(event) => {
              const checked = event.target.checked
              onChange(field.path, typeof currentValue === 'string' ? String(checked) : checked)
            }}
          />
          <span>
            <span className="block">{field.label}</span>
            {field.helper && <span className="mt-0.5 block text-[11px] text-[var(--text-muted)]">{field.helper}</span>}
          </span>
        </label>
      )
    }
    return (
      <label key={field.path} className="block">
        <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
          {field.label}
        </span>
        {field.type === 'select' ? (
          <select
            className="control-surface control-surface-compact"
            value={String(currentValue ?? '')}
            onChange={(event) => onChange(field.path, selectValueForField(field, event.target.value))}
          >
            {(field.options || []).map((option) => (
              <option key={`${field.path}:${String(option.value)}`} value={String(option.value)}>
                {option.label}
              </option>
            ))}
          </select>
        ) : (
          <input
            className="control-surface control-surface-compact"
            type={field.type === 'number' ? 'number' : 'text'}
            min={field.min}
            max={field.max}
            placeholder={field.placeholder || ''}
            value={String(currentValue ?? '')}
            onChange={(event) => {
              if (field.type === 'number') {
                onChange(field.path, event.target.value === '' ? null : Number(event.target.value))
                return
              }
              onChange(field.path, event.target.value)
            }}
          />
        )}
        {field.helper && <span className="mt-1 block text-[11px] text-[var(--text-muted)]">{field.helper}</span>}
      </label>
    )
  }

  return (
    <div className="overflow-hidden rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/25">
      {sections.map((section) => {
        const fields = section.fields || []
        const primaryFields = fields.filter((field) => !field.advanced)
        const advancedFields = fields.filter((field) => field.advanced)
        return (
          <div key={section.title} className="space-y-3 border-b border-[var(--border-soft)] p-3 last:border-b-0">
            <div>
              <h3 className="text-xs font-semibold text-[var(--text-primary)]">{section.title}</h3>
              {section.description && (
                <p className="mt-1 max-w-[65ch] text-xs leading-5 text-[var(--text-muted)]">{section.description}</p>
              )}
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              {primaryFields.map(renderField)}
            </div>
            {advancedFields.length > 0 && (
              <details className="rounded-lg border border-dashed border-[var(--border-soft)] bg-[var(--bg-input)] px-3 py-2">
                <summary className="cursor-pointer select-none text-xs font-medium text-[var(--text-secondary)]">
                  高级设置（通常无需配置）
                </summary>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  {advancedFields.map(renderField)}
                </div>
              </details>
            )}
          </div>
        )
      })}
    </div>
  )
}

function csvToList(value: string) {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function workflowStepsFromTemplate(template: Record<string, unknown> | null) {
  const steps = template?.steps
  return Array.isArray(steps) ? steps.filter(isPlainObject) : []
}

function TemplateDefinitionEditor({
  value,
  adapters,
  onChange,
  setError,
  t,
}: {
  value: string
  adapters: WorkflowAdapter[]
  onChange: (value: string) => void
  setError: (value: string) => void
  t: (key: TranslationKey, params?: Record<string, string | number>) => string
}) {
  const template = useMemo(() => {
    try {
      return parseJsonObject(value)
    } catch {
      return null
    }
  }, [value])
  const steps = workflowStepsFromTemplate(template)

  const updateTemplate = (updater: (draft: Record<string, unknown>) => Record<string, unknown>) => {
    try {
      const current = parseJsonObject(value)
      const next = updater(cloneRecord(current))
      onChange(formatJson(next))
      setError('')
    } catch (exc: unknown) {
      setError(exc instanceof Error ? exc.message : 'Invalid template JSON')
    }
  }

  const patchRoot = (patch: Record<string, unknown>) => {
    updateTemplate((draft) => ({ ...draft, ...patch }))
  }

  const patchStep = (index: number, patch: Record<string, unknown>) => {
    updateTemplate((draft) => {
      const nextSteps = workflowStepsFromTemplate(draft).map((step) => ({ ...step }))
      nextSteps[index] = { ...nextSteps[index], ...patch }
      draft.steps = nextSteps
      return draft
    })
  }

  const updateStepJsonField = (index: number, field: 'input' | 'if', text: string) => {
    try {
      const parsed = parseJsonObject(text)
      patchStep(index, { [field]: parsed })
    } catch {
      setError(t('workflows.invalidJson'))
    }
  }

  const addStep = () => {
    updateTemplate((draft) => {
      const nextSteps = workflowStepsFromTemplate(draft).map((step) => ({ ...step }))
      const id = `step_${nextSteps.length + 1}`
      nextSteps.push({
        id,
        name: id,
        uses: adapters[0]?.key || '',
        needs: nextSteps.length ? [String(nextSteps[nextSteps.length - 1].id || '')].filter(Boolean) : [],
        input: {},
        max_attempts: 1,
        retry_delay: '30s',
        timeout: '',
        concurrency: 0,
        on_failure: 'fail',
      })
      draft.steps = nextSteps
      return draft
    })
  }

  const removeStep = (index: number) => {
    updateTemplate((draft) => {
      const removedId = String(workflowStepsFromTemplate(draft)[index]?.id || '')
      const nextSteps = workflowStepsFromTemplate(draft)
        .filter((_, currentIndex) => currentIndex !== index)
        .map((step) => ({
          ...step,
          needs: Array.isArray(step.needs) ? step.needs.filter((item) => String(item) !== removedId) : [],
        }))
      draft.steps = nextSteps
      return draft
    })
  }

  const moveStep = (index: number, direction: -1 | 1) => {
    updateTemplate((draft) => {
      const nextSteps = workflowStepsFromTemplate(draft).map((step) => ({ ...step }))
      const target = index + direction
      if (target < 0 || target >= nextSteps.length) return draft
      const current = nextSteps[index]
      nextSteps[index] = nextSteps[target]
      nextSteps[target] = current
      draft.steps = nextSteps
      return draft
    })
  }

  if (!template) {
    return null
  }

  return (
    <div className="space-y-3 rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/25 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-xs font-semibold text-[var(--text-primary)]">
          {t('workflows.templateStepEditor')}
        </h3>
        <Button variant="outline" size="sm" onClick={addStep}>
          {t('workflows.addStep')}
        </Button>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">Key</span>
          <input
            className="control-surface control-surface-compact"
            value={String(template.key || '')}
            onChange={(event) => patchRoot({ key: event.target.value })}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">Version</span>
          <input
            className="control-surface control-surface-compact"
            type="number"
            min={1}
            value={Number(template.version || 1)}
            onChange={(event) => patchRoot({ version: Number(event.target.value || 1) })}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">Name</span>
          <input
            className="control-surface control-surface-compact"
            value={String(template.name || '')}
            onChange={(event) => patchRoot({ name: event.target.value })}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
            {t('workflows.stuckAfter')}
          </span>
          <input
            className="control-surface control-surface-compact"
            value={String(template.stuck_after || '30m')}
            onChange={(event) => patchRoot({ stuck_after: event.target.value })}
          />
        </label>
      </div>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">Description</span>
        <input
          className="control-surface control-surface-compact"
          value={String(template.description || '')}
          onChange={(event) => patchRoot({ description: event.target.value })}
        />
      </label>

      <div className="grid gap-2 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
            {t('workflows.adapterLimit')}
          </span>
          <textarea
            key={`adapter-limits:${String(template.key || '')}`}
            className="control-surface control-surface-mono min-h-20 resize-y"
            defaultValue={formatJson(isPlainObject(template.limits) ? template.limits.adapters || {} : {})}
            onBlur={(event) => {
              try {
                const parsed = parseJsonObject(event.target.value)
                patchRoot({
                  limits: {
                    ...(isPlainObject(template.limits) ? template.limits : {}),
                    adapters: parsed,
                  },
                })
              } catch {
                setError(t('workflows.invalidJson'))
              }
            }}
            spellCheck={false}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
            {t('workflows.stepConcurrency')}
          </span>
          <textarea
            key={`step-limits:${String(template.key || '')}`}
            className="control-surface control-surface-mono min-h-20 resize-y"
            defaultValue={formatJson(isPlainObject(template.limits) ? template.limits.steps || {} : {})}
            onBlur={(event) => {
              try {
                const parsed = parseJsonObject(event.target.value)
                patchRoot({
                  limits: {
                    ...(isPlainObject(template.limits) ? template.limits : {}),
                    steps: parsed,
                  },
                })
              } catch {
                setError(t('workflows.invalidJson'))
              }
            }}
            spellCheck={false}
          />
        </label>
      </div>

      <div className="space-y-2">
        {steps.map((step, index) => (
          <div key={`${String(step.id || '')}:${index}`} className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-card)] p-3">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-[var(--text-primary)]">
                  {String(step.name || step.id || `step ${index + 1}`)}
                </p>
                <p className="font-mono text-xs text-[var(--text-muted)]">{String(step.uses || '')}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" onClick={() => moveStep(index, -1)} disabled={index === 0}>
                  {t('workflows.moveStepUp')}
                </Button>
                <Button variant="outline" size="sm" onClick={() => moveStep(index, 1)} disabled={index >= steps.length - 1}>
                  {t('workflows.moveStepDown')}
                </Button>
                <Button variant="outline" size="sm" onClick={() => removeStep(index)} className="text-red-400 hover:text-red-300">
                  {t('workflows.removeStep')}
                </Button>
              </div>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.stepId')}
                </span>
                <input
                  className="control-surface control-surface-compact"
                  value={String(step.id || '')}
                  onChange={(event) => patchStep(index, { id: event.target.value })}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.stepName')}
                </span>
                <input
                  className="control-surface control-surface-compact"
                  value={String(step.name || '')}
                  onChange={(event) => patchStep(index, { name: event.target.value })}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.stepAdapter')}
                </span>
                <select
                  className="control-surface control-surface-compact"
                  value={String(step.uses || '')}
                  onChange={(event) => patchStep(index, { uses: event.target.value })}
                >
                  <option value="">{t('common.unknown')}</option>
                  {adapters.map((adapter) => (
                    <option key={`${String(step.id)}:${adapter.key}`} value={adapter.key}>
                      {adapter.key}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.stepNeeds')}
                </span>
                <input
                  className="control-surface control-surface-compact"
                  placeholder={t('workflows.stepNeedsHint')}
                  value={Array.isArray(step.needs) ? step.needs.join(', ') : ''}
                  onChange={(event) => patchStep(index, { needs: csvToList(event.target.value) })}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.onFailure')}
                </span>
                <select
                  className="control-surface control-surface-compact"
                  value={String(step.on_failure || 'fail')}
                  onChange={(event) => patchStep(index, { on_failure: event.target.value })}
                >
                  <option value="fail">{t('workflows.onFailure.fail')}</option>
                  <option value="needs_attention">{t('workflows.onFailure.needs_attention')}</option>
                  <option value="skip">{t('workflows.onFailure.skip')}</option>
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.stepConcurrency')}
                </span>
                <input
                  className="control-surface control-surface-compact"
                  type="number"
                  min={0}
                  max={200}
                  value={Number(step.concurrency || 0)}
                  onChange={(event) => patchStep(index, { concurrency: Number(event.target.value || 0) })}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.retryDelay')}
                </span>
                <input
                  className="control-surface control-surface-compact"
                  value={String(step.retry_delay || '30s')}
                  onChange={(event) => patchStep(index, { retry_delay: event.target.value })}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.timeout')}
                </span>
                <input
                  className="control-surface control-surface-compact"
                  value={String(step.timeout || '')}
                  onChange={(event) => patchStep(index, { timeout: event.target.value })}
                />
              </label>
            </div>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.stepInputMapping')}
                </span>
                <textarea
                  key={`${String(step.id)}:input`}
                  className="control-surface control-surface-mono min-h-24 resize-y"
                  defaultValue={formatJson(isPlainObject(step.input) ? step.input : {})}
                  onBlur={(event) => updateStepJsonField(index, 'input', event.target.value)}
                  spellCheck={false}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.stepCondition')}
                </span>
                <textarea
                  key={`${String(step.id)}:if`}
                  className="control-surface control-surface-mono min-h-24 resize-y"
                  defaultValue={formatJson(isPlainObject(step.if) ? step.if : {})}
                  onBlur={(event) => {
                    const trimmed = event.target.value.trim()
                    if (!trimmed || trimmed === '{}') {
                      patchStep(index, { if: undefined })
                      return
                    }
                    updateStepJsonField(index, 'if', event.target.value)
                  }}
                  spellCheck={false}
                />
              </label>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Workflows() {
  const { t, language } = useI18n()
  const [definitions, setDefinitions] = useState<WorkflowDefinition[]>([])
  const [adapters, setAdapters] = useState<WorkflowAdapter[]>([])
  const [definitionKey, setDefinitionKey] = useState('')
  const [definitionFilter, setDefinitionFilter] = useState('')
  const [status, setStatus] = useState('')
  const [runs, setRuns] = useState<WorkflowRun[]>([])
  const [batches, setBatches] = useState<WorkflowBatch[]>([])
  const [batchTotal, setBatchTotal] = useState(0)
  const [selectedBatchId, setSelectedBatchId] = useState('')
  const [selectedBatchSummary, setSelectedBatchSummary] = useState<WorkflowBatchSummary | null>(null)
  const [total, setTotal] = useState(0)
  const [running, setRunning] = useState(0)
  const [offset, setOffset] = useState(0)
  const [selectedRunId, setSelectedRunId] = useState('')
  const [selectedRun, setSelectedRun] = useState<WorkflowRun | null>(null)
  const [runSummary, setRunSummary] = useState<WorkflowRunSummary | null>(null)
  const [events, setEvents] = useState<WorkflowEvent[]>([])
  const [inputObject, setInputObject] = useState<Record<string, unknown>>(
    cloneRecord(DEFAULT_REGISTER_CODEX_PUSH_INPUT),
  )
  const [inputText, setInputText] = useState(formatJson(DEFAULT_REGISTER_CODEX_PUSH_INPUT))
  const [inputError, setInputError] = useState('')
  const [presetData, setPresetData] = useState<WorkflowInputPresetCollection | null>(null)
  const [presetSelection, setPresetSelection] = useState('template')
  const [presetDirty, setPresetDirty] = useState(false)
  const [presetBusy, setPresetBusy] = useState(false)
  const [presetMessage, setPresetMessage] = useState('')
  const [templateText, setTemplateText] = useState('{}')
  const [templateError, setTemplateError] = useState('')
  const [templateMessage, setTemplateMessage] = useState('')
  const [savingTemplate, setSavingTemplate] = useState(false)
  const [batchError, setBatchError] = useState('')
  const [batchItemsText, setBatchItemsText] = useState('')
  const [batchCount, setBatchCount] = useState(5)
  const [batchConcurrency, setBatchConcurrency] = useState(1)
  const [launchMode, setLaunchMode] = useState<LaunchMode>('single')
  const [stepInputText, setStepInputText] = useState('{}')
  const [stepInputError, setStepInputError] = useState('')
  const [editingStepId, setEditingStepId] = useState('')
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [batchActionId, setBatchActionId] = useState('')
  const [actionId, setActionId] = useState('')
  const [error, setError] = useState('')

  const selectedDefinition = useMemo(
    () => definitions.find((item) => item.key === definitionKey) || definitions[0],
    [definitionKey, definitions],
  )

  const uiSections = useMemo(() => normalizeUiSections(selectedDefinition), [selectedDefinition])
  const selectedBatch = useMemo(
    () => batches.find((item) => item.id === selectedBatchId) || null,
    [batches, selectedBatchId],
  )
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1
  const selectedBatchStatus = selectedBatchSummary?.status || selectedBatch?.status || ''
  const selectedBatchTerminal = Boolean(selectedBatchSummary?.terminal || selectedBatch?.terminal)
  const selectedInputPreset = useMemo(() => {
    if (!presetSelection.startsWith('preset:')) return null
    const presetId = Number(presetSelection.slice('preset:'.length) || 0)
    return presetData?.items.find((item) => item.id === presetId) || null
  }, [presetData, presetSelection])

  const loadDefinitions = useCallback(async () => {
    const items = await fetchWorkflowDefinitions()
    setDefinitions(items)
    setDefinitionKey((current) => current || items[0]?.key || '')
  }, [])

  const loadAdapters = useCallback(async () => {
    setAdapters(await fetchWorkflowAdapters())
  }, [])

  const loadBatches = useCallback(async () => {
    const data = await fetchWorkflowBatches({
      limit: BATCH_PAGE_SIZE,
      offset: 0,
      definition_key: definitionFilter,
    })
    setBatches(data.items || [])
    setBatchTotal(Number(data.total || 0))
  }, [definitionFilter])

  const loadBatchSummary = useCallback(async () => {
    if (!selectedBatchId) {
      setSelectedBatchSummary(null)
      return
    }
    try {
      setSelectedBatchSummary(await fetchWorkflowBatchSummary(selectedBatchId))
    } catch {
      setSelectedBatchSummary(null)
    }
  }, [selectedBatchId])

  const loadRuns = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await fetchWorkflowRuns({
        limit: PAGE_SIZE,
        offset,
        status,
        definition_key: definitionFilter,
        batch_id: selectedBatchId,
      })
      setRuns(data.items || [])
      setTotal(Number(data.total || 0))
      setRunning(Number(data.running || 0))
      setSelectedRunId((current) => {
        if (current && data.items?.some((item) => item.id === current)) return current
        return data.items?.[0]?.id || ''
      })
    } catch (exc: unknown) {
      setError(exc instanceof Error ? exc.message : t('workflows.loadFailed'))
    } finally {
      setLoading(false)
    }
  }, [definitionFilter, offset, selectedBatchId, status, t])

  const loadSelectedRun = useCallback(async () => {
    if (!selectedRunId) {
      setSelectedRun(null)
      setRunSummary(null)
      setEvents([])
      return
    }
    try {
      const [run, eventData, summary] = await Promise.all([
        fetchWorkflowRun(selectedRunId),
        fetchWorkflowEvents(selectedRunId),
        fetchWorkflowRunSummary(selectedRunId),
      ])
      setSelectedRun(run)
      setRunSummary(summary)
      setEvents(eventData.items || [])
    } catch (exc: unknown) {
      setSelectedRun(null)
      setRunSummary(null)
      setEvents([])
      setError(exc instanceof Error ? exc.message : t('workflows.loadFailed'))
    }
  }, [selectedRunId, t])

  useEffect(() => {
    loadDefinitions().catch((exc: unknown) => {
      setError(exc instanceof Error ? exc.message : t('workflows.loadFailed'))
    })
  }, [loadDefinitions, t])

  useEffect(() => {
    loadAdapters().catch(() => undefined)
  }, [loadAdapters])

  useEffect(() => {
    loadBatches().catch(() => undefined)
  }, [loadBatches])

  useEffect(() => {
    loadRuns()
  }, [loadRuns])

  useEffect(() => {
    loadBatchSummary()
  }, [loadBatchSummary])

  useEffect(() => {
    loadSelectedRun()
  }, [loadSelectedRun])

  useEffect(() => {
    if (!selectedDefinition) return
    let cancelled = false
    const next = workflowInputForDefinition(selectedDefinition)
    setInputObject(next)
    setInputText(formatJson(next))
    setInputError('')
    setLaunchMode('single')
    setBatchConcurrency(1)
    setBatchCount(5)
    setPresetData(null)
    setPresetSelection('template')
    setPresetDirty(false)
    setPresetMessage('')
    setTemplateText(formatJson(selectedDefinition.definition || {}))
    setTemplateError('')
    setTemplateMessage('')
    fetchWorkflowInputPresets(selectedDefinition.key, selectedDefinition.version)
      .then((data) => {
        if (cancelled) return
        setPresetData(data)
        const preferred = data.items.find((item) => item.id === data.default_id) || data.last_used
        if (!preferred) return
        setInputObject(cloneRecord(preferred.input))
        setInputText(formatJson(preferred.input))
        setLaunchMode(preferred.launch_mode)
        setBatchConcurrency(preferred.batch_concurrency)
        setBatchCount(preferred.batch_count)
        setPresetSelection(preferred.is_last_used ? 'last' : `preset:${preferred.id}`)
        setPresetDirty(false)
        if (preferred.version_mismatch) {
          setPresetMessage(`该配置保存于 v${preferred.definition_version}，已与当前 v${preferred.current_definition_version} 默认值合并`)
        }
      })
      .catch((exc: unknown) => {
        if (!cancelled) setPresetMessage(exc instanceof Error ? `读取运行配置失败：${exc.message}` : '读取运行配置失败')
      })
    return () => {
      cancelled = true
    }
  }, [selectedDefinition])

  useEffect(() => {
    const hasActiveRun =
      running > 0 ||
      runs.some((item) => CANCELLABLE_WORKFLOW_STATUSES.has(item.status)) ||
      batches.some((item) => CANCELLABLE_WORKFLOW_STATUSES.has(item.status)) ||
      (selectedRun && CANCELLABLE_WORKFLOW_STATUSES.has(selectedRun.status))
    if (!hasActiveRun) return
    const timer = window.setInterval(() => {
      loadRuns()
      loadBatches()
      loadBatchSummary()
      loadSelectedRun()
    }, 2500)
    return () => window.clearInterval(timer)
  }, [batches, loadBatchSummary, loadBatches, loadRuns, loadSelectedRun, running, runs, selectedRun])

  const setInputFromObject = (
    next: Record<string, unknown>,
    options: { markDirty?: boolean } = {},
  ) => {
    const markDirty = options.markDirty ?? true
    setInputObject(next)
    setInputText(formatJson(next))
    setInputError('')
    setBatchError('')
    if (markDirty) setPresetDirty(true)
  }

  const handleJsonChange = (nextText: string) => {
    setInputText(nextText)
    try {
      setInputObject(parseJsonObject(nextText))
      setInputError('')
      setPresetDirty(true)
    } catch {
      setInputError(t('workflows.invalidJson'))
    }
  }

  const updateInputField = (path: string, nextValue: unknown) => {
    setInputFromObject(setPathValue(inputObject, path, nextValue))
  }

  const resetInput = () => {
    const templateInput = presetData?.template_input || workflowInputForDefinition(selectedDefinition)
    setInputFromObject(cloneRecord(templateInput), { markDirty: false })
    setLaunchMode('single')
    setBatchConcurrency(1)
    setBatchCount(5)
    setPresetSelection('template')
    setPresetDirty(false)
    setPresetMessage('已恢复模板默认值')
  }

  const applyInputPreset = (selection: string) => {
    if (!selectedDefinition) return
    let preset: WorkflowInputPreset | null = null
    if (selection === 'last') {
      preset = presetData?.last_used || null
    } else if (selection.startsWith('preset:')) {
      const presetId = Number(selection.slice('preset:'.length) || 0)
      preset = presetData?.items.find((item) => item.id === presetId) || null
    }
    if (preset) {
      setInputFromObject(cloneRecord(preset.input), { markDirty: false })
      setLaunchMode(preset.launch_mode)
      setBatchConcurrency(preset.batch_concurrency)
      setBatchCount(preset.batch_count)
      setPresetSelection(selection)
      setPresetDirty(false)
      setPresetMessage(
        preset.version_mismatch
          ? `该配置保存于 v${preset.definition_version}，已与当前 v${preset.current_definition_version} 默认值合并`
          : '',
      )
      return
    }
    resetInput()
  }

  const currentPresetPayload = (): WorkflowInputPresetPayload => {
    if (!selectedDefinition) throw new Error('请先选择工作流')
    return {
      definition_version: selectedDefinition.version,
      input: parseJsonObject(inputText),
      launch_mode: launchMode,
      batch_concurrency: batchConcurrency,
      batch_count: batchCount,
    }
  }

  const reloadPresetData = async () => {
    if (!selectedDefinition) return null
    const data = await fetchWorkflowInputPresets(selectedDefinition.key, selectedDefinition.version)
    setPresetData(data)
    return data
  }

  const saveInputPreset = async (saveAs: boolean) => {
    if (!selectedDefinition || presetBusy) return
    const existing = saveAs ? null : selectedInputPreset
    const requestedName = existing?.name || window.prompt('运行配置名称', '')?.trim()
    if (!requestedName) return
    setPresetBusy(true)
    setPresetMessage('')
    try {
      const payload = {
        ...currentPresetPayload(),
        name: requestedName,
        is_default: existing?.is_default ?? false,
      }
      const saved = existing
        ? await updateWorkflowInputPreset(selectedDefinition.key, existing.id, payload)
        : await createWorkflowInputPreset(selectedDefinition.key, payload)
      await reloadPresetData()
      setPresetSelection(`preset:${saved.id}`)
      setPresetDirty(false)
      setPresetMessage(`运行配置“${saved.name}”已保存`)
    } catch (exc: unknown) {
      setPresetMessage(exc instanceof Error ? `保存失败：${exc.message}` : '保存失败')
    } finally {
      setPresetBusy(false)
    }
  }

  const setDefaultInputPreset = async () => {
    if (!selectedDefinition || !selectedInputPreset || presetBusy) return
    setPresetBusy(true)
    setPresetMessage('')
    try {
      const saved = await updateWorkflowInputPreset(selectedDefinition.key, selectedInputPreset.id, {
        ...currentPresetPayload(),
        name: selectedInputPreset.name,
        is_default: true,
      })
      await reloadPresetData()
      setPresetSelection(`preset:${saved.id}`)
      setPresetDirty(false)
      setPresetMessage(`“${saved.name}”已设为默认配置`)
    } catch (exc: unknown) {
      setPresetMessage(exc instanceof Error ? `设置默认失败：${exc.message}` : '设置默认失败')
    } finally {
      setPresetBusy(false)
    }
  }

  const removeInputPreset = async () => {
    if (!selectedDefinition || !selectedInputPreset || presetBusy) return
    if (!window.confirm(`确认删除运行配置“${selectedInputPreset.name}”？`)) return
    setPresetBusy(true)
    setPresetMessage('')
    try {
      await deleteWorkflowInputPreset(selectedDefinition.key, selectedInputPreset.id)
      const data = await reloadPresetData()
      const preferred = data?.items.find((item) => item.id === data.default_id) || data?.last_used || null
      if (preferred) {
        setInputFromObject(cloneRecord(preferred.input), { markDirty: false })
        setLaunchMode(preferred.launch_mode)
        setBatchConcurrency(preferred.batch_concurrency)
        setBatchCount(preferred.batch_count)
        setPresetSelection(preferred.is_last_used ? 'last' : `preset:${preferred.id}`)
      } else {
        resetInput()
      }
      setPresetDirty(false)
      setPresetMessage('运行配置已删除')
    } catch (exc: unknown) {
      setPresetMessage(exc instanceof Error ? `删除失败：${exc.message}` : '删除失败')
    } finally {
      setPresetBusy(false)
    }
  }

  const resetTemplate = () => {
    setTemplateText(formatJson(selectedDefinition?.definition || {}))
    setTemplateError('')
    setTemplateMessage('')
  }

  const saveTemplate = async () => {
    setTemplateError('')
    setTemplateMessage('')
    setSavingTemplate(true)
    try {
      const definition = parseJsonObject(templateText)
      const saved = await saveWorkflowDefinition(definition)
      setTemplateMessage(t('workflows.templateSaved'))
      await loadDefinitions()
      setDefinitionKey(saved.key)
    } catch (exc: unknown) {
      setTemplateError(exc instanceof Error ? exc.message : t('workflows.invalidJson'))
    } finally {
      setSavingTemplate(false)
    }
  }

  const refreshWorkflowPanels = async () => {
    await Promise.all([loadBatches(), loadBatchSummary(), loadRuns(), loadSelectedRun()])
  }

  const parseBatchItems = (baseInput: Record<string, unknown>) => {
    const trimmed = batchItemsText.trim()
    if (trimmed) {
      return parseJsonArray(trimmed).map((raw, index) => {
        if (!isPlainObject(raw)) {
          throw new Error(t('workflows.batchJsonInvalid'))
        }
        const itemInput = isPlainObject(raw.input) ? raw.input : raw
        return {
          name: String(raw.name || `${selectedDefinition?.name || selectedDefinition?.key || 'workflow'} #${index + 1}`),
          input: cloneRecord(itemInput),
          metadata: isPlainObject(raw.metadata) ? cloneRecord(raw.metadata) : { source: 'ui_batch_json' },
        }
      })
    }
    const count = Math.min(Math.max(Number(batchCount || 1), 1), 200)
    return Array.from({ length: count }, (_, index) => ({
      name: `${selectedDefinition?.name || selectedDefinition?.key || 'workflow'} #${index + 1}`,
      input: cloneRecord(baseInput),
      metadata: { source: 'ui_duplicate', index: index + 1 },
    }))
  }

  const startRun = async () => {
    if (!selectedDefinition) return
    setInputError('')
    setBatchError('')
    setSubmitting(true)
    try {
      const parsedInput = parseJsonObject(inputText)
      if (launchMode === 'batch') {
        let batchItems: ReturnType<typeof parseBatchItems>
        try {
          batchItems = parseBatchItems(parsedInput)
        } catch (exc: unknown) {
          setBatchError(exc instanceof Error ? exc.message : t('workflows.batchJsonInvalid'))
          return
        }
        const batch = await createWorkflowBatch({
          definition_key: selectedDefinition.key,
          version: selectedDefinition.version,
          concurrency: batchConcurrency,
          items: batchItems,
        })
        setSelectedBatchId(batch.id)
        setSelectedRunId(batch.runs?.[0]?.id || '')
        setSelectedRun(batch.runs?.[0] || null)
      } else {
        const run = await createWorkflowRun({
          definition_key: selectedDefinition.key,
          version: selectedDefinition.version,
          input: parsedInput,
        })
        setSelectedBatchId('')
        setSelectedRunId(run.id)
        setSelectedRun(run)
      }
      try {
        await saveLastUsedWorkflowInput(selectedDefinition.key, {
          ...currentPresetPayload(),
          input: parsedInput,
        })
        await reloadPresetData()
        setPresetSelection('last')
        setPresetDirty(false)
        setPresetMessage('已自动记住本次运行配置')
      } catch (exc: unknown) {
        setPresetMessage(exc instanceof Error ? `任务已启动，但记住配置失败：${exc.message}` : '任务已启动，但记住配置失败')
      }
      setOffset(0)
      await Promise.all([loadBatches(), loadRuns()])
    } catch (exc: unknown) {
      setInputError(exc instanceof Error ? exc.message : t('workflows.invalidJson'))
    } finally {
      setSubmitting(false)
    }
  }

  const pauseBatch = async () => {
    if (!selectedBatchId) return
    setBatchActionId('pause')
    try {
      await pauseWorkflowBatch(selectedBatchId)
      await refreshWorkflowPanels()
    } finally {
      setBatchActionId('')
    }
  }

  const resumeBatch = async () => {
    if (!selectedBatchId) return
    setBatchActionId('resume')
    try {
      await resumeWorkflowBatch(selectedBatchId)
      await refreshWorkflowPanels()
    } finally {
      setBatchActionId('')
    }
  }

  const cancelBatch = async () => {
    if (!selectedBatchId) return
    setBatchActionId('cancel')
    try {
      await cancelWorkflowBatch(selectedBatchId)
      await refreshWorkflowPanels()
    } finally {
      setBatchActionId('')
    }
  }

  const retryFailedBatch = async () => {
    if (!selectedBatchId) return
    setBatchActionId('retry')
    try {
      await retryFailedWorkflowBatch(selectedBatchId)
      await refreshWorkflowPanels()
    } finally {
      setBatchActionId('')
    }
  }

  const exportBatchSummary = () => {
    if (!selectedBatchSummary && !selectedBatch) return
    const payload = selectedBatchSummary || selectedBatch
    const blob = new Blob([formatJson(payload)], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${selectedBatchId || 'workflow-batch'}-summary.json`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  const cancelRun = async (runId: string) => {
    setActionId(runId)
    try {
      const run = await cancelWorkflowRun(runId)
      setSelectedRun(run)
      await Promise.all([loadRuns(), loadBatches(), loadBatchSummary()])
    } finally {
      setActionId('')
    }
  }

  const beginStepEdit = (step: WorkflowStepRun) => {
    setEditingStepId(step.step_id)
    setStepInputText(formatJson(step.input || {}))
    setStepInputError('')
  }

  const retryStep = async (step: WorkflowStepRun, overrideInput?: Record<string, unknown>) => {
    if (!selectedRun) return
    setActionId(`${selectedRun.id}:${step.step_id}`)
    try {
      let updatedRun = selectedRun
      if (overrideInput) {
        updatedRun = await updateWorkflowStepInput(selectedRun.id, step.step_id, overrideInput)
      }
      updatedRun = await retryWorkflowStep(updatedRun.id, step.step_id)
      setSelectedRun(updatedRun)
      setEditingStepId('')
      await Promise.all([loadRuns(), loadBatches(), loadBatchSummary()])
    } finally {
      setActionId('')
    }
  }

  const saveInputAndRetry = async (step: WorkflowStepRun) => {
    setStepInputError('')
    try {
      await retryStep(step, parseJsonObject(stepInputText))
    } catch (exc: unknown) {
      setStepInputError(exc instanceof Error ? exc.message : t('workflows.invalidJson'))
    }
  }

  const resetFilters = () => {
    setStatus('')
    setDefinitionFilter('')
    setSelectedBatchId('')
    setOffset(0)
  }

  const activeBatchSummary = selectedBatchSummary?.summary || selectedBatch?.summary
  const activeBatchObservability = selectedBatchSummary?.observability || selectedBatch?.observability

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-4 py-3 shadow-[var(--shadow-soft)]">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold text-[var(--text-primary)]">
              {t('workflows.title')}
            </h1>
            <p className="mt-1 text-sm text-[var(--text-muted)]">
              {t('workflows.subtitle')}
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              loadRuns()
              loadBatches()
              loadBatchSummary()
              loadSelectedRun()
            }}
            disabled={loading}
          >
            <RefreshCw className={cn('mr-1.5 h-4 w-4', loading && 'animate-spin')} />
            {t('common.refresh')}
          </Button>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <WorkflowStatChip label={t('workflows.metric.definitions')} value={definitions.length} />
          <WorkflowStatChip
            label={t('workflows.metric.running')}
            value={running}
            tone={running > 0 ? 'accent' : 'muted'}
          />
          <WorkflowStatChip label={t('workflows.metric.batches')} value={batchTotal} />
          <WorkflowStatChip
            label={t('workflows.metric.selected')}
            value={selectedDefinition?.name || '-'}
          />
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      <div className="grid gap-4 2xl:grid-cols-[420px_minmax(0,1fr)]">
        <div className="space-y-4 2xl:sticky 2xl:top-4 2xl:max-h-[calc(100vh-2rem)] 2xl:self-start 2xl:overflow-auto">
          <WorkflowPanel>
            <WorkflowPanelHeader
              title={t('workflows.startTitle')}
              description="选择模板、套用配置，确认后启动单次或批量任务。"
              icon={<Play className="h-4 w-4 text-[var(--accent-strong)]" />}
            />
            <div className="space-y-3 p-3">
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.definition')}
                </span>
                <select
                  className="control-surface"
                  value={selectedDefinition?.key || ''}
                  onChange={(event) => setDefinitionKey(event.target.value)}
                >
                  {definitions.map((definition) => (
                    <option key={`${definition.key}:${definition.version}`} value={definition.key}>
                      {definition.name} v{definition.version}
                    </option>
                  ))}
                </select>
              </label>
              {selectedDefinition?.description && (
                <p className="text-xs leading-5 text-[var(--text-muted)]">
                  {selectedDefinition.description}
                </p>
              )}

              <div className="space-y-2 rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/35 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-[var(--text-primary)]">运行配置</span>
                  <span className={cn(
                    'text-[11px]',
                    presetDirty ? 'text-amber-500' : 'text-[var(--text-muted)]',
                  )}>
                    {presetDirty ? '有未保存修改' : '已保存'}
                  </span>
                </div>
                <select
                  className="control-surface control-surface-compact"
                  value={presetSelection}
                  disabled={presetBusy}
                  onChange={(event) => applyInputPreset(event.target.value)}
                >
                  <option value="template">模板默认值</option>
                  {presetData?.last_used && <option value="last">上次使用</option>}
                  {(presetData?.items || []).map((preset) => (
                    <option key={preset.id} value={`preset:${preset.id}`}>
                      {preset.is_default ? '★ ' : ''}{preset.name}{preset.version_mismatch ? `（v${preset.definition_version}）` : ''}
                    </option>
                  ))}
                </select>
                <div className="flex flex-wrap gap-2">
                  <Button variant="outline" size="sm" disabled={presetBusy || Boolean(inputError)} onClick={() => void saveInputPreset(false)}>
                    <Save className="mr-1 h-3.5 w-3.5" /> 保存
                  </Button>
                  <Button variant="outline" size="sm" disabled={presetBusy || Boolean(inputError)} onClick={() => void saveInputPreset(true)}>
                    另存为
                  </Button>
                  <Button variant="outline" size="sm" disabled={presetBusy || !selectedInputPreset || selectedInputPreset.is_default} onClick={() => void setDefaultInputPreset()}>
                    设为默认
                  </Button>
                  <Button variant="outline" size="sm" disabled={presetBusy || !selectedInputPreset} onClick={() => void removeInputPreset()} className="text-red-400 hover:text-red-300">
                    删除
                  </Button>
                  <Button variant="ghost" size="sm" disabled={presetBusy} onClick={resetInput}>
                    恢复模板默认
                  </Button>
                </div>
                {presetMessage && <p className="text-[11px] leading-5 text-[var(--text-muted)]">{presetMessage}</p>}
              </div>

              <WorkflowInputFields
                sections={uiSections}
                value={inputObject}
                onChange={updateInputField}
              />

              <div className="grid gap-2 sm:grid-cols-2">
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-[var(--text-muted)]">
                    {t('workflows.launchMode')}
                  </span>
                  <select
                    className="control-surface control-surface-compact"
                    value={launchMode}
                    onChange={(event) => {
                      setLaunchMode(event.target.value as LaunchMode)
                      setPresetDirty(true)
                    }}
                  >
                    <option value="single">{t('workflows.launchMode.single')}</option>
                    <option value="batch">{t('workflows.launchMode.batch')}</option>
                  </select>
                </label>
                {launchMode === 'batch' && (
                  <label className="block">
                    <span className="mb-1.5 block text-xs font-medium text-[var(--text-muted)]">
                      {t('workflows.batchConcurrency')}
                    </span>
                    <input
                      className="control-surface control-surface-compact"
                      type="number"
                      min={1}
                      max={50}
                      value={batchConcurrency}
                      onChange={(event) => {
                        setBatchConcurrency(Math.min(Math.max(Number(event.target.value || 1), 1), 50))
                        setPresetDirty(true)
                      }}
                    />
                  </label>
                )}
              </div>

              {launchMode === 'batch' && (
                <div className="space-y-2 rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/35 p-3">
                  <div className="grid gap-2 sm:grid-cols-[140px_1fr]">
                    <label className="block">
                      <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                        {t('workflows.batchCount')}
                      </span>
                      <input
                        className="control-surface control-surface-compact"
                        type="number"
                        min={1}
                        max={200}
                        value={batchCount}
                        onChange={(event) => {
                          setBatchCount(Math.min(Math.max(Number(event.target.value || 1), 1), 200))
                          setPresetDirty(true)
                        }}
                      />
                    </label>
                    <div className="text-xs leading-5 text-[var(--text-muted)]">
                      {t('workflows.batchCountHint')}
                    </div>
                  </div>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-[var(--text-muted)]">
                      {t('workflows.batchItemsJson')}
                    </span>
                    <textarea
                      className="control-surface control-surface-mono min-h-28 resize-y"
                      value={batchItemsText}
                      onChange={(event) => {
                        setBatchItemsText(event.target.value)
                        setBatchError('')
                      }}
                      placeholder={t('workflows.batchItemsJsonPlaceholder')}
                      spellCheck={false}
                    />
                  </label>
                  {batchError && <p className="text-xs text-red-400">{batchError}</p>}
                </div>
              )}

              <details className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/20">
                <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
                  {t('workflows.advancedJson')}
                </summary>
                <div className="border-t border-[var(--border-soft)] p-3">
                  <textarea
                    className="control-surface control-surface-mono min-h-48 resize-y"
                    value={inputText}
                    onChange={(event) => handleJsonChange(event.target.value)}
                    spellCheck={false}
                  />
                </div>
              </details>

              {inputError && <p className="text-xs text-red-400">{inputError}</p>}
              <div className="flex items-center justify-between gap-2">
                <Button variant="outline" size="sm" onClick={resetInput}>
                  <RotateCcw className="mr-1.5 h-4 w-4" />
                  {t('workflows.resetInput')}
                </Button>
                <Button onClick={startRun} disabled={!selectedDefinition || submitting || Boolean(inputError || batchError)}>
                  <Play className="mr-1.5 h-4 w-4" />
                  {submitting
                    ? t('workflows.starting')
                    : launchMode === 'batch'
                      ? t('workflows.startBatch')
                      : t('workflows.startRun')}
                </Button>
              </div>
            </div>
          </WorkflowPanel>

          <WorkflowPanel>
            <details>
              <summary className="cursor-pointer border-b border-[var(--border)] px-4 py-3 text-sm font-semibold text-[var(--text-primary)]">
                {t('workflows.templateEditor')}
                <span className="ml-2 text-xs font-normal text-[var(--text-muted)]">仅维护模板时打开</span>
              </summary>
              <div className="space-y-3 p-3">
                <p className="text-xs leading-5 text-[var(--text-muted)]">
                  {t('workflows.templateEditorHint')}
                </p>
                {adapters.length > 0 && (
                  <details className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/25">
                    <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-[var(--text-accent)]">
                      {t('workflows.adapterKeys')}
                    </summary>
                    <div className="flex max-h-32 flex-wrap gap-1 overflow-auto border-t border-[var(--border-soft)] p-2">
                      {adapters.map((adapter) => (
                        <span
                          key={adapter.key}
                          className="rounded bg-[var(--chip-bg)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--text-secondary)]"
                        >
                          {adapter.key}
                        </span>
                      ))}
                    </div>
                  </details>
                )}
                <TemplateDefinitionEditor
                  value={templateText}
                  adapters={adapters}
                  onChange={(nextText) => {
                    setTemplateText(nextText)
                    setTemplateMessage('')
                  }}
                  setError={setTemplateError}
                  t={t}
                />
                <details className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/20">
                  <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
                    {t('workflows.advancedJson')}
                  </summary>
                  <div className="border-t border-[var(--border-soft)] p-3">
                    <textarea
                      className="control-surface control-surface-mono min-h-72 resize-y"
                      value={templateText}
                      onChange={(event) => {
                        setTemplateText(event.target.value)
                        setTemplateError('')
                        setTemplateMessage('')
                      }}
                      spellCheck={false}
                    />
                  </div>
                </details>
                {templateError && <p className="text-xs text-red-400">{templateError}</p>}
                {templateMessage && <p className="text-xs text-emerald-500">{templateMessage}</p>}
                <div className="flex flex-wrap justify-end gap-2">
                  <Button variant="outline" size="sm" onClick={resetTemplate}>
                    <RotateCcw className="mr-1.5 h-4 w-4" />
                    {t('workflows.resetTemplate')}
                  </Button>
                  <Button size="sm" onClick={saveTemplate} disabled={savingTemplate}>
                    <Save className="mr-1.5 h-4 w-4" />
                    {savingTemplate ? t('workflows.savingTemplate') : t('workflows.saveTemplate')}
                  </Button>
                </div>
              </div>
            </details>
          </WorkflowPanel>
        </div>

        <div className="grid gap-4 xl:grid-cols-[minmax(420px,0.95fr)_minmax(480px,1.05fr)]">
          <div className="space-y-4">
          <WorkflowPanel>
            <div className="border-b border-[var(--border)] px-4 py-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <Layers3 className="h-4 w-4 text-[var(--text-muted)]" />
                    <h2 className="text-sm font-semibold text-[var(--text-primary)]">
                      {t('workflows.batches')}
                    </h2>
                  </div>
                  <p className="mt-1 text-xs text-[var(--text-muted)]">
                    最近创建的批量任务，选中后会过滤下方运行记录。
                  </p>
                </div>
                {selectedBatchId && (
                  <div className="flex flex-wrap justify-end gap-2">
                    {selectedBatchStatus === 'paused' ? (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={resumeBatch}
                        disabled={Boolean(batchActionId) || selectedBatchTerminal}
                      >
                        <Play className="mr-1 h-3.5 w-3.5" />
                        {batchActionId === 'resume' ? t('workflows.batchActionRunning') : t('workflows.resumeBatch')}
                      </Button>
                    ) : (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={pauseBatch}
                        disabled={Boolean(batchActionId) || selectedBatchTerminal}
                      >
                        <Pause className="mr-1 h-3.5 w-3.5" />
                        {batchActionId === 'pause' ? t('workflows.batchActionRunning') : t('workflows.pauseBatch')}
                      </Button>
                    )}
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={retryFailedBatch}
                      disabled={
                        Boolean(batchActionId) ||
                        summaryCount(activeBatchSummary, 'failed') + summaryCount(activeBatchSummary, 'needs_attention') + summaryCount(activeBatchSummary, 'cancelled') <= 0
                      }
                    >
                      <RotateCcw className="mr-1 h-3.5 w-3.5" />
                      {batchActionId === 'retry' ? t('workflows.batchActionRunning') : t('workflows.retryFailedBatch')}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={cancelBatch}
                      disabled={Boolean(batchActionId) || selectedBatchTerminal}
                      className="text-amber-500 hover:text-amber-400"
                    >
                      <Square className="mr-1 h-3.5 w-3.5" />
                      {batchActionId === 'cancel' ? t('workflows.batchActionRunning') : t('workflows.cancelBatch')}
                    </Button>
                    <Button variant="outline" size="sm" onClick={exportBatchSummary}>
                      <Download className="mr-1 h-3.5 w-3.5" />
                      {t('workflows.exportBatch')}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setSelectedBatchId('')
                        setOffset(0)
                      }}
                    >
                      {t('workflows.clearBatch')}
                    </Button>
                  </div>
                )}
              </div>
              {activeBatchSummary && (
                <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-3 xl:grid-cols-6">
                  <div className="rounded-md bg-[var(--bg-pane)] px-2 py-1 text-[var(--text-muted)]">
                    {t('common.total')}: {summaryCount(activeBatchSummary, 'total')}
                  </div>
                  <div className="rounded-md bg-[var(--bg-pane)] px-2 py-1 text-emerald-500">
                    {t('common.success')}: {summaryCount(activeBatchSummary, 'succeeded')}
                  </div>
                  <div className="rounded-md bg-[var(--bg-pane)] px-2 py-1 text-red-400">
                    {t('common.failure')}: {summaryCount(activeBatchSummary, 'failed')}
                  </div>
                  <div className="rounded-md bg-[var(--bg-pane)] px-2 py-1 text-amber-400">
                    {t('workflows.needsAttentionShort')}: {summaryCount(activeBatchSummary, 'needs_attention')}
                  </div>
                  <div className="rounded-md bg-[var(--bg-pane)] px-2 py-1 text-[var(--text-muted)]">
                    {t('workflows.duration')}: {formatDuration(activeBatchObservability?.duration_seconds_avg)}
                  </div>
                  <div className={cn(
                    'rounded-md bg-[var(--bg-pane)] px-2 py-1',
                    Number(activeBatchObservability?.stuck || 0) > 0 ? 'text-amber-400' : 'text-[var(--text-muted)]',
                  )}>
                    {t('workflows.stuck')}: {activeBatchObservability?.stuck || 0}
                  </div>
                </div>
              )}
            </div>
            <div className="max-h-56 overflow-auto">
              {batches.length === 0 && (
                <div className="empty-state-panel m-3">{t('workflows.emptyBatches')}</div>
              )}
              {batches.map((batch) => (
                <button
                  key={batch.id}
                  className={cn(
                    'block w-full border-b border-[var(--border-soft)] px-3 py-2 text-left text-sm last:border-b-0 hover:bg-[var(--bg-hover)]',
                    selectedBatchId === batch.id && 'bg-[var(--accent-soft)]',
                  )}
                  onClick={() => {
                    setSelectedBatchId(batch.id)
                    setOffset(0)
                  }}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-medium text-[var(--text-primary)]">{batch.name}</span>
                    <Badge variant={WORKFLOW_STATUS_VARIANTS[batch.status] || 'secondary'}>
                      {getWorkflowStatusText(batch.status, language)}
                    </Badge>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-[var(--text-muted)]">
                    <span>{batch.total} items</span>
                    <span>{t('workflows.batchConcurrency')}: {batch.concurrency}</span>
                    <span className="font-mono">{batch.id}</span>
                  </div>
                </button>
              ))}
            </div>
          </WorkflowPanel>

          <WorkflowPanel>
            <div className="border-b border-[var(--border)] px-4 py-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold text-[var(--text-primary)]">运行记录</h2>
                  <p className="mt-1 text-xs text-[var(--text-muted)]">
                    {selectedBatchId ? '当前只显示选中批次下的运行。' : '按模板和状态筛选最近的工作流运行。'}
                  </p>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <select
                    className="control-surface control-surface-compact w-auto min-w-40"
                    value={definitionFilter}
                    onChange={(event) => {
                      setDefinitionFilter(event.target.value)
                      setOffset(0)
                    }}
                  >
                    <option value="">{t('workflows.allDefinitions')}</option>
                    {definitions.map((definition) => (
                      <option key={`${definition.key}:filter`} value={definition.key}>
                        {definition.name}
                      </option>
                    ))}
                  </select>
                  <select
                    className="control-surface control-surface-compact w-auto min-w-40"
                    value={status}
                    onChange={(event) => {
                      setStatus(event.target.value)
                      setOffset(0)
                    }}
                  >
                    {STATUS_OPTIONS.map((value) => (
                      <option key={value || 'all'} value={value}>
                        {value ? getWorkflowStatusText(value, language) : t('workflows.allStatuses')}
                      </option>
                    ))}
                  </select>
                  {(status || definitionFilter || selectedBatchId) && (
                    <Button variant="ghost" size="sm" onClick={resetFilters}>
                      {t('common.clear')}
                    </Button>
                  )}
                </div>
              </div>
            </div>

            <div className="glass-table-wrap">
              <table className="w-full table-fixed text-sm">
                <thead>
                  <tr className="border-b border-[var(--border)] text-[var(--text-muted)]">
                    <th className="w-[58%] px-3 py-2 text-left">{t('workflows.run')}</th>
                    <th className="w-[18%] px-3 py-2 text-left">{t('common.status')}</th>
                    <th className="w-[24%] px-3 py-2 text-right">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => (
                    <tr
                      key={run.id}
                      tabIndex={0}
                      aria-label={`${t('workflows.viewRun')}: ${run.name || run.definition_key}`}
                      className={cn(
                        'cursor-pointer border-b border-[var(--border-soft)] outline-none hover:bg-[var(--bg-hover)] focus-visible:bg-[var(--bg-hover)] focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent-strong)]',
                        selectedRunId === run.id && 'bg-[var(--accent-soft)]',
                      )}
                      onClick={() => setSelectedRunId(run.id)}
                      onKeyDown={(event) => {
                        if (event.target !== event.currentTarget) return
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          setSelectedRunId(run.id)
                        }
                      }}
                    >
                      <td className="px-3 py-3 align-top">
                        <div className="truncate text-sm font-medium text-[var(--text-primary)]">
                          {run.name || run.definition_key}
                        </div>
                        <div className="mt-1 truncate font-mono text-xs text-[var(--text-muted)]">
                          {run.id}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-[var(--text-muted)]">
                          <span className="truncate">
                            {t('workflows.currentStep')}: {run.current_step_id || '-'}
                          </span>
                          <span>{formatMaybeDate(run.created_at, language)}</span>
                          {run.batch_id && (
                            <span>{t('workflows.batchItem')}: #{run.batch_item_index || 0}</span>
                          )}
                        </div>
                      </td>
                      <td className="px-3 py-3 align-top">
                        <Badge variant={WORKFLOW_STATUS_VARIANTS[run.status] || 'secondary'}>
                          {getWorkflowStatusText(run.status, language)}
                        </Badge>
                      </td>
                      <td className="px-3 py-3 align-top">
                        <div className="flex flex-wrap justify-end gap-2">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={(event) => {
                              event.stopPropagation()
                              setSelectedRunId(run.id)
                            }}
                          >
                            {t('workflows.viewRun')}
                          </Button>
                          {CANCELLABLE_WORKFLOW_STATUSES.has(run.status) && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={(event) => {
                                event.stopPropagation()
                                cancelRun(run.id)
                              }}
                              disabled={actionId === run.id}
                              className="text-amber-500 hover:text-amber-400"
                            >
                              <Square className="mr-1 h-3.5 w-3.5" />
                              {actionId === run.id ? t('workflows.cancelling') : t('common.cancel')}
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!loading && runs.length === 0 && (
              <div className="empty-state-panel m-3">{t('workflows.emptyRuns')}</div>
            )}
            {loading && runs.length === 0 && (
              <div className="empty-state-panel m-3">{t('common.loading')}</div>
            )}
            <div className="flex items-center justify-between gap-3 border-t border-[var(--border)] px-3 py-3 text-xs text-[var(--text-muted)]">
              <span>{t('workflows.page', { current: currentPage, total: pages })}</span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset <= 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  {t('taskHistory.prevPage')}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset + PAGE_SIZE >= total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  {t('taskHistory.nextPage')}
                </Button>
              </div>
            </div>
          </WorkflowPanel>
          </div>

          <WorkflowPanel className="xl:sticky xl:top-4 xl:max-h-[calc(100vh-2rem)] xl:overflow-auto">
          <div className="flex items-center justify-between gap-3 border-b border-[var(--border)] px-4 py-3">
            <div className="min-w-0">
              <h2 className="truncate text-sm font-semibold text-[var(--text-primary)]">
                {selectedRun?.name || t('workflows.runDetail')}
              </h2>
              <p className="mt-0.5 truncate font-mono text-xs text-[var(--text-muted)]">
                {selectedRun?.id || t('workflows.noRunSelected')}
              </p>
            </div>
            {selectedRun && (
              <Badge variant={WORKFLOW_STATUS_VARIANTS[selectedRun.status] || 'secondary'}>
                {getWorkflowStatusText(selectedRun.status, language)}
              </Badge>
            )}
          </div>

          {!selectedRun && (
            <div className="empty-state-panel m-3">{t('workflows.selectRun')}</div>
          )}

          {selectedRun && (
            <div className="space-y-4 p-3">
              {selectedRun.error && (
                <div className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
                  {selectedRun.error}
                </div>
              )}

              {runSummary && (
                <div className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/35 p-3">
                  <div className="grid gap-2 sm:grid-cols-2">
                    <div>
                      <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.displayStatus')}</p>
                      <p className="mt-1 text-sm font-medium text-[var(--text-primary)]">{runSummary.display_status}</p>
                    </div>
                    <div>
                      <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.operatorAction')}</p>
                      <p className="mt-1 text-sm text-[var(--text-secondary)]">{runSummary.operator_action}</p>
                    </div>
                    <div>
                      <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.currentStage')}</p>
                      <p className="mt-1 font-mono text-xs text-[var(--text-secondary)]">{runSummary.current_stage || '-'}</p>
                    </div>
                    <div>
                      <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.accountRef')}</p>
                      <p className="mt-1 truncate text-xs text-[var(--text-secondary)]">
                        {runSummary.email || runSummary.account_id || '-'}
                      </p>
                    </div>
                    <div>
                      <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.duration')}</p>
                      <p className="mt-1 text-xs text-[var(--text-secondary)]">
                        {formatDuration(runSummary.duration_seconds)}
                      </p>
                    </div>
                    <div>
                      <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.stuckStep')}</p>
                      <p className={cn(
                        'mt-1 truncate text-xs',
                        runSummary.stuck ? 'text-amber-400' : 'text-[var(--text-secondary)]',
                      )}>
                        {runSummary.stuck ? `${runSummary.stuck_step_id || '-'}: ${runSummary.stuck_reason}` : '-'}
                      </p>
                    </div>
                  </div>
                </div>
              )}

              <div className="grid gap-2 sm:grid-cols-3">
                <div className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2">
                  <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.createdAt')}</p>
                  <p className="mt-1 text-xs text-[var(--text-secondary)]">
                    {formatMaybeDate(selectedRun.created_at, language)}
                  </p>
                </div>
                <div className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2">
                  <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.startedAt')}</p>
                  <p className="mt-1 text-xs text-[var(--text-secondary)]">
                    {formatMaybeDate(selectedRun.started_at, language)}
                  </p>
                </div>
                <div className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2">
                  <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.finishedAt')}</p>
                  <p className="mt-1 text-xs text-[var(--text-secondary)]">
                    {formatMaybeDate(selectedRun.finished_at, language)}
                  </p>
                </div>
              </div>

              <div>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold text-[var(--text-primary)]">
                    {t('workflows.steps')}
                  </h3>
                  {CANCELLABLE_WORKFLOW_STATUSES.has(selectedRun.status) && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => cancelRun(selectedRun.id)}
                      disabled={actionId === selectedRun.id}
                      className="text-amber-500 hover:text-amber-400"
                    >
                      <Square className="mr-1 h-3.5 w-3.5" />
                      {actionId === selectedRun.id ? t('workflows.cancelling') : t('common.cancel')}
                    </Button>
                  )}
                </div>
                <div className="overflow-hidden rounded-md border border-[var(--border-soft)]">
                  {(selectedRun.steps || []).map((step, index) => {
                    const Icon = stepIcon(step.status)
                    const taskUrl = externalTaskUrl(step)
                    const editing = editingStepId === step.step_id
                    const busy = actionId === `${selectedRun.id}:${step.step_id}`
                    const category = errorCategory(step.error)
                    const hint = operatorHint(step.error)
                    return (
                      <div
                        key={step.id}
                        className={cn(
                          'border-b border-[var(--border-soft)] px-3 py-3 last:border-b-0',
                          step.status === 'needs_attention' && 'bg-amber-500/5',
                        )}
                      >
                        <div className="flex gap-3">
                          <div className="flex flex-col items-center">
                            <div className="flex h-8 w-8 items-center justify-center rounded-md border border-[var(--border)] bg-[var(--bg-pane)]">
                              <Icon
                                className={cn(
                                  'h-4 w-4 text-[var(--text-muted)]',
                                  step.status === 'running' && 'animate-spin text-[var(--accent-strong)]',
                                  step.status === 'succeeded' && 'text-emerald-500',
                                  step.status === 'failed' && 'text-red-500',
                                  step.status === 'needs_attention' && 'text-amber-500',
                                )}
                              />
                            </div>
                            {index < (selectedRun.steps || []).length - 1 && (
                              <div className="my-1 h-8 w-px bg-[var(--border)]" />
                            )}
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-medium text-[var(--text-primary)]">
                                {step.name || step.step_id}
                              </span>
                              <Badge variant={WORKFLOW_STATUS_VARIANTS[step.status] || 'secondary'}>
                                {getWorkflowStatusText(step.status, language)}
                              </Badge>
                              <span className="text-xs text-[var(--text-muted)]">
                                {t('workflows.attempt', {
                                  attempt: step.attempt,
                                  total: step.max_attempts,
                                })}
                              </span>
                            </div>
                            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--text-muted)]">
                              <span>{step.adapter_key}</span>
                              {step.duration_seconds !== undefined && (
                                <span>
                                  {t('workflows.duration')}: {formatDuration(step.duration_seconds)}
                                </span>
                              )}
                              {step.next_run_at && (
                                <span>
                                  {t('workflows.nextRunAt')}: {formatMaybeDate(step.next_run_at, language)}
                                </span>
                              )}
                              {step.timeout_at && (
                                <span>
                                  {t('workflows.timeoutAt')}: {formatMaybeDate(step.timeout_at, language)}
                                </span>
                              )}
                              {taskUrl && (
                                <a
                                  href={taskUrl}
                                  className="inline-flex items-center gap-1 text-[var(--text-accent)] hover:underline"
                                >
                                  {t('workflows.childTask')}
                                  <ExternalLink className="h-3 w-3" />
                                </a>
                              )}
                            </div>
                            {shortError(step.error) && (
                              <p className="mt-2 text-xs text-red-400">{shortError(step.error)}</p>
                            )}
                            {(category || hint) && (
                              <div className="mt-2 rounded-md border border-amber-500/20 bg-amber-500/10 px-2 py-1.5 text-xs text-amber-300">
                                {category && (
                                  <span className="mr-2 font-mono text-amber-200">{category}</span>
                                )}
                                {hint || t('workflows.noOperatorHint')}
                              </div>
                            )}
                            {runSummary?.steps.find((item) => item.step_id === step.step_id)?.stuck && (
                              <div className="mt-2 rounded-md border border-amber-500/20 bg-amber-500/10 px-2 py-1.5 text-xs text-amber-300">
                                {runSummary.steps.find((item) => item.step_id === step.step_id)?.stuck_reason || t('workflows.stuck')}
                              </div>
                            )}
                            <details className="mt-2">
                              <summary className="cursor-pointer text-xs text-[var(--text-accent)]">
                                {t('workflows.stepData')}
                              </summary>
                              <pre className="mt-2 max-h-52 overflow-auto rounded-md bg-[var(--bg-input)] p-2 text-xs text-[var(--text-secondary)]">
                                {formatJson({ input: step.input, output: step.output, error: step.error })}
                              </pre>
                            </details>
                            {RETRYABLE_STEP_STATUSES.has(step.status) && (
                              <div className="mt-3 space-y-2">
                                {editing ? (
                                  <>
                                    <textarea
                                      className="control-surface control-surface-mono min-h-32 resize-y"
                                      value={stepInputText}
                                      onChange={(event) => setStepInputText(event.target.value)}
                                      spellCheck={false}
                                    />
                                    {stepInputError && (
                                      <p className="text-xs text-red-400">{stepInputError}</p>
                                    )}
                                    <div className="flex flex-wrap gap-2">
                                      <Button size="sm" onClick={() => saveInputAndRetry(step)} disabled={busy}>
                                        <RotateCcw className="mr-1.5 h-4 w-4" />
                                        {busy ? t('workflows.retrying') : t('workflows.saveInputAndRetry')}
                                      </Button>
                                      <Button variant="outline" size="sm" onClick={() => setEditingStepId('')}>
                                        {t('common.cancel')}
                                      </Button>
                                    </div>
                                  </>
                                ) : (
                                  <div className="flex flex-wrap gap-2">
                                    <Button variant="outline" size="sm" onClick={() => beginStepEdit(step)}>
                                      {t('workflows.editInput')}
                                    </Button>
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      onClick={() => retryStep(step)}
                                      disabled={busy}
                                    >
                                      <RotateCcw className="mr-1.5 h-4 w-4" />
                                      {busy ? t('workflows.retrying') : t('workflows.retryStep')}
                                    </Button>
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>

              <div>
                <div className="mb-2 flex items-center gap-2">
                  <GitBranch className="h-4 w-4 text-[var(--text-muted)]" />
                  <h3 className="text-sm font-semibold text-[var(--text-primary)]">
                    {t('workflows.events')}
                  </h3>
                </div>
                <div className="max-h-64 overflow-auto rounded-md border border-[var(--border-soft)] bg-[var(--bg-input)]">
                  {events.length === 0 && (
                    <div className="px-3 py-6 text-center text-sm text-[var(--text-muted)]">
                      {t('workflows.emptyEvents')}
                    </div>
                  )}
                  {events.map((event) => (
                    <div
                      key={event.id}
                      className="border-b border-[var(--border-soft)] px-3 py-2 text-xs last:border-b-0"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-[var(--text-muted)]">
                          {formatMaybeDate(event.created_at, language)}
                        </span>
                        {event.step_id && (
                          <span className="rounded bg-[var(--chip-bg)] px-1.5 py-0.5 text-[var(--text-secondary)]">
                            {event.step_id}
                          </span>
                        )}
                        <span
                          className={cn(
                            'text-[var(--text-secondary)]',
                            event.level === 'error' && 'text-red-400',
                            event.level === 'warning' && 'text-amber-400',
                          )}
                        >
                          {event.message}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
          </WorkflowPanel>
        </div>
      </div>
    </div>
  )
}
