import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  ArrowRight,
  CheckCircle2,
  Lock,
  PlayCircle,
  Rocket,
  Search,
  ShieldCheck,
  Sparkles,
  Swords,
} from 'lucide-react';
import {
  activateAgent,
  bootstrapOnboarding,
  completeOnboarding,
  createAgent,
  getAgents,
  getClasses,
  getOnboardingState,
  updateAgent,
} from '@/lib/api';
import {
  MacBadge,
  MacEmptyState,
  MacPage,
  MacPanel,
  MacSearchField,
  MacStat,
} from '@/components/macos/MacPrimitives';
import { useShell } from '@/components/shell/ShellProvider';
import socialMediaManagerArt from '@/assets/class-social-media-manager.png';
import vaArt from '@/assets/class-va.png';
import type {
  AgentClassManifest,
  AgentProfile,
  OnboardingBootstrapResponse,
  ResolvedAgentProfile,
} from '@/types/api';

type StudioStage = 'loading' | 'intro' | 'wizard' | 'studio';

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
    id: 'tanith',
    name: 'Tanith',
    avatar: 'radiant tactician',
    launch_on_startup: true,
    primary_class: 'social_media_manager',
    secondary_classes: ['va'],
    social_accounts: {},
    overrides: {
      summary:
        'Lead social growth while staying deeply useful in day-to-day coordination and follow-through.',
      system_prompt_appendix: '',
      provider: null,
      model: null,
      temperature: null,
      max_depth: null,
      agentic: true,
      max_iterations: null,
      tool_grants: [],
      skill_grants: [],
      soul: {
        voice: '',
        principles: [],
        boundaries: [],
        style: null,
      },
      identity: {},
    },
  };
}

