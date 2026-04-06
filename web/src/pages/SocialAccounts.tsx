import { useEffect, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  RefreshCw,
  Save,
  Share2,
} from 'lucide-react';
import {
  getAgentSocialAccounts,
  putAgentSocialAccounts,
  bootstrapAgentXHeadlessSession,
} from '@/lib/api';
import { useAgentContext } from '@/contexts/AgentContext';
import {
  MacBadge,
  MacEmptyState,
  MacPage,
  MacPanel,
} from '@/components/macos/MacPrimitives';
import { useShell } from '@/components/shell/ShellProvider';
import type { AgentSocialAccount, AgentXIntegrationStatus } from '@/types/api';

export default function SocialAccounts() {
  const { scopedAgent, refreshAgents } = useAgentContext();
  const { isDesktopMac } = useShell();
  const [accounts, setAccounts] = useState<AgentSocialAccount[]>([]);
  const [xStatus, setXStatus] = useState<AgentXIntegrationStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [bootstrapping, setBootstrapping] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const agentId = scopedAgent?.profile.id ?? '';

  useEffect(() => {
    getAgentSocialAccounts()
      .then((data) => {
        setAccounts(data.accounts);
        setXStatus(data.x_status);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  // Find or create the account entry for this agent
  const thisAccount = accounts.find((a) => a.agent_name === agentId) ?? {
    agent_name: agentId,
    twitter: { username: '', password: '', email: '' },
  };
  const thisXStatus = xStatus.find((s) => s.agent_name === agentId);

  const updateTwitterField = (field: 'username' | 'password' | 'email', value: string) => {
    setAccounts((prev) => {
      const existing = prev.find((a) => a.agent_name === agentId);
      if (existing) {
        return prev.map((a) =>
          a.agent_name === agentId
            ? { ...a, twitter: { ...a.twitter, [field]: value } }
            : a,
        );
      }
      return [...prev, { agent_name: agentId, twitter: { username: '', password: '', email: '', [field]: value } }];
    });
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const result = await putAgentSocialAccounts(accounts);
      setAccounts(result.accounts);
      setXStatus(result.x_status);
      await refreshAgents();
      setSuccess('Social accounts saved successfully.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save accounts');
    } finally {
      setSaving(false);
    }
  };

  const handleBootstrap = async (mode: 'headless' | 'interactive' | 'import_chrome') => {
    setBootstrapping(mode);
    setError(null);
    setSuccess(null);
    try {
      const result = await bootstrapAgentXHeadlessSession(agentId, mode);
      if (result.error) {
        setError(result.error);
      } else {
        setSuccess(result.message ?? 'X session bootstrapped successfully.');
      }
      const data = await getAgentSocialAccounts();
      setAccounts(data.accounts);
      setXStatus(data.x_status);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Bootstrap failed');
    } finally {
      setBootstrapping(null);
    }
  };

  const fieldClass = isDesktopMac
    ? 'w-full rounded-[1rem] border border-[rgba(15,23,42,0.08)] bg-white/80 px-4 py-2.5 text-sm text-slate-900 outline-none focus:border-sky-400 dark:border-white/10 dark:bg-black/20 dark:text-white'
    : 'w-full rounded-[1rem] border border-white/10 bg-[#0b1120] px-4 py-2.5 text-sm text-white outline-none focus:border-sky-400';

  if (!scopedAgent) return null;

  return (
    <MacPage
      eyebrow={scopedAgent.profile.name}
      title="Social Accounts"
      description="Manage X (Twitter) and LinkedIn connections for this agent."
    >
      {error ? (
        <div className="rounded-[1.4rem] border border-rose-300/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-200 mb-4">
          {error}
        </div>
      ) : null}
      {success ? (
        <div className="rounded-[1.4rem] border border-emerald-300/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-700 dark:text-emerald-200 mb-4">
          {success}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        {/* X (Twitter) Credentials */}
        <MacPanel title="X (Twitter)" detail="Enter credentials for headless login.">
          <div className="grid gap-4">
            <label className="block">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Username / handle</span>
              <input
                value={thisAccount.twitter?.username ?? ''}
                onChange={(e) => updateTwitterField('username', e.target.value)}
                placeholder="@handle"
                className={`mt-1 ${fieldClass}`}
              />
            </label>
            <label className="block">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Email</span>
              <input
                value={thisAccount.twitter?.email ?? ''}
                onChange={(e) => updateTwitterField('email', e.target.value)}
                placeholder="account@example.com"
                className={`mt-1 ${fieldClass}`}
              />
            </label>
            <label className="block">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Password</span>
              <input
                type="password"
                value={thisAccount.twitter?.password ?? ''}
                onChange={(e) => updateTwitterField('password', e.target.value)}
                placeholder="password"
                className={`mt-1 ${fieldClass}`}
              />
            </label>
          </div>

          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={saving}
              onClick={handleSave}
              className="inline-flex items-center gap-2 rounded-full bg-[#0a84ff] px-4 py-2 text-sm font-medium text-white transition hover:brightness-105 disabled:opacity-50"
            >
              <Save className="h-4 w-4" />
              {saving ? 'Saving...' : 'Save Credentials'}
            </button>
          </div>
        </MacPanel>

        {/* X Session Status */}
        <MacPanel title="X Session" detail="Authentication and bootstrap status.">
          {loading ? (
            <MacEmptyState
              icon={<RefreshCw className="h-7 w-7 animate-spin" />}
              title="Loading status"
              description="Checking X authentication..."
            />
          ) : thisXStatus ? (
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                {thisXStatus.browser_headless.authenticated ? (
                  <CheckCircle2 className="h-6 w-6 text-emerald-400" />
                ) : (
                  <AlertCircle className="h-6 w-6 text-amber-400" />
                )}
                <div>
                  <p className="text-sm font-semibold">
                    {thisXStatus.browser_headless.authenticated ? 'Authenticated' : 'Not authenticated'}
                  </p>
                  <p className="text-xs text-[var(--shell-muted)]">
                    {thisXStatus.browser_headless.detail || thisXStatus.twitter_x.detail || 'No details'}
                  </p>
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                {thisXStatus.supported_capabilities.post ? <MacBadge tone="accent">Post</MacBadge> : null}
                {thisXStatus.supported_capabilities.comment ? <MacBadge tone="accent">Reply</MacBadge> : null}
              </div>

              <div className="grid gap-2">
                <button
                  type="button"
                  disabled={!!bootstrapping}
                  onClick={() => handleBootstrap('headless')}
                  className="inline-flex items-center gap-2 rounded-full border border-sky-400/30 bg-sky-500/10 px-4 py-2 text-sm font-medium text-sky-400 transition hover:bg-sky-500/20 disabled:opacity-50"
                >
                  {bootstrapping === 'headless' ? <RefreshCw className="h-4 w-4 animate-spin" /> : <ExternalLink className="h-4 w-4" />}
                  Bootstrap Headless
                </button>
                <button
                  type="button"
                  disabled={!!bootstrapping}
                  onClick={() => handleBootstrap('interactive')}
                  className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-4 py-2 text-sm font-medium transition hover:bg-white/10 disabled:opacity-50"
                >
                  {bootstrapping === 'interactive' ? <RefreshCw className="h-4 w-4 animate-spin" /> : <ExternalLink className="h-4 w-4" />}
                  Bootstrap Interactive
                </button>
                <button
                  type="button"
                  disabled={!!bootstrapping}
                  onClick={() => handleBootstrap('import_chrome')}
                  className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-4 py-2 text-sm font-medium transition hover:bg-white/10 disabled:opacity-50"
                >
                  {bootstrapping === 'import_chrome' ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Share2 className="h-4 w-4" />}
                  Import from Chrome
                </button>
              </div>
            </div>
          ) : (
            <MacEmptyState
              icon={<Share2 className="h-7 w-7" />}
              title="No X status"
              description="Save credentials first, then bootstrap a session."
            />
          )}
        </MacPanel>
      </div>
    </MacPage>
  );
}
