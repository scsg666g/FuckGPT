import { useEffect, useState, useRef, useCallback, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { getConfig, getConfigOptions, getPlatforms } from '@/lib/app-data'
import type { ConfigOptionsResponse } from '@/lib/config-options'
import { getCaptchaStrategyLabel } from '@/lib/config-options'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { formatDateTime, translateAccountStatus } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { buildExecutorOptions, buildRegistrationOptions } from '@/lib/registration'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { getTaskStatusText, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { RefreshCw, Copy, Check, KeyRound, ExternalLink, Download, Upload, Plus, X, Mail, Trash2, Zap, ShieldCheck, Send, SlidersHorizontal, Save, RotateCcw, Search } from 'lucide-react'

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default', trial: 'success', subscribed: 'success',
  expired: 'warning', invalid: 'danger', deactivated: 'danger',
  free: 'secondary', eligible: 'secondary', valid: 'success', unknown: 'secondary',
}

const platformActionsCache = new Map<string, any[]>()
const platformActionsPromiseCache = new Map<string, Promise<any[]>>()

const ACCOUNT_TOOL_BUTTON_CLASS = 'h-8 shrink-0 whitespace-nowrap bg-transparent'
const ACCOUNT_PAGE_SIZE = 50
const SAVED_FILTERS_KEY = 'chatgpt-account-filter-presets-v2'

type AccountFilterState = {
  search: string
  status: string
  mailbox_bound: string
  mailbox_provider: string
  mailbox_email_match: string
  phone_state: string
  checked_state: string
  mfa_state: string
  codex_auth_state: string
  push_status: string
  push_target: string
  pushed_from: string
  pushed_to: string
  codex_refreshed_from: string
  codex_refreshed_to: string
  time_field: string
  time_from: string
  time_to: string
  source: string
  import_method: string
  region: string
  sort_by: string
  sort_order: string
}

type SavedFilterPreset = { name: string; filters: AccountFilterState }

const EMPTY_ACCOUNT_FILTERS: AccountFilterState = {
  search: '',
  status: '',
  mailbox_bound: '',
  mailbox_provider: '',
  mailbox_email_match: '',
  phone_state: '',
  checked_state: '',
  mfa_state: '',
  codex_auth_state: '',
  push_status: '',
  push_target: '',
  pushed_from: '',
  pushed_to: '',
  codex_refreshed_from: '',
  codex_refreshed_to: '',
  time_field: '',
  time_from: '',
  time_to: '',
  source: '',
  import_method: '',
  region: '',
  sort_by: 'created_at',
  sort_order: 'desc',
}

function readFiltersFromUrl(): AccountFilterState {
  const params = new URLSearchParams(window.location.search)
  const next = { ...EMPTY_ACCOUNT_FILTERS }
  ;(Object.keys(next) as Array<keyof AccountFilterState>).forEach(key => {
    const value = params.get(key)
    if (value !== null) next[key] = value
  })
  return next
}

function toApiFilters(filters: AccountFilterState) {
  const normalizeTime = (value: string) => {
    if (!value) return ''
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? '' : date.toISOString()
  }
  return {
    ...filters,
    time_from: normalizeTime(filters.time_from),
    time_to: normalizeTime(filters.time_to),
    pushed_from: normalizeTime(filters.pushed_from),
    pushed_to: normalizeTime(filters.pushed_to),
    codex_refreshed_from: normalizeTime(filters.codex_refreshed_from),
    codex_refreshed_to: normalizeTime(filters.codex_refreshed_to),
  }
}

function getActiveFilterCount(filters: AccountFilterState) {
  return (Object.keys(filters) as Array<keyof AccountFilterState>).filter(key => {
    if (key === 'search' || key === 'sort_by' || key === 'sort_order') return false
    return Boolean(filters[key])
  }).length
}

type AccountCredential = {
  scope: string
  provider_name: string
  credential_type: string
  key: string
  value: string
  is_primary: boolean
  source: string
}

const CREDENTIAL_LABELS: Record<string, string> = {
  access_token: 'Access Token',
  refresh_token: 'Refresh Token',
  id_token: 'ID Token',
  session_token: 'Session Token',
  cookies: 'Cookies',
  cookie: 'Cookie',
  codex_access_token: 'Codex Access Token',
  codex_refresh_token: 'Codex Refresh Token',
  codex_id_token: 'Codex ID Token',
}

function credentialLabel(credential: AccountCredential) {
  return CREDENTIAL_LABELS[credential.key] || credential.key
}

function selectCommonToken(credentials: AccountCredential[]) {
  const populated = credentials.filter(item => item.value)
  return populated.find(item => item.scope === 'platform' && item.key === 'access_token')
    || populated.find(item => item.scope === 'platform' && item.is_primary && item.credential_type === 'token')
    || populated.find(item => item.scope === 'platform' && item.credential_type === 'token')
    || populated.find(item => item.scope === 'platform' && item.key === 'session_token')
    || null
}

async function copyToClipboard(text: string) {
  if (!text) throw new Error('没有可复制的内容')
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // Fall through to the local textarea fallback.
    }
  }
  const element = document.createElement('textarea')
  element.value = text
  element.setAttribute('readonly', '')
  element.style.position = 'fixed'
  element.style.opacity = '0'
  document.body.appendChild(element)
  element.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(element)
  if (!copied) throw new Error('复制失败，请手动选择')
}

function getAccountView(acc: any) {
  return acc?.account_view && typeof acc.account_view === 'object' ? acc.account_view : {}
}

function getAccountIdentity(acc: any) {
  const identity = getAccountView(acc)?.identity
  return identity && typeof identity === 'object' ? identity : {}
}

function getAccountEmail(acc: any) {
  return String(getAccountIdentity(acc)?.email || '')
}

function getAccountStatus(acc: any) {
  const status = getAccountView(acc)?.status
  return status && typeof status === 'object' ? status : {}
}

function getAccountSubscription(acc: any) {
  const subscription = getAccountView(acc)?.subscription
  return subscription && typeof subscription === 'object' ? subscription : {}
}

function getAccountUsage(acc: any) {
  const usage = getAccountView(acc)?.usage
  return usage && typeof usage === 'object' ? usage : {}
}

function getAccountSecurity(acc: any) {
  const security = getAccountView(acc)?.security
  return security && typeof security === 'object' ? security : {}
}

function getDeactivationInfo(acc: any) {
  const security = getAccountSecurity(acc)
  return {
    reason: String(security?.deactivation_reason || ''),
    error: String(security?.deactivation_error || ''),
    detectedAt: String(security?.deactivation_detected_at || ''),
  }
}

function getPhoneBindingState(acc: any) {
  const security = getAccountSecurity(acc)
  const checked = Boolean(
    getAccountStatus(acc)?.checked_at
    || security?.phone_bound === true
    || security?.phone_number_masked,
  )
  if (security?.phone_bound === true) return { state: 'bound', label: '已绑手机' }
  if (checked) return { state: 'unbound', label: '未绑手机' }
  return { state: 'unchecked', label: '手机未检测' }
}

function getAccountDisplay(acc: any) {
  const display = getAccountView(acc)?.display
  return display && typeof display === 'object' ? display : {}
}

function getVerificationMailbox(acc: any) {
  const mailbox = getAccountView(acc)?.verification?.mailbox
  return mailbox && typeof mailbox === 'object' ? mailbox : null
}

function getLifecycleStatus(acc: any) {
  return String(getAccountStatus(acc)?.lifecycle || 'unknown')
}

function getDisplayStatus(acc: any) {
  return String(getAccountStatus(acc)?.display || 'unknown')
}

function getPlanState(acc: any) {
  return String(getAccountSubscription(acc)?.state || 'unknown')
}

function getPlanName(acc: any) {
  return String(getAccountSubscription(acc)?.plan || getAccountUsage(acc)?.plan_type || 'unknown')
}

function getValidityStatus(acc: any) {
  return String(getAccountStatus(acc)?.validity || 'unknown')
}

function getPrimaryMetrics(acc: any) {
  const metrics = getAccountDisplay(acc)?.metrics?.primary
  return Array.isArray(metrics) ? metrics : []
}

function getSecondaryMetrics(acc: any) {
  const metrics = getAccountDisplay(acc)?.metrics?.secondary
  return Array.isArray(metrics) ? metrics : []
}

function getDisplayWarnings(acc: any) {
  const warnings = getAccountDisplay(acc)?.warnings
  return Array.isArray(warnings) ? warnings : []
}

function getDisplayBadges(acc: any) {
  const badges = getAccountDisplay(acc)?.badges
  return Array.isArray(badges) ? badges : []
}

function getDisplaySections(acc: any) {
  const sections = getAccountDisplay(acc)?.sections
  return Array.isArray(sections) ? sections : []
}

function getCodexStatus(acc: any) {
  const codex = getAccountView(acc)?.codex
  const normalized = codex && typeof codex === 'object' ? codex : {}
  return {
    authorized: normalized.authorized === true,
    authPath: String(normalized.auth_path || ''),
    accountId: String(normalized.account_id || ''),
    email: String(normalized.email || ''),
    planType: String(normalized.plan_type || ''),
    expiresAt: String(normalized.expires_at || ''),
    lastRefresh: String(normalized.last_refresh || ''),
    hasAccessToken: normalized.has_access_token === true,
    hasRefreshToken: normalized.has_refresh_token === true,
  }
}

type PushTarget = {
  key: string
  label: string
  is_default: boolean
  payload_format: string
}

function getLatestPushDelivery(acc: any) {
  const deliveries = Array.isArray(acc?.push_deliveries) ? acc.push_deliveries : []
  return deliveries[0] || null
}

function markPushPending(
  accounts: any[],
  accountIds: number[],
  target: PushTarget,
  attemptedAt: string,
) {
  const selected = new Set(accountIds)
  return accounts.map(account => {
    if (!selected.has(Number(account?.id))) return account
    const deliveries = Array.isArray(account?.push_deliveries)
      ? [...account.push_deliveries]
      : []
    const existingIndex = deliveries.findIndex(delivery => delivery?.target_key === target.key)
    const existing = existingIndex >= 0 ? deliveries[existingIndex] : null
    const pendingDelivery = {
      ...(existing || {}),
      target_key: target.key,
      target_label: target.label,
      payload_format: target.payload_format || existing?.payload_format || 'codex',
      status: 'pending',
      attempt_count: Number(existing?.attempt_count || 0) + 1,
      http_status: 0,
      last_error: '',
      last_attempt_at: attemptedAt,
      pushed_at: existing?.pushed_at || null,
    }
    if (existingIndex >= 0) deliveries.splice(existingIndex, 1)
    deliveries.unshift(pendingDelivery)
    return { ...account, push_deliveries: deliveries }
  })
}