function classArtwork(classId: string): string | null {
  switch (classId) {
    case 'social_media_manager':
      return socialMediaManagerArt;
    case 'va':
      return vaArt;
    default:
      return null;
  }
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
          className="h-16 w-16 shrink-0 rounded-[1rem] object-cover"
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

export default function Studio() {
  const { isDesktopMac } = useShell();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [stage, setStage] = useState<StudioStage>('loading');
  const [classes, setClasses] = useState<AgentClassManifest[]>([]);
  const [agents, setAgents] = useState<ResolvedAgentProfile[]>([]);
  const [activeAgentId, setActiveAgentId] = useState('');
  const [bootstrap, setBootstrap] = useState<OnboardingBootstrapResponse | null>(null);
  const [draft, setDraft] = useState<AgentProfile>(defaultProfile());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rosterQuery, setRosterQuery] = useState('');

  const wizardRequested = searchParams.get('new') === '1';

  useEffect(() => {
    Promise.all([bootstrapOnboarding(), getClasses(), getAgents()])
      .then(([state, classList, agentList]) => {
        setBootstrap(state);
        setClasses(classList);
        setAgents(agentList.profiles);
        setActiveAgentId(agentList.active_agent_id);
        setDraft({
          ...defaultProfile(),
          id: slugify(state.active_profile.profile.name) || 'tanith',
          name: state.active_profile.profile.name,
        });
        setStage(wizardRequested ? 'wizard' : state.onboarding.completed ? 'studio' : 'intro');
      })
      .catch((err: Error) => {
        setError(err.message);
        setStage(wizardRequested ? 'wizard' : 'intro');
      });
  }, [wizardRequested]);

  useEffect(() => {
    if (!bootstrap) {
      return;
    }

    if (wizardRequested) {
      setStage('wizard');
      return;
    }

    if (stage === 'wizard') {
      setStage(bootstrap.onboarding.completed ? 'studio' : 'intro');
    }
  }, [bootstrap, stage, wizardRequested]);

  const activeClasses = useMemo(
    () => classes.filter((item) => item.status === 'active'),
    [classes],
  );

  const activeProfile =
    agents.find((item) => item.profile.id === activeAgentId) ?? bootstrap?.active_profile ?? null;

  const selectedSecondaryClasses = useMemo(
    () => draft.secondary_classes.filter((classId) => classId !== draft.primary_class),
    [draft.primary_class, draft.secondary_classes],
  );

  const wizardClassPreview = useMemo(
    () =>
      [draft.primary_class, ...selectedSecondaryClasses]
        .map((id) => classes.find((item) => item.id === id))
        .filter(Boolean) as AgentClassManifest[],
    [classes, draft.primary_class, selectedSecondaryClasses],
  );

  const filteredAgents = useMemo(() => {
    const normalized = rosterQuery.trim().toLowerCase();
    if (!normalized) {
      return agents;
    }

    return agents.filter((agent) => {
      const haystack = [
        agent.profile.name,
        agent.summary,
        agent.identity.role_title,
        ...agent.classes.map((item) => item.name),
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(normalized);
    });
  }, [agents, rosterQuery]);

  const topTools = useMemo(() => {
    const tools = activeProfile?.tool_grants ?? [];
    return Array.from(new Set(tools)).slice(0, 8);
  }, [activeProfile]);

  const readinessItems = [
    {
      label: 'Provider',
      value: bootstrap?.onboarding.has_provider_config ? 'Configured' : 'Needs setup',
    },
    {
      label: 'Runtime',
      value: bootstrap?.onboarding.runtime_ready ? 'Ready' : 'Starting',
    },
    {
      label: 'Flagship agent',
      value: bootstrap?.active_profile.profile.name ?? 'Tanith',
    },
  ];
  const providerReadiness = readinessItems[0]?.value ?? 'Unknown';
  const runtimeReadiness = readinessItems[1]?.value ?? 'Unknown';
  const starterAgent = readinessItems[2]?.value ?? 'Tanith';

  const refreshAgents = async () => {
    const [state, agentList] = await Promise.all([getOnboardingState(), getAgents()]);
    setBootstrap(state);
    setAgents(agentList.profiles);
    setActiveAgentId(agentList.active_agent_id);
  };

  const clearWizardMode = () => {
    const next = new URLSearchParams(searchParams);
    next.delete('new');
    setSearchParams(next, { replace: true });
  };

  const openWizard = () => {
    const next = new URLSearchParams(searchParams);
    next.set('new', '1');
    setSearchParams(next, { replace: true });
    setStage('wizard');
  };

  const handleCreateAgent = async () => {
    setBusy(true);
    setError(null);
    try {
      const prepared: AgentProfile = {
        ...draft,
        id: slugify(draft.id || draft.name) || 'new_agent',
        secondary_classes: selectedSecondaryClasses,
      };
      const created = await createAgent(prepared, true);
      setActiveAgentId(created.profile.id);
      await completeOnboarding(created.profile.id);
      await refreshAgents();
      clearWizardMode();
      setStage('studio');
      navigate('/dashboard');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create agent');
    } finally {
      setBusy(false);
    }
  };

  const handleActivate = async (agentId: string) => {
    setBusy(true);
    setError(null);
    try {
      await activateAgent(agentId);
      await refreshAgents();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to activate agent');
    } finally {
      setBusy(false);
    }
  };

  const handleStartupToggle = async (agent: ResolvedAgentProfile, enabled: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const profile: AgentProfile = {
        ...agent.profile,
        launch_on_startup: enabled,
      };
      await updateAgent(profile, enabled);
      await refreshAgents();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update startup preference');
    } finally {
      setBusy(false);
    }
  };

  if (stage === 'loading') {
    return (
      <MacPage
        eyebrow="Studio"
        title="Preparing workspace"
        description="Loading agents, classes, and the current onboarding state."
      >
        <MacPanel title="Starting up">
          <MacEmptyState
            icon={<Sparkles className="h-8 w-8 animate-pulse" />}
            title="Booting Agent HQ"
            description="Reading the local runtime and assembling the desktop workspace."
          />
        </MacPanel>
      </MacPage>
    );
  }

  return (
    <MacPage
      eyebrow="Studio"
      title={
        stage === 'wizard'
          ? 'Create an agent'
          : activeProfile?.profile.name
            ? `${activeProfile.profile.name} workspace`
            : 'Agent workspace'
      }
      description={
        stage === 'wizard'
          ? 'Set up identity, class loadout, and startup behavior in a tighter Mac-style flow.'
          : 'Manage the active roster, inspect class loadouts, and move into focused operational tools.'
      }
      actions={
        stage === 'studio' ? (
          <div className="flex flex-wrap gap-2">
            <Link to="/agent" className={actionButtonClass(false, isDesktopMac)}>
              Open Chat
            </Link>
            <Link to="/dashboard" className={actionButtonClass(true, isDesktopMac)}>
              Open Dashboard
            </Link>
          </div>
        ) : undefined
      }
    >
      {error ? (
        <div className="rounded-[1.4rem] border border-rose-300/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-200">
          {error}
        </div>
      ) : null}

      {stage === 'intro' ? (
        <div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
          <MacPanel
            title="Set up your first working agent"
            detail="Mac apps lead with the task at hand. Start with one strong agent profile, then refine deeper details in the dedicated tools."
          >
            <div className="grid gap-4 md:grid-cols-3">
              <MacStat label="Runtime" value={runtimeReadiness} detail="Local desktop runtime" />
              <MacStat label="Provider" value={providerReadiness} detail="Model access" />
              <MacStat label="Starter" value={starterAgent} detail="Flagship profile" />
            </div>

            <div className="mt-6 grid gap-4 md:grid-cols-3">
              {[
                {
                  icon: ShieldCheck,
                  title: 'Local-first control',
                  detail: 'Agent state, menus, and launch behavior stay anchored to this Mac runtime.',
                },
                {
                  icon: Swords,
                  title: 'Structured classes',
                  detail: 'Each class adds concrete tools, voice overlays, and operational guardrails.',
                },
                {
                  icon: Lock,
                  title: 'Progressive setup',
                  detail: 'The first-run flow stays short and leads into the real workspace rather than a splash page.',
                },
              ].map(({ icon: Icon, title, detail }) => (
                <div key={title} className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                  <Icon className="h-5 w-5 text-[#0a84ff]" />
                  <h3 className="mt-3 text-sm font-semibold">{title}</h3>
                  <p className="mt-2 text-sm text-[var(--shell-muted)]">{detail}</p>
                </div>
              ))}
            </div>

            <div className="mt-6 flex flex-wrap gap-3">
              <button type="button" onClick={openWizard} className={actionButtonClass(true, isDesktopMac)}>
                Create Agent
                <ArrowRight className="h-4 w-4" />
              </button>
              <Link to="/dashboard" className={actionButtonClass(false, isDesktopMac)}>
                Open Existing Workspace
              </Link>
            </div>
          </MacPanel>

          <MacPanel title="Current lead profile" detail="The active agent is the default startup and workspace target until you change it.">
            {activeProfile ? (
              <div className="space-y-4">
                <div className="flex items-start gap-4 rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                  {classArtwork(activeProfile.profile.primary_class) ? (
                    <img
                      src={classArtwork(activeProfile.profile.primary_class) ?? undefined}
                      alt={activeProfile.profile.name}
                      className="h-20 w-20 rounded-[1.1rem] object-cover"
                    />
                  ) : null}
                  <div className="min-w-0">
                    <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                      {activeProfile.identity.role_title}
                    </p>
                    <h3 className="mt-1 text-lg font-semibold">{activeProfile.profile.name}</h3>
                    <p className="mt-2 text-sm text-[var(--shell-muted)]">{activeProfile.summary}</p>
                  </div>
                </div>

                <div className="grid gap-3">
                  <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                    <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                      Class loadout
                    </p>
                    <p className="mt-2 text-sm font-semibold">
                      {activeProfile.classes.map((item) => item.name).join(' / ')}
                    </p>
                  </div>
                  <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                    <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                      Why this matters
                    </p>
                    <p className="mt-2 text-sm text-[var(--shell-muted)]">
                      The sidebar and menus should open real work areas. This profile becomes the anchor for those actions.
                    </p>
                  </div>
                </div>
              </div>
            ) : (
              <MacEmptyState
                icon={<Sparkles className="h-7 w-7" />}
                title="No active profile"
                description="Create the first agent to establish a startup target and workspace owner."
              />
            )}
          </MacPanel>
        </div>
      ) : null}

      {stage === 'wizard' ? (
        <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
          <MacPanel title="Agent identity" detail="Keep setup practical: name, role summary, primary class, then optional secondary support.">
            <div className="grid gap-4 md:grid-cols-2">
              <label className="block text-sm">
                <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  Agent name
                </span>
                <input
                  value={draft.name}
                  onChange={(event) =>
                    setDraft((prev) => ({
                      ...prev,
                      name: event.target.value,
                      id: slugify(event.target.value) || prev.id,
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
                  onChange={(event) =>
                    setDraft((prev) => ({ ...prev, id: slugify(event.target.value) }))
                  }
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
                onChange={(event) =>
                  setDraft((prev) => ({
                    ...prev,
                    overrides: { ...prev.overrides, summary: event.target.value },
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
                    onClick={() =>
                      setDraft((prev) => ({
                        ...prev,
                        primary_class: classItem.id,
                        secondary_classes: prev.secondary_classes.filter((id) => id !== classItem.id),
                      }))
                    }
                  />
                ))}
              </div>
            </div>

            <div className="mt-6">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold">Secondary classes</h3>
                <MacBadge tone="neutral">Optional</MacBadge>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                {activeClasses
                  .filter((item) => item.id !== draft.primary_class)
                  .map((classItem) => {
                    const selected = selectedSecondaryClasses.includes(classItem.id);
                    return (
                      <button
                        key={classItem.id}
                        type="button"
                        onClick={() =>
                          setDraft((prev) => ({
                            ...prev,
                            secondary_classes: selected
                              ? prev.secondary_classes.filter((id) => id !== classItem.id)
                              : [...prev.secondary_classes, classItem.id],
                          }))
                        }
                        className={[
                          'rounded-full border px-3 py-2 text-sm transition',
                          selected
                            ? 'border-sky-400/45 bg-sky-500/10 text-[#0a84ff] dark:text-sky-200'
                            : 'border-[var(--shell-border)] bg-[var(--shell-panel)] hover:bg-[var(--shell-selection)]',
                        ].join(' ')}
                      >
                        {classItem.name}
                      </button>
                    );
                  })}
              </div>
            </div>

            <div className="mt-6 flex flex-wrap gap-3">
              <button
                type="button"
                disabled={busy}
                onClick={handleCreateAgent}
                className={[actionButtonClass(true, isDesktopMac), busy ? 'opacity-60' : ''].join(' ')}
              >
                {busy ? 'Creating agent...' : 'Finish Setup'}
                <ArrowRight className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => {
                  clearWizardMode();
                  setStage('intro');
                }}
                className={actionButtonClass(false, isDesktopMac)}
              >
                Cancel
              </button>
            </div>
          </MacPanel>

          <div className="grid gap-4">
            <MacPanel title="Setup flow" detail="Short, sequential, and explicit.">
              <div className="grid gap-3">
                {[
                  'Check provider and runtime readiness',
                  'Name the agent and define its role summary',
                  'Select a primary class for default behavior',
                  'Add optional secondary support classes',
                  'Confirm startup ownership and finish',
                ].map((step, index) => (
                  <div
                    key={step}
                    className="flex items-start gap-3 rounded-[1.2rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] px-4 py-3"
                  >
                    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[var(--shell-selection)] text-xs font-semibold">
                      {index + 1}
                    </div>
                    <p className="text-sm">{step}</p>
                  </div>
                ))}
              </div>
            </MacPanel>

            <MacPanel title="Live preview" detail="This summary becomes the basis for the dashboard and agent inspector.">
              <div className="grid gap-3">
                <MacStat
                  label="Primary"
                  value={
                    classes.find((item) => item.id === draft.primary_class)?.name ?? 'Unassigned'
                  }
                  detail="Default decision-making lens"
                />
                <MacStat
                  label="Secondary"
                  value={
                    selectedSecondaryClasses.length > 0
                      ? selectedSecondaryClasses
                          .map((id) => classes.find((item) => item.id === id)?.name ?? id)
                          .join(' / ')
                      : 'None'
                  }
                  detail="Support capabilities"
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
                      <MacBadge key={tool} tone="neutral">
                        {tool}
                      </MacBadge>
                    ))}
                </div>
              </div>
            </MacPanel>
          </div>
        </div>
      ) : null}

      {stage === 'studio' ? (
        <>
          <div className="grid gap-4 lg:grid-cols-4">
            <MacStat
              label="Active agent"
              value={activeProfile?.profile.name ?? 'None'}
              detail={activeProfile?.identity.role_title ?? 'No role selected'}
            />
            <MacStat
              label="Primary class"
              value={activeProfile?.classes[0]?.name ?? 'None'}
              detail="Default behavior"
            />
            <MacStat
              label="Roster size"
              value={String(agents.length)}
              detail="Saved agent builds"
            />
            <MacStat
              label="Startup target"
              value={agents.find((agent) => agent.profile.launch_on_startup)?.profile.name ?? 'None'}
              detail="launchd owner"
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-[0.92fr_1.1fr_0.9fr]">
            <MacPanel title="Roster" detail="Use the roster like a source list: switch active agents and manage startup ownership.">
              <MacSearchField
                value={rosterQuery}
                onChange={setRosterQuery}
                placeholder="Search agents, roles, and classes"
              />

              <div className="mt-4 grid gap-3">
                {filteredAgents.length === 0 ? (
                  <MacEmptyState
                    icon={<Search className="h-7 w-7" />}
                    title="No matching agents"
                    description="Try a different name, class, or role title."
                  />
                ) : (
                  filteredAgents.map((agent) => {
                    const active = agent.profile.id === activeAgentId;
                    return (
                      <div
                        key={agent.profile.id}
                        className={[
                          'rounded-[1.25rem] border p-4 transition',
                          active
                            ? 'border-sky-400/45 bg-sky-500/10'
                            : 'border-[var(--shell-border)] bg-[var(--shell-panel)]',
                        ].join(' ')}
                      >
                        <div className="flex items-start gap-3">
                          {classArtwork(agent.profile.primary_class) ? (
                            <img
                              src={classArtwork(agent.profile.primary_class) ?? undefined}
                              alt={agent.profile.name}
                              className="h-14 w-14 rounded-[0.95rem] object-cover"
                            />
                          ) : (
                            <div className="flex h-14 w-14 items-center justify-center rounded-[0.95rem] bg-[var(--shell-selection)] text-lg">
                              {agent.identity.emoji}
                            </div>
                          )}

                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <h3 className="text-sm font-semibold">{agent.profile.name}</h3>
                              <MacBadge tone={active ? 'accent' : 'neutral'}>
                                {active ? 'Active' : 'Standby'}
                              </MacBadge>
                              {agent.profile.launch_on_startup ? (
                                <MacBadge tone="warning">Startup</MacBadge>
                              ) : null}
                            </div>
                            <p className="mt-1 text-sm text-[var(--shell-muted)]">
                              {agent.identity.role_title}
                            </p>
                            <p className="mt-2 text-sm text-[var(--shell-muted)]">{agent.summary}</p>
                          </div>
                        </div>

                        <div className="mt-4 flex flex-wrap items-center gap-2">
                          {!active ? (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => handleActivate(agent.profile.id)}
                              className={[actionButtonClass(true, isDesktopMac), busy ? 'opacity-60' : ''].join(' ')}
                            >
                              <PlayCircle className="h-4 w-4" />
                              Activate
                            </button>
                          ) : null}
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              handleStartupToggle(agent, !agent.profile.launch_on_startup)
                            }
                            className={[actionButtonClass(false, isDesktopMac), busy ? 'opacity-60' : ''].join(' ')}
                          >
                            {agent.profile.launch_on_startup ? 'Remove Startup' : 'Set Startup'}
                          </button>
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </MacPanel>

            <div className="grid gap-4">
              <MacPanel title="Control center" detail="Central actions belong in the content area, not hidden in decorative hero panels.">
                <div className="grid gap-3 md:grid-cols-3">
                  <Link to="/dashboard" className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4 transition hover:bg-[var(--shell-selection)]">
                    <Rocket className="h-5 w-5 text-[#0a84ff]" />
                    <h3 className="mt-3 text-sm font-semibold">Dashboard</h3>
                    <p className="mt-2 text-sm text-[var(--shell-muted)]">
                      Health, uptime, channels, and cost.
                    </p>
                  </Link>
                  <Link to="/agent" className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4 transition hover:bg-[var(--shell-selection)]">
                    <Sparkles className="h-5 w-5 text-[#0a84ff]" />
                    <h3 className="mt-3 text-sm font-semibold">Agent chat</h3>
                    <p className="mt-2 text-sm text-[var(--shell-muted)]">
                      Work directly with the active agent.
                    </p>
                  </Link>
                  <button
                    type="button"
                    onClick={openWizard}
                    className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4 text-left transition hover:bg-[var(--shell-selection)]"
                  >
                    <CheckCircle2 className="h-5 w-5 text-[#0a84ff]" />
                    <h3 className="mt-3 text-sm font-semibold">New agent</h3>
                    <p className="mt-2 text-sm text-[var(--shell-muted)]">
                      Create another roster member.
                    </p>
                  </button>
                </div>
              </MacPanel>

              <MacPanel title="Current class loadout" detail="Primary class defines the main posture. Secondary classes extend capabilities without displacing it.">
                {activeProfile ? (
                  <div className="grid gap-3">
                    {activeProfile.classes.map((classItem, index) => (
                      <div
                        key={classItem.id}
                        className="flex items-start gap-4 rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4"
                      >
                        {classArtwork(classItem.id) ? (
                          <img
                            src={classArtwork(classItem.id) ?? undefined}
                            alt={classItem.name}
                            className="h-16 w-16 rounded-[1rem] object-cover"
                          />
                        ) : null}
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <h3 className="text-sm font-semibold">{classItem.name}</h3>
                            <MacBadge tone={index === 0 ? 'accent' : 'neutral'}>
                              {index === 0 ? 'Primary' : 'Secondary'}
                            </MacBadge>
                          </div>
                          <p className="mt-2 text-sm text-[var(--shell-muted)]">
                            {classItem.default_role_summary}
                          </p>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <MacEmptyState
                    icon={<Swords className="h-7 w-7" />}
                    title="No class loadout"
                    description="Classes will appear here after you create or activate an agent."
                  />
                )}
              </MacPanel>
            </div>

            <MacPanel title="Inspector" detail="A Mac inspector is concise, scannable, and always contextual to the current selection.">
              {activeProfile ? (
                <div className="space-y-4">
                  <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                    {classArtwork(activeProfile.profile.primary_class) ? (
                      <img
                        src={classArtwork(activeProfile.profile.primary_class) ?? undefined}
                        alt={activeProfile.profile.name}
                        className="h-40 w-full rounded-[1rem] object-cover"
                      />
                    ) : null}
                    <h3 className="mt-4 text-lg font-semibold">{activeProfile.profile.name}</h3>
                    <p className="mt-2 text-sm text-[var(--shell-muted)]">{activeProfile.summary}</p>
                  </div>

                  <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                    <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                      Guardrails
                    </p>
                    <div className="mt-3 grid gap-2">
                      {activeProfile.guardrails.slice(0, 4).map((guardrail) => (
                        <div key={guardrail} className="rounded-[1rem] bg-[var(--shell-selection)] px-3 py-2 text-sm">
                          {guardrail}
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                    <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                      Tool grants
                    </p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {topTools.length > 0 ? (
                        topTools.map((tool) => (
                          <MacBadge key={tool} tone="neutral">
                            {tool}
                          </MacBadge>
                        ))
                      ) : (
                        <p className="text-sm text-[var(--shell-muted)]">No explicit tool grants listed.</p>
                      )}
                    </div>
                  </div>
                </div>
              ) : (
                <MacEmptyState
                  icon={<Sparkles className="h-7 w-7" />}
                  title="No active agent"
                  description="Activate an agent from the roster to populate the inspector."
                />
              )}
            </MacPanel>
          </div>
        </>
      ) : null}
    </MacPage>
  );
}
