import { useState, useEffect } from 'react';
import {
  Settings,
  Save,
  CheckCircle,
  AlertTriangle,
  ShieldAlert,
  Users,
  KeyRound,
} from 'lucide-react';
import {
  bootstrapAgentXHeadlessSession,
  getAgentSocialAccounts,
  getConfig,
  putAgentSocialAccounts,
  putConfig,
} from '@/lib/api';
import { applySetupConfig, parseSetupConfig, type SetupConfigDraft } from '@/lib/setupConfig';
import type { AgentSocialAccount, AgentXIntegrationStatus } from '@/types/api';

function normalizeAccounts(accounts: AgentSocialAccount[]): AgentSocialAccount[] {
  return accounts.map((account) => ({
    agent_name: account.agent_name,
    twitter: {
      username: account.twitter?.username ?? '',
      password: account.twitter?.password ?? '',
      email: account.twitter?.email ?? '',
    },
  }));
}

export default function Config() {
  const [config, setConfig] = useState('');
  const [accounts, setAccounts] = useState<AgentSocialAccount[]>([]);
  const [xStatuses, setXStatuses] = useState<Record<string, AgentXIntegrationStatus>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingAccounts, setSavingAccounts] = useState(false);
  const [bootstrappingAgent, setBootstrappingAgent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [setupDraft, setSetupDraft] = useState<SetupConfigDraft | null>(null);

  useEffect(() => {
    Promise.all([getConfig(), getAgentSocialAccounts()])
      .then(([configData, accountData]) => {
        const configText = typeof configData === 'string' ? configData : JSON.stringify(configData, null, 2);
        setConfig(configText);
        setSetupDraft(parseSetupConfig(configText));
        setAccounts(normalizeAccounts(accountData.accounts));
        setXStatuses(
          Object.fromEntries(accountData.x_status.map((status) => [status.agent_name, status])),
        );
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const handleSaveConfig = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      await putConfig(config);
      setSuccess('Configuration saved successfully.');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to save configuration');
    } finally {
      setSaving(false);
    }
  };

  const handleSaveSetup = async () => {
    if (!setupDraft) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const nextConfig = applySetupConfig(config, setupDraft);
      await putConfig(nextConfig);
      setConfig(nextConfig);
      setSetupDraft(parseSetupConfig(nextConfig));
      setSuccess('Runtime and channel setup saved successfully.');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to save setup');
    } finally {
      setSaving(false);
    }
  };

  const handleSaveAccounts = async () => {
    setSavingAccounts(true);
    setError(null);
    setSuccess(null);
    try {
      const saved = await putAgentSocialAccounts(accounts);
      setAccounts(normalizeAccounts(saved.accounts));
      setXStatuses(
        Object.fromEntries(saved.x_status.map((status) => [status.agent_name, status])),
      );
      setSuccess('Agent social accounts saved successfully.');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to save agent social accounts');
    } finally {
      setSavingAccounts(false);
    }
  };

  const handleBootstrapHeadless = async (
    agentName: string,
    mode: 'headless' | 'interactive' | 'import_chrome' = 'headless',
  ) => {
    setBootstrappingAgent(agentName);
    setError(null);
    setSuccess(null);
    try {
      const result = await bootstrapAgentXHeadlessSession(agentName, mode);
      const refreshed = await getAgentSocialAccounts();
      setXStatuses(
        Object.fromEntries(refreshed.x_status.map((status) => [status.agent_name, status])),
      );
      if (result.status === 'ok') {
        setSuccess(
          mode === 'import_chrome'
            ? `Imported Chrome X session for ${agentName}.`
            : mode === 'interactive'
              ? `Interactive X bootstrap started/completed for ${agentName}.`
              : `Headless X session bootstrapped for ${agentName}.`,
        );
      } else {
        setError(result.error ?? result.message ?? `Failed to bootstrap ${agentName}.`);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to bootstrap X headless session');
    } finally {
      setBootstrappingAgent(null);
    }
  };

  const updateTwitterField = (
    agentName: string,
    field: 'username' | 'password' | 'email',
    value: string,
  ) => {
    setAccounts((current) =>
      current.map((account) =>
        account.agent_name === agentName
          ? {
              ...account,
              twitter: {
                username: account.twitter?.username ?? '',
                password: account.twitter?.password ?? '',
                email: account.twitter?.email ?? '',
                [field]: value,
              },
            }
          : account,
      ),
    );
  };

  useEffect(() => {
    if (!success) return;
    const timer = setTimeout(() => setSuccess(null), 4000);
    return () => clearTimeout(timer);
  }, [success]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-blue-500 border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Settings className="h-5 w-5 text-blue-400" />
          <h2 className="text-base font-semibold text-white">Configuration</h2>
        </div>
        <button
          onClick={handleSaveConfig}
          disabled={saving}
          className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors disabled:opacity-50"
        >
          <Save className="h-4 w-4" />
          {saving ? 'Saving...' : 'Save TOML'}
        </button>
      </div>

      <div className="flex items-start gap-3 bg-yellow-900/20 border border-yellow-700/40 rounded-lg p-4">
        <ShieldAlert className="h-5 w-5 text-yellow-400 flex-shrink-0 mt-0.5" />
        <div>
          <p className="text-sm text-yellow-300 font-medium">
            Sensitive fields are masked
          </p>
          <p className="text-sm text-yellow-400/70 mt-0.5">
            Passwords and emails are hidden when stored. To change a masked field,
            replace the masked value with the new value and save.
          </p>
        </div>
      </div>

      {success && (
        <div className="flex items-center gap-2 bg-green-900/30 border border-green-700 rounded-lg p-3">
          <CheckCircle className="h-4 w-4 text-green-400 flex-shrink-0" />
          <span className="text-sm text-green-300">{success}</span>
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 bg-red-900/30 border border-red-700 rounded-lg p-3">
          <AlertTriangle className="h-4 w-4 text-red-400 flex-shrink-0" />
          <span className="text-sm text-red-300">{error}</span>
        </div>
      )}

      {setupDraft ? (
        <div className="grid gap-4 xl:grid-cols-2">
          <div className="bg-gray-900 rounded-xl border border-gray-800 p-5 space-y-4">
            <div>
              <h3 className="text-sm font-semibold text-white">Model Access</h3>
              <p className="text-sm text-gray-400 mt-1">
                Choose API-key setup or the official Codex session without editing raw TOML.
              </p>
            </div>

            <div className="grid gap-2">
              {[
                ['existing', 'Use current setup'],
                ['deepseek', 'DeepSeek API key'],
                ['codex', 'Codex session'],
              ].map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() =>
                    setSetupDraft((prev) =>
                      prev ? { ...prev, accessMode: value as SetupConfigDraft['accessMode'] } : prev,
                    )
                  }
                  className={`rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                    setupDraft.accessMode === value
                      ? 'border-blue-500 bg-blue-600/15 text-white'
                      : 'border-gray-700 bg-gray-950 text-gray-300 hover:bg-gray-800'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <label className="space-y-1">
                <span className="text-xs uppercase tracking-wide text-gray-500">Provider</span>
                <input
                  value={setupDraft.provider}
                  onChange={(e) =>
                    setSetupDraft((prev) => (prev ? { ...prev, provider: e.target.value } : prev))
                  }
                  className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs uppercase tracking-wide text-gray-500">Model</span>
                <input
                  value={setupDraft.model}
                  onChange={(e) =>
                    setSetupDraft((prev) => (prev ? { ...prev, model: e.target.value } : prev))
                  }
                  className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
                />
              </label>
            </div>

            {setupDraft.accessMode === 'deepseek' ? (
              <label className="space-y-1">
                <span className="text-xs uppercase tracking-wide text-gray-500">API key</span>
                <input
                  value={setupDraft.apiKey}
                  onChange={(e) =>
                    setSetupDraft((prev) => (prev ? { ...prev, apiKey: e.target.value } : prev))
                  }
                  placeholder={setupDraft.hasExistingApiKey ? 'Already configured' : 'sk-...'}
                  className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
                />
              </label>
            ) : null}
          </div>

          <div className="bg-gray-900 rounded-xl border border-gray-800 p-5 space-y-4">
            <div>
              <h3 className="text-sm font-semibold text-white">Channels</h3>
              <p className="text-sm text-gray-400 mt-1">
                Telegram should be configurable here, not only through raw config.
              </p>
            </div>

            <label className="flex items-center gap-2 text-sm text-white">
              <input
                type="checkbox"
                checked={setupDraft.telegramEnabled}
                onChange={(e) =>
                  setSetupDraft((prev) =>
                    prev ? { ...prev, telegramEnabled: e.target.checked } : prev,
                  )
                }
                className="rounded"
              />
              Enable Telegram
            </label>

            {setupDraft.telegramEnabled ? (
              <div className="grid gap-4">
                <label className="space-y-1">
                  <span className="text-xs uppercase tracking-wide text-gray-500">Bot token</span>
                  <input
                    value={setupDraft.telegramBotToken}
                    onChange={(e) =>
                      setSetupDraft((prev) =>
                        prev ? { ...prev, telegramBotToken: e.target.value } : prev,
                      )
                    }
                    placeholder={setupDraft.hasExistingTelegramToken ? 'Already configured' : '123456:ABC...'}
                    className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
                  />
                </label>

                <label className="space-y-1">
                  <span className="text-xs uppercase tracking-wide text-gray-500">Allowed users</span>
                  <input
                    value={setupDraft.telegramAllowedUsers}
                    onChange={(e) =>
                      setSetupDraft((prev) =>
                        prev ? { ...prev, telegramAllowedUsers: e.target.value } : prev,
                      )
                    }
                    placeholder="1703898290, 123456789"
                    className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
                  />
                </label>
              </div>
            ) : null}

            <button
              onClick={handleSaveSetup}
              disabled={saving}
              className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors disabled:opacity-50"
            >
              <Save className="h-4 w-4" />
              {saving ? 'Saving...' : 'Save Runtime Setup'}
            </button>
          </div>
        </div>
      ) : null}

      <div className="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800 bg-gray-800/50">
          <div className="flex items-center gap-2">
            <Users className="h-4 w-4 text-blue-300" />
            <span className="text-sm font-semibold text-white">Agent Accounts</span>
          </div>
          <button
            onClick={handleSaveAccounts}
            disabled={savingAccounts}
            className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
          >
            <Save className="h-4 w-4" />
            {savingAccounts ? 'Saving...' : 'Save Accounts'}
          </button>
        </div>

        <div className="p-4 space-y-4">
          <p className="text-sm text-gray-400">
            Per-agent social credentials. Today this is wired for X/Twitter login
            data used by the new headless social stack.
          </p>

          {accounts.map((account) => (
            <div key={account.agent_name} className="rounded-lg border border-gray-800 bg-gray-950/70 p-4 space-y-4">
              <div className="flex items-center gap-2">
                <KeyRound className="h-4 w-4 text-cyan-300" />
                <div>
                  <p className="text-sm font-semibold text-white">
                    {account.agent_name === 'primary' ? 'Primary Agent' : account.agent_name}
                  </p>
                  <p className="text-xs text-gray-500">X / Twitter credentials</p>
                </div>
              </div>

              {(() => {
                const status = xStatuses[account.agent_name];
                if (!status) {
                  return null;
                }

                return (
                <div className="rounded-lg border border-gray-800 bg-gray-900/70 p-3 space-y-2 text-xs text-gray-300">
                  <div className="flex flex-wrap gap-3">
                    <span>Direct: <span className="font-semibold text-white">{status.twitter_x.status}</span></span>
                    <span>Headless: <span className="font-semibold text-white">{status.browser_headless.status}</span></span>
                    <span>Headless Auth: <span className="font-semibold text-white">{status.browser_headless.authenticated ? 'yes' : 'no'}</span></span>
                    <span>Extension: <span className="font-semibold text-white">{status.browser_ext.status}</span></span>
                  </div>
                  {status.twitter_x.detail && (
                    <p className="text-gray-400">Direct detail: {status.twitter_x.detail}</p>
                  )}
                  {status.browser_headless.detail && (
                    <p className="text-gray-400">Headless detail: {status.browser_headless.detail}</p>
                  )}
                  {status.browser_headless.url && (
                    <p className="text-gray-400">Headless URL: {status.browser_headless.url}</p>
                  )}
                  {status.browser_headless.required_user_action && (
                    <p className="text-amber-300">Setup needed: {status.browser_headless.required_user_action}</p>
                  )}
                  <p className="text-gray-500">
                    Capabilities: headless(post/comment/article), extension(post/comment/article)
                  </p>
                </div>
                );
              })()}

              <div className="grid gap-4 md:grid-cols-3">
                <label className="space-y-1">
                  <span className="text-xs uppercase tracking-wide text-gray-500">
                    Username
                  </span>
                  <input
                    value={account.twitter?.username ?? ''}
                    onChange={(e) =>
                      updateTwitterField(account.agent_name, 'username', e.target.value)
                    }
                    placeholder="@handle or username"
                    className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
                  />
                </label>

                <label className="space-y-1">
                  <span className="text-xs uppercase tracking-wide text-gray-500">
                    Email
                  </span>
                  <input
                    value={account.twitter?.email ?? ''}
                    onChange={(e) =>
                      updateTwitterField(account.agent_name, 'email', e.target.value)
                    }
                    placeholder="login email"
                    className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
                  />
                </label>

                <label className="space-y-1">
                  <span className="text-xs uppercase tracking-wide text-gray-500">
                    Password
                  </span>
                  <input
                    type="password"
                    value={account.twitter?.password ?? ''}
                    onChange={(e) =>
                      updateTwitterField(account.agent_name, 'password', e.target.value)
                    }
                    placeholder="password"
                    className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
                  />
                </label>
              </div>

              <div className="flex items-center justify-between gap-3">
                <p className="text-xs text-gray-500">
                  One-time X setup for the dedicated headless profile. Import from Chrome first, then use interactive bootstrap only if import fails.
                </p>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => handleBootstrapHeadless(account.agent_name, 'import_chrome')}
                    disabled={bootstrappingAgent === account.agent_name}
                    className="flex items-center gap-2 bg-cyan-700 hover:bg-cyan-600 text-white text-sm font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
                  >
                    {bootstrappingAgent === account.agent_name ? 'Working...' : 'Import Chrome X Session'}
                  </button>
                  <button
                    onClick={() => handleBootstrapHeadless(account.agent_name, 'interactive')}
                    disabled={bootstrappingAgent === account.agent_name}
                    className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 text-white text-sm font-medium px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
                  >
                    {bootstrappingAgent === account.agent_name ? 'Working...' : 'Interactive X Bootstrap'}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
        <div className="flex items-center justify-between px-4 py-2 border-b border-gray-800 bg-gray-800/50">
          <span className="text-xs text-gray-400 font-medium uppercase tracking-wider">
            TOML Configuration
          </span>
          <span className="text-xs text-gray-500">
            {config.split('\n').length} lines
          </span>
        </div>
        <textarea
          value={config}
          onChange={(e) => setConfig(e.target.value)}
          spellCheck={false}
          className="w-full min-h-[500px] bg-gray-950 text-gray-200 font-mono text-sm p-4 resize-y focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-inset"
          style={{ tabSize: 4 }}
        />
      </div>
    </div>
  );
}