function formatOptionalDateTime(value: string, language: Parameters<typeof formatDateTime>[1]) {
  if (!value) return ''
  try {
    return formatDateTime(value, language, {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  } catch {
    return value
  }
}

function getCashierUrl(acc: any) {
  return String(getAccountSubscription(acc)?.cashier_url || '')
}

function getSafeExternalUrl(value: unknown) {
  const text = String(value || '').trim()
  if (!text) return ''
  try {
    const url = new URL(text)
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.toString() : ''
  } catch {
    return ''
  }
}

function scalarDetailValue(value: unknown) {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : ''
}

function formatUnixTimestamp(value: unknown, language: Parameters<typeof formatDateTime>[1]) {
  const timestamp = Number(value || 0)
  if (!Number.isFinite(timestamp) || timestamp <= 0) return ''
  return formatOptionalDateTime(new Date(timestamp * 1000).toISOString(), language)
}

function booleanLabel(value: boolean, positive = '是', negative = '否') {
  return value ? positive : negative
}

function escapeCsvField(value: unknown) {
  const text = value == null ? '' : String(value)
  if (!/[",\n\r]/.test(text)) return text
  return `"${text.replace(/"/g, '""')}"`
}

async function loadPlatformActions(platform: string, options?: { force?: boolean }) {
  const key = String(platform || '').trim()
  if (!key) return []
  const force = Boolean(options?.force)
  if (!force && platformActionsCache.has(key)) {
    return platformActionsCache.get(key) || []
  }
  if (!force && platformActionsPromiseCache.has(key)) {
    return platformActionsPromiseCache.get(key) || []
  }
  const pending = apiFetch(`/actions/${key}`)
    .then((data) => {
      const actions = Array.isArray(data?.actions) ? data.actions : []
      platformActionsCache.set(key, actions)
      platformActionsPromiseCache.delete(key)
      return actions
    })
    .catch((error) => {
      platformActionsPromiseCache.delete(key)
      throw error
    })
  platformActionsPromiseCache.set(key, pending)
  return pending
}

function buildActionParamDraft(action: any, acc: any) {
  const params = Array.isArray(action?.params) ? action.params : []
  const emailPrefix = String(acc?.email || '').split('@')[0] || 'Development'
  const draft: Record<string, string> = {}
  params.forEach((param: any) => {
    if (action?.id === 'create_api_key' && param?.key === 'name') {
      draft[param.key] = `${emailPrefix}Development`
      return
    }
    if (Array.isArray(param?.options) && param.options.length > 0) {
      draft[param?.key || ''] = String(param.options[0] ?? '')
      return
    }
    draft[param?.key || ''] = ''
  })
  return draft
}

function getActionOptionLabel(paramKey: string, value: string) {
  const labels: Record<string, Record<string, string>> = {
    oauth_mode: {
      browser: '浏览器模式',
      browser_protocol: '浏览器协议模式（Fetch 优先）',
      protocol: '协议模式（复用已有会话）',
    },
    browser_mode: {
      headless: '后台浏览器',
      headed: '可视浏览器',
    },
    keep_browser_open: {
      false: '否',
      true: '是',
    },
    platform_proxy_mode: {
      direct: '直连',
      manual: '手动填写',
      proxy_service: '使用代理服务',
      follow_platform: '跟随 ChatGPT/Codex',
    },
  }
  return labels[paramKey]?.[value] || value
}

// ── 注册弹框 ────────────────────────────────────────────────
function RegisterModal({
  platformMeta,
  onClose,
  onDone,
}: {
  platformMeta: any
  onClose: () => void
  onDone: () => void
}) {
  const { t, language } = useI18n()
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>({
    mailbox_providers: [],
    captcha_providers: [],
    mailbox_settings: [],
    captcha_settings: [],
    captcha_policy: {},
    executor_options: [],
    identity_mode_options: [],
  })
  const [configLoading, setConfigLoading] = useState(true)
  const [regCount, setRegCount] = useState(1)
  const [concurrency, setConcurrency] = useState(1)
  const [platformProxyMode, setPlatformProxyMode] = useState('direct')
  const [platformProxyValue, setPlatformProxyValue] = useState('')
  const [preferPasswordRegistration, setPreferPasswordRegistration] = useState(true)
  const [browserVisible, setBrowserVisible] = useState(false)
  const [autoCodexOAuth, setAutoCodexOAuth] = useState(false)
  const [codexOAuthMode, setCodexOAuthMode] = useState('browser')
  const [codexOAuthBrowserMode, setCodexOAuthBrowserMode] = useState('headless')
  const [keepCodexBrowserOpen, setKeepCodexBrowserOpen] = useState(false)
  const [startError, setStartError] = useState('')
  const [selection, setSelection] = useState({
    identityProvider: 'mailbox',
    executorType: 'browser',
  })
  const [taskId, setTaskId] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [starting, setStarting] = useState(false)

  const supportedExecutors: string[] = platformMeta?.supported_executors || []
  const registrationOptions = buildRegistrationOptions(platformMeta, language)
  const executorOptions = buildExecutorOptions(
    supportedExecutors,
    platformMeta?.supported_executor_options || [],
    language,
  )
  const visibleRegistrationOptions = registrationOptions
  const visibleExecutorOptions = executorOptions
  const selectedRegistration = registrationOptions.find(option =>
    option.identityProvider === selection.identityProvider,
  )
  const selectedExecutor = executorOptions.find(option => option.value === selection.executorType)

  useEffect(() => {
    let active = true
    setConfigLoading(true)
    getConfigOptions()
      .then((options) => {
        if (!active) return
        if (options) {
          setConfigOptions(options)
        }
      })
      .catch(() => {
        if (!active) return
        setConfigOptions({
          mailbox_providers: [],
          captcha_providers: [],
          mailbox_settings: [],
          captcha_settings: [],
          captcha_policy: {},
          executor_options: [],
          identity_mode_options: [],
        })
      })
      .finally(() => {
        if (active) setConfigLoading(false)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (configLoading || registrationOptions.length === 0) return
    const defaultRegistration = registrationOptions[0]
    setSelection((current) => {
      const identityProvider = current.identityProvider || defaultRegistration.identityProvider
      const validExecutorOptions = buildExecutorOptions(
        supportedExecutors,
        platformMeta?.supported_executor_options || [],
        language,
      )
        .filter(option => !option.disabled)
      const preferredExecutor = supportedExecutors.includes('browser')
        ? 'browser'
        : supportedExecutors.includes('headless')
          ? 'headless'
        : supportedExecutors[0] || ''
      const executorType = validExecutorOptions.some(option => option.value === current.executorType)
        ? current.executorType
        : (validExecutorOptions.find(option => option.value === preferredExecutor)?.value || validExecutorOptions[0]?.value || '')
      if (
        current.identityProvider === identityProvider &&
        current.executorType === executorType
      ) {
        return current
      }
      return { identityProvider, executorType }
    })
  }, [configLoading, registrationOptions, supportedExecutors])

  useEffect(() => {
    if (!selection.identityProvider) return
    const validExecutorOptions = buildExecutorOptions(
      supportedExecutors,
      platformMeta?.supported_executor_options || [],
      language,
    )
      .filter(option => !option.disabled)
    if (!validExecutorOptions.some(option => option.value === selection.executorType)) {
      setSelection(current => {
        const nextExecutorType = validExecutorOptions[0]?.value || ''
        if (current.executorType === nextExecutorType) {
          return current
        }
        return {
          ...current,
          executorType: nextExecutorType,
        }
      })
    }
  }, [selection.identityProvider, selection.executorType, supportedExecutors])

  const enabledMailboxProviders = (configOptions.mailbox_settings || []).filter(item => item.enabled)
  const defaultMailboxProvider = enabledMailboxProviders.find(item => item.is_default) || enabledMailboxProviders[0] || null

  const start = async () => {
    setStarting(true)
    setStartError('')
    try {
      const extra: Record<string, any> = {
        identity_provider: selection.identityProvider,
        auto_codex_oauth_after_register: autoCodexOAuth,
        codex_oauth_mode: codexOAuthMode,
        codex_oauth_browser_mode: codexOAuthBrowserMode,
        codex_oauth_keep_browser_open: keepCodexBrowserOpen,
      }
      if (platformMeta?.name === 'chatgpt' && selection.executorType !== 'protocol') {
        extra.prefer_password_registration = preferPasswordRegistration
      }
      if (
        platformMeta?.name === 'chatgpt'
        && ['browser_protocol', 'browser'].includes(selection.executorType)
      ) {
        extra.browser_visible = browserVisible
      }
      if (selection.identityProvider === 'mailbox') {
        if (!defaultMailboxProvider?.provider_key) {
          throw new Error(t('accounts.missingDefaultMailbox'))
        }
        extra.mail_provider = defaultMailboxProvider.provider_key
      }
      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          count: regCount, concurrency,
          executor_type: selection.executorType,
          captcha_solver: 'auto',
          proxy: platformProxyMode === 'manual' ? platformProxyValue.trim() || null : null,
          platform_proxy_mode: platformProxyMode,
          platform_proxy_value: platformProxyValue.trim(),
          mailbox_proxy_mode: 'direct',
          mailbox_proxy_value: '',
          extra,
        }),
      })
      setTaskId(res.task_id)
    } catch (error: any) {
      setStartError(error?.message || String(error))
    } finally { setStarting(false) }
  }

  const handleDone = (_status: string) => {
    setDone(true)
    onDone()
  }

  const dialog = (
    <div className="dialog-backdrop" onClick={!taskId ? onClose : undefined}>
      <div className="dialog-panel dialog-panel-md flex flex-col"
           onClick={e => e.stopPropagation()} style={{maxHeight: '88vh'}}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">{t('accounts.autoRegister')} {platformMeta?.display_name || 'ChatGPT'}</h2>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4 flex-1 overflow-y-auto flex flex-col gap-5">
          {!taskId ? (
            configLoading ? (
              <div className="text-sm text-[var(--text-muted)]">{t('accounts.loadingRegistrationConfig')}</div>
            ) : (
              <>
                <div>
                  <div className="text-xs uppercase tracking-[0.16em] text-[var(--text-muted)]">Step 1</div>
                  <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">{t('accounts.selectIdentity')}</div>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">{t('accounts.selectIdentityDesc')}</div>
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    {visibleRegistrationOptions.map(option => {
                      const active = selection.identityProvider === option.identityProvider
                      return (
                        <button
                          key={option.key}
                          type="button"
                          onClick={() => setSelection(current => ({
                            ...current,
                            identityProvider: option.identityProvider,
                          }))}
                          className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                            active
                              ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                              : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                          }`}
                        >
                          <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                            {option.identityProvider === 'mailbox' ? <Mail className="h-4 w-4" /> : null}
                            {option.label}
                          </div>
                          <div className="mt-1 text-xs text-[var(--text-muted)]">{option.description}</div>
                        </button>
                      )
                    })}
                  </div>
                </div>

                <div>
                  <div className="text-xs uppercase tracking-[0.16em] text-[var(--text-muted)]">Step 2</div>
                  <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">{t('accounts.selectExecutor')}</div>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">{t('accounts.selectExecutorDesc')}</div>
                  <div className="mt-3 grid gap-3 md:grid-cols-3">
                    {visibleExecutorOptions.map(option => {
                      const active = selection.executorType === option.value
                      return (
                        <button
                          key={option.value}
                          type="button"
                          disabled={option.disabled}
                          onClick={() => !option.disabled && setSelection(current => ({ ...current, executorType: option.value }))}
                          className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                            option.disabled
                              ? 'cursor-not-allowed border-[var(--border)] bg-[var(--bg-hover)] opacity-50'
                              : active
                                ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                                : 'border-[var(--border)] bg-[var(--bg-pane)]/45 hover:border-[var(--accent)]/60'
                          }`}
                        >
                          <div className="text-sm font-medium text-[var(--text-primary)]">{option.label}</div>
                          <div className="mt-1 text-xs text-[var(--text-muted)]">{option.description}</div>
                          {option.reason ? (
                            <div className="mt-2 text-xs text-amber-400">{option.reason}</div>
                          ) : null}
                        </button>
                      )
                    })}
                  </div>
                </div>

                {platformMeta?.name === 'chatgpt' && selection.executorType !== 'protocol' ? (
                  <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-pane)]/45 px-4 py-3">
                    <label className="flex cursor-pointer items-start gap-3">
                      <input
                        type="checkbox"
                        checked={preferPasswordRegistration}
                        onChange={(event) => setPreferPasswordRegistration(event.target.checked)}
                        className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
                      />
                      <span>
                        <span className="block text-sm font-medium text-[var(--text-primary)]">
                          {t('accounts.preferPasswordRegistration')}
                        </span>
                        <span className="mt-1 block text-xs text-[var(--text-muted)]">
                          {t('accounts.preferPasswordRegistrationHint')}
                        </span>
                      </span>
                    </label>
                  </div>
                ) : null}

                {platformMeta?.name === 'chatgpt' && ['browser_protocol', 'browser'].includes(selection.executorType) ? (
                  <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-pane)]/45 px-4 py-3">
                    <label className="flex cursor-pointer items-start gap-3">
                      <input
                        type="checkbox"
                        checked={browserVisible}
                        onChange={(event) => setBrowserVisible(event.target.checked)}
                        className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
                      />
                      <span>
                        <span className="block text-sm font-medium text-[var(--text-primary)]">
                          {t('accounts.browserVisible')}
                        </span>
                        <span className="mt-1 block text-xs text-[var(--text-muted)]">
                          {t('accounts.browserVisibleHint')}
                        </span>
                      </span>
                    </label>
                  </div>
                ) : null}

                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-pane)]/45 px-4 py-3">
                  <label className="flex cursor-pointer items-start gap-3">
                    <input
                      type="checkbox"
                      checked={autoCodexOAuth}
                      onChange={(event) => setAutoCodexOAuth(event.target.checked)}
                      className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
                    />
                    <span>
                      <span className="block text-sm font-medium text-[var(--text-primary)]">
                        注册后执行 Codex OAuth
                      </span>
                      <span className="mt-1 block text-xs text-[var(--text-muted)]">
                        注册先保存账号；开启后继续获取 Codex 授权数据。浏览器注册会复用当前窗口，协议注册会另起授权窗口。
                      </span>
                    </span>
                  </label>
                  {autoCodexOAuth ? (
                    <div className="mt-3 grid gap-2 sm:grid-cols-[160px_1fr]">
                      <label className="text-xs text-[var(--text-muted)] sm:pt-2">Codex 授权模式</label>
                      <select
                        value={codexOAuthMode}
                        onChange={(event) => setCodexOAuthMode(event.target.value)}
                        className="control-surface control-surface-compact"
                      >
                        <option value="browser">浏览器模式</option>
                        <option value="browser_protocol">浏览器协议模式（Fetch 优先）</option>
                        <option value="protocol">协议模式（复用已有会话）</option>
                      </select>
                      {codexOAuthMode !== 'protocol' ? (
                        <>
                          <label className="text-xs text-[var(--text-muted)] sm:pt-2">Codex 浏览器模式</label>
                          <select
                            value={codexOAuthBrowserMode}
                            onChange={(event) => setCodexOAuthBrowserMode(event.target.value)}
                            className="control-surface control-surface-compact"
                          >
                            <option value="headed">可视浏览器</option>
                            <option value="headless">后台浏览器</option>
                          </select>
                          {codexOAuthBrowserMode === 'headed' ? (
                            <label className="flex items-center gap-2 text-xs text-[var(--text-muted)] sm:col-span-2">
                              <input
                                type="checkbox"
                                checked={keepCodexBrowserOpen}
                                onChange={(event) => setKeepCodexBrowserOpen(event.target.checked)}
                                className="h-4 w-4 accent-[var(--accent)]"
                              />
                              完成后保留浏览器窗口
                            </label>
                          ) : null}
                        </>
                      ) : (
                        <div className="text-xs text-[var(--text-muted)] sm:col-span-2">
                          协议模式直接复用账号已有 session/cookies，不启动浏览器。
                        </div>
                      )}
                    </div>
                  ) : null}
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">{t('accounts.registrationCount')}</label>
                    <input type="number" min={1} max={99} value={regCount}
                      onChange={e => setRegCount(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                  <div>
                    <label className="text-xs text-[var(--text-muted)] block mb-1">{t('accounts.concurrency')}</label>
                    <input type="number" min={1} max={20} value={concurrency}
                      onChange={e => setConcurrency(Number(e.target.value))}
                      className="control-surface control-surface-compact text-center" />
                  </div>
                </div>

                  <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-hover)]/40 p-3">
                    <div>
                      <label className="text-xs text-[var(--text-muted)] block mb-1">ChatGPT/Codex 代理</label>
                      <select
                        value={platformProxyMode}
                        onChange={(e) => setPlatformProxyMode(e.target.value)}
                        className="control-surface control-surface-compact appearance-none"
                      >
                        <option value="direct">直连</option>
                        <option value="proxy_service">使用代理服务</option>
                        <option value="manual">手动填写</option>
                      </select>
                      {platformProxyMode === 'manual' ? (
                        <input
                          type="text"
                          value={platformProxyValue}
                          onChange={(e) => setPlatformProxyValue(e.target.value)}
                          placeholder="socks5://user:pass@host:port"
                          spellCheck={false}
                          className="control-surface control-surface-compact mt-2 w-full font-mono text-xs"
                        />
                      ) : null}
                    </div>
                    <div className="mt-2 text-xs text-[var(--text-muted)]">
                      邮箱 API 默认跟随 ChatGPT/Codex 代理，不再单独配置。
                    </div>
                  </div>


                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3 text-xs text-[var(--text-secondary)]">
                  <div>{t('accounts.identitySummary')}: <span className="text-[var(--text-primary)]">{selectedRegistration?.label || '-'}</span></div>
                  <div className="mt-1">{t('accounts.executorSummary')}: <span className="text-[var(--text-primary)]">
                    {selectedExecutor?.label || '-'}
                    {['browser_protocol', 'browser'].includes(selection.executorType) && browserVisible ? ` · ${t('accounts.visibleWindow')}` : ''}
                  </span></div>
                  <div className="mt-1">{t('accounts.verificationSummary')}: <span className="text-[var(--text-primary)]">{
                    selection.executorType === 'protocol'
                      ? t('accounts.protocolVerificationSummary')
                      : getCaptchaStrategyLabel(selection.executorType, configOptions.captcha_policy, configOptions.captcha_providers, language)
                  }</span></div>
                </div>

                {startError ? (
                  <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                    {startError}
                  </div>
                ) : null}

                <Button
                  onClick={start}
                  disabled={starting || !selection.identityProvider || !selection.executorType}
                  className="w-full"
                >
                  {starting ? t('accounts.starting') : t('accounts.startAutoRegister')}
                </Button>
              </>
            )
          ) : (
            <div className="flex min-h-0 flex-1 flex-col gap-3">
              <div className="min-h-0 flex-1">
                <TaskLogPanel taskId={taskId} onDone={handleDone} />
              </div>
            </div>
          )}
        </div>
        <div className="px-6 py-3 border-t border-[var(--border)] flex justify-end">
          <Button variant="outline" size="sm" onClick={onClose}>
            {done ? t('common.close') : t('common.cancel')}
          </Button>
        </div>
      </div>
    </div>
  )

  return typeof document !== 'undefined' ? createPortal(dialog, document.body) : dialog
}

