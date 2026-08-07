import { useCallback, useEffect, useState } from 'react'
import ProviderCards from '@/components/settings/ProviderCards'
import { getConfigOptions } from '@/lib/app-data'
import type { ProviderOption, ProviderSetting } from '@/lib/config-options'
import { useI18n } from '@/lib/i18n-context'

type ProviderSettingsTab = 'mailbox' | 'sms' | 'proxy' | 'push'

const TAB_COPY: Record<ProviderSettingsTab, {
  catalogKey: 'mailbox_providers' | 'sms_providers' | 'proxy_providers' | 'push_providers'
  settingsKey: 'mailbox_settings' | 'sms_settings' | 'proxy_settings' | 'push_settings'
  usageKey: 'settings.provider.mailboxUsage' | 'settings.provider.smsUsage' | 'settings.provider.proxyUsage' | 'settings.provider.pushUsage'
}> = {
  mailbox: {
    catalogKey: 'mailbox_providers',
    settingsKey: 'mailbox_settings',
    usageKey: 'settings.provider.mailboxUsage',
  },
  sms: {
    catalogKey: 'sms_providers',
    settingsKey: 'sms_settings',
    usageKey: 'settings.provider.smsUsage',
  },
  proxy: {
    catalogKey: 'proxy_providers',
    settingsKey: 'proxy_settings',
    usageKey: 'settings.provider.proxyUsage',
  },
  push: {
    catalogKey: 'push_providers',
    settingsKey: 'push_settings',
    usageKey: 'settings.provider.pushUsage',
  },
}

export default function Settings({ providerType = 'mailbox' }: { providerType?: ProviderSettingsTab }) {
  const { t } = useI18n()
  const [catalog, setCatalog] = useState<ProviderOption[]>([])
  const [settings, setSettings] = useState<ProviderSetting[]>([])
  const [error, setError] = useState('')
  const tabCopy = TAB_COPY[providerType]

  const loadProviders = useCallback(async () => {
    try {
      const options = await getConfigOptions()
      setCatalog(options[tabCopy.catalogKey] || [])
      setSettings(options[tabCopy.settingsKey] || [])
      setError('')
    } catch {
      setCatalog([])
      setSettings([])
      setError(t('register.providerMetadataError'))
    }
  }, [t, tabCopy])

  useEffect(() => {
    void loadProviders()
  }, [loadProviders])

  return (
    <div className="space-y-4">
      {error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}
      <div className="rounded-lg border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-4 py-3 text-sm text-[var(--text-secondary)]">
        {t(tabCopy.usageKey)}
      </div>
      <ProviderCards
        providerType={providerType}
        catalog={catalog}
        settings={settings}
        onReload={loadProviders}
      />
    </div>
  )
}
