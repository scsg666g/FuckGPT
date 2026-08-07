import { useState, useEffect, useRef } from 'react'
import { apiFetch } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import type { TranslationKey } from '@/lib/i18n'
import type { ProviderOption, ProviderSetting } from '@/lib/config-options'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Save, Eye, EyeOff, X, Pencil, Plus, Trash2, FlaskConical, Search, RefreshCw } from 'lucide-react'
import { invalidateConfigOptionsCache } from '@/lib/app-data'

const CATEGORY_GROUPS = [
  { key: 'free', labelKey: 'providers.category.free', descKey: 'providers.category.freeDesc' },
  { key: 'selfhost', labelKey: 'providers.category.selfhost', descKey: 'providers.category.selfhostDesc' },
  { key: 'thirdparty', labelKey: 'providers.category.thirdparty', descKey: 'providers.category.thirdpartyDesc' },
  { key: 'custom', labelKey: 'providers.category.custom', descKey: 'providers.category.customDesc' },
] satisfies Array<{ key: string; labelKey: TranslationKey; descKey: TranslationKey }>

/* ------------------------------------------------------------------ */
/*  Toggle                                                             */
/* ------------------------------------------------------------------ */
function Toggle({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
        disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'
      } ${checked ? 'bg-[var(--accent)]' : 'bg-[var(--chip-bg)] border border-[var(--border)]'}`}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
    </button>
  )
}

/* ------------------------------------------------------------------ */
/*  Searchable Select                                                  */
/* ------------------------------------------------------------------ */
type SearchableOption = { value: string; label: string; searchText?: string }

function localizeCountryOption(item: any, fallbackLabel: string, language: string): SearchableOption {
  const value = String(item?.value ?? item?.id ?? item?.country ?? '')
  const regionCode = String(item?.region_code ?? item?.iso ?? '').trim().toUpperCase()
  const dialCode = String(item?.dial_code ?? '').trim()
  const englishName = String(item?.english_name ?? item?.name ?? '').trim()
  const providerLocalizedName = String(item?.localized_name ?? '').trim()
  let regionName = ''
  if (/^[A-Z]{2}$/.test(regionCode)) {
    try {
      regionName = new Intl.DisplayNames([language], { type: 'region' }).of(regionCode) || ''
    } catch {
      regionName = ''
    }
  }
  const isChinese = language === 'zh-CN'
  const name = isChinese
    ? providerLocalizedName || regionName || englishName || fallbackLabel || value
    : englishName || regionName || fallbackLabel || value
  const details = [regionCode, dialCode].filter(Boolean).join(' · ') || value
  return {
    value,
    label: `${name}${isChinese ? `（${details}）` : ` (${details})`}`,
    searchText: [fallbackLabel, englishName, providerLocalizedName, regionCode, dialCode, value]
      .filter(Boolean)
      .join(' '),
  }
}

function SearchableSelect({ value, options, placeholder, onChange }: {
  value: string
  options: SearchableOption[]
  placeholder?: string
  onChange: (v: string) => void
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const filtered = search
    ? options.filter(o =>
        o.label.toLowerCase().includes(search.toLowerCase())
        || o.searchText?.toLowerCase().includes(search.toLowerCase())
        || o.value.includes(search)
      )
    : options

  const selectedLabel = options.find(o => o.value.toLowerCase() === value.toLowerCase())?.label || value

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus()
  }, [open])

  return (
    <div ref={containerRef} className="relative min-w-0 flex-1">
      <button
        type="button"
        onClick={() => { setOpen(!open); setSearch('') }}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="control-surface w-full text-left flex items-center justify-between"
      >
        <span className={selectedLabel ? 'text-[var(--text-primary)]' : 'text-[var(--text-muted)]'}>
          {selectedLabel || placeholder || t('providers.selectPlaceholder')}
        </span>
        <svg className="h-4 w-4 text-[var(--text-muted)]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={open ? 'M5 15l7-7 7 7' : 'M19 9l-7 7-7-7'} />
        </svg>
      </button>
      {open && (
        <div role="listbox" className="absolute z-50 mt-1 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-pane)] shadow-lg">
          <div className="p-2 border-b border-[var(--border)]">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-[var(--text-muted)]" />
              <input
                ref={inputRef}
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Escape') setOpen(false)
                }}
                placeholder={t('providers.searchPlaceholder')}
                className="w-full rounded-md border border-[var(--border)] bg-[var(--bg-base)] pl-8 pr-3 py-1.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
              />
            </div>
          </div>
          <div className="max-h-48 overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <div className="px-3 py-2 text-sm text-[var(--text-muted)]">{t('providers.noMatches')}</div>
            ) : filtered.map(o => (
              <button
                key={o.value}
                type="button"
                role="option"
                aria-selected={o.value.toLowerCase() === value.toLowerCase()}
                onClick={() => { onChange(o.value); setOpen(false); setSearch('') }}
                className={`w-full text-left px-3 py-1.5 text-sm hover:bg-[var(--chip-bg)] ${
                  o.value === value ? 'bg-[var(--accent)]/10 text-[var(--accent)] font-medium' : 'text-[var(--text-primary)]'
                }`}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Edit modal                                                         */
/* ------------------------------------------------------------------ */
function EditModal({
  provider, setting, providerType, onClose, onSaved,
}: {
  provider: ProviderOption; setting: ProviderSetting | null; providerType: string
  onClose: () => void; onSaved: () => void
}) {
  const { t, language } = useI18n()
  const fields = provider.fields || []
  const [form, setForm] = useState<Record<string, string>>(() => {
    const data: Record<string, string> = {}
    for (const field of fields) {
      data[field.key] = (setting?.auth?.[field.key] || '') || (setting?.config?.[field.key] || '') || field.default_value || ''
    }
    return data
  })
  const [showSecret, setShowSecret] = useState<Record<string, boolean>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message?: string; error?: string } | null>(null)
  const [asyncOptions, setAsyncOptions] = useState<Record<string, Array<{ value: string; label: string }>>>({})
  const [asyncLoading, setAsyncLoading] = useState<Record<string, boolean>>({})
  const [asyncErrors, setAsyncErrors] = useState<Record<string, string>>({})

  const splitFormValues = () => {
    const config: Record<string, string> = {}
    const auth: Record<string, string> = {}
    for (const field of fields) {
      if (field.category === 'auth') auth[field.key] = form[field.key] || ''
      else config[field.key] = form[field.key] || ''
    }
    return { config, auth }
  }

  const loadAsyncOptions = async (field: (typeof fields)[number]) => {
    if (!field.asyncUrl) return
    setAsyncLoading(prev => ({ ...prev, [field.key]: true }))
    setAsyncErrors(prev => ({ ...prev, [field.key]: '' }))
    try {
      const { config, auth } = splitFormValues()
      const data = await apiFetch(field.asyncUrl, {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType,
          provider_key: provider.value,
          field_key: field.key,
          config,
          auth,
        }),
      })
      if (data?.ok === false) throw new Error(data.error || t('providers.optionsLoadFailed'))
      const valueKey = field.asyncValueKey || 'value'
      const labelKey = field.asyncLabelKey || 'label'
      let items: any[] = []
      if (Array.isArray(data)) items = data
      else if (Array.isArray(data?.options)) items = data.options
      else if (Array.isArray(data?.countries)) items = data.countries
      else if (Array.isArray(data?.services)) items = data.services
      else if (Array.isArray(data?.data)) items = data.data

      const options = items.map((item: any) => {
        if (typeof item === 'object') {
          const value = String(item[valueKey] ?? item.id ?? item.country ?? '')
          const fallbackLabel = String(item[labelKey] ?? item.name ?? item.title ?? item.eng ?? value)
          if (item.region_code || item.localized_name || item.dial_code) {
            return localizeCountryOption({ ...item, value }, fallbackLabel, language)
          }
          return { value, label: fallbackLabel, searchText: fallbackLabel }
        }
        return { value: String(item), label: String(item) }
      }).filter(option => option.value)
      const currentValue = form[field.key] || ''
      if (currentValue && !options.some(option => option.value.toLowerCase() === currentValue.toLowerCase())) {
        options.unshift({ value: currentValue, label: `${currentValue} (${t('providers.currentValue')})` })
      }
      setAsyncOptions(prev => ({ ...prev, [field.key]: options }))
    } catch (error: any) {
      setAsyncErrors(prev => ({
        ...prev,
        [field.key]: error?.message || t('providers.optionsLoadFailed'),
      }))
    } finally {
      setAsyncLoading(prev => ({ ...prev, [field.key]: false }))
    }
  }

  // 加载 async-select 字段的选项
  useEffect(() => {
    const requiresCredential = provider.value === 'smsbower' || provider.value === 'herosms'
    const hasCredential = fields.some(field => field.category === 'auth' && Boolean(form[field.key]))
    for (const field of fields) {
      if (field.type === 'async-select' && field.asyncUrl && (!requiresCredential || hasCredential)) {
        void loadAsyncOptions(field)
      }
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const handleSave = async () => {
    const { config, auth } = splitFormValues()
    setSaving(true)
    try {
      if (setting) {
        await apiFetch('/provider-settings', {
          method: 'PUT',
          body: JSON.stringify({
            id: setting.id, provider_type: providerType, provider_key: provider.value,
            display_name: setting.display_name || provider.label,
            auth_mode: setting.auth_mode || provider.default_auth_mode || '',
            enabled: setting.enabled, is_default: setting.is_default, config, auth,
            metadata: setting.metadata || {},
          }),
        })
      } else {
        await apiFetch('/provider-settings', {
          method: 'POST',
          body: JSON.stringify({
            provider_type: providerType, provider_key: provider.value,
            display_name: provider.label, auth_mode: provider.default_auth_mode || '',
            enabled: false, is_default: false, config, auth, metadata: {},
          }),
        })
      }
      invalidateConfigOptionsCache()
      setSaved(true)
      setTimeout(() => { onSaved(); onClose() }, 500)
    } catch (e) { console.error(e) } finally { setSaving(false) }
  }

  const handleTest = async () => {
    const { config, auth } = splitFormValues()
    setTesting(true)
    setTestResult(null)
    try {
      const result = await apiFetch('/provider-settings/test', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType,
          provider_key: provider.value,
          config, auth,
        }),
      })
      setTestResult(result)
    } catch (e: any) {
      setTestResult({ ok: false, error: e.message || t('providers.testRequestFailed') })
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm flex flex-col" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--border)] px-5 py-4">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{provider.label}</h2>
            {provider.description && <p className="mt-0.5 text-xs text-[var(--text-muted)]">{provider.description}</p>}
          </div>
          <button onClick={onClose} aria-label={t('common.close')} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {fields.length === 0 ? (
            <p className="text-sm text-[var(--text-muted)]">{t('providers.noConfig')}</p>
          ) : fields.map(field => {
            const sk = `${provider.value}:${field.key}`
            return (
              <div key={field.key}>
                <label className="mb-1.5 block text-sm font-medium text-[var(--text-secondary)]">{field.label}</label>
                {field.hint && <p className="mb-1.5 text-xs text-[var(--text-muted)]">{field.hint}</p>}
                <div className="relative">
                  {field.type === 'toggle' ? (
                    <div className="flex items-center gap-3">
                      <Toggle
                        checked={['true', '1', 'yes', 'on'].includes((form[field.key] || '').toLowerCase())}
                        onChange={v => setForm(f => ({ ...f, [field.key]: v ? 'true' : 'false' }))}
                      />
                      <span className="text-sm text-[var(--text-muted)]">
                        {['true', '1', 'yes', 'on'].includes((form[field.key] || '').toLowerCase()) ? t('providers.enabledState') : t('providers.disabledState')}
                      </span>
                    </div>
                  ) : field.type === 'async-select' ? (
                    <div>
                      <div className="flex items-start gap-2">
                        <SearchableSelect
                          value={form[field.key] || ''}
                          options={asyncOptions[field.key] || []}
                          placeholder={asyncLoading[field.key] ? t('common.loading') : field.placeholder}
                          onChange={v => setForm(f => ({ ...f, [field.key]: v }))}
                        />
                        <button
                          type="button"
                          onClick={() => void loadAsyncOptions(field)}
                          disabled={asyncLoading[field.key]}
                          className="table-action-btn h-[37px] w-[37px] p-0"
                          title={t('providers.reloadOptions')}
                          aria-label={t('providers.reloadOptions')}
                        >
                          <RefreshCw className={`h-3.5 w-3.5 ${asyncLoading[field.key] ? 'animate-spin' : ''}`} />
                        </button>
                      </div>
                      {asyncErrors[field.key] && (
                        <p className="mt-1.5 text-xs text-red-500">{asyncErrors[field.key]}</p>
                      )}
                    </div>
                  ) : field.type === 'select' && field.options?.length ? (
                    <select value={form[field.key] || ''} onChange={e => setForm(f => ({ ...f, [field.key]: e.target.value }))} className="control-surface appearance-none">
                      {field.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'textarea' ? (
                    <textarea
                      value={form[field.key] || ''}
                      onChange={e => setForm(f => ({ ...f, [field.key]: e.target.value }))}
                      placeholder={field.placeholder || ''}
                      className="control-surface min-h-32 font-mono text-xs"
                      autoComplete="off"
                      data-1p-ignore
                      data-lpignore="true"
                    />
                  ) : (
                    <>
                      <input type={field.secret && !showSecret[sk] ? 'password' : 'text'} value={form[field.key] || ''}
                        onChange={e => setForm(f => ({ ...f, [field.key]: e.target.value }))}
                        placeholder={field.placeholder || ''} className="control-surface pr-9" autoComplete="new-password"
                        data-1p-ignore data-lpignore="true" />
                      {field.secret && (
                        <button onClick={() => setShowSecret(s => ({ ...s, [sk]: !s[sk] }))}
                          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                          {showSecret[sk] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                        </button>
                      )}
                    </>
                  )}
                </div>
              </div>
            )
          })}
        </div>
        {/* Test result */}
        {testResult && (
          <div className={`mx-5 rounded-lg px-3 py-2 text-xs ${
            testResult.ok
              ? 'border border-emerald-500/20 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
              : 'border border-red-500/20 bg-red-500/10 text-red-600 dark:text-red-400'
          }`}>
            {testResult.ok ? testResult.message : testResult.error}
          </div>
        )}

        {/* Footer */}
        <div className="flex gap-1.5 border-t border-[var(--border)] px-3 py-3 sm:gap-2 sm:px-5">
          <Button onClick={handleSave} disabled={saving} className="min-w-0 flex-1 whitespace-nowrap">
            <Save className="h-3.5 w-3.5 mr-1.5" />
            {saved ? `${t('common.saved')} ✓` : saving ? t('common.saving') : t('common.save')}
          </Button>
          <Button variant="outline" onClick={handleTest} disabled={testing || fields.length === 0} className="min-w-0 flex-1 whitespace-nowrap">
            <FlaskConical className="h-3.5 w-3.5 mr-1.5" />
            {testing ? t('providers.testing') : t('providers.testConnection')}
          </Button>
          <Button variant="outline" onClick={onClose} className="whitespace-nowrap">{t('common.cancel')}</Button>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Main                                                               */
/* ------------------------------------------------------------------ */
type Props = {
  providerType: string
  catalog: ProviderOption[]
  settings: ProviderSetting[]
  onReload: () => Promise<void>
  onCreateCustom?: () => void
}

export default function ProviderCards({ providerType, catalog, settings, onReload, onCreateCustom }: Props) {
  const { t } = useI18n()
  const [editTarget, setEditTarget] = useState<{ provider: ProviderOption; setting: ProviderSetting | null } | null>(null)
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message?: string; error?: string }>>({})
  const [testingKeys, setTestingKeys] = useState<Record<string, boolean>>({})

  const settingsMap: Record<string, ProviderSetting> = {}
  for (const s of settings) settingsMap[s.provider_key] = s
  const defaultKey = settings.find(s => s.enabled && s.is_default)?.provider_key || ''
  const providerTypeLabelKey = `providers.type.${providerType}` as TranslationKey

  const grouped: Record<string, ProviderOption[]> = {}
  for (const p of catalog) {
    const cat = p.category || 'custom'
    if (!grouped[cat]) grouped[cat] = []
    grouped[cat].push(p)
  }

  const withLoading = async (key: string, fn: () => Promise<void>) => {
    setLoading(p => ({ ...p, [key]: true }))
    try { await fn() } finally { setLoading(p => ({ ...p, [key]: false })) }
  }

  const handleToggle = (provider: ProviderOption, enable: boolean) => withLoading(provider.value, async () => {
    const setting = settingsMap[provider.value]
    if (!setting) {
      await apiFetch('/provider-settings', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType, provider_key: provider.value,
          display_name: provider.label, auth_mode: provider.default_auth_mode || '',
          enabled: enable, is_default: false, config: {}, auth: {}, metadata: {},
        }),
      })
    } else {
      await apiFetch('/provider-settings', {
        method: 'PUT',
        body: JSON.stringify({
          id: setting.id, provider_type: providerType, provider_key: provider.value,
          display_name: setting.display_name, auth_mode: setting.auth_mode,
          enabled: enable, is_default: setting.is_default,
          config: setting.config, auth: setting.auth, metadata: setting.metadata || {},
        }),
      })
    }
    invalidateConfigOptionsCache()
    await onReload()
  })

  const handleSetDefault = (provider: ProviderOption) => withLoading(provider.value, async () => {
    const setting = settingsMap[provider.value]
    if (!setting) return
    await apiFetch('/provider-settings', {
      method: 'PUT',
      body: JSON.stringify({
        id: setting.id, provider_type: providerType, provider_key: provider.value,
        display_name: setting.display_name, auth_mode: setting.auth_mode,
        enabled: true, is_default: true, config: setting.config, auth: setting.auth,
        metadata: setting.metadata || {},
      }),
    })
    invalidateConfigOptionsCache()
    await onReload()
  })

  const handleTestInline = async (provider: ProviderOption) => {
    const setting = settingsMap[provider.value]
    if (!setting) return
    const key = provider.value
    setTestingKeys(p => ({ ...p, [key]: true }))
    setTestResults(p => { const n = { ...p }; delete n[key]; return n })
    try {
      const result = await apiFetch('/provider-settings/test', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: providerType,
          provider_key: key,
          config: setting.config || {},
          auth: setting.auth || {},
        }),
      })
      setTestResults(p => ({ ...p, [key]: result }))
    } catch (e: any) {
      setTestResults(p => ({ ...p, [key]: { ok: false, error: e.message || t('providers.testFailed') } }))
    } finally {
      setTestingKeys(p => ({ ...p, [key]: false }))
    }
  }

  const handleDelete = (provider: ProviderOption) => withLoading(provider.value, async () => {
    const setting = settingsMap[provider.value]
    if (setting) {
      await apiFetch(`/provider-settings/${setting.id}`, { method: 'DELETE' })
    }
    // Delete the definition (only works for non-builtin)
    const def = catalog.find(p => p.value === provider.value)
    if (def && !def.is_builtin && (def as any).id) {
      try {
        await apiFetch(`/provider-definitions/${(def as any).id}`, { method: 'DELETE' })
      } catch {
        // definition delete may fail if it's builtin, ignore
      }
    }
    invalidateConfigOptionsCache()
    await onReload()
  })

  const renderCard = (provider: ProviderOption, allowDelete = false) => {
    const key = provider.value
    const setting = settingsMap[key]
    const isConfigured = !!setting
    const isEnabled = Boolean(setting?.enabled)
    const isDefault = key === defaultKey
    const hasFields = (provider.fields || []).length > 0

    return (
      <div key={key}>
        <div className="flex flex-col items-stretch gap-3 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-4 py-3 sm:flex-row sm:items-center">
          {/* Left: name + desc + badge */}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-[var(--text-primary)]">{provider.label}</span>
              {isDefault && <Badge variant="success">{t('providers.default')}</Badge>}
              {isConfigured && !isEnabled && <Badge variant="secondary">{t('providers.configured')}</Badge>}
            </div>
            {provider.description && (
              <p className="mt-0.5 text-xs text-[var(--text-muted)] line-clamp-2 sm:line-clamp-1">{provider.description}</p>
            )}
          </div>

          {/* Right: actions */}
          <div className="flex min-w-0 flex-wrap items-center justify-end gap-1.5 sm:shrink-0 sm:gap-2">
            <button
              onClick={() => hasFields ? setEditTarget({ provider, setting: setting || null }) : undefined}
              disabled={!hasFields}
              className={`table-action-btn ${!hasFields ? 'opacity-30 cursor-not-allowed' : ''}`}
            >
              <Pencil className="h-3 w-3 mr-1" /> {t('providers.edit')}
            </button>

            <button
              onClick={() => isConfigured ? handleTestInline(provider) : undefined}
              disabled={!isConfigured || testingKeys[key]}
              className={`table-action-btn ${!isConfigured ? 'opacity-30 cursor-not-allowed' : ''}`}
            >
              <FlaskConical className="h-3 w-3 mr-1" /> {testingKeys[key] ? t('providers.testing') : t('providers.test')}
            </button>

            <button
              onClick={() => isEnabled && !isDefault ? handleSetDefault(provider) : undefined}
              disabled={!isEnabled || isDefault || loading[key]}
              className={`table-action-btn ${(!isEnabled || isDefault) ? 'opacity-30 cursor-not-allowed' : ''}`}
            >
              {isDefault ? t('providers.defaultDone') : t('providers.setDefault')}
            </button>

            {allowDelete && (
              <button
                onClick={() => !isDefault ? handleDelete(provider) : undefined}
                disabled={isDefault || loading[key]}
                className={`table-action-btn table-action-btn-danger ${isDefault ? 'opacity-30 cursor-not-allowed' : ''}`}
              >
                <Trash2 className="h-3 w-3 mr-1" /> {t('common.delete')}
              </button>
            )}

            <Toggle
              checked={isEnabled}
              onChange={v => handleToggle(provider, v)}
              disabled={loading[key]}
              label={`${provider.label}: ${isEnabled ? t('providers.enabledState') : t('providers.disabledState')}`}
            />
          </div>
        </div>
        {/* Inline test result */}
        {testResults[key] && (
          <div className={`mt-1 rounded-lg px-3 py-2 text-xs ${
            testResults[key].ok
              ? 'border border-emerald-500/20 bg-emerald-500/10 text-emerald-600'
              : 'border border-red-500/20 bg-red-500/10 text-red-600'
          }`}>
            {testResults[key].ok ? testResults[key].message : testResults[key].error}
          </div>
        )}
      </div>
    )
  }

  return (
    <>
      <div className="space-y-6">
        {CATEGORY_GROUPS.map(({ key: cat, labelKey, descKey }) => {
          const providers = grouped[cat]
          if (!providers || providers.length === 0) return null

          return (
            <div key={cat}>
              <div className="mb-2">
                <h3 className="text-sm font-semibold text-[var(--text-primary)]">{t(labelKey)}</h3>
                <p className="text-xs text-[var(--text-muted)]">{t(descKey)}</p>
              </div>
              <div className="space-y-1.5">
                {providers.map(p => renderCard(p, cat === 'custom'))}
                {cat === 'custom' && onCreateCustom && (
                  <button
                    className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-[var(--border)] px-4 py-3 text-sm text-[var(--text-muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--accent)]"
                    onClick={() => onCreateCustom?.()}
                  >
                    <Plus className="h-4 w-4" />
                    {t('providers.addCustom', { type: t(providerTypeLabelKey) })}
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {editTarget && (
        <EditModal
          provider={editTarget.provider}
          setting={editTarget.setting}
          providerType={providerType}
          onClose={() => setEditTarget(null)}
          onSaved={onReload}
        />
      )}
    </>
  )
}