// ── 新增账号弹框 ─────────────────────────────────────────
function formatResultValue(value: any) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

function ResultStat({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2">
      <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--text-muted)]">{label}</div>
      <div className="mt-1 text-sm font-medium text-[var(--text-primary)] break-all">{formatResultValue(value)}</div>
    </div>
  )
}

function metricToneClass(tone?: string) {
  if (tone === 'good') return 'border-emerald-500/25 bg-emerald-500/10 text-emerald-200'
  if (tone === 'warning') return 'border-amber-500/25 bg-amber-500/10 text-amber-200'
  if (tone === 'danger') return 'border-red-500/25 bg-red-500/10 text-red-200'
  return 'border-[var(--border)] bg-[var(--bg-hover)] text-[var(--text-primary)]'
}

function metricAccentClass(tone?: string) {
  if (tone === 'good') return 'from-emerald-400/70 to-cyan-300/50'
  if (tone === 'warning') return 'from-amber-300/80 to-orange-300/50'
  if (tone === 'danger') return 'from-red-400/80 to-rose-300/50'
  return 'from-[var(--accent)]/80 to-[var(--accent-strong)]/45'
}

function DisplayMetricCard({ metric, compact = false }: { metric: any; compact?: boolean }) {
  return (
    <div className={`group relative overflow-hidden rounded-lg border px-3.5 py-3 ${metricToneClass(metric?.tone)}`}>
      <div className={`pointer-events-none absolute inset-y-0 left-0 w-1 bg-gradient-to-b ${metricAccentClass(metric?.tone)}`} />
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-[0.18em] opacity-65">{metric?.label || '-'}</div>
          {metric?.sub ? <div className="mt-1 truncate text-[11px] opacity-65">{metric.sub}</div> : null}
        </div>
        <div className={`${compact ? 'text-sm' : 'text-lg'} shrink-0 font-semibold tracking-[-0.03em]`}>{formatResultValue(metric?.value)}</div>
      </div>
      {typeof metric?.percent === 'number' ? (
        <div className="relative mt-3 h-1.5 overflow-hidden rounded-full bg-black/25">
          <div className={`h-full rounded-full bg-gradient-to-r ${metricAccentClass(metric?.tone)}`} style={{ width: `${Math.max(0, Math.min(100, metric.percent))}%` }} />
        </div>
      ) : null}
    </div>
  )
}

function DisplayWarnings({ warnings }: { warnings: any[] }) {
  if (!warnings.length) return null
  return (
    <div className="space-y-2">
      {warnings.map((item: any, index: number) => (
        <div key={`${item?.key || 'warning'}-${index}`} className={`rounded-xl border px-3 py-2 text-xs ${metricToneClass(item?.tone || 'warning')}`}>
          {item?.message || '-'}
        </div>
      ))}
    </div>
  )
}

function DisplaySections({ sections }: { sections: any[] }) {
  if (!sections.length) return null
  return (
    <div className="space-y-3">
      {sections.map((section: any) => (
        <div key={section?.key || section?.title} className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
          <div className="text-xs font-semibold text-[var(--text-primary)]">{section?.title || '明细'}</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {(Array.isArray(section?.items) ? section.items : []).map((item: any, index: number) => (
              <div key={`${item?.title || 'item'}-${index}`} className="rounded-lg border border-[var(--border)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{item?.title || '-'}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  {(Array.isArray(item?.metrics) ? item.metrics : []).map((metric: any) => (
                    <div key={metric?.key || metric?.label}>
                      <span className="text-[var(--text-muted)]">{metric?.label || '-'}: </span>
                      <span>{formatResultValue(metric?.value)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function ActionResultHighlights({ payload }: { payload: any }) {
  if (!payload || typeof payload !== 'object') return null

  const stats: Array<{ label: string; value: any }> = []
  if ('valid' in payload) stats.push({ label: '账号有效', value: payload.valid })
  if (payload.membership_type) stats.push({ label: '套餐', value: payload.membership_type })
  if (payload.plan) stats.push({ label: '套餐', value: payload.plan })
  if (payload.plan_id) stats.push({ label: 'Plan ID', value: payload.plan_id })
  if (typeof payload.has_valid_payment_method === 'boolean') stats.push({ label: '已绑卡', value: payload.has_valid_payment_method })
  if ('trial_eligible' in payload) stats.push({ label: '可试用', value: payload.trial_eligible })
  if (payload.trial_length_days) stats.push({ label: '试用天数', value: payload.trial_length_days })
  if (payload.remaining_credits) stats.push({ label: '剩余额度', value: payload.remaining_credits })
  if (payload.usage_total) stats.push({ label: '已用额度', value: payload.usage_total })
  if (payload.plan_credits) stats.push({ label: '总额度', value: payload.plan_credits })
  if (payload.desktop_app_state?.app_name) stats.push({ label: '桌面应用', value: payload.desktop_app_state.app_name })
  if ('running' in (payload.desktop_app_state || {})) stats.push({ label: '桌面已打开', value: payload.desktop_app_state.running })
  if ('ready' in (payload.desktop_app_state || {})) stats.push({ label: '桌面就绪', value: payload.desktop_app_state.ready })

  if (stats.length === 0) return null
  return (
    <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {stats.map(item => <ResultStat key={item.label} label={item.label} value={item.value} />)}
    </div>
  )
}

function ActionResultModal({
  title,
  payload,
  onClose,
}: {
  title: string
  payload: any
  onClose: () => void
}) {
  const content = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-lg"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{title}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">操作结果</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => navigator.clipboard.writeText(content)}>
              <Copy className="h-4 w-4 mr-1" />
              复制
            </Button>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="px-6 py-4">
          <ActionResultHighlights payload={payload} />
          <pre className="bg-[var(--bg-hover)] border border-[var(--border)] rounded-xl p-4 text-xs text-[var(--text-secondary)] whitespace-pre-wrap break-all overflow-auto max-h-[65vh]">
            {content}
          </pre>
        </div>
      </div>
    </div>
  )
}

function ActionTaskModal({
  title,
  taskId,
  taskStatus,
  onClose,
  onDone,
}: {
  title: string
  taskId: string
  taskStatus: string | null
  onClose: () => void
  onDone: (status: string) => void
}) {
  const { t, language } = useI18n()
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel flex w-[min(960px,calc(100vw-32px))] max-w-none flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
        style={{ maxHeight: '90vh' }}
      >
        <div className="relative overflow-hidden border-b border-[var(--border)] px-6 py-5">
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_12%_0%,rgba(9,182,162,0.18),transparent_34%),linear-gradient(90deg,rgba(255,255,255,0.04),transparent)]" />
          <div className="relative flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-2 inline-flex rounded-full border border-[var(--border)] bg-[var(--chip-bg)] px-3 py-1 text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
                Platform Action
              </div>
              <h2 className="truncate text-lg font-semibold text-[var(--text-primary)]">{title}</h2>
              <p className="mt-1 text-xs text-[var(--text-muted)]">任务状态、错误摘要与实时日志集中展示</p>
            </div>
            <div className="flex items-center gap-2">
              {taskStatus ? (
                <Badge variant={TASK_STATUS_VARIANTS[taskStatus] || 'secondary'}>
                  {getTaskStatusText(taskStatus, language)}
                </Badge>
              ) : null}
              <button onClick={onClose} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] p-2 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-5">
          <TaskLogPanel taskId={taskId} onDone={onDone} />
        </div>
        <div className="flex items-center justify-between border-t border-[var(--border)] px-6 py-3 text-xs text-[var(--text-muted)]">
          <span>{t('taskHistory.taskId')}: {taskId}</span>
          <Button variant="outline" size="sm" onClick={onClose}>
            {t('common.close')}
          </Button>
        </div>
      </div>
    </div>
  )
}

function RefreshCreditsOptionsModal({
  targetCount,
  scopeLabel,
  platformLabel,
  proxyLabel,
  concurrency,
  onClose,
  onSubmit,
}: {
  targetCount: number
  scopeLabel: string
  platformLabel: string
  proxyLabel: string
  concurrency: number
  onClose: () => void
  onSubmit: (options: { reloginInvalid: boolean }) => void
}) {
  const [reloginInvalid, setReloginInvalid] = useState(false)

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-md"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">刷新额度设置</h2>
            <p className="mt-0.5 text-xs text-[var(--text-muted)]">默认只检测账号状态和额度，不会重新登录。</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-4 px-6 py-5">
          <div className="grid gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-hover)]/40 p-4 text-sm sm:grid-cols-2">
            <div>
              <div className="text-xs text-[var(--text-muted)]">范围</div>
              <div className="mt-1 font-medium text-[var(--text-primary)]">{scopeLabel}</div>
            </div>
            <div>
              <div className="text-xs text-[var(--text-muted)]">账号数</div>
              <div className="mt-1 font-medium text-[var(--text-primary)]">{targetCount}</div>
            </div>
            <div>
              <div className="text-xs text-[var(--text-muted)]">平台</div>
              <div className="mt-1 font-medium text-[var(--text-primary)]">{platformLabel}</div>
            </div>
            <div>
              <div className="text-xs text-[var(--text-muted)]">并发</div>
              <div className="mt-1 font-medium text-[var(--text-primary)]">{concurrency}</div>
            </div>
            <div className="sm:col-span-2">
              <div className="text-xs text-[var(--text-muted)]">代理</div>
              <div className="mt-1 break-all font-medium text-[var(--text-primary)]">{proxyLabel}</div>
            </div>
          </div>
          <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4 hover:bg-[var(--bg-hover)]/50">
            <input
              type="checkbox"
              checked={reloginInvalid}
              onChange={e => setReloginInvalid(e.target.checked)}
              className="mt-0.5 h-4 w-4 accent-amber-500"
            />
            <span>
              <span className="block text-sm font-medium text-[var(--text-primary)]">检测到失效后自动重登</span>
              <span className="mt-1 block text-xs leading-5 text-[var(--text-muted)]">
                只对本次刷新中检测为失效的账号触发浏览器重新登录。不开启时仅记录失效状态。
              </span>
            </span>
          </label>
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-[var(--border)] px-6 py-4">
          <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
          <Button size="sm" onClick={() => onSubmit({ reloginInvalid })}>
            开始刷新
          </Button>
        </div>
      </div>
    </div>
  )
}

function ActionParamsModal({
  action,
  initialValues,
  submitting,
  onClose,
  onSubmit,
}: {
  action: any
  initialValues: Record<string, string>
  submitting: boolean
  onClose: () => void
  onSubmit: (params: Record<string, string>) => void
}) {
  const [form, setForm] = useState<Record<string, string>>(initialValues)

  useEffect(() => {
    setForm(initialValues)
  }, [action?.id, initialValues])

  const params = Array.isArray(action?.params) ? action.params : []

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-md"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{action?.label || '动作参数'}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">填写执行该动作所需的参数</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="px-6 py-4 space-y-4">
          {params.map((param: any) => {
            if (param.key === 'platform_proxy_value' && form.platform_proxy_mode !== 'manual') {
              return null
            }
            const value = form[param.key] ?? ''
            if (Array.isArray(param.options) && param.options.length > 0) {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <select
                    value={value}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  >
                    {param.options.map((option: string) => (
                      <option key={option} value={option}>{getActionOptionLabel(param.key, option)}</option>
                    ))}
                  </select>
                </label>
              )
            }
            if (param.type === 'textarea') {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <textarea
                    value={value}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    rows={3}
                    className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  />
                </label>
              )
            }
            return (
              <label key={param.key} className="block">
                <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                <input
                  type={param.type === 'number' ? 'number' : 'text'}
                  value={value}
                  onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                  className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                />
              </label>
            )
          })}
        </div>
        <div className="px-6 py-4 border-t border-[var(--border)] flex gap-3">
          <Button onClick={() => onSubmit(form)} disabled={submitting} className="flex-1">
            {submitting ? '执行中...' : '执行'}
          </Button>
          <Button variant="outline" onClick={onClose} disabled={submitting} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}
