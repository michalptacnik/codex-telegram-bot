import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowRight, Sparkles } from 'lucide-react';
import {
  completeOnboarding,
  createAgent,
  getClasses,
} from '@/lib/api';
import { useAgentContext } from '@/contexts/AgentContext';
import {
  MacBadge,
  MacEmptyState,
  MacPage,
  MacPanel,
  MacStat,
} from '@/components/macos/MacPrimitives';
import { useShell } from '@/components/shell/ShellProvider';
import socialMediaManagerArt from '@/assets/class-social-media-manager.png';
import salesArt from '@/assets/class-sales.png';
import vaArt from '@/assets/class-va.png';
import type { AgentClassManifest, AgentProfile } from '@/types/api';

function classArtwork(classId: string): string | null {
  switch (classId) {
    case 'social_media_manager':
      return socialMediaManagerArt;
    case 'sales':
      return salesArt;
    case 'va':
      return vaArt;
    default:
      return null;
  }
}

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

function panelFieldClass(isDesktopMac: boolean): string {
  return isDesktopMac
    ? 'mt-2 w-full rounded-[1rem] border border-[rgba(15,23,42,0.08)] bg-white/80 px-4 py-3 text-[0.97rem] text-slate-900 outline-none shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] focus:border-sky-400 dark:border-white/10 dark:bg-black/20 dark:text-white'
    : 'mt-2 w-full rounded-[1rem] border border-white/10 bg-[#0b1120] px-4 py-3 text-[0.97rem] text-white outline-none focus:border-sky-400';
}

function actionButtonClass(primary: boolean, isDesktopMac: boolean): string {
  if (primary) {
    return isDesktopMac
      ? 'inline-flex items-center gap-2 rounded-full bg-[#0a84ff] px-5 py-2.5 text-sm font-semibold text-white shadow-[0_12px_24px_rgba(10,132,255,0.22)] transition hover:brightness-105'
      : 'inline-flex items-center gap-2 rounded-full bg-sky-500 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-400';
  }
  return isDesktopMac
    ? 'inline-flex items-center gap-2 rounded-full border border-[rgba(15,23,42,0.08)] bg-white/70 px-5 py-2.5 text-sm font-semibold text-slate-800 transition hover:bg-white dark:border-white/10 dark:bg-white/6 dark:text-white'
    : 'inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-white/10';
}

function ClassChoiceCard({
  classItem,
  selected,
  disabled,
  onClick,
}: {
  classItem: AgentClassManifest;
  selected: boolean;
  disabled?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={[
        'flex items-start gap-4 rounded-[1.25rem] border px-4 py-4 text-left transition',
        selected
          ? 'border-sky-400/50 bg-sky-500/10'
          : 'border-[var(--shell-border)] bg-[var(--shell-panel)] hover:bg-[var(--shell-selection)]',
        disabled ? 'cursor-not-allowed opacity-55' : '',
      ].join(' ')}
    >
      {classArtwork(classItem.id) ? (
        <img
          src={classArtwork(classItem.id) ?? undefined}
          alt={classItem.name}
          className="h-16 w-16 shrink-0 rounded-[1rem] object-contain p-2 studio-art-frame"
        />
      ) : (
        <div className="h-16 w-16 shrink-0 rounded-[1rem] bg-[var(--shell-selection)]" />
      )}
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">{classItem.name}</h3>
          <MacBadge tone={classItem.status === 'coming_soon' ? 'neutral' : 'accent'}>
            {classItem.status === 'coming_soon' ? 'Soon' : 'Ready'}
          </MacBadge>
        </div>
        <p className="mt-2 text-sm text-[var(--shell-muted)]">{classItem.description}</p>
        <p className="mt-2 text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
          {classItem.fantasy_theme}
        </p>
      </div>
    </button>
  );
}

