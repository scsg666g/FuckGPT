import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  AlertTriangle,
  Archive,
  ArchiveRestore,
  Check,
  CheckCircle2,
  ChevronDown,
  Copy,
  ExternalLink,
  FileText,
  KeyRound,
  Link2,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  ScanLine,
  Search,
  Send,
  Settings2,
  ShieldCheck,
  Smartphone,
  Trash2,
  X,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { apiFetch, cn } from '@/lib/utils'

type SettingKind = 'supplier' | 'scanner' | 'scanner_546789'

type KakaoSetting = {
  id: number | null
  kind: SettingKind
  display_name: string
  base_url: string
  has_cdk: boolean
  cdk_count: number
  cdk_keys: string[]
  cdk_preview: string
  driver_type: string
}

type ScannerKind = 'scanner' | 'scanner_546789'
type AccountProxyMode = 'direct' | 'proxy_service' | 'manual'
type PipelineView = 'workspace' | 'completed' | 'archived' | 'all'

type AccountProxySetting = {
  mode: AccountProxyMode
  value: string
  preview: string
}

type PipelinePostAction = {
  status?: string | null
  task_id?: string | number | null
  error?: string | null
  authorized?: boolean
  enabled?: boolean
  target_key?: string | null
}

type PushDelivery = {
  status?: string | null
  last_error?: string | null
  error?: string | null
  target_key?: string | null
}

type KakaoSettings = Record<SettingKind, KakaoSetting> & {
  default_scanner_kind: ScannerKind
  auto_upload_after_extract: boolean
  account_proxy: AccountProxySetting
}

type Pipeline = {
  id?: number
  account_id?: number
  state: string
  payment_method?: string
  supplier_name?: string
  supplier_status: string
  supplier_order_id?: string
  supplier_stage?: number
  supplier_stage_total?: number
  supplier_stage_name?: string
  supplier_processing_started_at?: string | null
  supplier_deadline_at?: string | null
  payment_url?: string
  scanner_driver?: string
  scanner_name?: string
  scanner_status: string
  scanner_order_id?: string
  scanner_subscription_status?: string
  scan_url?: string
  scan_expires_at?: string
  scanner_submit_attempts?: number
  scanner_compensation_attempted?: boolean
  scanner_poll_failures?: number
  scanner_recovery_reason?: string
  scanner_recovery_check_count?: number
  scanner_recovery_started_at?: string | null
  scanner_recovery_next_check_at?: string | null
  scanner_recovery_deadline_at?: string | null
  scanner_processing_started_at?: string | null
  scanner_deadline_at?: string | null
  plus_status: string
  final_result: string
  completion_source?: string
  plus_check_count?: number
  plus_check_started_at?: string | null
  plus_next_check_at?: string | null
  plus_check_deadline_at?: string | null
  plus_check_paused_at?: string | null
  last_error_code: string
  last_error_message: string
  created_at?: string | null
  updated_at?: string | null
  latest_event_at?: string | null
  completed_at?: string | null
  archived_at?: string | null
  archive_reason?: string | null
  archive_disposition?: string | null
  purged_at?: string | null
  events?: Array<{ time: string; level: string; message: string }>
  supplier_response?: Record<string, unknown>
  scanner_response?: Record<string, unknown>
  post_actions?: {
    codex?: PipelinePostAction | null
    push?: PipelinePostAction | null
  } | null
}

type KakaoAccount = {
  id: number
  email: string
  plan: string
  plan_state: string
  validity: string
  checked_at?: string | null
  account_view?: {
    status?: { checked_at?: string | null }
    security?: {
      phone_bound?: boolean
      phone_number_masked?: string
    }
    codex?: {
      authorized?: boolean
    }
    push_deliveries?: PushDelivery[]
  }
  push_deliveries?: PushDelivery[]
  pipeline: Pipeline
}

type PushTarget = {
  key: string
  label: string
  is_default: boolean
  payload_format: string
}

type SettingDraft = {
  display_name: string
  base_url: string
  cdk_keys: string
}

type CdkCheckResult = {
  cdk_key: string
  status: 'valid' | 'depleted' | 'invalid'
  message: string
  product_type?: string
  cdk_status?: string
  total_count?: number
  used_count?: number
  frozen_count?: number
  available_count?: number
  expires_at?: string | null
  remark?: string
  unlimited?: boolean
}

const SETTING_KINDS: SettingKind[] = ['supplier', 'scanner', 'scanner_546789']
const SETTING_TITLES: Record<SettingKind, string> = {
  supplier: '提链供应商',
  scanner: 'I7wap 扫码',
  scanner_546789: '546789 扫码',
}

const PAGE_SIZE = 20
const PIPELINE_VIEWS: Array<{ key: PipelineView; label: string }> = [
  { key: 'workspace', label: '工作台' },
  { key: 'completed', label: '已完成' },
  { key: 'archived', label: '归档' },
  { key: 'all', label: '全部' },
]
const ACTIVE_PIPELINE_STATES = new Set([
  'supplier_submitting',
  'supplier_processing',
  'scanner_submitting',
  'scanner_processing',
  'scanner_accepted_untracked',
  'scanner_succeeded',
  'plus_checking',
  'plus_pending',
])
const ATTENTION_PIPELINE_STATES = new Set([
  'supplier_failed',
  'supplier_poll_failed',
  'supplier_submit_unconfirmed',
  'scanner_failed',
  'scanner_poll_failed',
  'scanner_submit_unconfirmed',
  'scanner_recovery_unconfirmed',
  'plus_unconfirmed',
  'plus_check_failed',
])
const ADVANCE_PLUS_PIPELINE_STATES = new Set([
  'scanner_succeeded',
  'scanner_accepted_untracked',
  'scanner_recovery_unconfirmed',
  'scanner_submit_unconfirmed',
  'plus_checking',
  'plus_pending',
  'plus_unconfirmed',
  'plus_check_failed',
])

const UNCERTAIN_PIPELINE_STATES = new Set([
  'supplier_poll_failed',
  'supplier_submit_unconfirmed',
  'scanner_poll_failed',
  'scanner_submit_unconfirmed',
  'scanner_recovery_unconfirmed',
  'plus_unconfirmed',
  'plus_check_failed',
])

function parseError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error || '操作失败')
  try {
    const payload = JSON.parse(message)
    const detail = payload?.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object') return detail.message || detail.code || message
  } catch {
    // Keep the original response text.
  }
  return message
}

async function copyText(value: string) {
  if (!value) throw new Error('没有可复制的内容')
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(value)
      return
    } catch {
      // Fall back to a temporary textarea for restricted clipboard contexts.
    }
  }
  const element = document.createElement('textarea')
  element.value = value
  element.setAttribute('readonly', '')
  element.style.position = 'fixed'
  element.style.opacity = '0'
  document.body.appendChild(element)
  element.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(element)
  if (!copied) throw new Error('复制失败，请手动复制')
}

function sessionTokenFromCookies(value: string) {
  const raw = String(value || '').trim()
  if (!raw) return ''
  try {
    const parsed = JSON.parse(raw)
    const cookies = Array.isArray(parsed) ? parsed : (Array.isArray(parsed?.cookies) ? parsed.cookies : [])
    const match = cookies.find((item: { name?: string; value?: string }) => (
      ['__Secure-next-auth.session-token', '__Secure-authjs.session-token'].includes(String(item?.name || ''))
    ))
    if (match?.value) return String(match.value)
  } catch {
    // Raw Cookie headers are handled below.
  }
  const match = raw.match(/(?:^|[;\s])(?:__Secure-next-auth\.session-token|__Secure-authjs\.session-token)=([^;\s]+)/)
  return match?.[1] ? decodeURIComponent(match[1]) : ''
}

function planBadge(account: KakaoAccount) {
  const plan = String(account.plan || '').toLowerCase()
  const subscribed = account.plan_state === 'subscribed' || ['plus', 'pro', 'team', 'business', 'enterprise'].some(item => plan.includes(item))
  if (subscribed) return <Badge variant="success">PLUS</Badge>
  if (plan === 'free' || account.plan_state === 'free') return <Badge variant="secondary">FREE</Badge>
  return <Badge variant="secondary">{account.plan || '未检测'}</Badge>
}

function accountIsPlus(account: KakaoAccount) {
  const plan = String(account.plan || '').toLowerCase()
  return account.plan_state === 'subscribed' || ['plus', 'pro', 'team', 'business', 'enterprise'].some(item => plan.includes(item))
}

function phoneBindingBadge(account: KakaoAccount) {
  const status = account.account_view?.status || {}
  const security = account.account_view?.security || {}
  const checked = Boolean(status.checked_at || security.phone_bound === true || security.phone_number_masked)
  if (security.phone_bound === true) {
    return (
      <Badge variant="success" title={security.phone_number_masked || '该账号已绑定手机'}>
        <Smartphone className="mr-1 h-3 w-3" />
        已绑手机
      </Badge>
    )
  }
  if (checked) {
    return (
      <Badge variant="secondary" title="最近一次账号检测未发现绑定手机">
        <Smartphone className="mr-1 h-3 w-3" /> 未绑手机
      </Badge>
    )
  }
  return (
    <Badge variant="secondary" title="尚未检测手机绑定状态">
      <Smartphone className="mr-1 h-3 w-3" /> 手机未检测
    </Badge>
  )
}

