import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  ExternalLink,
  Plus,
  RefreshCw,
  Share2,
} from 'lucide-react';
import {
  getAgentSocialAccounts,
  getAutomations,
  bootstrapAgentXHeadlessSession,
} from '@/lib/api';
import {
  MacBadge,
  MacEmptyState,
  MacPage,
  MacPanel,
  MacStat,
} from '@/components/macos/MacPrimitives';
import type {
  AgentSocialAccount,
  AgentXIntegrationStatus,
  AutomationRecord,
  ResolvedAgentProfile,
} from '@/types/api';

export default function SocialMediaManagerDashboard({ agent }: { agent: ResolvedAgentProfile }) {
  const [socialAccounts, setSocialAccounts] = useState<AgentSocialAccount[]>([]);
  const [xStatus, setXStatus] = useState<AgentXIntegrationStatus[]>([]);
  const [automations, setAutomations] = useState<AutomationRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [bootstrapping, setBootstrapping] = useState<string | null>(null);

  const agentId = agent.profile.id;

  useEffect(() => {
    Promise.all([getAgentSocialAccounts(), getAutomations()])
      .then(([social, autos]) => {
        setSocialAccounts(social.accounts);
        setXStatus(social.x_status);
        setAutomations(autos.filter((a) => a.owner_agent_id === agentId));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [agentId]);

  const thisAgentAccounts = socialAccounts.filter((a) => a.agent_name === agentId);
  const thisAgentXStatus = xStatus.filter((s) => s.agent_name === agentId);

  const activeAutomations = automations.filter((a) => a.enabled);
  const pausedAutomations = automations.filter((a) => !a.enabled);

  const handleBootstrapX = async (accountAgentName: string) => {
    setBootstrapping(accountAgentName);
    try {
      await bootstrapAgentXHeadlessSession(accountAgentName, 'headless');
      const social = await getAgentSocialAccounts();
      setSocialAccounts(social.accounts);
      setXStatus(social.x_status);
    } catch {
      // silently fail — user can retry
    } finally {
      setBootstrapping(null);
    }
  };

  return (
    <MacPage
      eyebrow={agent.identity.role_title}
      title={`${agent.profile.name} Dashboard`}
      description="Social media accounts, automations, and content overview."
    >
      {/* Stats row */}
      <div className="grid gap-4 md:grid-cols-4">
        <MacStat
          label="Social Accounts"
          value={String(thisAgentAccounts.length)}
          detail="Connected platforms"
        />
        <MacStat
          label="Active Automations"
          value={String(activeAutomations.length)}
          detail="Scheduled tasks"
        />
        <MacStat
          label="Paused"
          value={String(pausedAutomations.length)}
          detail="Inactive automations"
        />
        <MacStat
          label="Class"
          value="Social Media"
          detail={agent.classes.map((c) => c.name).join(', ')}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        {/* Social Accounts Panel */}
        <MacPanel
          title="Social Accounts"
          detail="Connected X and LinkedIn accounts for this agent."
        >
          {loading ? (
            <MacEmptyState
              icon={<RefreshCw className="h-7 w-7 animate-spin" />}
              title="Loading accounts"
              description="Fetching social account status..."
            />
          ) : thisAgentAccounts.length === 0 && thisAgentXStatus.length === 0 ? (
            <MacEmptyState
              icon={<Share2 className="h-7 w-7" />}
              title="No social accounts"
              description="Add X or LinkedIn accounts in Social Accounts settings."
            />
          ) : (
            <div className="grid gap-3">
              {/* X accounts from x_status */}
              {thisAgentXStatus.map((status) => (
                <div
                  key={status.agent_name}
                  className="flex items-start gap-4 rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4"
                >
                  <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-black text-white text-lg font-bold shrink-0">
                    X
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h4 className="text-sm font-semibold">X (Twitter)</h4>
                      <MacBadge
                        tone={
                          status.browser_headless.authenticated
                            ? 'accent'
                            : 'neutral'
                        }
                      >
                        {status.browser_headless.authenticated
                          ? 'Authenticated'
                          : 'Not Connected'}
                      </MacBadge>
                    </div>
                    <p className="mt-1 text-sm text-[var(--shell-muted)]">
                      {status.twitter_x.detail || status.browser_headless.detail || 'No details available'}
                    </p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {status.supported_capabilities.post ? (
                        <MacBadge tone="accent">Post</MacBadge>
                      ) : null}
                      {status.supported_capabilities.comment ? (
                        <MacBadge tone="accent">Reply</MacBadge>
                      ) : null}
                      {status.supported_capabilities.article ? (
                        <MacBadge tone="accent">Article</MacBadge>
                      ) : null}
                    </div>
                  </div>
                  {!status.browser_headless.authenticated ? (
                    <button
                      type="button"
                      disabled={bootstrapping === status.agent_name}
                      onClick={() => handleBootstrapX(status.agent_name)}
                      className="inline-flex items-center gap-1.5 rounded-full bg-sky-500/10 border border-sky-400/30 px-3 py-1.5 text-xs font-medium text-sky-400 transition hover:bg-sky-500/20"
                    >
                      {bootstrapping === status.agent_name ? (
                        <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <ExternalLink className="h-3.5 w-3.5" />
                      )}
                      Connect
                    </button>
                  ) : null}
                </div>
              ))}

              {/* Twitter credentials from social_accounts */}
              {thisAgentAccounts
                .filter((a) => a.twitter?.username)
                .map((account) => {
                  const matchingStatus = thisAgentXStatus.find((s) => s.agent_name === account.agent_name);
                  if (matchingStatus) return null; // already shown above
                  return (
                    <div
                      key={`twitter-${account.agent_name}`}
                      className="flex items-start gap-4 rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4"
                    >
                      <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-black text-white text-lg font-bold shrink-0">
                        X
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <h4 className="text-sm font-semibold">@{account.twitter?.username}</h4>
                          <MacBadge tone="neutral">Credentials Only</MacBadge>
                        </div>
                        <p className="mt-1 text-sm text-[var(--shell-muted)]">
                          Session needs to be bootstrapped.
                        </p>
                      </div>
                    </div>
                  );
                })}
            </div>
          )}

          <div className="mt-4">
            <Link
              to={`/agents/${agentId}/social-accounts`}
              className="inline-flex items-center gap-2 text-sm text-sky-400 hover:text-sky-300 transition-colors"
            >
              <Plus className="h-4 w-4" />
              Manage Social Accounts
            </Link>
          </div>
        </MacPanel>

        {/* Automations Panel */}
        <MacPanel
          title="Automations"
          detail="Scheduled posts, replies, and recurring tasks."
        >
          {loading ? (
            <MacEmptyState
              icon={<RefreshCw className="h-7 w-7 animate-spin" />}
              title="Loading automations"
              description="Fetching scheduled tasks..."
            />
          ) : automations.length === 0 ? (
            <MacEmptyState
              icon={<Clock className="h-7 w-7" />}
              title="No automations"
              description="Create automations for scheduled posts and auto-replies."
            />
          ) : (
            <div className="grid gap-3">
              {automations.slice(0, 8).map((auto) => (
                <div
                  key={auto.id}
                  className="flex items-start gap-3 rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] px-4 py-3"
                >
                  <div className="mt-0.5">
                    {auto.enabled ? (
                      <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                    ) : (
                      <AlertCircle className="h-4 w-4 text-amber-400" />
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium truncate">
                      {auto.name || auto.prompt?.slice(0, 60) || 'Untitled'}
                    </p>
                    <div className="mt-1 flex items-center gap-2">
                      <MacBadge tone={auto.enabled ? 'accent' : 'neutral'}>
                        {auto.enabled ? 'Active' : 'Paused'}
                      </MacBadge>
                      {auto.next_run ? (
                        <span className="text-xs text-[var(--shell-muted)]">
                          Next: {new Date(auto.next_run).toLocaleString()}
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="mt-4">
            <Link
              to={`/agents/${agentId}/automations`}
              className="inline-flex items-center gap-2 text-sm text-sky-400 hover:text-sky-300 transition-colors"
            >
              <Clock className="h-4 w-4" />
              Manage Automations
            </Link>
          </div>
        </MacPanel>
      </div>
    </MacPage>
  );
}