// ── 行操作菜单 ─────────────────────────────────────────────
function ActionMenu({
  acc,
  onDetail,
  onDelete,
  onResult,
  onChanged,
  canPush,
  onPush,
}: {
  acc: any
  onDetail: () => void
  onDelete: () => void
  onResult: (title: string, payload: any) => void
  onChanged: () => void
  canPush: boolean
  onPush: () => Promise<any>
}) {
  const { language } = useI18n()
  const [open, setOpen] = useState(false)
  const [actions, setActions] = useState<any[]>([])
  const [running, setRunning] = useState<string | null>(null)
  const [copyingToken, setCopyingToken] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [tokenCopied, setTokenCopied] = useState(false)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [actionTask, setActionTask] = useState<{ taskId: string; title: string } | null>(null)
  const [actionTaskStatus, setActionTaskStatus] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<{ action: any; params: Record<string, string> } | null>(null)
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, maxHeight: 320 })
  const menuRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  const runAction = (action: any, params: Record<string, any>) => {
    setRunning(action.id)
    setActionTaskStatus(null)
    apiFetch(`/actions/chatgpt/${acc.id}/${action.id}`, { method: 'POST', body: JSON.stringify({ params }) })
      .then(resp => {
        if (resp?.sync) {
          setRunning(null)
          if (!resp.ok) {
            setToast({ type: 'error', text: resp.error || 'Operation failed' })
            return
          }
          onChanged()
          if (resp.data?.url || resp.data?.checkout_url || resp.data?.cashier_url) {
            const actionUrl = resp.data?.url || resp.data?.checkout_url || resp.data?.cashier_url
            window.open(actionUrl, '_blank')
            try {
              navigator.clipboard.writeText(actionUrl)
            } catch {
              // Ignore clipboard errors
            }
          }
          onResult(action.label, resp.data)
          return
        }
        setActionTask({
          taskId: resp.task_id,
          title: `${getAccountEmail(acc)} · ${action.label}`,
        })
      })
      .catch(() => {
        setRunning(null)
        setToast({ type: 'error', text: 'Request failed' })
      })
  }

  const updateMenuPosition = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return

    const rect = trigger.getBoundingClientRect()
    const viewportPadding = 12
    const menuWidth = 220
    const estimatedHeight = Math.min(320, actions.length * 40 + 56)

    let left = rect.right - menuWidth
    if (left < viewportPadding) left = viewportPadding
    if (left + menuWidth > window.innerWidth - viewportPadding) {
      left = Math.max(viewportPadding, window.innerWidth - menuWidth - viewportPadding)
    }

    const spaceBelow = window.innerHeight - rect.bottom - viewportPadding
    const spaceAbove = rect.top - viewportPadding
    const openUp = spaceBelow < estimatedHeight && spaceAbove > spaceBelow
    const maxHeight = Math.max(160, Math.min(estimatedHeight, openUp ? spaceAbove : spaceBelow))
    const top = openUp
      ? Math.max(viewportPadding, rect.top - maxHeight - 8)
      : rect.bottom + 8

    setMenuPosition({
      top: Math.round(top),
      left: Math.round(left),
      maxHeight,
    })
  }, [actions.length])

  useEffect(() => {
    let active = true
    loadPlatformActions('chatgpt')
      .then((items) => {
        if (active) setActions(items)
      })
      .catch(() => {
        if (active) setActions([])
      })
    return () => {
      active = false
    }
  }, [])
  useEffect(() => {
    if (toast) { const t = setTimeout(() => setToast(null), 4000); return () => clearTimeout(t) }
  }, [toast])
  useEffect(() => {
    if (!tokenCopied) return
    const timer = setTimeout(() => setTokenCopied(false), 1600)
    return () => clearTimeout(timer)
  }, [tokenCopied])
  useEffect(() => {
    if (!open) return
    let active = true
    loadPlatformActions('chatgpt', { force: true })
      .then((items) => {
        if (active) setActions(items)
      })
      .catch(() => {
        if (active) setActions([])
      })
    updateMenuPosition()
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    const reposition = () => updateMenuPosition()
    document.addEventListener('mousedown', handler)
    window.addEventListener('resize', reposition)
    window.addEventListener('scroll', reposition, true)
    return () => {
      active = false
      document.removeEventListener('mousedown', handler)
      window.removeEventListener('resize', reposition)
      window.removeEventListener('scroll', reposition, true)
    }
  }, [open, updateMenuPosition])

  const handleActionDone = async (status: string) => {
    if (!actionTask) return
    setActionTaskStatus(status)
    setRunning(null)
    try {
      const task = await apiFetch(`/tasks/${actionTask.taskId}`)
      const data = task?.data ?? task?.result?.data
      if (status !== 'succeeded') {
        setToast({ type: 'error', text: task?.error || getTaskStatusText(status, language) })
        return
      }
      onChanged()
      const actionUrl = data?.url || data?.checkout_url || data?.cashier_url
      if (actionUrl) {
        window.open(actionUrl, '_blank')
        try {
          await navigator.clipboard.writeText(actionUrl)
        } catch {
          // ignore clipboard failures
        }
      }
      if (data && typeof data === 'object') {
        if (actionUrl) {
          setToast({ type: 'success', text: data.message || '链接已在新标签打开并复制' })
          return
        }
        const detailKeys = Object.keys(data).filter(key => !['message', 'url', 'checkout_url', 'cashier_url'].includes(key))
        if (detailKeys.length > 0) {
          onResult(actionTask.title, data)
        }
        setToast({ type: 'success', text: data.message || '操作成功' })
        return
      }
      setToast({ type: 'success', text: typeof data === 'string' && data ? data : '操作成功' })
    } catch (error: any) {
      setToast({ type: 'error', text: error?.message || '读取任务结果失败' })
    }
  }

  const copyPrimaryToken = async () => {
    setCopyingToken(true)
    try {
      const response = await apiFetch(`/accounts/${acc.id}/credentials?scope=platform`)
      const credentials = Array.isArray(response?.items) ? response.items as AccountCredential[] : []
      const credential = selectCommonToken(credentials)
      if (!credential) throw new Error('该账号没有已保存的平台 Token')
      await copyToClipboard(credential.value)
      setTokenCopied(true)
      setToast({ type: 'success', text: `已复制 ${credentialLabel(credential)}` })
    } catch (error) {
      setToast({
        type: 'error',
        text: error instanceof Error ? error.message : '读取 Token 失败',
      })
    } finally {
      setCopyingToken(false)
    }
  }

  return (
    <div className="relative flex min-w-[176px] items-center justify-end gap-1.5 whitespace-nowrap">
      {toast && (
        <div
          className="fixed top-5 right-5 z-[9999] flex items-center gap-2.5 rounded-xl border px-4 py-3 text-[13px] font-medium shadow-lg  cursor-pointer transition-all"
          style={{
            background: toast.type === 'success' ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)',
            borderColor: toast.type === 'success' ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.25)',
            color: toast.type === 'success' ? '#6ee7b7' : '#fca5a5',
          }}
          onClick={() => setToast(null)}
        >
          <span className="text-base">{toast.type === 'success' ? '✓' : '✗'}</span>
          <span>{toast.text}</span>
        </div>
      )}
      {actionTask && (
        <ActionTaskModal
          title={actionTask.title}
          taskId={actionTask.taskId}
          taskStatus={actionTaskStatus}
          onClose={() => {
            setActionTask(null)
            setActionTaskStatus(null)
          }}
          onDone={handleActionDone}
        />
      )}
      {pendingAction && (
        <ActionParamsModal
          action={pendingAction.action}
          initialValues={pendingAction.params}
          submitting={running === pendingAction.action?.id}
          onClose={() => {
            if (!running) setPendingAction(null)
          }}
          onSubmit={(params) => {
            const action = pendingAction.action
            setPendingAction(null)
            runAction(action, params)
          }}
        />
      )}
      <button
        onClick={copyPrimaryToken}
        disabled={copyingToken}
        className="table-action-btn"
        title="复制平台常用 Token"
        aria-live="polite"
      >
        {tokenCopied ? <Check className="mr-1 h-3 w-3" /> : <KeyRound className="mr-1 h-3 w-3" />}
        {copyingToken ? '复制中…' : tokenCopied ? '已复制' : '复制 Token'}
      </button>
      <button onClick={onDetail} className="table-action-btn">详情</button>
      {(actions.length > 0 || canPush) && (
        <div className="relative">
          <button ref={triggerRef} onClick={() => setOpen(o => !o)}
            className="table-action-btn">更多 ▾</button>
          {open && typeof document !== 'undefined' && createPortal(
            <div
              ref={menuRef}
              className="fixed z-[9999] w-[220px] overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-card)]/96 py-1.5 shadow-[var(--shadow-soft)] "
              style={{ top: menuPosition.top, left: menuPosition.left, maxHeight: menuPosition.maxHeight }}
            >
              {canPush && (
                <>
                  <button
                    onClick={async () => {
                      setOpen(false)
                      setPushing(true)
                      try {
                        const result = await onPush()
                        if (Number(result?.failed || 0) > 0) {
                          throw new Error(result?.results?.[0]?.error || '推送失败')
                        }
                        setToast({ type: 'success', text: '推送成功' })
                      } catch (error: any) {
                        setToast({ type: 'error', text: error?.message || '推送失败' })
                      } finally {
                        setPushing(false)
                      }
                    }}
                    disabled={pushing || !!running}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-50"
                  >
                    <Send className="h-3.5 w-3.5" />
                    {pushing ? '推送中...' : '推送到默认目标'}
                  </button>
                  <div className="my-1 border-t border-[var(--border)]/70" />
                </>
              )}
              {actions.map(a => (
                <button key={a.id}
                  onClick={() => {
                    setOpen(false)
                    if (Array.isArray(a.params) && a.params.length > 0) {
                      setPendingAction({
                        action: a,
                        params: buildActionParamDraft(a, acc),
                      })
                      return
                    }
                    runAction(a, {})
                  }}
                  disabled={!!running}
                  className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-50">
                  {running === a.id ? '执行中...' : a.label}
                </button>
              ))}
              <div className="my-1 border-t border-[var(--border)]/70" />
              <button
                onClick={() => {
                  setOpen(false)
                  if (confirm(`确认删除 ${getAccountEmail(acc)}？`)) {
                    apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete)
                  }
                }}
                className="w-full px-3 py-2 text-left text-xs text-[#f0b0b0] transition-colors hover:bg-[rgba(239,68,68,0.08)] hover:text-[#ffd5d5]"
              >
                删除
              </button>
            </div>,
            document.body,
          )}
        </div>
      )}
      {actions.length === 0 && !canPush && (
        <button
          onClick={() => { if (confirm(`确认删除 ${getAccountEmail(acc)}？`)) apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete) }}
          className="table-action-btn table-action-btn-danger"
        >
          删除
        </button>
      )}
    </div>
  )
}

function AccountDetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border-t border-[var(--border-soft)] px-4 py-4 first:border-t-0 sm:px-5">
      <h3 className="text-sm font-semibold text-[var(--text-primary)]">{title}</h3>
      <dl className="mt-2 grid gap-x-6 sm:grid-cols-2">{children}</dl>
    </section>
  )
}

function AccountDetailField({
  label,
  value,
  mono = false,
  wide = false,
}: {
  label: string
  value: ReactNode
  mono?: boolean
  wide?: boolean
}) {
  const empty = value === null || value === undefined || value === ''
  return (
    <div className={`min-w-0 border-t border-[var(--border-soft)] py-2.5 first:border-t-0 ${wide ? 'sm:col-span-2' : ''}`}>
      <dt className="text-[11px] leading-4 text-[var(--text-muted)]">{label}</dt>
      <dd className={`mt-0.5 break-words text-xs leading-5 text-[var(--text-primary)] ${mono ? 'font-mono' : ''}`}>
        {empty ? <span className="text-[var(--text-muted)]">-</span> : value}
      </dd>
    </div>
  )
}