function AccountMoreMenu({
  accountId,
  onCopyCredential,
  onForceReset,
  onCheckPlus,
  onAuthorizeCodex,
  onPush,
  codexReady,
  codexTitle,
  pushReady,
  pushTitle,
  actionDisabled = false,
  onShowLog,
}: {
  accountId: number
  onCopyCredential: (accountId: number, credentialType: 'session' | 'access') => void
  onForceReset: () => void
  onCheckPlus: () => void
  onAuthorizeCodex: () => void
  onPush: () => void
  codexReady: boolean
  codexTitle: string
  pushReady: boolean
  pushTitle: string
  actionDisabled?: boolean
  onShowLog?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 })
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const menuItemCount = 6 + (onShowLog ? 1 : 0)

  useEffect(() => {
    if (!open) return

    const updatePosition = () => {
      const rect = triggerRef.current?.getBoundingClientRect()
      if (!rect) return
      const width = 176
      const height = 20 + menuItemCount * 34
      const gap = 6
      const openUp = window.innerHeight - rect.bottom < height + 16 && rect.top > height
      setMenuPosition({
        top: openUp ? rect.top - height - gap : rect.bottom + gap,
        left: Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8)),
      })
    }
    const closeOnOutsideClick = (event: MouseEvent) => {
      const target = event.target as Node
      if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) return
      setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }

    updatePosition()
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    document.addEventListener('mousedown', closeOnOutsideClick)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
      document.removeEventListener('mousedown', closeOnOutsideClick)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [menuItemCount, open])

  const copyCredential = (credentialType: 'session' | 'access') => {
    setOpen(false)
    onCopyCredential(accountId, credentialType)
  }

  return (
    <>
      <Button
        ref={triggerRef}
        variant="ghost"
        size="sm"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        更多 <ChevronDown className="ml-1 h-3.5 w-3.5" />
      </Button>
      {open && typeof document !== 'undefined' && createPortal(
        <div
          ref={menuRef}
          role="menu"
          className="fixed z-[60] w-44 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-card)] py-1 shadow-[var(--shadow-soft)]"
          style={{ top: menuPosition.top, left: menuPosition.left }}
        >
          <button
            type="button"
            role="menuitem"
            disabled={actionDisabled || !codexReady}
            title={codexTitle}
            className="flex w-full items-center px-3 py-2 text-left text-xs font-medium text-violet-500 transition-colors hover:bg-violet-500/10 focus-visible:bg-violet-500/10 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40"
            onClick={() => {
              setOpen(false)
              onAuthorizeCodex()
            }}
          >
            <ShieldCheck className="mr-2 h-3.5 w-3.5" /> Codex 授权
          </button>
          <button
            type="button"
            role="menuitem"
            disabled={actionDisabled || !pushReady}
            title={pushTitle}
            className="flex w-full items-center px-3 py-2 text-left text-xs font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-hover)] focus-visible:bg-[var(--bg-hover)] focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40"
            onClick={() => {
              setOpen(false)
              onPush()
            }}
          >
            <Send className="mr-2 h-3.5 w-3.5" /> 推送到默认目标
          </button>
          <div className="my-1 h-px bg-[var(--border)]" />
          <button
            type="button"
            role="menuitem"
            disabled={actionDisabled}
            className="flex w-full items-center px-3 py-2 text-left text-xs font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-hover)] focus-visible:bg-[var(--bg-hover)] focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40"
            onClick={() => {
              setOpen(false)
              onCheckPlus()
            }}
          >
            <Check className="mr-2 h-3.5 w-3.5" /> 检测 Plus
          </button>
          <button
            type="button"
            role="menuitem"
            disabled={actionDisabled}
            className="flex w-full items-center px-3 py-2 text-left text-xs font-medium text-red-500 transition-colors hover:bg-red-500/10 focus-visible:bg-red-500/10 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40"
            onClick={() => {
              setOpen(false)
              onForceReset()
            }}
          >
            <RotateCcw className="mr-2 h-3.5 w-3.5" /> 重置记录
          </button>
          <div className="my-1 h-px bg-[var(--border)]" />
          <button
            type="button"
            role="menuitem"
            className="flex w-full items-center px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:bg-[var(--bg-hover)]"
            onClick={() => copyCredential('access')}
          >
            <Copy className="mr-2 h-3.5 w-3.5" /> 复制 AT
          </button>
          <button
            type="button"
            role="menuitem"
            className="flex w-full items-center px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:bg-[var(--bg-hover)]"
            onClick={() => copyCredential('session')}
          >
            <KeyRound className="mr-2 h-3.5 w-3.5" /> 复制 ST
          </button>
          {onShowLog ? (
            <button
              type="button"
              role="menuitem"
              className="flex w-full items-center px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:bg-[var(--bg-hover)]"
              onClick={() => {
                setOpen(false)
                onShowLog()
              }}
            >
              <FileText className="mr-2 h-3.5 w-3.5" /> 查看日志
            </button>
          ) : null}
        </div>,
        document.body,
      )}
    </>
  )
}

type StepState = 'waiting' | 'active' | 'complete' | 'skipped' | 'paused' | 'error'

const ACTIVE_POST_ACTION_STATUSES = new Set(['pending', 'queued', 'claimed', 'running', 'cancel_requested'])

function normalizePostActionStatus(value?: string | null) {
  return String(value || '').trim().toLowerCase()
}

function pipelinePlusComplete(account: KakaoAccount) {
  const pipeline = account.pipeline
  return pipeline.state === 'completed' || pipeline.final_result === 'plus' || accountIsPlus(account)
}

function latestPushDelivery(account: KakaoAccount, targetKey = 'nvtokens') {
  const deliveries = Array.isArray(account.push_deliveries)
    ? account.push_deliveries
    : Array.isArray(account.account_view?.push_deliveries)
      ? account.account_view.push_deliveries
      : []
  return deliveries.find(delivery => String(delivery?.target_key || '').trim() === targetKey) || null
}

function postActionStepState(
  action?: PipelinePostAction | null,
  options: { complete?: boolean; disabled?: boolean } = {},
): StepState {
  const status = normalizePostActionStatus(action?.status)
  if (['success', 'succeeded'].includes(status)) return 'complete'
  if (status === 'failed') return 'error'
  if (['paused', 'interrupted', 'cancelled'].includes(status)) return 'paused'
  if (['skipped', 'disabled'].includes(status) || options.disabled) return 'skipped'
  if (ACTIVE_POST_ACTION_STATUSES.has(status)) return 'active'
  if (options.complete) return 'complete'
  return 'waiting'
}

function getPostActionPresentation(account: KakaoAccount) {
  const plusComplete = pipelinePlusComplete(account)
  const codex = account.pipeline.post_actions?.codex || null
  const codexAuthorized = typeof codex?.authorized === 'boolean'
    ? codex.authorized
    : account.account_view?.codex?.authorized === true
  const codexStatus = normalizePostActionStatus(codex?.status)
  // A stored authorization may satisfy an untouched/waiting stage, but it must
  // never mask the outcome of a newer task that is active, paused, or failed.
  const reusableCodexAuthorization = codexAuthorized && (!codexStatus || codexStatus === 'waiting')
  const codexState = plusComplete
    ? postActionStepState(codex, { complete: reusableCodexAuthorization })
    : 'waiting'

  const explicitPush = account.pipeline.post_actions?.push || null
  const delivery = explicitPush ? null : latestPushDelivery(account)
  const push: PipelinePostAction | null = explicitPush || (delivery
    ? {
        status: delivery.status,
        error: delivery.last_error || delivery.error,
        target_key: delivery.target_key,
      }
    : null)
  const codexSettled = codexState === 'complete' || (codexState === 'skipped' && codexAuthorized)
  const pushState = plusComplete && codexSettled
    ? postActionStepState(push, { disabled: explicitPush?.enabled === false })
    : 'waiting'

  return {
    plusComplete,
    codex,
    codexAuthorized,
    codexSettled,
    codexState,
    push,
    pushState,
  }
}

function postActionsAreActive(account: KakaoAccount) {
  const rawStatuses = [
    account.pipeline.post_actions?.codex?.status,
    account.pipeline.post_actions?.push?.status,
  ]
  if (rawStatuses.some(status => ACTIVE_POST_ACTION_STATUSES.has(normalizePostActionStatus(status)))) {
    return true
  }
  const { codexState, pushState } = getPostActionPresentation(account)
  return codexState === 'active' || pushState === 'active'
}

function postActionsHaveError(account: KakaoAccount) {
  const { codexState, pushState } = getPostActionPresentation(account)
  return codexState === 'error' || pushState === 'error'
}

function pipelineFlowComplete(account: KakaoAccount) {
  const { plusComplete, codexSettled, pushState } = getPostActionPresentation(account)
  return plusComplete && codexSettled && ['complete', 'skipped'].includes(pushState)
}

function pipelineIsArchived(account: KakaoAccount) {
  return Boolean(account.pipeline.archived_at || account.pipeline.purged_at)
}

function pipelineIsPurged(account: KakaoAccount) {
  const disposition = String(account.pipeline.archive_disposition || '').trim().toLowerCase()
  return Boolean(account.pipeline.purged_at || disposition === 'purged')
}

function pipelineWasAbandoned(account: KakaoAccount) {
  const disposition = String(account.pipeline.archive_disposition || '').trim().toLowerCase()
  return disposition.includes('abandon') || disposition.includes('discard')
}

function pipelineIsCompleteForArchive(account: KakaoAccount) {
  return account.pipeline.state === 'completed'
    || account.pipeline.final_result === 'plus'
}

function pipelineArchiveRequiresForce(account: KakaoAccount) {
  return ACTIVE_PIPELINE_STATES.has(account.pipeline.state)
    || UNCERTAIN_PIPELINE_STATES.has(account.pipeline.state)
    || postActionsAreActive(account)
}

function formatLatestTime(value?: string | null) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return { primary: value, relative: '', full: value }
  }

  const now = new Date()
  const pad = (part: number) => String(part).padStart(2, '0')
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate()
  const primary = sameDay ? time : `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`
  const full = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`
  const elapsedSeconds = Math.max(0, Math.floor((now.getTime() - date.getTime()) / 1000))
  const relative = elapsedSeconds < 10
    ? '刚刚'
    : elapsedSeconds < 60
      ? `${elapsedSeconds} 秒前`
      : elapsedSeconds < 3600
        ? `${Math.floor(elapsedSeconds / 60)} 分钟前`
        : elapsedSeconds < 86400
          ? `${Math.floor(elapsedSeconds / 3600)} 小时前`
          : `${Math.floor(elapsedSeconds / 86400)} 天前`

  return { primary, relative, full }
}

function LatestEventTime({ value }: { value?: string | null }) {
  const formatted = formatLatestTime(value)
  if (!formatted) {
    return (
      <div className="whitespace-nowrap">
        <div className="font-mono text-xs text-[var(--text-muted)]">—</div>
        <div className="mt-1 text-[11px] text-[var(--text-muted)]">暂无日志</div>
      </div>
    )
  }

  return (
    <time dateTime={value || undefined} title={formatted.full} className="block whitespace-nowrap">
      <span className="block font-mono text-xs text-[var(--text-secondary)]">{formatted.primary}</span>
      {formatted.relative ? <span className="mt-1 block text-[11px] text-[var(--text-muted)]">{formatted.relative}</span> : null}
    </time>
  )
}

function ArchiveStateBadge({ account }: { account: KakaoAccount }) {
  if (pipelineIsPurged(account)) return <Badge variant="danger">已清除</Badge>
  if (pipelineWasAbandoned(account)) return <Badge variant="warning">已放弃</Badge>
  return <Badge variant="secondary">已归档</Badge>
}

function ArchivedPipelineSummary({ account }: { account: KakaoAccount }) {
  const pipeline = account.pipeline
  const archived = formatLatestTime(pipeline.purged_at || pipeline.archived_at)
  const reason = String(pipeline.archive_reason || '').trim() || '未填写归档原因'

  return (
    <div className="min-w-[320px] max-w-[460px]">
      <div className="flex flex-wrap items-center justify-center gap-2">
        <ArchiveStateBadge account={account} />
        {pipelineIsPurged(account) ? (
          <span className="text-xs text-red-500">流水线详情已永久清除</span>
        ) : (
          <span className="text-xs text-[var(--text-secondary)]">
            {pipelineWasAbandoned(account) ? '流程已停止并归档' : '流程记录已归档'}
          </span>
        )}
      </div>
      <p className="mt-2 break-words text-center text-xs leading-5 text-[var(--text-secondary)]" title={reason}>
        {reason}
      </p>
      {archived ? (
        <p className="mt-1 text-center text-[11px] text-[var(--text-muted)]" title={archived.full}>
          {pipelineIsPurged(account) ? '清除于' : '归档于'} {archived.primary}
        </p>
      ) : null}
    </div>
  )
}

function formatNextPlusCheck(value?: string | null) {
  if (!value) return ''
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`
  const nextCheck = new Date(normalized)
  if (Number.isNaN(nextCheck.getTime())) return ''
  const seconds = Math.ceil((nextCheck.getTime() - Date.now()) / 1000)
  if (seconds <= 3) return '即将再次检测'
  if (seconds < 60) return `${seconds} 秒后再查`
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = seconds % 60
  return remainingSeconds ? `${minutes} 分 ${remainingSeconds} 秒后再查` : `${minutes} 分钟后再查`
}