export default function AgentCreationWizard() {
  const { isDesktopMac } = useShell();
  const navigate = useNavigate();
  const { refreshAgents } = useAgentContext();
  const [classes, setClasses] = useState<AgentClassManifest[]>([]);
  const [draft, setDraft] = useState<AgentProfile>(defaultProfile());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingClasses, setLoadingClasses] = useState(true);

  useEffect(() => {
    getClasses()
      .then(setClasses)
      .catch(() => {})
      .finally(() => setLoadingClasses(false));
  }, []);

  const wizardClassPreview = useMemo(
    () =>
      [draft.primary_class]
        .map((id) => classes.find((item) => item.id === id))
        .filter(Boolean) as AgentClassManifest[],
    [classes, draft.primary_class],
  );

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
      description="Set up identity, class loadout, and startup behavior in one focused pass."
    >
      {error ? (
        <div className="rounded-[1.4rem] border border-rose-300/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-200 mb-4">
          {error}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <MacPanel title="Agent identity" detail="Name, role summary, and one primary class.">
          <div className="grid gap-4 md:grid-cols-2">
            <label className="block text-sm">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                Agent name
              </span>
              <input
                value={draft.name}
                onChange={(e) =>
                  setDraft((prev) => ({
                    ...prev,
                    name: e.target.value,
                    id: slugify(e.target.value) || prev.id,
                  }))
                }
                className={panelFieldClass(isDesktopMac)}
              />
            </label>
            <label className="block text-sm">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                Agent id
              </span>
              <input
                value={draft.id}
                onChange={(e) => setDraft((prev) => ({ ...prev, id: slugify(e.target.value) }))}
                className={panelFieldClass(isDesktopMac)}
              />
            </label>
          </div>

          <label className="mt-4 block text-sm">
            <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
              Role summary
            </span>
            <textarea
              rows={4}
              value={draft.overrides.summary ?? ''}
              onChange={(e) =>
                setDraft((prev) => ({
                  ...prev,
                  overrides: { ...prev.overrides, summary: e.target.value },
                }))
              }
              className={panelFieldClass(isDesktopMac)}
            />
          </label>

          <div className="mt-6">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold">Primary class</h3>
              <MacBadge tone="accent">Required</MacBadge>
            </div>
            <div className="mt-3 grid gap-3">
              {classes.map((classItem) => (
                <ClassChoiceCard
                  key={classItem.id}
                  classItem={classItem}
                  selected={draft.primary_class === classItem.id}
                  disabled={classItem.status === 'coming_soon'}
                  onClick={() => setDraft((prev) => ({ ...prev, primary_class: classItem.id }))}
                />
              ))}
            </div>
          </div>

          <div className="mt-6 flex flex-wrap gap-3">
            <button
              type="button"
              disabled={busy}
              onClick={handleCreate}
              className={[actionButtonClass(true, isDesktopMac), busy ? 'opacity-60' : ''].join(' ')}
            >
              {busy ? 'Creating agent...' : 'Finish Setup'}
              <ArrowRight className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={() => navigate('/agents')}
              className={actionButtonClass(false, isDesktopMac)}
            >
              <ArrowLeft className="h-4 w-4" />
              Cancel
            </button>
          </div>
        </MacPanel>

        <div className="grid gap-4">
          <MacPanel title="Live preview" detail="Summary of the agent being created.">
            <div className="grid gap-3">
              <MacStat
                label="Primary"
                value={classes.find((item) => item.id === draft.primary_class)?.name ?? 'Unassigned'}
                detail="Default decision-making lens"
              />
            </div>
            <div className="mt-4 rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                Soul voice blend
              </p>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">
                {wizardClassPreview
                  .map((item) => item.default_soul_overlay.voice)
                  .filter(Boolean)
                  .join(' / ') || 'Base voice'}
              </p>
            </div>
            <div className="mt-4 rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                Tool grants
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                {Array.from(new Set(wizardClassPreview.flatMap((item) => item.tool_grants)))
                  .slice(0, 8)
                  .map((tool) => (
                    <MacBadge key={tool} tone="neutral">{tool}</MacBadge>
                  ))}
              </div>
            </div>
          </MacPanel>
        </div>
      </div>
    </MacPage>
  );
}
