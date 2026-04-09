import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Sparkles } from 'lucide-react';
import { completeOnboarding, createAgent, getClasses, getConfig, putConfig } from '@/lib/api';
import { getStarterClassesFallback } from '@/lib/starterClasses';
import { useAgentContext } from '@/contexts/AgentContext';
import { MacEmptyState, MacPage, MacPanel } from '@/components/macos/MacPrimitives';
import AgentSetupWizard from '@/components/onboarding/AgentSetupWizard';
import type { AgentClassManifest, AgentProfile } from '@/types/api';

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 40);
}

function defaultProfile(): AgentProfile {
  return {
    id: 'agent',
    name: 'Agent',
    avatar: 'local operator',
    launch_on_startup: false,
    primary_class: 'va',
    social_accounts: {},
    overrides: {
      summary: 'Handle practical work across the workspace with reliable follow-through.',
      system_prompt_appendix: '',
      provider: null,
      model: null,
      temperature: null,
      max_depth: null,
      agentic: true,
      max_iterations: null,
      tool_grants: [],
      skill_grants: [],
      soul: { voice: '', principles: [], boundaries: [], style: null },
      identity: {},
    },
  };
}

export default function AgentCreationWizard() {
  const navigate = useNavigate();
  const { refreshAgents } = useAgentContext();
  const [classes, setClasses] = useState<AgentClassManifest[]>([]);
  const [draft, setDraft] = useState<AgentProfile>(defaultProfile());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loadingClasses, setLoadingClasses] = useState(true);
  const [configText, setConfigText] = useState<string | null>(null);

  useEffect(() => {
    getClasses()
      .then((items) => {
        const resolved = items.length > 0 ? items : getStarterClassesFallback();
        setClasses(resolved);
        if (items.length === 0) {
          setNotice('Setup is using local starter templates because the live template list came back empty.');
        }
      })
      .catch(() => {
        setClasses(getStarterClassesFallback());
        setNotice('Setup is using local starter templates because the live template list could not be loaded.');
      })
      .finally(() => setLoadingClasses(false));
  }, []);

  useEffect(() => {
    getConfig()
      .then((value) => setConfigText(typeof value === 'string' ? value : JSON.stringify(value, null, 2)))
      .catch(() => setConfigText(null));
  }, []);

  const handleCreate = async () => {
    setBusy(true);
    setError(null);
    try {
      const prepared: AgentProfile = {
        ...draft,
        id: slugify(draft.id || draft.name) || 'new_agent',
      };
      const created = await createAgent(prepared, true);
      await completeOnboarding(created.profile.id);
      await refreshAgents();
      navigate(`/agents/${created.profile.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create agent');
    } finally {
      setBusy(false);
    }
  };

  if (loadingClasses) {
    return (
      <MacPage eyebrow="Setup" title="Loading..." description="Fetching agent classes.">
        <MacPanel title="Starting up">
          <MacEmptyState
            icon={<Sparkles className="h-8 w-8 animate-pulse" />}
            title="Loading classes"
            description="Preparing the agent creation flow."
          />
        </MacPanel>
      </MacPage>
    );
  }

  return (
    <MacPage
      eyebrow="Setup"
      title="Create an Agent"
      description="A friendlier setup flow with one decision per step."
    >
      {error ? (
        <div className="mb-4 rounded-[1.4rem] border border-rose-300/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-200">
          {error}
        </div>
      ) : null}
      {notice ? (
        <div className="mb-4 rounded-[1.4rem] border border-amber-300/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-800 dark:text-amber-100">
          {notice}
        </div>
      ) : null}

      <AgentSetupWizard
        classes={classes}
        draft={draft}
        setDraft={setDraft}
        busy={busy}
        onSubmit={handleCreate}
        onCancel={() => navigate('/agents')}
        mode="setup"
        configText={configText}
        onSaveConfig={async (nextConfig) => {
          await putConfig(nextConfig);
          setConfigText(nextConfig);
        }}
      />
    </MacPage>
  );
}