function PipelineProgress({ account }: { account: KakaoAccount }) {
  const pipeline = account.pipeline
  const {
    plusComplete,
    codex,
    codexAuthorized,
    codexState,
    push,
    pushState,
  } = getPostActionPresentation(account)
  const scannerReached = plusComplete || [
    'scanner_submitting',
    'scanner_processing',
    'scanner_accepted_untracked',
    'scanner_recovery_unconfirmed',
    'scanner_submit_unconfirmed',
    'scanner_poll_failed',
    'scanner_failed',
    'scanner_succeeded',
    'plus_checking',
    'plus_pending',
    'plus_unconfirmed',
    'plus_check_failed',
    'completed',
  ].includes(pipeline.state)
  const scannerComplete = plusComplete || ['scanner_succeeded', 'plus_checking', 'plus_pending', 'plus_unconfirmed', 'plus_check_failed', 'completed'].includes(pipeline.state)
  const scannerUncertain = ['scanner_accepted_untracked', 'scanner_recovery_unconfirmed', 'scanner_submit_unconfirmed', 'scanner_poll_failed'].includes(pipeline.state)
  const steps: Array<{ label: string; state: StepState }> = [
    {
      label: '提链',
      state: pipeline.state === 'supplier_failed'
        ? 'error'
        : pipeline.payment_url || scannerReached
          ? 'complete'
          : ['supplier_poll_failed', 'supplier_submit_unconfirmed'].includes(pipeline.state)
            ? 'paused'
          : ['supplier_submitting', 'supplier_processing'].includes(pipeline.state)
            ? 'active'
            : 'waiting',
    },
    {
      label: '扫码',
      state: pipeline.state === 'scanner_failed'
        ? 'error'
        : scannerComplete
          ? 'complete'
          : scannerUncertain
            ? 'paused'
          : ['scanner_submitting', 'scanner_processing'].includes(pipeline.state)
            ? 'active'
            : 'waiting',
    },
    {
      label: 'Plus',
      state: pipeline.state === 'plus_check_failed'
        ? 'error'
        : plusComplete
          ? 'complete'
          : ['plus_unconfirmed', 'scanner_accepted_untracked', 'scanner_recovery_unconfirmed', 'scanner_submit_unconfirmed'].includes(pipeline.state)
            ? 'paused'
          : ['plus_checking', 'plus_pending'].includes(pipeline.state)
            ? 'active'
            : 'waiting',
    },
    {
      label: 'Codex',
      state: codexState,
    },
    {
      label: '推送',
      state: pushState,
    },
  ]

  const stateText = (() => {
    if (plusComplete) {
      const codexError = String(codex?.error || '').trim()
      const pushError = String(push?.error || '').trim()
      const codexStatus = normalizePostActionStatus(codex?.status)
      const pushStatus = normalizePostActionStatus(push?.status)

      if (codexState === 'error') return `Plus 已确认，Codex 授权失败${codexError ? `：${codexError}` : ''}`
      if (codexState === 'paused') return `Plus 已确认，Codex 授权已中断${codexError ? `：${codexError}` : ''}`
      if (codexState === 'active') {
        return ['running', 'cancel_requested'].includes(codexStatus)
          ? 'Plus 已确认，正在进行 Codex 授权'
          : 'Plus 已确认，Codex 授权任务排队中'
      }
      if (codexState === 'skipped' && !codexAuthorized) return 'Plus 已确认，Codex 授权已跳过'
      if (codexState === 'waiting') return 'Plus 已确认，等待 Codex 授权'

      if (pushState === 'error') return `Codex 已授权，推送失败${pushError ? `：${pushError}` : ''}`
      if (pushState === 'paused') return `Codex 已授权，推送已中断${pushError ? `：${pushError}` : ''}`
      if (pushState === 'active') return ['running', 'cancel_requested'].includes(pushStatus) ? 'Codex 已授权，正在推送' : 'Codex 已授权，推送任务排队中'
      if (pushState === 'skipped') {
        const disabled = pipeline.post_actions?.push?.enabled === false || pushStatus === 'disabled'
        return disabled ? 'Codex 已授权，自动推送未启用' : 'Codex 已授权，推送已跳过'
      }
      if (pushState === 'complete') return 'Codex 授权及推送已完成'
      return 'Codex 已授权，等待推送'
    }
    if (pipeline.state === 'supplier_processing') {
      const stage = pipeline.supplier_stage_total ? `${pipeline.supplier_stage || 0}/${pipeline.supplier_stage_total} ` : ''
      return `${stage}${pipeline.supplier_stage_name || pipeline.supplier_status || '供应商处理中'}`
    }
    if (pipeline.state === 'supplier_poll_failed') return pipeline.last_error_message || '提链订单查询已暂停，可继续查询原订单'
    if (pipeline.state === 'supplier_submit_unconfirmed') return pipeline.last_error_message || '提链提交结果无法确认，请重置后重试'
    if (pipeline.state === 'scanner_processing') return `${pipeline.scanner_name || '扫码平台'}处理中`
    if (pipeline.state === 'scanner_submitting') return `${pipeline.scanner_name || '扫码平台'}提交中`
    if (pipeline.state === 'scanner_accepted_untracked') {
      const count = Number(pipeline.scanner_recovery_check_count || 0)
      const nextCheck = formatNextPlusCheck(pipeline.scanner_recovery_next_check_at)
      return [
        pipeline.scanner_status === 'DUPLICATE_ACCEPTED'
          ? '已提交给供应商，正在通过账号状态确认结果'
          : '扫码提交结果无法确认，正在通过账号状态确认结果',
        count ? `已检测 ${count} 次` : '',
        nextCheck,
      ].filter(Boolean).join(' · ')
    }
    if (pipeline.state === 'scanner_recovery_unconfirmed') return pipeline.last_error_message || '30 分钟内未确认 Plus，请人工检查是否已扣费或重复提交'
    if (pipeline.state === 'scanner_submit_unconfirmed') return pipeline.last_error_message || '扫码提交结果无法确认，请人工检查是否已扣费或重复提交'
    if (pipeline.state === 'scanner_poll_failed') return pipeline.last_error_message || '扫码订单查询已暂停，可继续查询原订单'
    if (pipeline.state === 'link_ready') return '长链已就绪，等待上传扫码'
    if (pipeline.state === 'scanner_succeeded') return '扫码已完成，正在启动 Plus 确认'
    if (pipeline.state === 'plus_pending') {
      const count = Number(pipeline.plus_check_count || 0)
      const nextCheck = formatNextPlusCheck(pipeline.plus_next_check_at)
      return [
        '正在确认 Plus，无需操作',
        count ? `已检测 ${count} 次` : '',
        nextCheck,
      ].filter(Boolean).join(' · ')
    }
    if (pipeline.state === 'plus_unconfirmed') return pipeline.last_error_message || '10 分钟内未确认 Plus，请人工检查供应商结果'
    if (pipeline.state === 'plus_checking') return '正在检查 Plus，请稍候'
    if (['supplier_failed', 'scanner_failed', 'plus_check_failed'].includes(pipeline.state)) {
      return pipeline.last_error_message || '本次操作失败'
    }
    return '等待提取 Kakao 长链'
  })()

  return (
    <div className="min-w-[320px] max-w-[460px]">
      <div className="flex items-start" aria-label={`流水线状态：${stateText}`}>
        {steps.map((step, index) => (
          <div key={step.label} className="relative flex flex-1 flex-col items-center gap-1">
            {index > 0 ? (
              <div className={cn(
                'absolute right-1/2 top-[6px] h-px w-full',
                ['complete', 'skipped'].includes(steps[index - 1].state) ? 'bg-emerald-500/60' : 'bg-[var(--border)]',
              )} />
            ) : null}
            <span className={cn(
              'relative z-10 h-3 w-3 rounded-full ring-4 ring-[var(--bg-surface)]',
              step.state === 'complete' && 'bg-emerald-500',
              step.state === 'active' && 'bg-[var(--accent)]',
              step.state === 'skipped' && 'bg-slate-400',
              step.state === 'paused' && 'bg-amber-500',
              step.state === 'error' && 'bg-red-500',
              step.state === 'waiting' && 'bg-[var(--border)]',
            )} />
            <span className={cn(
              'text-[11px]',
              step.state === 'complete' && 'text-emerald-500',
              step.state === 'active' && 'font-medium text-[var(--accent)]',
              step.state === 'skipped' && 'text-[var(--text-muted)]',
              step.state === 'paused' && 'font-medium text-amber-500',
              step.state === 'error' && 'font-medium text-red-500',
              step.state === 'waiting' && 'text-[var(--text-muted)]',
            )}>{step.label}</span>
          </div>
        ))}
      </div>
      <p className={cn(
        'mt-2 text-center text-xs leading-5',
        ATTENTION_PIPELINE_STATES.has(pipeline.state) || codexState === 'error' || pushState === 'error'
          ? 'text-red-500'
          : codexState === 'paused' || pushState === 'paused'
            ? 'text-amber-500'
          : 'text-[var(--text-secondary)]',
      )} title={stateText}>{stateText}</p>
    </div>
  )
}