function AccountCredentialPanel({ accountId }: { accountId: number }) {
  const [credentials, setCredentials] = useState<AccountCredential[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [copiedKey, setCopiedKey] = useState('')
  const [copyFeedback, setCopyFeedback] = useState('')

  const loadCredentials = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const response = await apiFetch(`/accounts/${accountId}/credentials`)
      setCredentials(Array.isArray(response?.items) ? response.items : [])
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '读取凭证失败')
    } finally {
      setLoading(false)
    }
  }, [accountId])

  useEffect(() => {
    loadCredentials()
  }, [loadCredentials])

  useEffect(() => {
    if (!copiedKey) return
    const timer = setTimeout(() => {
      setCopiedKey('')
      setCopyFeedback('')
    }, 1600)
    return () => clearTimeout(timer)
  }, [copiedKey])

  const visibleCredentials = credentials.filter(item => (
    item.value
    && item.credential_type !== 'identifier'
  ))
  const commonToken = selectCommonToken(credentials)

  const copyCredential = async (credential: AccountCredential) => {
    const itemKey = `${credential.scope}:${credential.provider_name}:${credential.key}`
    try {
      await copyToClipboard(credential.value)
      setCopiedKey(itemKey)
      setCopyFeedback(`已复制 ${credentialLabel(credential)}`)
    } catch (error) {
      setCopyFeedback(error instanceof Error ? error.message : '复制失败，请手动选择')
    }
  }

  return (
    <section className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-surface)]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-soft)] px-4 py-3 sm:px-5">
        <div>
          <h3 className="text-sm font-semibold text-[var(--text-primary)]">登录凭证</h3>
          <p className="mt-0.5 text-xs text-[var(--text-muted)]">从本地数据库按需读取，关闭详情后不再保留在页面状态中。</p>
        </div>
        <div className="flex items-center gap-2">
          <span aria-live="polite" className="text-xs text-[var(--text-muted)]">{copyFeedback}</span>
          {commonToken && (
            <Button variant="outline" size="sm" onClick={() => copyCredential(commonToken)}>
              {copiedKey === `${commonToken.scope}:${commonToken.provider_name}:${commonToken.key}`
                ? <Check className="mr-1.5 h-3.5 w-3.5" />
                : <Copy className="mr-1.5 h-3.5 w-3.5" />}
              复制常用 Token
            </Button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="space-y-3 px-4 py-4 sm:px-5" aria-label="正在读取登录凭证">
          {[0, 1].map(item => (
            <div key={item} className="animate-pulse space-y-2 motion-reduce:animate-none">
              <div className="h-3 w-28 rounded bg-[var(--bg-hover)]" />
              <div className="h-12 rounded-md bg-[var(--bg-hover)]" />
            </div>
          ))}
        </div>
      ) : loadError ? (
        <div className="flex items-center justify-between gap-3 px-4 py-4 text-xs sm:px-5">
          <span role="alert" className="text-red-400">{loadError}</span>
          <Button variant="outline" size="sm" onClick={loadCredentials}>重新读取</Button>
        </div>
      ) : visibleCredentials.length === 0 ? (
        <div className="px-4 py-4 text-xs text-[var(--text-muted)] sm:px-5">没有可显示的 Token 或 Cookie。</div>
      ) : (
        <div>
          {visibleCredentials.map(credential => {
            const itemKey = `${credential.scope}:${credential.provider_name}:${credential.key}`
            const copied = copiedKey === itemKey
            return (
              <div key={itemKey} className="border-t border-[var(--border-soft)] px-4 py-3 first:border-t-0 sm:px-5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="text-xs font-medium text-[var(--text-primary)]">{credentialLabel(credential)}</span>
                    <span className="rounded border border-[var(--border)] px-1.5 py-0.5 text-[10px] text-[var(--text-muted)]">
                      {credential.scope === 'codex' ? 'Codex OAuth' : '平台'}
                    </span>
                    {credential.is_primary && (
                      <span className="rounded bg-[var(--accent-soft)] px-1.5 py-0.5 text-[10px] text-[var(--text-primary)]">主凭证</span>
                    )}
                    <span className="font-mono text-[10px] text-[var(--text-muted)]">{credential.key}</span>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => copyCredential(credential)}>
                    {copied ? <Check className="mr-1 h-3 w-3" /> : <Copy className="mr-1 h-3 w-3" />}
                    {copied ? '已复制' : '复制'}
                  </Button>
                </div>
                <textarea
                  readOnly
                  value={credential.value}
                  rows={credential.credential_type === 'cookie' ? 3 : 2}
                  spellCheck={false}
                  aria-label={`${credentialLabel(credential)} 明文`}
                  onFocus={event => event.currentTarget.select()}
                  className="control-surface control-surface-mono mt-2 resize-y break-all"
                />
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

// ── 账号详情弹框 ───────────────────────────────────────────
function DetailModal({ acc, onClose, onSave }: { acc: any; onClose: () => void; onSave: () => void }) {
  const { language } = useI18n()
  const [form, setForm] = useState({
    lifecycle_status: getLifecycleStatus(acc),
    primary_token: '',
    cashier_url: getCashierUrl(acc),
  })
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const identity = getAccountIdentity(acc)
  const status = getAccountStatus(acc)
  const subscription = getAccountSubscription(acc)
  const security = getAccountSecurity(acc)
  const deactivation = getDeactivationInfo(acc)
  const usage = getAccountUsage(acc)
  const codex = getCodexStatus(acc)
  const verificationMailbox = getVerificationMailbox(acc)
  const primaryMetrics = getPrimaryMetrics(acc)
  const secondaryMetrics = getSecondaryMetrics(acc)
  const warnings = getDisplayWarnings(acc)
  const displayBadges = getDisplayBadges(acc)
  const displaySections = getDisplaySections(acc)
  const pushDeliveries = Array.isArray(acc?.push_deliveries) ? acc.push_deliveries : []
  const amr = Array.isArray(security?.amr) ? security.amr.filter(Boolean) : []
  const credits: Record<string, unknown> = usage?.credits && typeof usage.credits === 'object' ? usage.credits : {}
  const creditFields = [
    { label: 'Credits 余额', value: scalarDetailValue(credits.balance) },
    {
      label: '无限 Credits',
      value: typeof credits.unlimited === 'boolean' ? booleanLabel(credits.unlimited, '是', '否') : '',
    },
    { label: '约可用本地消息', value: scalarDetailValue(credits.approx_local_messages) },
    { label: '约可用云端消息', value: scalarDetailValue(credits.approx_cloud_messages) },
  ].filter(item => item.value !== '')
  const usageObserved = Boolean(
    usage?.plan_type
    || usage?.used_percent !== null && usage?.used_percent !== undefined
    || usage?.limit_reached === true
    || Number(usage?.reset_at || 0) > 0
    || Object.keys(credits).length > 0,
  )
  const checkedAt = formatOptionalDateTime(String(status?.checked_at || ''), language)
  const securityObserved = Boolean(
    status?.checked_at
    || security?.phone_bound === true
    || security?.phone_number_masked
    || security?.mfa_enabled === true
    || amr.length > 0,
  )
  const cashierUrl = getSafeExternalUrl(subscription?.cashier_url)

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  const save = async () => {
    setSaving(true)
    setSaveError('')
    try {
      const payload: Record<string, string> = {
        lifecycle_status: form.lifecycle_status,
        cashier_url: form.cashier_url,
      }
      if (form.primary_token) payload.primary_token = form.primary_token
      await apiFetch(`/accounts/${acc.id}`, { method: 'PATCH', body: JSON.stringify(payload) })
      onSave()
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : '保存失败，请稍后重试')
    } finally { setSaving(false) }
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-lg flex flex-col"
        style={{ maxHeight: '92vh' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="account-detail-title"
        onClick={e => e.stopPropagation()}
      >
        {/* ── Sticky Header ── */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)] shrink-0">
          <div>
            <h2 id="account-detail-title" className="text-base font-semibold text-[var(--text-primary)]">账号详情</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">{getAccountEmail(acc)}</p>
          </div>
          <button autoFocus aria-label="关闭账号详情" onClick={onClose} className="rounded-md p-1.5 text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        {/* ── Scrollable Content ── */}
        <div className="px-6 py-4 space-y-3 flex-1 overflow-y-auto min-h-0">
          <div className="relative overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--accent-soft)] p-4 shadow-[var(--shadow-soft)]">
            <div className="pointer-events-none absolute -right-16 -top-20 h-44 w-44 rounded-full bg-[var(--accent-soft)] blur-3xl" />
            <div className="relative flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--text-muted)]">核心状态</div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Badge variant={STATUS_VARIANT[getDisplayStatus(acc)] || 'secondary'}>{getDisplayStatus(acc)}</Badge>
                  <span className="text-lg font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{getPlanName(acc)}</span>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2 text-right text-[11px] text-[var(--text-muted)] sm:grid-cols-4">
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">生命周期</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getLifecycleStatus(acc)}</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">有效性</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getValidityStatus(acc)}</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">套餐状态</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getPlanState(acc)}</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">最后检测</div>
                  <div className="mt-1 text-[var(--text-primary)]">{checkedAt || '未检测'}</div>
                </div>
              </div>
            </div>
            {secondaryMetrics.length > 0 && (
              <div className="relative mt-4 grid gap-2 sm:grid-cols-2">
                {secondaryMetrics.slice(0, 4).map((metric: any) => (
                  <DisplayMetricCard key={metric.key || metric.label} metric={metric} compact />
                ))}
              </div>
            )}
          </div>

          <AccountCredentialPanel accountId={Number(acc.id)} />

          {primaryMetrics.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-2">
              {primaryMetrics.map((metric: any) => (
                <DisplayMetricCard key={metric.key || metric.label} metric={metric} />
              ))}
            </div>
          )}

          <DisplayWarnings warnings={warnings} />
          <DisplaySections sections={displaySections} />

          {displayBadges.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {displayBadges.map((badge: any, index: number) => (
                <span key={`${badge?.label || 'badge'}-${index}`} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] px-2 py-0.5 text-[11px] text-[var(--text-secondary)]">
                  {badge?.label}
                </span>
              ))}
            </div>
          )}

          <div className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-surface)]">
            <AccountDetailSection title="身份与检测">
              <AccountDetailField label="本地账号 ID" value={identity?.id} mono />
              <AccountDetailField label="平台" value={identity?.platform} />
              <AccountDetailField label="登录邮箱" value={identity?.email} mono wide />
              <AccountDetailField label="远端邮箱" value={identity?.remote_email || '未获取'} mono />
              <AccountDetailField label="远端账号 ID" value={identity?.account_id || '未获取'} mono />
              <AccountDetailField label="用户 ID" value={identity?.user_id || '未获取'} mono wide />
              <AccountDetailField label="生命周期" value={status?.lifecycle || 'unknown'} />
              <AccountDetailField label="有效性" value={status?.validity || 'unknown'} />
              <AccountDetailField label="展示状态" value={status?.display || 'unknown'} />
              <AccountDetailField label="最后检测时间" value={checkedAt || '未检测'} />
            </AccountDetailSection>

            <AccountDetailSection title="订阅">
              <AccountDetailField label="套餐" value={subscription?.plan || '未检测'} />
              <AccountDetailField label="套餐状态" value={subscription?.state || 'unknown'} />
              <AccountDetailField label="判断来源" value={subscription?.source || '未记录'} />
              <AccountDetailField
                label="试用结束时间"
                value={formatUnixTimestamp(subscription?.trial_end_time, language) || '未记录'}
              />
              <AccountDetailField
                label="Cashier URL"
                wide
                mono
                value={cashierUrl ? (
                  <a className="text-[var(--accent)] underline-offset-2 hover:underline" href={cashierUrl} target="_blank" rel="noreferrer">
                    {subscription.cashier_url}
                  </a>
                ) : subscription?.cashier_url || '未记录'}
              />
            </AccountDetailSection>

            <AccountDetailSection title="安全">
              <AccountDetailField
                label="手机号"
                value={securityObserved ? booleanLabel(security?.phone_bound === true, '已绑定', '未绑定') : '未检测'}
              />
              <AccountDetailField label="脱敏号码" value={security?.phone_number_masked || '未记录'} mono />
              <AccountDetailField
                label="MFA"
                value={securityObserved ? booleanLabel(security?.mfa_enabled === true, '已启用', '未启用') : '未检测'}
              />
              <AccountDetailField
                label="认证方式 (AMR)"
                value={amr.length > 0 ? amr.map(String).join(' · ') : '未记录'}
              />
              {deactivation.reason && (
                <AccountDetailField label="封号原因" value={deactivation.reason} wide />
              )}
              {deactivation.detectedAt && (
                <AccountDetailField label="封号检测时间" value={formatOptionalDateTime(deactivation.detectedAt, language)} />
              )}
              {deactivation.error && (
                <AccountDetailField label="封号原始错误" value={deactivation.error} mono wide />
              )}
            </AccountDetailSection>

            <AccountDetailSection title="额度">
              {!usageObserved ? (
                <AccountDetailField label="检测结果" value="尚未获取额度数据" wide />
              ) : (
                <>
                  <AccountDetailField label="套餐类型" value={usage?.plan_type || '未记录'} />
                  <AccountDetailField
                    label="已用比例"
                    value={usage?.used_percent !== null && usage?.used_percent !== undefined ? `${usage.used_percent}%` : '未记录'}
                  />
                  <AccountDetailField label="达到上限" value={booleanLabel(usage?.limit_reached === true, '是', '否')} />
                  <AccountDetailField label="重置时间" value={formatUnixTimestamp(usage?.reset_at, language) || '未记录'} />
                  {creditFields.map(item => (
                    <AccountDetailField key={item.label} label={item.label} value={item.value} />
                  ))}
                </>
              )}
            </AccountDetailSection>

            <AccountDetailSection title="Codex OAuth">
              <AccountDetailField label="凭证范围" value="仅指 Codex OAuth，不代表平台登录凭证。" wide />
              <AccountDetailField label="授权状态" value={booleanLabel(codex.authorized, '已授权', '未授权')} />
              <AccountDetailField label="Codex 套餐" value={codex.planType || '未记录'} />
              <AccountDetailField label="Codex 邮箱" value={codex.email || '未记录'} mono />
              <AccountDetailField label="Codex 账号 ID" value={codex.accountId || '未记录'} mono />
              <AccountDetailField label="过期时间" value={formatOptionalDateTime(codex.expiresAt, language) || '未记录'} />
              <AccountDetailField label="最后刷新" value={formatOptionalDateTime(codex.lastRefresh, language) || '未记录'} />
              <AccountDetailField label="授权文件" value={codex.authPath || '未记录'} mono wide />
              <AccountDetailField label="Codex Access Token" value={booleanLabel(codex.hasAccessToken, '已保存', '未保存')} />
              <AccountDetailField label="Codex Refresh Token" value={booleanLabel(codex.hasRefreshToken, '已保存', '未保存')} />
            </AccountDetailSection>

            <AccountDetailSection title="远端推送">
              {pushDeliveries.length > 0 ? pushDeliveries.map((delivery: any) => (
                <AccountDetailField
                  key={delivery.target_key}
                  wide
                  label={delivery.target_label || delivery.target_key}
                  value={
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className={delivery.status === 'success' ? 'text-emerald-500' : delivery.status === 'failed' ? 'text-red-500' : 'text-amber-500'}>
                        {delivery.status === 'success' ? '已推送' : delivery.status === 'failed' ? '推送失败' : '推送中'}
                      </span>
                      <span className="text-[var(--text-muted)]">{delivery.payload_format || 'codex'} · 尝试 {delivery.attempt_count || 0} 次</span>
                      {delivery.last_attempt_at && <span className="text-[var(--text-muted)]">{formatOptionalDateTime(delivery.last_attempt_at, language)}</span>}
                      {delivery.last_error && <span className="w-full text-red-400">{delivery.last_error}</span>}
                    </div>
                  }
                />
              )) : (
                <AccountDetailField wide label="状态" value="尚未推送到任何远端目标" />
              )}
            </AccountDetailSection>

            <AccountDetailSection title="验证邮箱">
              {verificationMailbox ? (
                <>
                  <AccountDetailField label="Provider" value={verificationMailbox.provider || '未记录'} />
                  <AccountDetailField label="邮箱" value={verificationMailbox.email || '未记录'} mono />
                  <AccountDetailField label="Provider 账号 ID" value={verificationMailbox.account_id || '未记录'} mono wide />
                </>
              ) : (
                <AccountDetailField label="关联状态" value="未关联验证邮箱" wide />
              )}
            </AccountDetailSection>
          </div>

          <section className="rounded-lg border border-[var(--border)] bg-[var(--bg-surface)] p-4 sm:p-5">
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">管理操作</h3>
            <p className="mt-1 text-xs text-[var(--text-muted)]">修改本地管理状态；敏感凭证不会回显。</p>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <div>
                <label className="text-xs text-[var(--text-muted)] block mb-1">生命周期状态</label>
                <select value={form.lifecycle_status} onChange={e => setForm(f => ({ ...f, lifecycle_status: e.target.value }))}
                  className="control-surface appearance-none">
                  {['registered','trial','subscribed','expired','invalid','deactivated'].map(s => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-[var(--text-muted)] block mb-1">试用链接</label>
                <textarea value={form.cashier_url} onChange={e => setForm(f => ({ ...f, cashier_url: e.target.value }))}
                  rows={2} className="control-surface control-surface-mono resize-none" />
              </div>
              <div className="sm:col-span-2">
                <label className="text-xs text-[var(--text-muted)] block mb-1">更新主凭证（留空则不修改）</label>
                <textarea value={form.primary_token} onChange={e => setForm(f => ({ ...f, primary_token: e.target.value }))}
                  rows={2} autoComplete="off" placeholder="仅在需要替换凭证时输入" className="control-surface control-surface-mono resize-none" />
              </div>
            </div>
            {saveError && (
              <p role="alert" className="mt-3 text-xs text-red-400">保存失败：{saveError}</p>
            )}
          </section>
        </div>
        {/* ── Sticky Footer ── */}
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)] shrink-0">
          <Button onClick={save} disabled={saving} className="flex-1">{saving ? '保存中...' : '保存修改'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

// ── 导入弹框 ────────────────────────────────────────────────
function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const submit = async () => {
    setLoading(true)
    try {
      const lines = text.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', { method: 'POST', body: JSON.stringify({ platform: 'chatgpt', lines }) })
      setResult(`导入成功 ${res.created} 个`); onDone()
    } catch (e: any) { setResult(`失败: ${e.message}`) } finally { setLoading(false) }
  }
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm p-6" onClick={e => e.stopPropagation()}>
        <h2 className="text-base font-semibold text-[var(--text-primary)] mb-2">批量导入</h2>
        <p className="text-xs text-[var(--text-muted)] mb-3">每行格式: <code className="bg-[var(--bg-hover)] px-1 rounded">email password [cashier_url]</code></p>
        <textarea value={text} onChange={e => setText(e.target.value)} rows={8}
          className="control-surface control-surface-mono resize-none mb-3" />
        {result && <p className="text-sm text-emerald-400 mb-3">{result}</p>}
        <div className="flex gap-2">
          <Button onClick={submit} disabled={loading} className="flex-1">{loading ? '导入中...' : '导入'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

function ExportMenu({
  total,
  filters,
  selectedIds,
}: {
  total: number
  filters: AccountFilterState
  selectedIds: number[]
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const hasSelection = selectedIds.length > 0

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const doExport = async (format: string) => {
    setLoading(format)
    try {
      const { blob, filename } = await apiDownload(`/accounts/export/${format}`, {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids: hasSelection ? selectedIds : [],
          select_all: !hasSelection,
          filters: hasSelection ? {} : toApiFilters(filters),
        }),
      })
      triggerBrowserDownload(blob, filename)
      setOpen(false)
    } catch (e: any) {
      window.alert(e?.message || '导出失败')
    } finally {
      setLoading(null)
    }
  }

  const options = [
    { key: 'json', label: '导出 JSON' },
    { key: 'csv', label: '导出 CSV' },
    { key: 'any2api', label: '导出 Any2Api' },
    { key: 'sub2api', label: '导出 Sub2Api' },
    { key: 'codex', label: '导出 Codex' },
    { key: 'cpa', label: '导出 CPA' },
  ]

  return (
    <div className="relative" ref={menuRef}>
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(v => !v)}
        disabled={total === 0 || !!loading}
        className={ACCOUNT_TOOL_BUTTON_CLASS}
      >
        <Download className="h-4 w-4 mr-1 shrink-0" />
        {loading ? '导出中...' : hasSelection ? `导出已选(${selectedIds.length})` : '导出'}
      </Button>
      {open && (
        <div className="absolute right-0 top-10 z-20 min-w-[148px] rounded-lg border border-[var(--border)] bg-[var(--bg-card)] py-1 shadow-lg">
          <div className="px-3 py-1 text-[11px] text-[var(--text-muted)]">
            {hasSelection ? `导出 ${selectedIds.length} 个已选账号` : '导出当前筛选结果'}
          </div>
          {options.map(option => (
            <button
              key={option.key}
              onClick={() => doExport(option.key)}
              className="w-full px-3 py-1.5 text-left text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function PushMenu({
  targets,
  total,
  filters,
  selectedIds,
  onPush,
}: {
  targets: PushTarget[]
  total: number
  filters: AccountFilterState
  selectedIds: number[]
  onPush: (ids: number[], targetKey: string, selectAll: boolean, filters: AccountFilterState) => Promise<any>
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const defaultTarget = targets.find(target => target.is_default) || targets[0]

  useEffect(() => {
    if (!open) return
    const handler = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const pushTo = async (target: PushTarget) => {
    if (total === 0) return
    const selectAll = selectedIds.length === 0
    const targetCount = selectAll ? total : selectedIds.length
    if (!window.confirm(`将当前${selectAll ? '完整筛选结果' : '已选账号'}（${targetCount} 个）推送到 ${target.label}？`)) return
    setLoading(true)
    try {
      await onPush(selectedIds, target.key, selectAll, filters)
      setOpen(false)
    } catch (error: any) {
      window.alert(error?.message || '推送失败')
    } finally {
      setLoading(false)
    }
  }

  const title = targets.length === 0
    ? '请先到设置 → 推送目标中配置并启用目标'
    : selectedIds.length === 0
      ? `推送当前完整筛选结果，共 ${total} 个账号`
      : `推送 ${selectedIds.length} 个已选账号`

  return (
    <div className="relative" ref={menuRef} title={title}>
      <Button
        variant="outline"
        size="sm"
        disabled={total === 0 || targets.length === 0 || loading}
        onClick={() => {
          if (targets.length === 1 && defaultTarget) void pushTo(defaultTarget)
          else setOpen(value => !value)
        }}
        className={ACCOUNT_TOOL_BUTTON_CLASS}
      >
        <Send className="mr-1.5 h-3.5 w-3.5 shrink-0" />
        {loading ? '推送中...' : selectedIds.length ? `推送已选(${selectedIds.length})` : `推送筛选(${total})`}
      </Button>
      {open && targets.length > 1 && (
        <div className="absolute right-0 top-10 z-20 min-w-[190px] rounded-lg border border-[var(--border)] bg-[var(--bg-card)] py-1 shadow-lg">
          <div className="px-3 py-1 text-[11px] text-[var(--text-muted)]">选择推送目标</div>
          {targets.map(target => (
            <button
              key={target.key}
              onClick={() => void pushTo(target)}
              className="flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
            >
              <span>{target.label}</span>
              <span className="text-[10px] text-[var(--text-muted)]">{target.payload_format}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Main ────────────────────────────────────────────────────
export default function Accounts() {
  const { t, language } = useI18n()
  const tab = 'chatgpt'

  const [accounts, setAccounts] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [filters, setFilters] = useState<AccountFilterState>(() => readFiltersFromUrl())
  const [searchDraft, setSearchDraft] = useState(() => readFiltersFromUrl().search)
  const [showMoreFilters, setShowMoreFilters] = useState(false)
  const [filterStats, setFilterStats] = useState<any>({ total: 0, mailbox_providers: [], regions: [] })
  const [savedFilters, setSavedFilters] = useState<SavedFilterPreset[]>(() => {
    try { return JSON.parse(localStorage.getItem(SAVED_FILTERS_KEY) || '[]') } catch { return [] }
  })
  const [detail, setDetail] = useState<any | null>(null)
  const [showImport, setShowImport] = useState(false)
  const [showRegister, setShowRegister] = useState(false)
  const [platformsMap, setPlatformsMap] = useState<Record<string, any>>({})
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [actionResult, setActionResult] = useState<{ title: string; payload: any } | null>(null)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [batchRefreshing, setBatchRefreshing] = useState(false)
  const [batchCodexAuthorizing, setBatchCodexAuthorizing] = useState(false)
  const [showRefreshOptions, setShowRefreshOptions] = useState(false)
  const [batchTask, setBatchTask] = useState<{ taskId: string; title: string } | null>(null)
  const [batchTaskStatus, setBatchTaskStatus] = useState<string | null>(null)
  const [batchProxyMode, setBatchProxyMode] = useState('direct')
  const [batchProxyValue, setBatchProxyValue] = useState('')
  const [batchCodexOAuthMode, setBatchCodexOAuthMode] = useState('browser')
  const [batchCodexConcurrency, setBatchCodexConcurrency] = useState(2)
  const [pushTargets, setPushTargets] = useState<PushTarget[]>([])
  const loadRequestRef = useRef(0)
  const filterKey = JSON.stringify(filters)

  useEffect(() => {
    getConfig().then((config) => {
      setBatchProxyMode(String(config.account_validity_proxy_mode || 'direct'))
      setBatchProxyValue(String(config.account_validity_proxy_url || ''))
    }).catch(() => {})
    getPlatforms().then((list: any[]) => {
      const map: Record<string, any> = {}
      list.forEach(p => { map[p.name] = p })
      setPlatformsMap(map)
    }).catch(() => {})
    apiFetch('/accounts/push-targets')
      .then(data => setPushTargets(Array.isArray(data?.items) ? data.items : []))
      .catch(() => setPushTargets([]))
    apiFetch(`/accounts/stats?platform=${tab}`)
      .then(data => setFilterStats(data || {}))
      .catch(() => {})
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => {
      setPage(1)
      setFilters(current => current.search === searchDraft ? current : { ...current, search: searchDraft })
    }, 350)
    return () => clearTimeout(timer)
  }, [searchDraft])

  useEffect(() => {
    const params = new URLSearchParams()
    ;(Object.keys(filters) as Array<keyof AccountFilterState>).forEach(key => {
      const value = filters[key]
      if (value && !(key === 'sort_by' && value === 'created_at') && !(key === 'sort_order' && value === 'desc')) {
        params.set(key, value)
      }
    })
    const nextUrl = `${window.location.pathname}${params.size ? `?${params}` : ''}${window.location.hash}`
    window.history.replaceState(null, '', nextUrl)
  }, [filterKey])

  useEffect(() => {
    setSelectedIds(new Set())
    setPage(1)
  }, [tab, filterKey])

  const load = useCallback(async (requestedPage = page) => {
    const requestId = ++loadRequestRef.current
    setLoading(true)
    try {
      const params = new URLSearchParams({
        platform: tab,
        page: String(requestedPage),
        page_size: String(ACCOUNT_PAGE_SIZE),
      })
      const apiFilters = toApiFilters(filters)
      Object.entries(apiFilters).forEach(([key, value]) => { if (value) params.set(key, value) })
      const data = await apiFetch(`/accounts?${params}`)
      if (requestId !== loadRequestRef.current) return
      const nextTotal = Number(data.total || 0)
      const lastPage = Math.max(1, Math.ceil(nextTotal / ACCOUNT_PAGE_SIZE))
      setAccounts(data.items)
      setTotal(nextTotal)
      if (requestedPage > lastPage) setPage(lastPage)
    } finally {
      if (requestId === loadRequestRef.current) setLoading(false)
    }
  }, [tab, filterKey, page])

  useEffect(() => { load(page) }, [load, page])

  const pushAccounts = useCallback(async (ids: number[], targetKey = '', selectAll = false, activeFilters = filters) => {
    const target = pushTargets.find(item => item.key === targetKey)
      || pushTargets.find(item => item.is_default)
      || pushTargets[0]
    if (!target) throw new Error('未配置已启用的推送目标')

    if (!selectAll) setAccounts(current => markPushPending(current, ids, target, new Date().toISOString()))
    try {
      const result = await apiFetch('/accounts/push', {
        method: 'POST',
        body: JSON.stringify({
          platform: tab,
          ids,
          select_all: selectAll,
          filters: selectAll ? toApiFilters(activeFilters) : {},
          target_key: target.key,
        }),
      })
      setActionResult({
        title: `${result?.target_label || target.label}推送结果：成功 ${result?.succeeded || 0}，失败 ${result?.failed || 0}`,
        payload: result,
      })
      return result
    } finally {
      await load()
    }
  }, [filterKey, load, pushTargets, tab])

  
  const exportCsv = () => {
    const header = 'email,password,display_status,lifecycle_status,plan_state,validity_status,cashier_url,created_at'
    const rowsSource = selectedIds.size > 0 ? accounts.filter(a => selectedIds.has(a.id)) : accounts
    const rows = rowsSource.map(a => [
      getAccountEmail(a),
      a.password,
      getDisplayStatus(a),
      getLifecycleStatus(a),
      getPlanState(a),
      getValidityStatus(a),
      getCashierUrl(a),
      a.created_at,
    ].map(escapeCsvField).join(','))
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv' })
    triggerBrowserDownload(blob, `${tab}_accounts.csv`)
  }

  const pageIds = accounts.map(acc => acc.id)
  const allSelectedOnPage = pageIds.length > 0 && pageIds.every(id => selectedIds.has(id))
  const selectedCount = selectedIds.size
  const totalPages = Math.max(1, Math.ceil(total / ACCOUNT_PAGE_SIZE))
  const rangeStart = total > 0 ? (page - 1) * ACCOUNT_PAGE_SIZE + 1 : 0
  const rangeEnd = total > 0 ? Math.min(page * ACCOUNT_PAGE_SIZE, total) : 0

  const toggleOne = (id: number) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const togglePage = () => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (allSelectedOnPage) pageIds.forEach(id => next.delete(id))
      else pageIds.forEach(id => next.add(id))
      return next
    })
  }

  const copy = (text: string) => {
    void copyToClipboard(text)
  }
  const emailApiLine = (email: string) =>
    `${email} https://hsxhome.com/api/find/openai?email=${email}&t=fzKIywnF4KEGGB_i`

  const currentPlatformMeta = platformsMap[tab]
  const platformLabel = currentPlatformMeta?.display_name || tab
  const activeFilterCount = getActiveFilterCount(filters)
  const pushTargetOptions: Array<{ key: string; label: string }> = Array.from(new Map<string, { key: string; label: string }>([
    ...pushTargets.map(item => [item.key, { key: item.key, label: item.label }] as const),
    ...(filterStats.push_targets || []).map((item: any) => [item.key, { key: item.key, label: item.label || item.key }] as const),
  ]).values())
  const updateFilter = (key: keyof AccountFilterState, value: string) => {
    setFilters(current => ({ ...current, [key]: value }))
  }
  const resetFilters = () => {
    setFilters({ ...EMPTY_ACCOUNT_FILTERS })
    setSearchDraft('')
  }
  const saveCurrentFilters = () => {
    const name = window.prompt('筛选方案名称')?.trim()
    if (!name) return
    const next = [...savedFilters.filter(item => item.name !== name), { name, filters }]
    setSavedFilters(next)
    localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify(next))
  }
  const applyPreset = (name: string) => {
    const preset = savedFilters.find(item => item.name === name)
    if (!preset) return
    setFilters({ ...EMPTY_ACCOUNT_FILTERS, ...preset.filters })
    setSearchDraft(preset.filters.search || '')
  }
  const refreshTargetCount = selectedCount || total
  const refreshScopeLabel = selectedCount > 0 ? '已选账号' : '当前完整筛选结果'
  const refreshProxyLabel = batchProxyMode === 'manual'
    ? `手动代理 ${batchProxyValue.trim() || '未填写'}`
    : (batchProxyMode === 'proxy_service' ? '代理服务' : '直连')
  const startBatchRefresh = async ({ reloginInvalid }: { reloginInvalid: boolean }) => {
    setShowRefreshOptions(false)
    setBatchRefreshing(true)
    try {
      const hasSelection = selectedIds.size > 0
      const targetCount = hasSelection ? selectedIds.size : total
      const res = await apiFetch('/accounts/check-all', {
        method: 'POST',
        body: JSON.stringify({
          platform: tab,
          ids: hasSelection ? [...selectedIds] : [],
          select_all: !hasSelection,
          filters: !hasSelection ? toApiFilters(filters) : {},
          platform_proxy_mode: batchProxyMode,
          platform_proxy_value: batchProxyValue.trim(),
          concurrency: batchCodexConcurrency,
          relogin_invalid: reloginInvalid,
          relogin_params: reloginInvalid ? {
            browser_mode: 'headless',
            keep_browser_open: 'false',
            platform_proxy_mode: batchProxyMode,
            platform_proxy_value: batchProxyValue.trim(),
          } : {},
        }),
      })
      if (res?.task_id) {
        setBatchTask({
          taskId: res.task_id,
          title: `${t('accounts.refreshAllCreditsTask', { platform: platformLabel })}${reloginInvalid ? ' + 失效重登' : ''} (${targetCount})`,
        })
        setBatchTaskStatus(null)
      } else {
        setBatchRefreshing(false)
      }
    } catch (e) {
      console.error(e)
      setBatchRefreshing(false)
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 overflow-hidden">
      {detail && <DetailModal acc={detail} onClose={() => setDetail(null)} onSave={() => { setDetail(null); load() }} />}
      {showImport && <ImportModal onClose={() => setShowImport(false)} onDone={() => { setShowImport(false); load() }} />}
      {showRegister && <RegisterModal platformMeta={platformsMap[tab]} onClose={() => setShowRegister(false)} onDone={() => load()} />}
      {showRefreshOptions && (
        <RefreshCreditsOptionsModal
          targetCount={refreshTargetCount}
          scopeLabel={refreshScopeLabel}
          platformLabel={platformLabel}
          proxyLabel={refreshProxyLabel}
          concurrency={batchCodexConcurrency}
          onClose={() => setShowRefreshOptions(false)}
          onSubmit={startBatchRefresh}
        />
      )}
      {actionResult && <ActionResultModal title={actionResult.title} payload={actionResult.payload} onClose={() => setActionResult(null)} />}
      {batchTask && (
        <ActionTaskModal
          title={batchTask.title}
          taskId={batchTask.taskId}
          taskStatus={batchTaskStatus}
          onClose={() => {
            setBatchTask(null)
            setBatchTaskStatus(null)
            setBatchRefreshing(false)
            setBatchCodexAuthorizing(false)
            load()
          }}
          onDone={(status) => {
            setBatchTaskStatus(status)
            setBatchRefreshing(false)
            setBatchCodexAuthorizing(false)
            load()
          }}
        />
      )}
      <Card className="shrink-0 bg-[var(--bg-pane)]/40 border border-[var(--border)] shadow-sm">
        <div className="flex flex-col gap-3 px-5 py-4 border-b border-[var(--border)]/50 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-3">
            <h1 className="shrink-0 text-lg font-semibold tracking-tight text-[var(--text-primary)]">
              {platformLabel}
            </h1>
            <div className="hidden h-4 w-[1px] bg-[var(--border)] sm:block"></div>
            <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
              <button onClick={resetFilters} className="filter-stat-chip">全部 {filterStats.total || 0}</button>
              <button onClick={() => updateFilter('status', filters.status === 'trial' ? '' : 'trial')} className={`filter-stat-chip filter-stat-success ${filters.status === 'trial' ? 'is-active' : ''}`}>试用 {filterStats.trial || 0}</button>
              <button onClick={() => updateFilter('status', filters.status === 'subscribed' ? '' : 'subscribed')} className={`filter-stat-chip filter-stat-accent ${filters.status === 'subscribed' ? 'is-active' : ''}`}>订阅 {filterStats.subscribed || 0}</button>
              <button onClick={() => updateFilter('mailbox_bound', filters.mailbox_bound === 'bound' ? '' : 'bound')} className={`filter-stat-chip ${filters.mailbox_bound === 'bound' ? 'is-active' : ''}`}>验证邮箱 {filterStats.mailbox_bound || 0}</button>
              <button onClick={() => updateFilter('checked_state', filters.checked_state === 'unchecked' ? '' : 'unchecked')} className={`filter-stat-chip filter-stat-warning ${filters.checked_state === 'unchecked' ? 'is-active' : ''}`}>未检测 {filterStats.unchecked || 0}</button>
              <button onClick={() => updateFilter('codex_auth_state', filters.codex_auth_state === 'unauthorized' ? '' : 'unauthorized')} className={`filter-stat-chip ${filters.codex_auth_state === 'unauthorized' ? 'is-active' : ''}`}>Codex 未授权 {filterStats.codex_unauthorized || 0}</button>
              <button onClick={() => updateFilter('push_status', filters.push_status === 'not_pushed' ? '' : 'not_pushed')} className={`filter-stat-chip ${filters.push_status === 'not_pushed' ? 'is-active' : ''}`}>未推送 {filterStats.push_not_pushed || 0}</button>
              <button onClick={() => updateFilter('push_status', filters.push_status === 'failed' ? '' : 'failed')} className={`filter-stat-chip filter-stat-danger ${filters.push_status === 'failed' ? 'is-active' : ''}`}>推送失败 {filterStats.push_failed || 0}</button>
              <button onClick={() => updateFilter('status', filters.status === 'invalid' ? '' : 'invalid')} className={`filter-stat-chip filter-stat-danger ${filters.status === 'invalid' ? 'is-active' : ''}`}>失效 {filterStats.invalid || 0}</button>
              <button onClick={() => updateFilter('status', filters.status === 'deactivated' ? '' : 'deactivated')} className={`filter-stat-chip filter-stat-danger ${filters.status === 'deactivated' ? 'is-active' : ''}`}>封号 {filterStats.deactivated || 0}</button>
              {selectedCount > 0 && <span className="flex items-center rounded-full bg-[var(--text-primary)]/10 px-2 py-0.5 font-medium text-[var(--text-primary)] ring-1 ring-inset ring-[var(--text-primary)]/20">{t('accounts.selected', { count: selectedCount })}</span>}
            </div>
          </div>
          <div className="flex w-full flex-wrap items-center gap-2 lg:w-auto lg:justify-end">
            <Button size="sm" onClick={() => setShowRegister(true)} className="h-8 shrink-0 whitespace-nowrap shadow-sm">
              <Plus className="mr-1.5 h-3.5 w-3.5 shrink-0" />
              {t('accounts.autoRegister')}
            </Button>
            <div className="hidden h-4 w-[1px] shrink-0 bg-[var(--border)] sm:block"></div>
            <Button size="sm" variant="outline" onClick={() => setShowImport(true)} className={ACCOUNT_TOOL_BUTTON_CLASS}>
              <Upload className="mr-1.5 h-3.5 w-3.5 shrink-0" />
              {t('accounts.import')}
            </Button>
            {tab === 'chatgpt' ? (
              <ExportMenu
                total={total}
                filters={filters}
                selectedIds={[...selectedIds]}
              />
            ) : (
              <Button size="sm" variant="outline" onClick={exportCsv} disabled={accounts.length === 0} className={ACCOUNT_TOOL_BUTTON_CLASS}>
                <Download className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                {t('accounts.export')}
              </Button>
            )}
            <PushMenu
              targets={pushTargets}
              total={total}
              filters={filters}
              selectedIds={[...selectedIds]}
              onPush={pushAccounts}
            />
          </div>
        </div>
        
        {/* Search & Filter Toolbar */}
        <div className="flex flex-wrap items-center justify-between gap-3 bg-[var(--bg-pane)]/20 px-5 py-2.5">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
            <div className="relative min-w-[260px] flex-1 max-w-xl">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--text-muted)]" />
              <input
                type="text"
                placeholder="搜索 ChatGPT/Codex/验证邮箱、User ID、Account ID"
                value={searchDraft}
                onChange={e => setSearchDraft(e.target.value)}
                className="filter-control w-full pl-8"
              />
            </div>
            <select
              value={filters.status}
              onChange={e => updateFilter('status', e.target.value)}
              className="filter-control w-auto min-w-28"
            >
              <option value="">{t('accounts.allStatuses')}</option>
              <option value="registered">{translateAccountStatus('registered', language)}</option>
              <option value="trial">{t('dashboard.trial')}</option>
              <option value="subscribed">{t('dashboard.subscribed')}</option>
              <option value="free">{t('accounts.free')}</option>
              <option value="eligible">{t('accounts.eligible')}</option>
              <option value="expired">{t('accounts.expired')}</option>
              <option value="invalid">{t('dashboard.invalid')}</option>
              <option value="deactivated">{translateAccountStatus('deactivated', language)}</option>
            </select>
            <select value={filters.sort_by} onChange={e => updateFilter('sort_by', e.target.value)} className="filter-control w-auto min-w-32" title="排序字段">
              <option value="created_at">注册时间</option>
              <option value="checked_at">检测时间</option>
              <option value="expires_at">到期时间</option>
              <option value="updated_at">更新时间</option>
            </select>
            <button onClick={() => updateFilter('sort_order', filters.sort_order === 'desc' ? 'asc' : 'desc')} className="filter-control w-auto whitespace-nowrap">
              {filters.sort_order === 'desc' ? '降序' : '升序'}
            </button>
            <button onClick={() => setShowMoreFilters(value => !value)} className={`filter-control inline-flex w-auto items-center gap-1.5 ${showMoreFilters || activeFilterCount ? 'is-active' : ''}`}>
              <SlidersHorizontal className="h-3.5 w-3.5" />
              更多筛选{activeFilterCount ? ` ${activeFilterCount}` : ''}
            </button>
            {savedFilters.length > 0 && (
              <select defaultValue="" onChange={e => { applyPreset(e.target.value); e.currentTarget.value = '' }} className="filter-control w-auto max-w-40">
                <option value="" disabled>常用方案</option>
                {savedFilters.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
              </select>
            )}
          </div>
          
          <div className="flex items-center gap-2">
            <select
              value={batchProxyMode}
              onChange={e => setBatchProxyMode(e.target.value)}
              className="h-7 rounded-md border border-[var(--border)] bg-transparent px-2 text-xs text-[var(--text-secondary)] outline-none focus:border-[var(--text-primary)]"
              title="批量刷新代理"
            >
              <option value="direct">直连</option>
              <option value="proxy_service">代理服务</option>
              <option value="manual">手动代理</option>
            </select>
            {batchProxyMode === 'manual' ? (
              <input
                value={batchProxyValue}
                onChange={e => setBatchProxyValue(e.target.value)}
                placeholder="socks5://user:pass@host:port"
                spellCheck={false}
                className="h-7 w-56 rounded-md border border-[var(--border)] bg-transparent px-2 font-mono text-xs text-[var(--text-secondary)] outline-none focus:border-[var(--text-primary)]"
              />
            ) : null}
            {tab === 'chatgpt' && total > 0 ? (
              <>
                <select
                  value={batchCodexOAuthMode}
                  onChange={e => setBatchCodexOAuthMode(e.target.value)}
                  className="h-7 rounded-md border border-[var(--border)] bg-transparent px-2 text-xs text-[var(--text-secondary)] outline-none focus:border-[var(--text-primary)]"
                  title="Codex OAuth 授权模式"
                >
                  <option value="browser">浏览器</option>
                  <option value="browser_protocol">浏览器协议（Fetch 优先）</option>
                  <option value="protocol">协议（复用会话）</option>
                </select>
                <select
                  value={batchCodexConcurrency}
                  onChange={e => setBatchCodexConcurrency(Number(e.target.value) || 1)}
                  className="h-7 rounded-md border border-[var(--border)] bg-transparent px-2 text-xs text-[var(--text-secondary)] outline-none focus:border-[var(--text-primary)]"
                  title="批量动作并发数"
                >
                  {[1, 2, 3, 4, 5, 10, 20].map(value => (
                    <option key={value} value={value}>并发 {value}</option>
                  ))}
                </select>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={batchCodexAuthorizing || batchRefreshing || loading}
                  className="h-7 px-2.5 text-[var(--text-muted)] hover:text-violet-500 hover:bg-violet-500/10"
                  title="为勾选账号批量执行 Codex OAuth 授权"
                  onClick={async () => {
                    setBatchCodexAuthorizing(true)
                    try {
                      const ids = [...selectedIds]
                      const selectAll = ids.length === 0
                      const targetCount = selectAll ? total : ids.length
                      if (!window.confirm(`为当前${selectAll ? '完整筛选结果' : '已选账号'}（${targetCount} 个）执行 Codex OAuth 授权？`)) {
                        setBatchCodexAuthorizing(false)
                        return
                      }
                      const res = await apiFetch('/accounts/codex-oauth/authorize', {
                        method: 'POST',
                        body: JSON.stringify({
                          platform: tab,
                          ids,
                          select_all: selectAll,
                          filters: selectAll ? toApiFilters(filters) : {},
                          platform_proxy_mode: batchProxyMode,
                          platform_proxy_value: batchProxyValue.trim(),
                          concurrency: batchCodexConcurrency,
                          params: {
                            oauth_mode: batchCodexOAuthMode,
                            browser_mode: 'headless',
                            keep_browser_open: 'false',
                            platform_proxy_mode: batchProxyMode,
                            platform_proxy_value: batchProxyValue.trim(),
                          },
                        }),
                      })
                      if (res?.task_id) {
                        setBatchTask({
                          taskId: res.task_id,
                          title: `Codex OAuth 批量授权 (${targetCount})`,
                        })
                        setBatchTaskStatus(null)
                      }
                    } catch (e) {
                      console.error(e)
                      setBatchCodexAuthorizing(false)
                    }
                  }}
                >
                  <ShieldCheck className={`mr-1 h-3.5 w-3.5 ${batchCodexAuthorizing ? 'animate-pulse' : ''}`} />
                  {batchCodexAuthorizing ? '授权中...' : `Codex授权(${selectedCount || total})`}
                </Button>
              </>
            ) : null}
            <Button
              variant="ghost"
              size="sm"
              disabled={batchRefreshing || batchCodexAuthorizing || loading}
              className="h-7 px-2.5 text-[var(--text-muted)] hover:text-amber-500 hover:bg-amber-500/10"
              title="打开刷新额度设置"
              onClick={() => setShowRefreshOptions(true)}
            >
              <Zap className={`mr-1 h-3.5 w-3.5 ${batchRefreshing ? 'animate-pulse' : ''}`} />
              {batchRefreshing ? t('accounts.refreshingCredits') : t('accounts.refreshCredits')}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => load()} disabled={loading} className="h-7 w-7 p-0 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            </Button>
            {selectedCount > 0 && (
              <Button
                size="sm"
                variant="ghost"
                disabled={bulkDeleting}
                className="h-7 px-2.5 text-red-500 hover:bg-red-500/10 hover:text-red-600"
                onClick={async () => {
                  if (!confirm(t('accounts.deleteSelectedConfirm', { count: selectedCount }))) return
                  setBulkDeleting(true)
                  try {
                    await Promise.allSettled(
                      [...selectedIds].map(id => apiFetch(`/accounts/${id}`, { method: 'DELETE' }))
                    )
                    setSelectedIds(new Set())
                    load()
                  } finally {
                    setBulkDeleting(false)
                  }
                }}
              >
                <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                {bulkDeleting ? t('common.deleting') : t('common.delete')}
              </Button>
            )}
          </div>
        </div>
        {showMoreFilters && (
          <div className="border-t border-[var(--border)] bg-[var(--bg-card)] px-5 py-4">
            <div className="account-filter-grid">
              <label><span>验证邮箱</span><select value={filters.mailbox_bound} onChange={e => updateFilter('mailbox_bound', e.target.value)} className="filter-control"><option value="">全部</option><option value="bound">已绑定</option><option value="unbound">未绑定</option></select></label>
              <label><span>邮箱服务商</span><select value={filters.mailbox_provider} onChange={e => updateFilter('mailbox_provider', e.target.value)} className="filter-control"><option value="">全部服务商</option>{(filterStats.mailbox_providers || []).map((value: string) => <option key={value} value={value}>{value}</option>)}</select></label>
              <label><span>邮箱一致性</span><select value={filters.mailbox_email_match} onChange={e => updateFilter('mailbox_email_match', e.target.value)} className="filter-control"><option value="">全部</option><option value="same">验证邮箱一致</option><option value="different">验证邮箱不同</option></select></label>
              <label><span>手机状态</span><select value={filters.phone_state} onChange={e => updateFilter('phone_state', e.target.value)} className="filter-control"><option value="">全部</option><option value="bound">已绑定</option><option value="unbound">未绑定</option><option value="unchecked">未检测</option></select></label>
              <label><span>检测状态</span><select value={filters.checked_state} onChange={e => updateFilter('checked_state', e.target.value)} className="filter-control"><option value="">全部</option><option value="checked">已检测</option><option value="unchecked">未检测</option></select></label>
              <label><span>MFA</span><select value={filters.mfa_state} onChange={e => updateFilter('mfa_state', e.target.value)} className="filter-control"><option value="">全部</option><option value="enabled">已启用</option><option value="disabled">未启用</option><option value="unchecked">未检测</option></select></label>
              <label><span>Codex 授权</span><select value={filters.codex_auth_state} onChange={e => updateFilter('codex_auth_state', e.target.value)} className="filter-control"><option value="">全部</option><option value="authorized">已授权</option><option value="unauthorized">未授权</option></select></label>
              <label><span>推送状态</span><select value={filters.push_status} onChange={e => updateFilter('push_status', e.target.value)} className="filter-control"><option value="">全部状态</option><option value="not_pushed">未推送</option><option value="success">推送成功</option><option value="failed">推送失败</option><option value="pending">推送中</option></select></label>
              <label><span>推送目标</span><select value={filters.push_target} onChange={e => updateFilter('push_target', e.target.value)} className="filter-control"><option value="">全部目标</option>{pushTargetOptions.map(item => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label>
              <label><span>推送时间起</span><input type="datetime-local" value={filters.pushed_from} onChange={e => updateFilter('pushed_from', e.target.value)} className="filter-control" /></label>
              <label><span>推送时间止</span><input type="datetime-local" value={filters.pushed_to} onChange={e => updateFilter('pushed_to', e.target.value)} className="filter-control" /></label>
              <label><span>Codex 刷新起</span><input type="datetime-local" value={filters.codex_refreshed_from} onChange={e => updateFilter('codex_refreshed_from', e.target.value)} className="filter-control" /></label>
              <label><span>Codex 刷新止</span><input type="datetime-local" value={filters.codex_refreshed_to} onChange={e => updateFilter('codex_refreshed_to', e.target.value)} className="filter-control" /></label>
              <label><span>来源</span><select value={filters.source} onChange={e => updateFilter('source', e.target.value)} className="filter-control"><option value="">全部来源</option><option value="protocol">协议注册</option><option value="browser">浏览器注册</option><option value="import">导入</option></select></label>
              <label><span>导入方式</span><select value={filters.import_method} onChange={e => updateFilter('import_method', e.target.value)} className="filter-control"><option value="">全部方式</option><option value="text">文本导入</option><option value="csv">CSV 导入</option></select></label>
              <label><span>Region</span><select value={filters.region} onChange={e => updateFilter('region', e.target.value)} className="filter-control"><option value="">全部地区</option>{(filterStats.regions || []).map((value: string) => <option key={value} value={value}>{value}</option>)}</select></label>
              <label><span>时间字段</span><select value={filters.time_field} onChange={e => updateFilter('time_field', e.target.value)} className="filter-control"><option value="">注册时间</option><option value="created_at">注册时间</option><option value="updated_at">更新时间</option><option value="checked_at">最近检测</option><option value="expires_at">到期时间</option></select></label>
              <label><span>开始时间</span><input type="datetime-local" value={filters.time_from} onChange={e => updateFilter('time_from', e.target.value)} className="filter-control" /></label>
              <label><span>结束时间</span><input type="datetime-local" value={filters.time_to} onChange={e => updateFilter('time_to', e.target.value)} className="filter-control" /></label>
            </div>
            <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs text-[var(--text-muted)]">筛选条件已同步到 URL，刷新后自动恢复。</span>
              <div className="flex items-center gap-2">
                <Button size="sm" variant="outline" onClick={resetFilters} className="h-8"><RotateCcw className="mr-1.5 h-3.5 w-3.5" />清空筛选</Button>
                <Button size="sm" onClick={saveCurrentFilters} className="h-8"><Save className="mr-1.5 h-3.5 w-3.5" />保存方案</Button>
              </div>
            </div>
          </div>
        )}
      </Card>

      <Card className="min-h-0 flex-1 overflow-hidden p-0 border border-[var(--border)] shadow-sm">
        <div className="flex h-full min-h-0 flex-col">
          <div className="glass-table-wrap min-h-0 flex-1 overflow-auto">
        <table className="table-fixed w-full min-w-[1280px] text-sm">
          <colgroup>
            <col className="w-10" />
            <col className="w-[240px]" />
            <col className="w-[110px]" />
            <col className="w-[220px]" />
            <col className="w-[120px]" />
            <col className="w-[150px]" />
            <col className="w-[64px]" />
            <col className="w-[128px]" />
            <col className="w-[218px]" />
          </colgroup>
          <thead className="sticky top-0 z-10  bg-[var(--bg-pane)]/80">
            <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wider font-medium text-[var(--text-muted)]">
              <th className="w-10 px-3 py-2 text-left">
                <input
                  type="checkbox"
                  checked={allSelectedOnPage}
                  onChange={togglePage}
                  className="checkbox-accent rounded-[3px] border-[var(--border)] focus:ring-[var(--text-primary)] focus:ring-offset-0 bg-transparent text-[var(--text-primary)]"
                />
              </th>
              <th className="px-3 py-2 text-left">{t('common.email')}</th>
              <th className="px-3 py-2 text-left">{t('common.password')}</th>
              <th className="px-3 py-2 text-left">{t('common.status')}</th>
              <th className="px-3 py-2 text-left">Codex</th>
              <th className="px-3 py-2 text-left">推送状态</th>
              <th className="px-3 py-2 text-left">{t('accounts.link')}</th>
              <th className="px-3 py-2 text-left">{t('accounts.registeredAt')}</th>
              <th className="px-3 py-2 text-right">{t('common.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {accounts.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-24 text-center">
                  <div className="flex flex-col items-center justify-center space-y-3">
                    <div className="flex h-12 w-12 items-center justify-center rounded-full bg-[var(--bg-pane)] border border-[var(--border)] shadow-sm">
                      <svg className="h-6 w-6 text-[var(--text-muted)]" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" /></svg>
                    </div>
                    <h3 className="text-sm font-medium text-[var(--text-primary)]">{t('accounts.emptyTitle')}</h3>
                    <p className="text-xs text-[var(--text-muted)] max-w-sm">{t('accounts.emptyDesc')}</p>
                  </div>
                </td>
              </tr>
            )}
            {accounts.map(acc => (
              (() => {
                const accountEmail = getAccountEmail(acc)
                const primaryMetrics = getPrimaryMetrics(acc)
                const phoneBinding = getPhoneBindingState(acc)
                const accountPlan = getPlanName(acc)
                const planBadge = accountPlan && accountPlan !== 'unknown' ? accountPlan : ''
                const codexStatus = getCodexStatus(acc)
                const pushDelivery = getLatestPushDelivery(acc)
                const pushDisplayTime = pushDelivery?.status === 'success'
                  ? pushDelivery?.pushed_at || pushDelivery?.last_attempt_at
                  : pushDelivery?.last_attempt_at
                const registeredDate = acc.created_at ? formatDateTime(acc.created_at, language, {
                  month: '2-digit', day: '2-digit',
                }) : '-'
                const registeredTime = acc.created_at ? formatDateTime(acc.created_at, language, {
                  hour: '2-digit', minute: '2-digit', hour12: false,
                }) : ''
                const registeredAt = registeredTime ? `${registeredDate} ${registeredTime}` : registeredDate
                return (
              <tr key={acc.id} className="group border-b border-[var(--border)]/30 hover:bg-[var(--text-primary)]/[0.02] transition-colors cursor-pointer"
                  onClick={() => setDetail(acc)}>
                <td className="px-3 py-2.5" onClick={e => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={selectedIds.has(acc.id)}
                    onChange={() => toggleOne(acc.id)}
                    className="checkbox-accent rounded-[3px] border-[var(--border)] focus:ring-[var(--text-primary)] focus:ring-offset-0 bg-transparent text-[var(--text-primary)] transition-all opacity-40 group-hover:opacity-100 data-[state=checked]:opacity-100"
                  />
                </td>
                <td className="overflow-hidden px-3 py-2.5 font-mono text-sm text-[var(--text-primary)] align-top">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate tracking-tight" title={accountEmail}>{accountEmail}</span>
                    <button onClick={e => { e.stopPropagation(); copy(emailApiLine(accountEmail)) }} title="复制 Email+邮件API" className="text-[var(--text-muted)] hover:text-[var(--text-primary)] opacity-0 group-hover:opacity-100 transition-opacity"><Copy className="h-3 w-3" /></button>
                  </div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1 font-sans">
                    {planBadge && (
                      <span className="rounded border border-[var(--border)] bg-[var(--bg-pane)]/50 px-1.5 py-0.5 text-[10px] font-medium capitalize text-[var(--text-secondary)]">
                        {planBadge}
                      </span>
                    )}
                    <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${
                      phoneBinding.state === 'bound'
                        ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-500'
                        : 'border-[var(--border)] bg-[var(--bg-pane)]/50 text-[var(--text-muted)]'
                    }`}>
                      {phoneBinding.label}
                    </span>
                  </div>
                </td>
                <td className="overflow-hidden px-3 py-2.5 font-mono text-[13px] text-[var(--text-muted)] align-top">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate blur-[3px] transition-all cursor-default hover:blur-none select-none hover:select-auto hover:text-[var(--text-primary)]" title={acc.password}>{acc.password}</span>
                    <button onClick={e => { e.stopPropagation(); copy(acc.password) }} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] opacity-0 group-hover:opacity-100 transition-opacity"><Copy className="h-3 w-3" /></button>
                  </div>
                </td>
                <td className="overflow-hidden px-3 py-2.5 align-top">
                  <div className="flex w-full min-w-0 flex-col items-start gap-1.5 overflow-hidden">
                    {(() => {
                      const status = getDisplayStatus(acc);
                      const variant = String(STATUS_VARIANT[status] || 'secondary');
                      const styles = (({
                        success: "bg-emerald-500/10 text-emerald-500 ring-emerald-500/20",
                        warning: "bg-amber-500/10 text-amber-500 ring-amber-500/20",
                        danger: "bg-red-500/10 text-red-500 ring-red-500/20",
                        secondary: "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]",
                        default: "bg-blue-500/10 text-blue-500 ring-blue-500/20"
                      } as Record<string, string>)[variant]) || "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]";
                      
                      return (
                        <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${styles}`}>
                          <span className={`mr-1 h-1 w-1 rounded-full ${variant === 'success' ? 'bg-emerald-500 shadow-[0_0_4px_rgba(16,185,129,0.6)]' : variant === 'warning' ? 'bg-amber-500 shadow-[0_0_4px_rgba(245,158,11,0.6)]' : variant === 'danger' ? 'bg-red-500 shadow-[0_0_4px_rgba(239,68,68,0.6)]' : variant === 'default' ? 'bg-blue-500' : 'bg-gray-400'}`}></span>
                          {translateAccountStatus(status, language)}
                        </span>
                      );
                    })()}
                    {primaryMetrics.length > 0 && (
                      <div className="flex w-full min-w-0 max-w-full flex-col gap-1 overflow-hidden">
                        {primaryMetrics.slice(0, 2).map((metric: any) => (
                          <div key={metric.key || metric.label} className="flex min-w-0 items-center gap-1.5 overflow-hidden">
                            <span className="h-1 w-1 rounded-full bg-[var(--text-muted)] opacity-50"></span>
                            <span className="min-w-0 truncate text-xs tracking-tight text-[var(--text-muted)]">
                              <span className="font-medium text-[var(--text-secondary)] mr-0.5">{metric.label}:</span>
                              {metric.value}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </td>
                <td className="overflow-hidden px-3 py-2.5 align-top">
                  {codexStatus.authorized ? (
                    <div className="min-w-0 space-y-1">
                      <span className="inline-flex items-center rounded-full bg-violet-500/10 px-2 py-0.5 text-xs font-medium text-violet-500 ring-1 ring-inset ring-violet-500/20">
                        <span className="mr-1 h-1 w-1 rounded-full bg-violet-500 shadow-[0_0_4px_rgba(139,92,246,0.6)]"></span>
                        已授权
                      </span>
                      <div className="truncate text-xs text-[var(--text-muted)]" title={codexStatus.email || codexStatus.accountId || codexStatus.authPath || ''}>
                        {codexStatus.planType || 'codex'}{codexStatus.expiresAt ? ` · ${formatOptionalDateTime(codexStatus.expiresAt, language)}` : ''}
                      </div>
                    </div>
                  ) : (
                    <span className="inline-flex items-center rounded-full bg-[var(--text-primary)]/5 px-2 py-0.5 text-xs font-medium text-[var(--text-muted)] ring-1 ring-inset ring-[var(--border)]">
                      未授权
                    </span>
                  )}
                </td>
                <td className="overflow-hidden px-3 py-2.5 align-top">
                  {pushDelivery ? (
                    <div className="min-w-0 space-y-1" title={pushDelivery.last_error || pushDelivery.target_label || pushDelivery.target_key}>
                      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
                        pushDelivery.status === 'success'
                          ? 'bg-emerald-500/10 text-emerald-500 ring-emerald-500/20'
                          : pushDelivery.status === 'failed'
                            ? 'bg-red-500/10 text-red-500 ring-red-500/20'
                            : 'bg-amber-500/10 text-amber-500 ring-amber-500/20'
                      }`}>
                        {pushDelivery.status === 'success' ? '已推送' : pushDelivery.status === 'failed' ? '推送失败' : '推送中'}
                      </span>
                      <div className="truncate text-[11px] text-[var(--text-muted)]">
                        {pushDelivery.target_label || pushDelivery.target_key}
                        {pushDisplayTime ? ` · ${formatOptionalDateTime(pushDisplayTime, language)}` : ''}
                      </div>
                    </div>
                  ) : (
                    <span className="text-xs text-[var(--text-muted)]/70">未推送</span>
                  )}
                </td>
                <td className="overflow-hidden px-3 py-2.5 align-top">
                  {getCashierUrl(acc) ? (
                    <div className="flex items-center gap-1.5 whitespace-nowrap opacity-70 group-hover:opacity-100 transition-opacity">
                      <button onClick={e => { e.stopPropagation(); copy(getCashierUrl(acc)) }} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors p-0.5 rounded hover:bg-[var(--bg-pane)]" title="复制链接"><Copy className="h-3 w-3" /></button>
                      <a href={getCashierUrl(acc)} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors p-0.5 rounded hover:bg-[var(--bg-pane)]" title="打开收银台"><ExternalLink className="h-3 w-3" /></a>
                    </div>
                  ) : <span className="text-[var(--text-muted)]/50 text-xs">-</span>}
                </td>
                <td className="whitespace-nowrap px-3 py-2.5 align-top font-mono text-xs text-[var(--text-muted)]" title={registeredAt}>
                  {registeredAt}
                </td>
                <td className="px-3 py-2.5 align-top" onClick={e => e.stopPropagation()}>
                  <div className="flex items-center justify-end">
                    <ActionMenu
                      acc={acc}
                      onDetail={() => setDetail(acc)}
                      onDelete={() => load()}
                      onResult={(title, payload) => setActionResult({ title, payload })}
                      onChanged={() => load()}
                      canPush={pushTargets.length > 0}
                      onPush={() => pushAccounts([acc.id], '')}
                    />
                  </div>
                </td>
              </tr>
                )
              })()
            ))}
          </tbody>
        </table>
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] px-4 py-2.5 text-xs text-[var(--text-muted)]">
            <span>
              {t('accounts.range', { start: rangeStart, end: rangeEnd, total })}
            </span>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 px-2.5"
                disabled={loading || page <= 1}
                onClick={() => setPage(current => Math.max(1, current - 1))}
              >
                {t('taskHistory.prevPage')}
              </Button>
              <span className="min-w-20 text-center text-[var(--text-secondary)]">
                {t('accounts.page', { current: page, total: totalPages })}
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 px-2.5"
                disabled={loading || page >= totalPages}
                onClick={() => setPage(current => Math.min(totalPages, current + 1))}
              >
                {t('taskHistory.nextPage')}
              </Button>
            </div>
          </div>
        </div>
      </Card>
    </div>
  )
}