function PipelineLogDrawer({
  account,
  detail,
  onClose,
  onCopyLink,
}: {
  account: KakaoAccount
  detail?: Pipeline
  onClose: () => void
  onCopyLink: (value: string) => void
}) {
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  if (typeof document === 'undefined') return null

  return createPortal(
    <div className="fixed inset-0 z-[50]">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        aria-label="关闭日志"
        onClick={onClose}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="kakao-log-title"
        className="absolute inset-y-0 right-0 flex w-full max-w-3xl flex-col border-l border-[var(--border)] bg-[var(--bg-surface)] shadow-[-4px_0_8px_rgba(0,0,0,0.2)]"
      >
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-[var(--border)] px-5 py-4">
          <div className="min-w-0">
            <h2 id="kakao-log-title" className="text-base font-semibold text-[var(--text-primary)]">流水线日志</h2>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2 text-xs text-[var(--text-muted)]">
              <span className="max-w-md truncate text-[var(--text-secondary)]" title={account.email}>{account.email}</span>
              <span className="font-mono">#{account.id}</span>
              {planBadge(account)}
            </div>
          </div>
          <Button variant="ghost" size="icon" autoFocus onClick={onClose} aria-label="关闭日志">
            <X className="h-4 w-4" />
          </Button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          {!detail ? (
            <div className="space-y-3" aria-label="正在读取日志">
              <div className="h-4 w-32 animate-pulse rounded bg-[var(--chip-bg)]" />
              {Array.from({ length: 6 }).map((_, index) => (
                <div key={index} className="h-9 animate-pulse rounded-md bg-[var(--bg-hover)]" />
              ))}
            </div>
          ) : (
            <div className="space-y-6">
              <section>
                <div className="mb-3 flex items-center justify-between gap-3">
                  <h3 className="text-sm font-semibold text-[var(--text-primary)]">操作记录</h3>
                  <span className="text-xs text-[var(--text-muted)]">{(detail.events || []).length} 条</span>
                </div>
                {(detail.events || []).length === 0 ? (
                  <p className="rounded-lg bg-[var(--bg-hover)] px-4 py-6 text-center text-sm text-[var(--text-muted)]">暂无日志</p>
                ) : (
                  <div className="divide-y divide-[var(--border-soft)] border-y border-[var(--border)]">
                    {[...(detail.events || [])].reverse().map((event, index) => (
                      <div key={`${event.time}-${index}`} className="grid grid-cols-[76px_minmax(0,1fr)] gap-3 py-3 text-xs">
                        <time className="font-mono text-[var(--text-muted)]">
                          {new Date(event.time).toLocaleTimeString()}
                        </time>
                        <span className={cn(
                          'leading-5',
                          event.level === 'error'
                            ? 'text-red-500'
                            : event.level === 'warning'
                              ? 'text-amber-500'
                              : 'text-[var(--text-secondary)]',
                        )}>{event.message}</span>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              {detail.payment_url ? (
                <section>
                  <h3 className="mb-2 text-sm font-semibold text-[var(--text-primary)]">Kakao 长链</h3>
                  <div className="flex min-w-0 items-center gap-2 rounded-lg bg-[var(--bg-input)] p-2">
                    <code className="min-w-0 flex-1 truncate px-1 text-xs text-[var(--text-secondary)]">{detail.payment_url}</code>
                    <Button variant="outline" size="sm" onClick={() => onCopyLink(detail.payment_url || '')}>
                      <Copy className="mr-1.5 h-3.5 w-3.5" /> 复制
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => window.open(detail.payment_url, '_blank', 'noopener,noreferrer')}>
                      <ExternalLink className="mr-1.5 h-3.5 w-3.5" /> 打开
                    </Button>
                  </div>
                </section>
              ) : null}

              <details className="border-t border-[var(--border)] pt-4">
                <summary className="cursor-pointer text-sm font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
                  查看脱敏接口响应
                </summary>
                <div className="mt-3 grid gap-3 lg:grid-cols-2">
                  <div className="min-w-0">
                    <div className="mb-1 text-xs text-[var(--text-muted)]">供应商</div>
                    <pre className="max-h-80 overflow-auto rounded-md bg-[var(--bg-input)] p-3 font-mono text-[11px] leading-5 text-[var(--text-secondary)]">
                      {JSON.stringify(detail.supplier_response || {}, null, 2)}
                    </pre>
                  </div>
                  <div className="min-w-0">
                    <div className="mb-1 text-xs text-[var(--text-muted)]">扫码平台</div>
                    <pre className="max-h-80 overflow-auto rounded-md bg-[var(--bg-input)] p-3 font-mono text-[11px] leading-5 text-[var(--text-secondary)]">
                      {JSON.stringify(detail.scanner_response || {}, null, 2)}
                    </pre>
                  </div>
                </div>
              </details>
            </div>
          )}
        </div>
      </aside>
    </div>,
    document.body,
  )
}

function SettingsPanel({
  settings,
  onClose,
  onSaved,
}: {
  settings: KakaoSettings
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const [drafts, setDrafts] = useState<Record<SettingKind, SettingDraft>>({
    supplier: {
      display_name: settings.supplier.display_name,
      base_url: settings.supplier.base_url,
      cdk_keys: settings.supplier.cdk_keys.join('\n'),
    },
    scanner: {
      display_name: settings.scanner.display_name,
      base_url: settings.scanner.base_url,
      cdk_keys: settings.scanner.cdk_keys.join('\n'),
    },
    scanner_546789: {
      display_name: settings.scanner_546789.display_name,
      base_url: settings.scanner_546789.base_url,
      cdk_keys: settings.scanner_546789.cdk_keys.join('\n'),
    },
  })
  const [busy, setBusy] = useState('')
  const [activeKind, setActiveKind] = useState<SettingKind>('supplier')
  const [feedback, setFeedback] = useState<Record<SettingKind, string>>({ supplier: '', scanner: '', scanner_546789: '' })
  const [checkResults, setCheckResults] = useState<Record<SettingKind, CdkCheckResult[]>>({ supplier: [], scanner: [], scanner_546789: [] })
  const [accountProxy, setAccountProxy] = useState({
    mode: settings.account_proxy.mode,
    value: settings.account_proxy.value,
  })
  const [accountProxyFeedback, setAccountProxyFeedback] = useState('')

  useEffect(() => {
    setDrafts({
      supplier: {
        display_name: settings.supplier.display_name,
        base_url: settings.supplier.base_url,
        cdk_keys: settings.supplier.cdk_keys.join('\n'),
      },
      scanner: {
        display_name: settings.scanner.display_name,
        base_url: settings.scanner.base_url,
        cdk_keys: settings.scanner.cdk_keys.join('\n'),
      },
      scanner_546789: {
        display_name: settings.scanner_546789.display_name,
        base_url: settings.scanner_546789.base_url,
        cdk_keys: settings.scanner_546789.cdk_keys.join('\n'),
      },
    })
    setAccountProxy({
      mode: settings.account_proxy.mode,
      value: settings.account_proxy.value,
    })
  }, [settings])

  const update = (kind: SettingKind, key: keyof SettingDraft, value: string) => {
    setDrafts(current => ({ ...current, [kind]: { ...current[kind], [key]: value } }))
    if (key === 'cdk_keys') setCheckResults(current => ({ ...current, [kind]: [] }))
  }

  const selectDefaultScanner = async (scannerKind: ScannerKind) => {
    setBusy('select-default-scanner')
    try {
      await apiFetch('/kakao-pipeline/settings/default-scanner/select', {
        method: 'PUT',
        body: JSON.stringify({ scanner_kind: scannerKind }),
      })
      await onSaved()
    } catch (error) {
      setFeedback(current => ({ ...current, [scannerKind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  const setAutoUpload = async (enabled: boolean) => {
    setBusy('set-auto-upload')
    try {
      await apiFetch('/kakao-pipeline/settings/options/auto-upload', {
        method: 'PUT',
        body: JSON.stringify({ enabled }),
      })
      await onSaved()
    } catch (error) {
      setFeedback(current => ({ ...current, [settings.default_scanner_kind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  const saveAccountProxy = async () => {
    setBusy('save-account-proxy')
    setAccountProxyFeedback('')
    try {
      await apiFetch('/kakao-pipeline/settings/options/account-proxy', {
        method: 'PUT',
        body: JSON.stringify(accountProxy),
      })
      setAccountProxyFeedback('账号检查与重登代理已保存')
      await onSaved()
    } catch (error) {
      setAccountProxyFeedback(parseError(error))
    } finally {
      setBusy('')
    }
  }

  const save = async (kind: SettingKind) => {
    setBusy(`save-${kind}`)
    setFeedback(current => ({ ...current, [kind]: '' }))
    try {
      await apiFetch(`/kakao-pipeline/settings/${kind}`, {
        method: 'PUT',
        body: JSON.stringify(drafts[kind]),
      })
      setFeedback(current => ({ ...current, [kind]: '配置已保存' }))
      await onSaved()
    } catch (error) {
      setFeedback(current => ({ ...current, [kind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  const checkCdks = async (kind: SettingKind) => {
    setBusy(`check-${kind}`)
    setFeedback(current => ({ ...current, [kind]: '' }))
    try {
      await apiFetch(`/kakao-pipeline/settings/${kind}`, {
        method: 'PUT',
        body: JSON.stringify(drafts[kind]),
      })
      const result = await apiFetch(`/kakao-pipeline/settings/${kind}/check-cdks`, {
        method: 'POST',
        body: JSON.stringify(drafts[kind]),
      })
      const removed = new Set<string>(Array.isArray(result.removed) ? result.removed : [])
      if (removed.size) {
        setDrafts(current => ({
          ...current,
          [kind]: {
            ...current[kind],
            cdk_keys: current[kind].cdk_keys
              .split(/\r?\n/)
              .map(item => item.trim())
              .filter(item => item && !removed.has(item))
              .join('\n'),
          },
        }))
      }
      const items = Array.isArray(result.items) ? result.items : []
      setCheckResults(current => ({ ...current, [kind]: items }))
      const valid = items.filter((item: { status: string }) => item.status === 'valid').length
      const invalid = items.length - valid - removed.size
      setFeedback(current => ({
        ...current,
        [kind]: kind !== 'supplier'
          ? `已保存并校验额度：可用 ${valid}，已用完并删除 ${removed.size}，无效/异常 ${invalid}。`
          : `已保存并校验：有效 ${valid}，已用完并删除 ${removed.size}，无效/异常 ${invalid}。提链接口不提供精确剩余次数。`,
      }))
      await onSaved()
    } catch (error) {
      setFeedback(current => ({ ...current, [kind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  const test = async (kind: SettingKind) => {
    setBusy(`test-${kind}`)
    setFeedback(current => ({ ...current, [kind]: '' }))
    try {
      const result = await apiFetch(`/kakao-pipeline/settings/${kind}/test`, {
        method: 'POST',
        body: JSON.stringify(drafts[kind]),
      })
      setFeedback(current => ({ ...current, [kind]: result.message || '连接成功' }))
    } catch (error) {
      setFeedback(current => ({ ...current, [kind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="mb-5 border border-[var(--border)] bg-[var(--bg-surface)]">
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-[var(--text-primary)]">Kakao 流水线配置</h2>
          <p className="mt-0.5 text-xs text-[var(--text-muted)]">配置供应商、CDK，以及 Plus 检查和自动重登使用的网络。</p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-3">
          <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
            <input
              type="checkbox"
              checked={settings.auto_upload_after_extract}
              disabled={Boolean(busy)}
              onChange={event => void setAutoUpload(event.target.checked)}
            />
            <span>提链成功后自动上传</span>
          </label>
          <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
            <span>默认扫码供应商</span>
            <select
              className="control-surface control-surface-compact min-w-36"
              value={settings.default_scanner_kind}
              disabled={Boolean(busy)}
              onChange={event => void selectDefaultScanner(event.target.value as ScannerKind)}
            >
              <option value="scanner">I7wap</option>
              <option value="scanner_546789">546789</option>
            </select>
          </label>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="关闭配置">
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>
      <div className="flex flex-wrap items-end gap-3 border-b border-[var(--border)] bg-[var(--bg-hover)]/30 px-4 py-3">
        <label className="block min-w-44">
          <span className="mb-1 block text-xs text-[var(--text-secondary)]">Plus 检查与自动重登代理</span>
          <select
            className="control-surface control-surface-compact w-full"
            value={accountProxy.mode}
            disabled={Boolean(busy)}
            onChange={event => setAccountProxy(current => ({
              ...current,
              mode: event.target.value as AccountProxyMode,
            }))}
          >
            <option value="direct">直连</option>
            <option value="proxy_service">代理服务</option>
            <option value="manual">手动代理</option>
          </select>
        </label>
        {accountProxy.mode === 'manual' ? (
          <label className="block min-w-64 flex-1">
            <span className="mb-1 block text-xs text-[var(--text-secondary)]">代理 URL</span>
            <input
              className="control-surface control-surface-compact control-surface-mono w-full"
              value={accountProxy.value}
              placeholder="http://127.0.0.1:7897"
              spellCheck={false}
              disabled={Boolean(busy)}
              onChange={event => setAccountProxy(current => ({ ...current, value: event.target.value }))}
            />
          </label>
        ) : null}
        <Button size="sm" disabled={Boolean(busy)} onClick={saveAccountProxy}>
          {busy === 'save-account-proxy' ? <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
          保存账号网络
        </Button>
        {accountProxyFeedback ? (
          <span className={cn(
            'w-full text-xs',
            accountProxyFeedback.includes('已保存') ? 'text-emerald-500' : 'text-red-500',
          )}>
            {accountProxyFeedback}
          </span>
        ) : null}
      </div>
      <div className="grid lg:grid-cols-[220px_minmax(0,1fr)]">
        <nav className="flex gap-1 overflow-x-auto border-b border-[var(--border)] p-2 lg:flex-col lg:border-b-0 lg:border-r" aria-label="接口类型">
          {SETTING_KINDS.map(kind => {
            const setting = settings[kind]
            const selected = activeKind === kind
            return (
              <button
                key={kind}
                type="button"
                aria-current={selected ? 'page' : undefined}
                onClick={() => setActiveKind(kind)}
                className={cn(
                  'flex min-w-44 items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-left text-sm transition-colors lg:min-w-0',
                  selected
                    ? 'bg-[var(--bg-active)] font-medium text-[var(--text-primary)]'
                    : 'text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]',
                )}
              >
                <span>{SETTING_TITLES[kind]}</span>
                <span className={cn(
                  'h-2 w-2 shrink-0 rounded-full',
                  setting.has_cdk ? 'bg-emerald-500' : 'bg-amber-500',
                )} aria-label={setting.has_cdk ? `${setting.cdk_count} 个 CDK` : 'CDK 未配置'} />
              </button>
            )
          })}
        </nav>
        {SETTING_KINDS.filter(kind => kind === activeKind).map(kind => {
          const title = SETTING_TITLES[kind]
          const setting = settings[kind]
          return (
            <section key={kind} className="min-w-0 p-4 lg:p-5">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h3 className="text-sm font-medium text-[var(--text-primary)]">{title}</h3>
                <Badge variant={setting.has_cdk ? 'success' : 'warning'}>
                  {setting.has_cdk ? `${setting.cdk_count} 个 CDK` : 'CDK 未配置'}
                </Badge>
              </div>
              <div className="space-y-3">
                <label className="block">
                  <span className="mb-1 block text-xs text-[var(--text-secondary)]">显示名称</span>
                  <input
                    className="control-surface control-surface-compact"
                    value={drafts[kind].display_name}
                    onChange={event => update(kind, 'display_name', event.target.value)}
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-[var(--text-secondary)]">接口地址</span>
                  <input
                    className="control-surface control-surface-compact control-surface-mono"
                    value={drafts[kind].base_url}
                    onChange={event => update(kind, 'base_url', event.target.value)}
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-[var(--text-secondary)]">{kind === 'scanner_546789' ? 'submit_cdk' : 'X-CDK-Key'}（每行一个，明文）</span>
                  <textarea
                    rows={5}
                    className="control-surface control-surface-mono min-h-28 resize-y px-3 py-2 text-xs"
                    value={drafts[kind].cdk_keys}
                    placeholder={'cdk-1\ncdk-2\ncdk-3'}
                    onChange={event => update(kind, 'cdk_keys', event.target.value)}
                    spellCheck={false}
                  />
                </label>
                {feedback[kind] && (
                  <p className={cn(
                    'text-xs',
                    feedback[kind].includes('成功') || feedback[kind].includes('保存') || feedback[kind].includes('校验完成')
                      ? 'text-emerald-500'
                      : 'text-red-500',
                  )}>
                    {feedback[kind]}
                  </p>
                )}
                {checkResults[kind].length > 0 && (
                  <div className="max-h-44 space-y-1 overflow-y-auto border border-[var(--border)] p-2">
                    {checkResults[kind].map(item => (
                      <div key={item.cdk_key} className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-2 text-[11px]">
                        <Badge variant={item.status === 'valid' ? 'success' : item.status === 'depleted' ? 'danger' : 'warning'}>
                          {item.status === 'valid' ? '有效' : item.status === 'depleted' ? '已用完' : '无效'}
                        </Badge>
                        <div className="min-w-0">
                          <div className="break-all font-mono text-[var(--text-primary)]">{item.cdk_key}</div>
                          <div className="mt-0.5 text-[var(--text-muted)]">{item.message}</div>
                          {typeof item.available_count === 'number' && (
                            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[var(--text-secondary)]">
                              <span>可用 {item.available_count}</span>
                              <span>已用 {item.used_count ?? 0}</span>
                              <span>冻结 {item.frozen_count ?? 0}</span>
                              <span>总计 {item.total_count ?? 0}</span>
                              {item.product_type ? <span>{item.product_type}</span> : null}
                              {item.cdk_status ? <span>{item.cdk_status}</span> : null}
                              {item.unlimited ? <span>无限额度</span> : null}
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                <div className="flex flex-wrap justify-end gap-2">
                  <Button variant="outline" size="sm" disabled={Boolean(busy)} onClick={() => checkCdks(kind)}>
                    {busy === `check-${kind}` ? <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                    校验 CDK
                  </Button>
                  <Button variant="outline" size="sm" disabled={Boolean(busy)} onClick={() => test(kind)}>
                    {busy === `test-${kind}` ? <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                    测试连接
                  </Button>
                  <Button size="sm" disabled={Boolean(busy)} onClick={() => save(kind)}>
                    {busy === `save-${kind}` ? <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                    保存配置
                  </Button>
                </div>
              </div>
            </section>
          )
        })}
      </div>
    </div>
  )
}

export default function KakaoPipeline() {
  const [settings, setSettings] = useState<KakaoSettings | null>(null)
  const [accounts, setAccounts] = useState<KakaoAccount[]>([])
  const [pushTargets, setPushTargets] = useState<PushTarget[]>([])
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [view, setView] = useState<PipelineView>('workspace')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [pageLoading, setPageLoading] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [operations, setOperations] = useState<Record<number, string>>({})
  const [selectedAccountIds, setSelectedAccountIds] = useState<Set<number>>(() => new Set())
  const [archiveBusy, setArchiveBusy] = useState('')
  const [expanded, setExpanded] = useState<number | null>(null)
  const [details, setDetails] = useState<Record<number, Pipeline>>({})
  const [toast, setToast] = useState<{ type: 'success' | 'warning' | 'error'; text: string } | null>(null)
  const activeRequests = useRef(new Set<number>())
  const loadRequestRef = useRef(0)
  const accountLoadInFlightRef = useRef(false)
  const hasLoadedAccountsRef = useRef(false)
  const selectAllCheckboxRef = useRef<HTMLInputElement | null>(null)

  const loadSettings = useCallback(async () => {
    const result = await apiFetch('/kakao-pipeline/settings')
    setSettings(result)
  }, [])

  const loadPushTargets = useCallback(async () => {
    const result = await apiFetch('/accounts/push-targets')
    setPushTargets(Array.isArray(result?.items) ? result.items : [])
  }, [])

  const loadAccounts = useCallback(async (showLoading = true) => {
    const requestId = ++loadRequestRef.current
    accountLoadInFlightRef.current = true
    const showSkeleton = showLoading && !hasLoadedAccountsRef.current
    if (showSkeleton) setLoading(true)
    if (showLoading && !showSkeleton) setPageLoading(true)
    try {
      const params = new URLSearchParams({
        page: String(page),
        pageSize: String(PAGE_SIZE),
        view,
      })
      if (debouncedSearch) params.set('search', debouncedSearch)
      const result = await apiFetch(`/kakao-pipeline/accounts?${params.toString()}`)
      if (requestId !== loadRequestRef.current) return
      const nextAccounts = Array.isArray(result?.items) ? result.items : []
      const nextTotal = Number(result?.total || 0)
      const nextTotalPages = Math.max(1, Math.ceil(nextTotal / PAGE_SIZE))
      setTotal(nextTotal)
      if (page > nextTotalPages) {
        setAccounts([])
        setSelectedAccountIds(new Set())
        setPage(nextTotalPages)
        return
      }
      const visibleIds = new Set<number>(nextAccounts.map((item: KakaoAccount) => item.id))
      setAccounts(nextAccounts)
      setSelectedAccountIds(current => {
        const next = new Set([...current].filter(accountId => visibleIds.has(accountId)))
        return next.size === current.size ? current : next
      })
      hasLoadedAccountsRef.current = true
    } catch (error) {
      if (requestId !== loadRequestRef.current) return
      setToast({ type: 'error', text: parseError(error) })
    } finally {
      if (requestId === loadRequestRef.current && showLoading) {
        setLoading(false)
        setPageLoading(false)
      }
      if (requestId === loadRequestRef.current) accountLoadInFlightRef.current = false
    }
  }, [debouncedSearch, page, view])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setPage(1)
      setDebouncedSearch(search.trim())
    }, 350)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    setSelectedAccountIds(new Set())
  }, [page, search, view])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 4500)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    loadSettings().catch(error => {
      setToast({ type: 'error', text: parseError(error) })
    })
    loadPushTargets().catch(error => {
      setToast({ type: 'error', text: parseError(error) })
    })
  }, [loadPushTargets, loadSettings])

  useEffect(() => {
    void loadAccounts()
  }, [loadAccounts])

  const run = useCallback(async (
    accountId: number,
    label: string,
    path: string,
    body: Record<string, unknown> = {},
    options?: { quiet?: boolean; checkPlusAfter?: boolean; refreshAfter?: boolean },
  ) => {
    if (activeRequests.current.has(accountId)) return null
    activeRequests.current.add(accountId)
    setOperations(current => ({ ...current, [accountId]: label }))
    try {
      const result = await apiFetch(path, { method: 'POST', body: JSON.stringify(body) })
      if (options?.checkPlusAfter && result?.state === 'scanner_succeeded') {
        await apiFetch(`/kakao-pipeline/accounts/${accountId}/plus/check`, {
          method: 'POST',
          body: JSON.stringify({ advance_pipeline: true }),
        })
      }
      if (options?.refreshAfter !== false) await loadAccounts(false)
      if (!options?.quiet) setToast({ type: 'success', text: `${label}完成` })
      return result
    } catch (error) {
      if (!options?.quiet) setToast({ type: 'error', text: parseError(error) })
      await Promise.all([
        options?.refreshAfter !== false ? loadAccounts(false) : Promise.resolve(),
        path.endsWith('/scanner') ? loadSettings() : Promise.resolve(),
      ])
      return null
    } finally {
      activeRequests.current.delete(accountId)
      if (!options?.quiet) {
        setDetails(current => {
          if (!(accountId in current)) return current
          const next = { ...current }
          delete next[accountId]
          return next
        })
      }
      setOperations(current => {
        const next = { ...current }
        delete next[accountId]
        return next
      })
    }
  }, [loadAccounts, loadSettings])

  useEffect(() => {
    const refreshVisiblePage = () => {
      if (document.visibilityState !== 'visible' || accountLoadInFlightRef.current) return
      void loadAccounts(false)
    }
    const refreshInterval = view === 'workspace' || view === 'all' ? 3000 : 15000
    const timer = window.setInterval(refreshVisiblePage, refreshInterval)
    document.addEventListener('visibilitychange', refreshVisiblePage)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refreshVisiblePage)
    }
  }, [loadAccounts, view])

  const toggleDetail = async (accountId: number) => {
    if (expanded === accountId) {
      setExpanded(null)
      return
    }
    setExpanded(accountId)
  }

  useEffect(() => {
    if (expanded === null) return
    let cancelled = false
    const refreshDetail = async () => {
      try {
        const detail = await apiFetch(`/kakao-pipeline/accounts/${expanded}`)
        if (!cancelled) setDetails(current => ({ ...current, [expanded]: detail }))
      } catch (error) {
        if (!cancelled) {
          setExpanded(null)
          setToast({ type: 'error', text: parseError(error) })
        }
      }
    }
    void refreshDetail()
    const timer = window.setInterval(refreshDetail, 2000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [expanded])

  const copyLink = async (value: string) => {
    try {
      await copyText(value)
      setToast({ type: 'success', text: 'Kakao 长链已复制' })
    } catch {
      setToast({ type: 'error', text: '复制失败，请打开详情手动复制' })
    }
  }

  const copyAccountCredential = async (accountId: number, credentialType: 'session' | 'access') => {
    try {
      const response = await apiFetch(`/accounts/${accountId}/credentials?scope=platform`)
      const items = Array.isArray(response?.items) ? response.items : []
      const keys = credentialType === 'session'
        ? ['session_token', 'sessionToken', 'session_cookie']
        : ['access_token', 'accessToken']
      const credential = items.find((item: { key?: string; value?: string }) => keys.includes(String(item.key || '')) && item.value)
      const cookieCredential = credentialType === 'session'
        ? items.find((item: { key?: string; value?: string }) => ['cookies', 'cookie'].includes(String(item.key || '')) && item.value)
        : null
      const value = credential?.value || sessionTokenFromCookies(cookieCredential?.value || '')
      if (!value) throw new Error(credentialType === 'session' ? '该账号没有 Session Token' : '该账号没有 Access Token')
      await copyText(value)
      setToast({ type: 'success', text: credentialType === 'session' ? 'Session Token 已复制' : 'Access Token 已复制' })
    } catch (error) {
      setToast({ type: 'error', text: parseError(error) })
    }
  }

  const authorizeCodex = async (account: KakaoAccount) => {
    if (!pipelinePlusComplete(account)) {
      setToast({ type: 'error', text: '请先完成 Plus 确认，再启动 Codex 授权' })
      return
    }
    if (activeRequests.current.has(account.id)) return
    activeRequests.current.add(account.id)
    setOperations(current => ({ ...current, [account.id]: '启动 Codex 授权' }))
    try {
      const result = await apiFetch(`/kakao-pipeline/accounts/${account.id}/codex`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      const action = result?.post_actions?.codex || result?.codex || result
      const status = normalizePostActionStatus(action?.status)
      setToast({
        type: 'success',
        text: ['success', 'succeeded', 'skipped'].includes(status)
          ? `${account.email} 已完成 Codex 授权`
          : `${account.email} 的 Codex 授权任务已启动`,
      })
      await loadAccounts(false)
    } catch (error) {
      setToast({ type: 'error', text: parseError(error) })
      await loadAccounts(false)
    } finally {
      activeRequests.current.delete(account.id)
      setOperations(current => {
        const next = { ...current }
        delete next[account.id]
        return next
      })
    }
  }

  const pushAccount = async (account: KakaoAccount) => {
    const linkedTargetKey = String(account.pipeline.post_actions?.push?.target_key || '').trim()
    const target = pushTargets.find(item => item.key === linkedTargetKey)
      || pushTargets.find(item => item.is_default)
      || pushTargets[0]
    if (!target) {
      setToast({ type: 'error', text: '请先到设置中配置并启用推送目标' })
      return
    }
    if (activeRequests.current.has(account.id)) return
    activeRequests.current.add(account.id)
    setOperations(current => ({ ...current, [account.id]: '推送中' }))
    try {
      const result = await apiFetch('/accounts/push', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids: [account.id],
          select_all: false,
          target_key: target.key,
        }),
      })
      const failed = Number(result?.failed || 0)
      if (failed > 0) throw new Error(result?.results?.[0]?.error || '推送失败')
      setToast({ type: 'success', text: `${account.email} 已推送到 ${result?.target_label || target.label}` })
    } catch (error) {
      setToast({ type: 'error', text: parseError(error) })
    } finally {
      activeRequests.current.delete(account.id)
      setOperations(current => {
        const next = { ...current }
        delete next[account.id]
        return next
      })
      await loadAccounts(false)
    }
  }

  const runArchiveRequest = async (
    targets: KakaoAccount[],
    label: string,
    path: string,
    body: Record<string, unknown>,
    successText: string,
  ) => {
    const accountIds = targets.map(account => account.id)
    if (!accountIds.length || archiveBusy) return
    if (accountIds.some(accountId => activeRequests.current.has(accountId))) {
      setToast({ type: 'error', text: '所选账号仍有操作正在提交，请稍候再试' })
      return
    }

    accountIds.forEach(accountId => activeRequests.current.add(accountId))
    setArchiveBusy(label)
    setOperations(current => {
      const next = { ...current }
      accountIds.forEach(accountId => { next[accountId] = label })
      return next
    })
    try {
      const result = await apiFetch(path, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const errorCount = Number(result?.failed ?? result?.error_count ?? 0)
      const successCount = Number(result?.succeeded ?? result?.success_count ?? Math.max(0, accountIds.length - errorCount))
      const resultItems = Array.isArray(result?.results) ? result.results : (Array.isArray(result?.items) ? result.items : [])
      const failedAccountIds = new Set<number>(
        resultItems.flatMap((item: { account_id?: unknown; ok?: boolean; success?: boolean }) => {
          const accountId = Number(item?.account_id || 0)
          return (item?.ok === false || item?.success === false) && accountId > 0 ? [accountId] : []
        }),
      )
      setSelectedAccountIds(failedAccountIds)
      setDetails(current => {
        const next = { ...current }
        accountIds.forEach(accountId => {
          if (!failedAccountIds.has(accountId)) delete next[accountId]
        })
        return next
      })
      const warningMessages = [...new Set(
        resultItems.flatMap((item: { warnings?: unknown }) => (
          Array.isArray(item?.warnings)
            ? item.warnings.map(warning => String(warning || '').trim()).filter(Boolean)
            : []
        )),
      )]
      const warningSuffix = warningMessages.length ? `。警告：${warningMessages.join('；')}` : ''
      if (result?.ok === false || errorCount > 0) {
        const firstError = resultItems.length
          ? String(resultItems.find((item: { ok?: boolean; success?: boolean; error?: string }) => (
            item?.ok === false || item?.success === false
          ))?.error || '').trim()
          : ''
        setToast({
          type: 'error',
          text: `${label}部分失败：成功 ${successCount}，失败 ${errorCount}${firstError ? `。${firstError}` : ''}${warningSuffix}`,
        })
      } else if (warningMessages.length > 0) {
        setToast({ type: 'warning', text: `${successText}${warningSuffix}` })
      } else {
        setToast({ type: 'success', text: successText })
      }
      await loadAccounts(false)
    } catch (error) {
      setToast({ type: 'error', text: parseError(error) })
    } finally {
      accountIds.forEach(accountId => activeRequests.current.delete(accountId))
      setOperations(current => {
        const next = { ...current }
        accountIds.forEach(accountId => { delete next[accountId] })
        return next
      })
      setArchiveBusy('')
    }
  }

  const archiveAccounts = async (targets: KakaoAccount[]) => {
    const candidates = targets.filter(account => !pipelineIsArchived(account))
    if (!candidates.length) return

    const incomplete = candidates.filter(account => !pipelineIsCompleteForArchive(account))
    const force = candidates.some(pipelineArchiveRequiresForce)
    const defaultReason = incomplete.length
      ? (candidates.length === 1 ? '人工放弃 Kakao 流程' : `批量放弃并归档 ${incomplete.length} 个未完成流程`)
      : (candidates.length === 1 ? 'Kakao 流程已完成' : `批量归档 ${candidates.length} 个已完成流程`)
    const reasonInput = window.prompt(
      incomplete.length
        ? `所选账号中有 ${incomplete.length} 个流程尚未完成，将标记为“已放弃”。请输入归档原因：`
        : '请输入归档原因：',
      defaultReason,
    )
    if (reasonInput === null) return
    const reason = reasonInput.trim()
    if (!reason) {
      setToast({ type: 'error', text: '归档原因不能为空' })
      return
    }

    const confirmed = window.confirm(
      force
        ? `强制归档确认\n\n所选账号中有正在执行或结果尚不确定的流程。继续后只会停止本地后续推进并归档 ${candidates.length} 个账号；远端供应商或扫码任务无法从本地取消，仍可能继续执行。此操作需要稍后手动恢复才能继续。`
        : incomplete.length
          ? `确认放弃并归档 ${candidates.length} 个账号？未完成流程会标记为“已放弃”。`
          : `确认归档 ${candidates.length} 个已完成账号？`,
    )
    if (!confirmed) return

    await runArchiveRequest(
      candidates,
      force ? '强制归档中' : '归档中',
      '/kakao-pipeline/archive',
      {
        account_ids: candidates.map(account => account.id),
        reason,
        disposition: 'auto',
        force,
      },
      incomplete.length
        ? `已放弃并归档 ${candidates.length} 个账号`
        : `已归档 ${candidates.length} 个账号`,
    )
  }

  const restoreArchivedAccounts = async (targets: KakaoAccount[]) => {
    const candidates = targets.filter(account => pipelineIsArchived(account) && !pipelineIsPurged(account))
    if (!candidates.length) return
    if (!window.confirm(`确认恢复 ${candidates.length} 个归档账号到工作台？`)) return
    await runArchiveRequest(
      candidates,
      '恢复归档中',
      '/kakao-pipeline/archive/restore',
      { account_ids: candidates.map(account => account.id) },
      `已恢复 ${candidates.length} 个账号`,
    )
  }

  const retryArchivedTaskCancellation = async (targets: KakaoAccount[]) => {
    const candidates = targets.filter(account => (
      pipelineIsArchived(account)
      && !pipelineIsPurged(account)
      && postActionsAreActive(account)
    ))
    if (!candidates.length) return
    await runArchiveRequest(
      candidates,
      '重试停止任务中',
      '/kakao-pipeline/archive',
      {
        account_ids: candidates.map(account => account.id),
        reason: '',
        disposition: 'auto',
        force: true,
      },
      `已重新请求停止 ${candidates.length} 个账号的关联任务`,
    )
  }

  const purgeArchivedAccounts = async (targets: KakaoAccount[]) => {
    const candidates = targets.filter(account => pipelineIsArchived(account) && !pipelineIsPurged(account))
    if (!candidates.length) return
    const confirmationText = candidates.length === 1
      ? candidates[0].email
      : `永久清除 ${candidates.length} 个账号`
    const typed = window.prompt(
      `永久清除会删除归档流水线的订单、响应和日志详情，且不可恢复。\n请输入“${confirmationText}”确认：`,
      '',
    )
    if (typed === null) return
    if (typed.trim() !== confirmationText) {
      setToast({ type: 'error', text: '确认文字不匹配，已取消永久清除' })
      return
    }
    await runArchiveRequest(
      candidates,
      '永久清除中',
      '/kakao-pipeline/archive/purge',
      { account_ids: candidates.map(account => account.id) },
      `已永久清除 ${candidates.length} 个账号的流水线详情`,
    )
  }

  const renderActions = (account: KakaoAccount) => {
    const pipeline = account.pipeline
    const busy = operations[account.id]

    if (pipelineIsArchived(account)) {
      if (busy) {
        return (
          <div className="flex items-center justify-end">
            <Button variant="outline" size="sm" disabled>
              <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> {busy}
            </Button>
          </div>
        )
      }
      if (pipelineIsPurged(account)) {
        return (
          <div className="flex items-center justify-end">
            <span className="text-xs text-[var(--text-muted)]">详情已永久清除</span>
          </div>
        )
      }
      const linkedTaskActive = postActionsAreActive(account)
      return (
        <div className="flex flex-wrap items-center justify-end gap-1.5">
          <Button
            variant="outline"
            size="sm"
            disabled={Boolean(archiveBusy)}
            onClick={() => void toggleDetail(account.id)}
          >
            <FileText className="mr-1.5 h-3.5 w-3.5" /> 查看日志
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={Boolean(archiveBusy)}
            onClick={() => void restoreArchivedAccounts([account])}
          >
            <ArchiveRestore className="mr-1.5 h-3.5 w-3.5" /> 恢复
          </Button>
          {linkedTaskActive ? (
            <Button
              variant="outline"
              size="sm"
              disabled={Boolean(archiveBusy)}
              title="再次请求取消仍在运行的关联任务"
              onClick={() => void retryArchivedTaskCancellation([account])}
            >
              <RotateCcw className="mr-1.5 h-3.5 w-3.5" /> 重试停止任务
            </Button>
          ) : null}
          <Button
            variant="destructive"
            size="sm"
            disabled={Boolean(archiveBusy) || linkedTaskActive}
            title={linkedTaskActive ? '等待关联任务停止后再永久清除' : '永久清除流水线详情'}
            onClick={() => void purgeArchivedAccounts([account])}
          >
            <Trash2 className="mr-1.5 h-3.5 w-3.5" /> 永久清除
          </Button>
        </div>
      )
    }

    const { plusComplete, codexSettled, codexState, pushState } = getPostActionPresentation(account)
    const supplierReady = Boolean(settings?.supplier.has_cdk)
    const scannerKind = settings?.default_scanner_kind || 'scanner'
    const selectedScanner = settings?.[scannerKind]
    const scannerReady = Boolean(selectedScanner?.has_cdk)
    const hasPushTarget = pushTargets.length > 0
    const codexReady = plusComplete && codexState !== 'active'
    const codexTitle = !plusComplete
      ? '请先完成 Plus 确认'
      : codexState === 'active'
        ? 'Codex 授权任务正在执行'
        : '启动或重试 Codex 授权'
    const pushReady = hasPushTarget && codexSettled && pushState !== 'active'
    const pushTitle = !codexSettled
      ? '请先完成 Codex 授权'
      : !hasPushTarget
        ? '请先到设置中配置并启用推送目标'
        : pushState === 'active'
          ? '推送任务正在执行'
          : '推送到默认目标'

    const checkPlus = () => {
      const advancePipeline = pipeline.state !== 'completed' && ADVANCE_PLUS_PIPELINE_STATES.has(pipeline.state)
      void run(
        account.id,
        '检测 Plus',
        `/kakao-pipeline/accounts/${account.id}/plus/check`,
        advancePipeline ? { advance_pipeline: true } : {},
      )
    }

    const refreshAccountStatus = () => {
      void run(account.id, '刷新账号状态', `/kakao-pipeline/accounts/${account.id}/plus/check`)
    }

    const forceReset = () => {
      const confirmed = window.confirm(
        `确认强制重置 ${account.email} 的 Kakao 流水线？正在执行或卡住的任务记录也会被清除。`,
      )
      if (!confirmed) return
      void run(
        account.id,
        '重置记录',
        `/kakao-pipeline/accounts/${account.id}/reset`,
        { force: true },
      )
    }

    if (busy) {
      return (
        <div className="flex items-center justify-end gap-1.5">
          <Button variant="outline" size="sm" disabled>
            <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> {busy}
          </Button>
          <AccountMoreMenu
            accountId={account.id}
            onCopyCredential={copyAccountCredential}
            onForceReset={forceReset}
            onCheckPlus={checkPlus}
            onAuthorizeCodex={() => void authorizeCodex(account)}
            onPush={() => void pushAccount(account)}
            codexReady={codexReady}
            codexTitle={codexTitle}
            pushReady={pushReady}
            pushTitle={pushTitle}
            actionDisabled
          />
        </div>
      )
    }
    return (
      <div className="flex flex-wrap items-center justify-end gap-1.5">
        {(pipeline.state === 'idle' || pipeline.state === 'supplier_failed') && !accountIsPlus(account) && (
          <Button
            size="sm"
            disabled={!supplierReady}
            title={supplierReady ? '调用供应商提取 Kakao 长链' : '请先配置供应商 CDK'}
            onClick={() => run(account.id, '提取链接', `/kakao-pipeline/accounts/${account.id}/extract`, {
              supplier_setting_id: settings?.supplier.id,
              payment_method: 'kakao_pay',
            })}
          >
            <Link2 className="mr-1.5 h-3.5 w-3.5" /> 提取 Kakao 链接
          </Button>
        )}
        {['supplier_processing', 'supplier_poll_failed'].includes(pipeline.state) && pipeline.supplier_order_id && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => run(account.id, '查询原提链订单', `/kakao-pipeline/accounts/${account.id}/supplier/poll`)}
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> 查询提链
          </Button>
        )}
        {['link_ready', 'scanner_failed'].includes(pipeline.state) && pipeline.payment_url && (
          <>
            <Button variant="outline" size="sm" onClick={() => copyLink(pipeline.payment_url || '')}>
              <Copy className="mr-1.5 h-3.5 w-3.5" /> 复制链接
            </Button>
            <Button
              size="sm"
              disabled={!scannerReady}
              title={scannerReady ? `上传到 ${selectedScanner?.display_name}` : `请先配置 ${selectedScanner?.display_name || '默认扫码供应商'} CDK`}
              onClick={() => {
                if (!window.confirm(`确认将 ${account.email} 的 Kakao 长链上传到 ${selectedScanner?.display_name}？`)) return
                void run(account.id, '上传扫码', `/kakao-pipeline/accounts/${account.id}/scanner`, {
                  scanner_setting_id: selectedScanner?.id,
                  scanner_kind: scannerKind,
                })
              }}
            >
              <ScanLine className="mr-1.5 h-3.5 w-3.5" /> 上传扫码
            </Button>
          </>
        )}
        {['scanner_processing', 'scanner_poll_failed'].includes(pipeline.state) && pipeline.scanner_order_id && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => run(
              account.id,
              '查询原扫码订单',
              `/kakao-pipeline/accounts/${account.id}/scanner/poll`,
              {},
              { checkPlusAfter: true },
            )}
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> 查询扫码
          </Button>
        )}
        {ADVANCE_PLUS_PIPELINE_STATES.has(pipeline.state) && pipeline.state !== 'plus_checking' ? (
          <Button
            variant="outline"
            size="sm"
            onClick={checkPlus}
          >
            <Check className="mr-1.5 h-3.5 w-3.5" /> 检测 Plus
          </Button>
        ) : null}
        {pipeline.state === 'completed' || (accountIsPlus(account) && !ADVANCE_PLUS_PIPELINE_STATES.has(pipeline.state)) ? (
          <Button variant="outline" size="sm" onClick={refreshAccountStatus}>
            <Check className="mr-1.5 h-3.5 w-3.5" /> 刷新账号状态
          </Button>
        ) : null}
        <Button
          variant="outline"
          size="sm"
          disabled={Boolean(archiveBusy)}
          className={pipelineIsCompleteForArchive(account) ? undefined : 'text-amber-500 hover:text-amber-500'}
          title={pipelineIsCompleteForArchive(account) ? '归档已完成流水线' : '放弃当前流程并归档'}
          onClick={() => void archiveAccounts([account])}
        >
          <Archive className="mr-1.5 h-3.5 w-3.5" />
          {pipelineIsCompleteForArchive(account) ? '归档' : '放弃并归档'}
        </Button>
        <AccountMoreMenu
          accountId={account.id}
          onCopyCredential={copyAccountCredential}
          onForceReset={forceReset}
          onCheckPlus={checkPlus}
          onAuthorizeCodex={() => void authorizeCodex(account)}
          onPush={() => void pushAccount(account)}
          codexReady={codexReady}
          codexTitle={codexTitle}
          pushReady={pushReady}
          pushTitle={pushTitle}
          onShowLog={pipeline.state !== 'idle' ? () => toggleDetail(account.id) : undefined}
        />
      </div>
    )
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const activeCount = accounts.filter(account => (
    !pipelineIsArchived(account)
    && (ACTIVE_PIPELINE_STATES.has(account.pipeline.state) || postActionsAreActive(account))
  )).length
  const readyCount = accounts.filter(account => !pipelineIsArchived(account) && account.pipeline.state === 'link_ready').length
  const errorCount = accounts.filter(account => (
    !pipelineIsArchived(account)
    && (ATTENTION_PIPELINE_STATES.has(account.pipeline.state) || postActionsHaveError(account))
  )).length
  const completedCount = accounts.filter(account => !pipelineIsArchived(account) && pipelineFlowComplete(account)).length
  const archivedCount = accounts.filter(account => pipelineIsArchived(account) && !pipelineIsPurged(account)).length
  const abandonedCount = accounts.filter(account => pipelineIsArchived(account) && pipelineWasAbandoned(account) && !pipelineIsPurged(account)).length
  const purgedCount = accounts.filter(pipelineIsPurged).length
  const selectableAccounts = accounts.filter(account => !pipelineIsPurged(account))
  const selectedAccounts = accounts.filter(account => selectedAccountIds.has(account.id))
  const selectedUnarchivedAccounts = selectedAccounts.filter(account => !pipelineIsArchived(account))
  const selectedArchivedAccounts = selectedAccounts.filter(account => pipelineIsArchived(account) && !pipelineIsPurged(account))
  const selectedArchivedActiveAccounts = selectedArchivedAccounts.filter(postActionsAreActive)
  const selectedArchivedHasActiveTask = selectedArchivedActiveAccounts.length > 0
  const selectedUnarchivedIncompleteCount = selectedUnarchivedAccounts.filter(account => !pipelineIsCompleteForArchive(account)).length
  const selectedHasPendingOperation = selectedAccounts.some(account => Boolean(operations[account.id]))
  const selectedOnPageCount = selectableAccounts.filter(account => selectedAccountIds.has(account.id)).length
  const allPageSelected = selectableAccounts.length > 0 && selectedOnPageCount === selectableAccounts.length
  const expandedAccount = expanded === null ? null : accounts.find(account => account.id === expanded) || null

  useEffect(() => {
    if (!selectAllCheckboxRef.current) return
    selectAllCheckboxRef.current.indeterminate = selectedOnPageCount > 0 && !allPageSelected
  }, [allPageSelected, selectedOnPageCount])

  const toggleAccountSelection = (accountId: number, checked: boolean) => {
    setSelectedAccountIds(current => {
      const next = new Set(current)
      if (checked) next.add(accountId)
      else next.delete(accountId)
      return next
    })
  }

  const togglePageSelection = (checked: boolean) => {
    setSelectedAccountIds(current => {
      const next = new Set(current)
      selectableAccounts.forEach(account => {
        if (checked) next.add(account.id)
        else next.delete(account.id)
      })
      return next
    })
  }

  return (
    <div className="space-y-5">
      {expandedAccount ? (
        <PipelineLogDrawer
          account={expandedAccount}
          detail={details[expandedAccount.id]}
          onClose={() => setExpanded(null)}
          onCopyLink={copyLink}
        />
      ) : null}
      {toast && (
        <button
          type="button"
          aria-live="polite"
          onClick={() => setToast(null)}
          className={cn(
            'fixed right-5 top-5 z-[70] flex max-w-md items-center gap-2 rounded-lg border px-4 py-3 text-left text-sm shadow-[var(--shadow-hard)]',
            toast.type === 'success'
              ? 'border-emerald-500/30 bg-[var(--bg-card)] text-emerald-500'
              : toast.type === 'warning'
                ? 'border-amber-500/30 bg-[var(--bg-card)] text-amber-500'
                : 'border-red-500/30 bg-[var(--bg-card)] text-red-500',
          )}
        >
          {toast.type === 'success' ? <CheckCircle2 className="h-4 w-4 shrink-0" /> : <AlertTriangle className="h-4 w-4 shrink-0" />}
          {toast.text}
        </button>
      )}

      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[var(--accent-soft)] text-[var(--accent)]">
              <ScanLine className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-[var(--text-primary)]">Kakao 流水线</h1>
              <p className="mt-0.5 text-sm text-[var(--text-muted)]">按账号完成提链、扫码、Plus 复检、Codex 授权与推送。</p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => loadAccounts()}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> 刷新账号
          </Button>
          <Button variant={settingsOpen ? 'default' : 'outline'} size="sm" onClick={() => setSettingsOpen(value => !value)}>
            <Settings2 className="mr-1.5 h-3.5 w-3.5" /> 流水线配置
          </Button>
        </div>
      </header>

      {settings && settingsOpen && (
        <SettingsPanel settings={settings} onClose={() => setSettingsOpen(false)} onSaved={loadSettings} />
      )}

      {settings && (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-y border-[var(--border)] py-3 text-xs">
          <div className="flex items-center gap-2">
            <span className={cn('h-2 w-2 rounded-full', settings.supplier.has_cdk ? 'bg-emerald-500' : 'bg-amber-500')} />
            <span className="text-[var(--text-muted)]">提链</span>
            <span className="font-medium text-[var(--text-primary)]">{settings.supplier.display_name}</span>
            <span className="text-[var(--text-secondary)]">{settings.supplier.has_cdk ? `${settings.supplier.cdk_count} 个 CDK` : '未配置 CDK'}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className={cn('h-2 w-2 rounded-full', settings[settings.default_scanner_kind].has_cdk ? 'bg-emerald-500' : 'bg-amber-500')} />
            <span className="text-[var(--text-muted)]">扫码</span>
            <span className="font-medium text-[var(--text-primary)]">{settings[settings.default_scanner_kind].display_name}</span>
            <span className="text-[var(--text-secondary)]">
              {settings[settings.default_scanner_kind].has_cdk ? `${settings[settings.default_scanner_kind].cdk_count} 个 CDK` : '未配置 CDK'}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[var(--text-muted)]">提链后</span>
            <span className="font-medium text-[var(--text-primary)]">{settings.auto_upload_after_extract ? '自动上传扫码' : '人工确认上传'}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[var(--text-muted)]">账号网络</span>
            <span className="font-medium text-[var(--text-primary)]">
              {settings.account_proxy.mode === 'proxy_service'
                ? '代理服务'
                : settings.account_proxy.mode === 'manual'
                  ? `手动代理 ${settings.account_proxy.preview}`
                  : '直连'}
            </span>
          </div>
        </div>
      )}

      <nav
        className="flex gap-1 overflow-x-auto border-b border-[var(--border)]"
        role="tablist"
        aria-label="Kakao 流水线视图"
      >
        {PIPELINE_VIEWS.map(item => {
          const active = view === item.key
          return (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={active}
              aria-controls="kakao-accounts-table"
              className={cn(
                'relative shrink-0 px-4 py-2.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]',
                active
                  ? 'text-[var(--accent)] after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:rounded-full after:bg-[var(--accent)]'
                  : 'text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]',
              )}
              onClick={() => {
                if (item.key === view) return
                setPage(1)
                setView(item.key)
              }}
            >
              {item.label}
              {active ? <span className="ml-1.5 text-xs text-[var(--text-muted)]">{total}</span> : null}
            </button>
          )
        })}
      </nav>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input
            className="control-surface pl-9"
            placeholder="搜索本地 ChatGPT 账号"
            value={search}
            onChange={event => setSearch(event.target.value)}
          />
        </div>
        <div className="flex flex-wrap items-center justify-end gap-x-4 gap-y-1 text-xs">
          <span className="text-[var(--text-muted)]">本页</span>
          {activeCount > 0 ? <span className="text-[var(--accent)]">处理中 {activeCount}</span> : null}
          {readyCount > 0 ? <span className="text-amber-500">待上传 {readyCount}</span> : null}
          {errorCount > 0 ? <span className="text-red-500">异常 {errorCount}</span> : null}
          {completedCount > 0 ? <span className="text-emerald-500">已完成 {completedCount}</span> : null}
          {archivedCount > 0 ? <span className="text-[var(--text-secondary)]">已归档 {archivedCount}</span> : null}
          {abandonedCount > 0 ? <span className="text-amber-500">已放弃 {abandonedCount}</span> : null}
          {purgedCount > 0 ? <span className="text-red-500">已清除 {purgedCount}</span> : null}
          <span className="text-[var(--text-muted)]">共 {total} 个账号</span>
        </div>
      </div>

      {selectedAccounts.length > 0 ? (
        <div
          className="flex flex-wrap items-center justify-between gap-3 border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-4 py-3"
          aria-label="批量归档操作"
        >
          <div className="flex items-center gap-3 text-sm">
            <span className="font-medium text-[var(--text-primary)]">已选 {selectedAccounts.length} 个账号</span>
            {archiveBusy ? (
              <span className="flex items-center gap-1.5 text-xs text-[var(--text-secondary)]" role="status">
                <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> {archiveBusy}
              </span>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {selectedUnarchivedAccounts.length > 0 ? (
              <Button
                variant="outline"
                size="sm"
                disabled={Boolean(archiveBusy) || selectedHasPendingOperation}
                className={selectedUnarchivedIncompleteCount > 0 ? 'text-amber-500 hover:text-amber-500' : undefined}
                onClick={() => void archiveAccounts(selectedUnarchivedAccounts)}
              >
                <Archive className="mr-1.5 h-3.5 w-3.5" />
                {selectedUnarchivedIncompleteCount > 0 ? '放弃并归档' : '归档'} {selectedUnarchivedAccounts.length} 个
              </Button>
            ) : null}
            {selectedArchivedAccounts.length > 0 ? (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={Boolean(archiveBusy) || selectedHasPendingOperation}
                  onClick={() => void restoreArchivedAccounts(selectedArchivedAccounts)}
                >
                  <ArchiveRestore className="mr-1.5 h-3.5 w-3.5" /> 恢复 {selectedArchivedAccounts.length} 个
                </Button>
                {selectedArchivedActiveAccounts.length > 0 ? (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={Boolean(archiveBusy) || selectedHasPendingOperation}
                    title="再次请求取消仍在运行的关联任务"
                    onClick={() => void retryArchivedTaskCancellation(selectedArchivedActiveAccounts)}
                  >
                    <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                    重试停止 {selectedArchivedActiveAccounts.length} 个
                  </Button>
                ) : null}
                <Button
                  variant="destructive"
                  size="sm"
                  disabled={Boolean(archiveBusy) || selectedHasPendingOperation || selectedArchivedHasActiveTask}
                  title={selectedArchivedHasActiveTask ? '所选账号仍有关联任务未停止' : '永久清除所选流水线详情'}
                  onClick={() => void purgeArchivedAccounts(selectedArchivedAccounts)}
                >
                  <Trash2 className="mr-1.5 h-3.5 w-3.5" /> 永久清除 {selectedArchivedAccounts.length} 个
                </Button>
              </>
            ) : null}
            <Button
              variant="ghost"
              size="sm"
              disabled={Boolean(archiveBusy)}
              onClick={() => setSelectedAccountIds(new Set())}
            >
              取消选择
            </Button>
          </div>
        </div>
      ) : null}

      <div id="kakao-accounts-table" className="overflow-hidden border border-[var(--border)] bg-[var(--bg-surface)]">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1160px] table-fixed text-left" aria-busy={loading || pageLoading}>
            <colgroup>
              <col className="w-12" />
              <col className="w-[23%]" />
              <col className="w-[32%]" />
              <col className="w-[13%]" />
              <col />
            </colgroup>
            <thead className="border-b border-[var(--border)] bg-[var(--bg-pane)] text-[var(--text-muted)]">
              <tr>
                <th className="py-3 pl-4 pr-1">
                  <input
                    ref={selectAllCheckboxRef}
                    type="checkbox"
                    checked={allPageSelected}
                    disabled={loading || pageLoading || Boolean(archiveBusy) || selectableAccounts.length === 0}
                    aria-label="选择本页可操作账号"
                    onChange={event => togglePageSelection(event.target.checked)}
                  />
                </th>
                <th className="px-4 py-3">账号</th>
                <th className="px-4 py-3 text-center">{view === 'archived' ? '归档信息' : view === 'all' ? '流程 / 归档信息' : '流程进度'}</th>
                <th className="px-4 py-3">最新时间</th>
                <th className="px-4 py-3 text-right">{view === 'archived' ? '归档操作' : '下一步'}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-soft)]">
              {loading ? (
                Array.from({ length: 6 }).map((_, index) => (
                  <tr key={index}>
                    {Array.from({ length: 5 }).map((__, cell) => (
                      <td key={cell} className="px-4 py-4">
                        <div className="h-4 animate-pulse rounded bg-[var(--chip-bg)]" />
                      </td>
                    ))}
                  </tr>
                ))
              ) : accounts.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-14 text-center text-sm text-[var(--text-muted)]">
                    {view === 'archived' ? '没有找到归档账号。' : view === 'completed' ? '没有找到已完成账号。' : '没有找到本地 ChatGPT 账号。'}
                  </td>
                </tr>
              ) : accounts.map(account => (
                    <tr key={account.id} className={cn(
                      'align-top transition-colors hover:bg-[var(--bg-hover)]',
                      pipelineIsPurged(account)
                        ? 'bg-red-500/[0.025]'
                        : pipelineIsArchived(account)
                          ? 'bg-slate-500/[0.035]'
                          : ATTENTION_PIPELINE_STATES.has(account.pipeline.state) || postActionsHaveError(account)
                            ? 'bg-red-500/[0.025]'
                            : pipelineFlowComplete(account)
                              ? 'bg-emerald-500/[0.025]'
                              : '',
                    )}>
                      <td className="py-4 pl-4 pr-1">
                        <input
                          type="checkbox"
                          checked={selectedAccountIds.has(account.id)}
                          disabled={Boolean(archiveBusy) || pipelineIsPurged(account)}
                          aria-label={`选择账号 ${account.email}`}
                          onChange={event => toggleAccountSelection(account.id, event.target.checked)}
                        />
                      </td>
                      <td className="px-4 py-4">
                        <div className="max-w-full truncate text-sm font-medium text-[var(--text-primary)]" title={account.email}>
                          {account.email}
                        </div>
                        <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-[var(--text-muted)]">
                          <span className="font-mono">#{account.id}</span>
                          {planBadge(account)}
                          {phoneBindingBadge(account)}
                          {pipelineIsArchived(account) ? <ArchiveStateBadge account={account} /> : null}
                          <span>{account.validity === 'valid' ? '账号有效' : `有效性：${account.validity || '未检测'}`}</span>
                        </div>
                      </td>
                      <td className="px-4 py-4">
                        {pipelineIsArchived(account) ? <ArchivedPipelineSummary account={account} /> : <PipelineProgress account={account} />}
                      </td>
                      <td className="px-4 py-4">
                        <LatestEventTime value={account.pipeline.purged_at || account.pipeline.archived_at || account.pipeline.latest_event_at} />
                      </td>
                      <td className="px-4 py-4">{renderActions(account)}</td>
                    </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex items-center justify-between border-t border-[var(--border)] px-4 py-3">
          <span className="flex items-center gap-1.5 text-xs text-[var(--text-muted)]" role="status" aria-live="polite">
            {pageLoading ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : null}
            {pageLoading ? `正在加载第 ${page} 页` : `第 ${page} / ${totalPages} 页`}
          </span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={pageLoading || page <= 1} onClick={() => setPage(value => value - 1)}>上一页</Button>
            <Button variant="outline" size="sm" disabled={pageLoading || page >= totalPages} onClick={() => setPage(value => value + 1)}>下一页</Button>
          </div>
        </div>
      </div>
    </div>
  )
}
