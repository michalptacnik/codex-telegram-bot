import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Sparkles,
  ShieldCheck,
  Wand2,
  ArrowRight,
  CheckCircle2,
  Swords,
  Orbit,
  Cpu,
  Lock,
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
import socialMediaManagerArt from '@/assets/class-social-media-manager.svg';
import vaArt from '@/assets/class-va.svg';
import type {
  AgentClassManifest,
  AgentProfile,
  OnboardingBootstrapResponse,
  ResolvedAgentProfile,
} from '@/types/api';
import { useShell } from '@/components/shell/ShellProvider';

type StudioStage = 'loading' | 'intro' | 'wizard' | 'studio';

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 40);
}

const defaultProfile = (): AgentProfile => ({
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
});

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

function ClassCard({
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
        'rounded-[28px] border p-5 text-left transition-all',
        disabled
          ? 'border-white/10 bg-white/5 opacity-65'
          : selected
            ? 'border-amber-300/60 bg-amber-300/12 shadow-[0_0_0_1px_rgba(252,211,77,0.25)]'
            : 'border-white/12 bg-white/6 hover:border-white/30 hover:bg-white/10',
      ].join(' ')}
    >
      {classArtwork(classItem.id) && (
        <div className="mb-4 overflow-hidden rounded-[24px] border border-white/10 bg-[linear-gradient(180deg,rgba(255,255,255,0.14),rgba(255,255,255,0.04))]">
          <img
            src={classArtwork(classItem.id) ?? undefined}
            alt={`${classItem.name} class portrait`}
            className="h-56 w-full object-cover"
          />
        </div>
      )}
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.28em] text-amber-100/70">
            {classItem.fantasy_theme}
          </div>
          <h3 className="mt-2 text-lg font-semibold text-white">{classItem.name}</h3>
        </div>
        <span
          className={[
            'rounded-full px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em]',
            classItem.status === 'coming_soon'
              ? 'bg-white/10 text-white/70'
              : 'bg-emerald-400/15 text-emerald-200',
          ].join(' ')}
        >
          {classItem.status === 'coming_soon' ? 'Coming Soon' : 'Ready'}
        </span>
      </div>
      <p className="mt-4 text-sm leading-6 text-slate-300">{classItem.description}</p>
      <div className="mt-4 text-xs uppercase tracking-[0.2em] text-slate-400">
        {classItem.default_role_summary}
      </div>
    </button>
  );
}

function macClassNames(base: string, desktop: string, isDesktopMac: boolean): string {
  return isDesktopMac ? desktop : base;
}

export default function Studio() {
  const { isDesktopMac } = useShell();
  const navigate = useNavigate();
  const [stage, setStage] = useState<StudioStage>('loading');
  const [classes, setClasses] = useState<AgentClassManifest[]>([]);
  const [agents, setAgents] = useState<ResolvedAgentProfile[]>([]);
  const [activeAgentId, setActiveAgentId] = useState('');
  const [bootstrap, setBootstrap] = useState<OnboardingBootstrapResponse | null>(null);
  const [draft, setDraft] = useState<AgentProfile>(defaultProfile());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const activeClasses = useMemo(
    () => classes.filter((item) => item.status === 'active'),
    [classes],
  );

  const primaryClass = useMemo(
    () => classes.find((item) => item.id === draft.primary_class) ?? null,
    [classes, draft.primary_class],
  );

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
        setStage(state.onboarding.completed ? 'studio' : 'intro');
      })
      .catch((err: Error) => {
        setError(err.message);
        setStage('intro');
      });
  }, []);

  const selectedSecondaryClasses = draft.secondary_classes.filter(
    (classId) => classId !== draft.primary_class,
  );

  const wizardProfilePreview = useMemo(() => {
    const picks = [draft.primary_class, ...selectedSecondaryClasses]
      .map((id) => classes.find((item) => item.id === id))
      .filter(Boolean) as AgentClassManifest[];
    return picks;
  }, [classes, draft.primary_class, selectedSecondaryClasses]);

  const refreshAgents = async () => {
    const [state, agentList] = await Promise.all([getOnboardingState(), getAgents()]);
    setBootstrap(state);
    setAgents(agentList.profiles);
    setActiveAgentId(agentList.active_agent_id);
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

  const handleStartWizard = () => {
    setStage('wizard');
  };

  const activeProfile = agents.find((item) => item.profile.id === activeAgentId) ?? bootstrap?.active_profile ?? null;

  if (stage === 'loading') {
    return (
      <div
        className={
          isDesktopMac
            ? 'min-h-[calc(100vh-3.5rem)] flex items-center justify-center text-[var(--shell-text)]'
            : 'min-h-[calc(100vh-3.5rem)] bg-[#08111f] text-white flex items-center justify-center'
        }
      >
        <div className="text-center">
          <div className="mx-auto h-12 w-12 animate-spin rounded-full border-2 border-amber-300/50 border-t-transparent" />
          <p className="mt-4 text-sm uppercase tracking-[0.3em] text-slate-400">Booting Agent Studio</p>
        </div>
      </div>
    );
  }

  return (
    <div
      className={
        isDesktopMac
          ? 'min-h-[calc(100vh-3.5rem)] text-[var(--shell-text)]'
          : 'min-h-[calc(100vh-3.5rem)] bg-[radial-gradient(circle_at_top,#19304d_0%,#0a1220_35%,#060b13_100%)] text-white'
      }
    >
      <div className={isDesktopMac ? 'mx-auto max-w-7xl px-1 py-2' : 'mx-auto max-w-7xl px-6 py-8'}>
        {error && (
          <div className="mb-6 rounded-3xl border border-rose-300/30 bg-rose-400/10 px-5 py-4 text-sm text-rose-100">
            {error}
          </div>
        )}

        {(stage === 'intro' || stage === 'wizard') && (
          <section
            className={macClassNames(
              'rounded-[36px] border border-white/10 bg-white/6 p-8 shadow-[0_20px_80px_rgba(0,0,0,0.35)] backdrop-blur',
              'rounded-[32px] border border-white/55 bg-white/70 p-8 shadow-[0_18px_54px_rgba(36,48,74,0.12)] backdrop-blur-2xl dark:border-white/12 dark:bg-white/8',
              isDesktopMac,
            )}
          >
            <div className="grid gap-8 lg:grid-cols-[1.2fr_0.8fr]">
              <div>
                <div
                  className={macClassNames(
                    'inline-flex items-center gap-2 rounded-full border border-amber-300/25 bg-amber-300/10 px-4 py-2 text-xs uppercase tracking-[0.3em] text-amber-100',
                    'inline-flex items-center gap-2 rounded-full border border-sky-300/30 bg-sky-200/25 px-4 py-2 text-[11px] font-medium uppercase tracking-[0.24em] text-sky-800 dark:border-sky-300/18 dark:bg-sky-300/10 dark:text-sky-100',
                    isDesktopMac,
                  )}
                >
                  <Sparkles className="h-4 w-4" />
                  Intro Sequence
                </div>
                <h1
                  className={macClassNames(
                    'mt-6 max-w-3xl font-serif text-5xl leading-tight text-white',
                    'mt-6 max-w-4xl text-[clamp(2.8rem,6vw,4.6rem)] font-semibold leading-[0.94] tracking-[-0.065em] text-slate-900 dark:text-white',
                    isDesktopMac,
                  )}
                >
                  Agent HQ now opens like an agent studio, not a terminal utility.
                </h1>
                <p
                  className={macClassNames(
                    'mt-5 max-w-2xl text-lg leading-8 text-slate-300',
                    'mt-5 max-w-3xl text-[1.08rem] leading-8 text-slate-600 dark:text-slate-300',
                    isDesktopMac,
                  )}
                >
                  Build your first operative, choose a class loadout, review the generated soul and tool access,
                  then drop into the deeper control center when you are ready to operate.
                </p>

                <div className="mt-8 grid gap-4 md:grid-cols-3">
                  <div className={macClassNames('rounded-3xl border border-white/10 bg-[#0b1626] p-4', 'rounded-[24px] border border-white/55 bg-white/76 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] dark:border-white/10 dark:bg-white/6', isDesktopMac)}>
                    <ShieldCheck className="h-5 w-5 text-emerald-300" />
                    <p className="mt-3 text-sm font-semibold">Local-first runtime</p>
                    <p className="mt-2 text-sm text-slate-400">
                      Runs on this Mac, keeps class logic explicit, and preserves the existing Rust runtime.
                    </p>
                  </div>
                  <div className={macClassNames('rounded-3xl border border-white/10 bg-[#0b1626] p-4', 'rounded-[24px] border border-white/55 bg-white/76 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] dark:border-white/10 dark:bg-white/6', isDesktopMac)}>
                    <Cpu className="h-5 w-5 text-cyan-300" />
                    <p className="mt-3 text-sm font-semibold">Structured specialization</p>
                    <p className="mt-2 text-sm text-slate-400">
                      Classes grant real tools, guardrails, and evaluation scenarios instead of vague roleplay.
                    </p>
                  </div>
                  <div className={macClassNames('rounded-3xl border border-white/10 bg-[#0b1626] p-4', 'rounded-[24px] border border-white/55 bg-white/76 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] dark:border-white/10 dark:bg-white/6', isDesktopMac)}>
                    <Lock className="h-5 w-5 text-amber-300" />
                    <p className="mt-3 text-sm font-semibold">Progressive setup</p>
                    <p className="mt-2 text-sm text-slate-400">
                      The first run stays focused: provider, agent build, class selection, and a final review.
                    </p>
                  </div>
                </div>

                {stage === 'intro' && (
                  <div className="mt-8 flex flex-wrap gap-4">
                    <button
                      type="button"
                      onClick={handleStartWizard}
                      className="inline-flex items-center gap-2 rounded-full bg-amber-300 px-6 py-3 text-sm font-semibold text-slate-900 transition hover:bg-amber-200"
                    >
                      Create Your First Agent
                      <ArrowRight className="h-4 w-4" />
                    </button>
                    <Link
                      to="/dashboard"
                      className={macClassNames(
                        'inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm font-semibold text-white/90 hover:bg-white/8',
                        'inline-flex items-center gap-2 rounded-full border border-slate-300/70 bg-white/60 px-6 py-3 text-sm font-semibold text-slate-800 hover:bg-white/85 dark:border-white/14 dark:bg-white/6 dark:text-white',
                        isDesktopMac,
                      )}
                    >
                      Open Existing Workspace
                    </Link>
                  </div>
                )}
              </div>

              <div
                className={macClassNames(
                  'rounded-[32px] border border-white/10 bg-[#08111f]/85 p-6',
                  'rounded-[28px] border border-white/55 bg-white/76 p-6 shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] dark:border-white/10 dark:bg-white/7',
                  isDesktopMac,
                )}
              >
                <div className="text-[11px] uppercase tracking-[0.28em] text-slate-400">System Readiness</div>
                <div className="mt-5 space-y-4">
                  {[
                    {
                      label: 'Provider setup',
                      value: bootstrap?.onboarding.has_provider_config ? 'Configured' : 'Needs attention',
                    },
                    {
                      label: 'Runtime shell',
                      value: bootstrap?.onboarding.runtime_ready ? 'Ready' : 'Initializing',
                    },
                    {
                      label: 'Seeded flagship agent',
                      value: bootstrap?.active_profile.profile.name ?? 'Tanith',
                    },
                  ].map((item) => (
                    <div
                      key={item.label}
                      className="flex items-center justify-between rounded-2xl border border-white/8 bg-white/4 px-4 py-3"
                    >
                      <span className="text-sm text-slate-300">{item.label}</span>
                      <span className="text-sm font-semibold text-white">{item.value}</span>
                    </div>
                  ))}
                </div>
                <div className="mt-8 rounded-3xl border border-amber-300/20 bg-amber-300/10 p-5">
                  <div className="flex items-center gap-3">
                    <Wand2 className="h-5 w-5 text-amber-200" />
                    <div>
                      <p className="text-sm font-semibold text-white">
                        Tanith is pre-seeded as Social Media Manager / VA
                      </p>
                      <p className="mt-1 text-sm text-amber-100/80">
                        She is your flagship multiclass build and the baseline for the new class system.
                      </p>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {stage === 'wizard' && (
              <div className="mt-10 grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
                <div className="rounded-[32px] border border-white/10 bg-[#08111f]/80 p-6">
                  <div className="text-[11px] uppercase tracking-[0.28em] text-slate-400">Wizard Flow</div>
                  <div className="mt-5 space-y-3">
                    {[
                      'Welcome and system framing',
                      'Local runtime and provider readiness',
                      'Create the agent identity',
                      'Choose primary and secondary classes',
                      'Review soul, tools, and guardrails',
                      'Finish and enter Agent Studio',
                    ].map((step, index) => (
                      <div key={step} className="flex items-center gap-3 rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
                        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-amber-300/15 text-xs font-semibold text-amber-100">
                          {index + 1}
                        </div>
                        <span className="text-sm text-slate-200">{step}</span>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="rounded-[32px] border border-white/10 bg-white/5 p-6">
                  <div className="grid gap-4 md:grid-cols-2">
                    <label className="block">
                      <span className="text-xs uppercase tracking-[0.24em] text-slate-400">Agent Name</span>
                      <input
                        value={draft.name}
                        onChange={(e) =>
                          setDraft((prev) => ({
                            ...prev,
                            name: e.target.value,
                            id: slugify(e.target.value) || prev.id,
                          }))
                        }
                        className="mt-2 w-full rounded-2xl border border-white/10 bg-[#08111f] px-4 py-3 text-white outline-none focus:border-amber-300/50"
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs uppercase tracking-[0.24em] text-slate-400">Agent ID</span>
                      <input
                        value={draft.id}
                        onChange={(e) => setDraft((prev) => ({ ...prev, id: slugify(e.target.value) }))}
                        className="mt-2 w-full rounded-2xl border border-white/10 bg-[#08111f] px-4 py-3 text-white outline-none focus:border-amber-300/50"
                      />
                    </label>
                  </div>

                  <label className="mt-4 block">
                    <span className="text-xs uppercase tracking-[0.24em] text-slate-400">Role Summary</span>
                    <textarea
                      value={draft.overrides.summary ?? ''}
                      onChange={(e) =>
                        setDraft((prev) => ({
                          ...prev,
                          overrides: { ...prev.overrides, summary: e.target.value },
                        }))
                      }
                      rows={3}
                      className="mt-2 w-full rounded-2xl border border-white/10 bg-[#08111f] px-4 py-3 text-white outline-none focus:border-amber-300/50"
                    />
                  </label>

                  <div className="mt-6">
                    <div className="text-xs uppercase tracking-[0.24em] text-slate-400">Primary Class</div>
                    <div className="mt-3 grid gap-4 md:grid-cols-2">
                      {classes.map((classItem) => (
                        <ClassCard
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
                    <div className="flex items-center gap-2 text-xs uppercase tracking-[0.24em] text-slate-400">
                      <Swords className="h-4 w-4" />
                      Secondary Classes
                    </div>
                    <div className="mt-3 flex flex-wrap gap-3">
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
                                'rounded-full border px-4 py-2 text-sm transition',
                                selected
                                  ? 'border-amber-300/60 bg-amber-300/12 text-amber-100'
                                  : 'border-white/12 bg-white/4 text-slate-300 hover:bg-white/10',
                              ].join(' ')}
                            >
                              {classItem.name}
                            </button>
                          );
                        })}
                    </div>
                  </div>

                  <div className="mt-6 rounded-[28px] border border-emerald-300/20 bg-emerald-300/8 p-5">
                    <div className="flex items-center gap-2 text-xs uppercase tracking-[0.24em] text-emerald-100/80">
                      <CheckCircle2 className="h-4 w-4" />
                      Review Build
                    </div>
                    <div className="mt-4 space-y-3 text-sm text-slate-200">
                      <p>
                        <span className="text-slate-400">Primary:</span> {primaryClass?.name ?? 'Unassigned'}
                      </p>
                      <p>
                        <span className="text-slate-400">Focus:</span>{' '}
                        The primary class leads the agent&apos;s default emphasis, taste, and decision-making style.
                      </p>
                      <p>
                        <span className="text-slate-400">Secondary:</span>{' '}
                        {selectedSecondaryClasses.length > 0
                          ? selectedSecondaryClasses
                              .map((id) => classes.find((item) => item.id === id)?.name ?? id)
                              .join(' / ')
                          : 'None'}
                      </p>
                      <p>
                        <span className="text-slate-400">Soul voice:</span>{' '}
                        {wizardProfilePreview.map((item) => item.default_soul_overlay.voice).filter(Boolean).join(' / ') || 'Base voice'}
                      </p>
                      <p>
                        <span className="text-slate-400">Tool grants:</span>{' '}
                        {Array.from(
                          new Set(wizardProfilePreview.flatMap((item) => item.tool_grants)),
                        )
                          .slice(0, 6)
                          .join(', ') || 'No tools yet'}
                      </p>
                    </div>
                  </div>

                  <div className="mt-6 flex flex-wrap gap-4">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={handleCreateAgent}
                      className="inline-flex items-center gap-2 rounded-full bg-amber-300 px-6 py-3 text-sm font-semibold text-slate-900 transition hover:bg-amber-200 disabled:opacity-60"
                    >
                      {busy ? 'Forging agent...' : 'Finish and Enter Studio'}
                      <ArrowRight className="h-4 w-4" />
                    </button>
                    <button
                      type="button"
                      onClick={() => setStage('intro')}
                      className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm font-semibold text-white/90 hover:bg-white/8"
                    >
                      Back
                    </button>
                  </div>
                </div>
              </div>
            )}
          </section>
        )}

        {stage === 'studio' && (
          <section className="space-y-8">
            <div className="grid gap-6 xl:grid-cols-[1.2fr_0.8fr]">
              <div
                className={macClassNames(
                  'rounded-[36px] border border-white/10 bg-white/6 p-8 backdrop-blur',
                  'rounded-[32px] border border-white/55 bg-white/72 p-9 shadow-[0_18px_54px_rgba(36,48,74,0.12)] backdrop-blur-2xl dark:border-white/12 dark:bg-white/8',
                  isDesktopMac,
                )}
              >
                <div
                  className={macClassNames(
                    'inline-flex items-center gap-2 rounded-full border border-cyan-300/20 bg-cyan-300/10 px-4 py-2 text-xs uppercase tracking-[0.28em] text-cyan-100',
                    'inline-flex items-center gap-2 rounded-full border border-sky-300/30 bg-sky-200/25 px-4 py-2 text-[11px] font-medium uppercase tracking-[0.24em] text-sky-800 dark:border-sky-300/18 dark:bg-sky-300/10 dark:text-sky-100',
                    isDesktopMac,
                  )}
                >
                  <Orbit className="h-4 w-4" />
                  Agent Studio
                </div>
                <h1
                  className={macClassNames(
                    'mt-6 font-serif text-5xl leading-tight text-white',
                    'mt-6 max-w-4xl text-[clamp(3rem,6vw,4.8rem)] font-semibold leading-[0.94] tracking-[-0.07em] text-slate-900 dark:text-white',
                    isDesktopMac,
                  )}
                >
                  {activeProfile?.profile.name ?? 'Tanith'} is live and ready.
                </h1>
                <p
                  className={macClassNames(
                    'mt-4 max-w-3xl text-lg leading-8 text-slate-300',
                    'mt-4 max-w-3xl text-[1.1rem] leading-8 text-slate-600 dark:text-slate-300',
                    isDesktopMac,
                  )}
                >
                  Switch agents, inspect their class loadouts, and then drop into the comprehensive workspace when
                  you want tools, logs, memory, and runtime administration.
                </p>

                <div className="mt-8 flex flex-wrap gap-4">
                  <Link
                    to="/dashboard"
                    className="inline-flex items-center gap-2 rounded-full bg-amber-300 px-6 py-3 text-sm font-semibold text-slate-900 transition hover:bg-amber-200"
                  >
                    Open Control Center
                    <ArrowRight className="h-4 w-4" />
                  </Link>
                  <Link
                    to="/agent"
                    className={macClassNames(
                      'inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm font-semibold text-white/90 hover:bg-white/8',
                      'inline-flex items-center gap-2 rounded-full border border-slate-300/70 bg-white/60 px-6 py-3 text-sm font-semibold text-slate-800 hover:bg-white/85 dark:border-white/14 dark:bg-white/6 dark:text-white',
                      isDesktopMac,
                    )}
                  >
                    Open Agent Chat
                  </Link>
                </div>
              </div>

              <div
                className={macClassNames(
                  'rounded-[36px] border border-white/10 bg-[#08111f]/85 p-6',
                  'rounded-[32px] border border-white/55 bg-white/76 p-6 shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] dark:border-white/10 dark:bg-white/7',
                  isDesktopMac,
                )}
              >
                <div className="text-[11px] uppercase tracking-[0.28em] text-slate-400">Active Loadout</div>
                {activeProfile ? (
                  <div className="mt-4 space-y-4">
                    <div className={macClassNames('rounded-3xl border border-white/8 bg-white/4 p-4', 'rounded-[24px] border border-white/60 bg-white/72 p-4 dark:border-white/10 dark:bg-white/5', isDesktopMac)}>
                      <div className="text-xs uppercase tracking-[0.24em] text-slate-400">Classes</div>
                      <p className="mt-2 text-lg font-semibold text-white">
                        {activeProfile.classes.map((item) => item.name).join(' / ')}
                      </p>
                      <p className="mt-2 text-sm leading-6 text-slate-400">
                        Primary class sets the main focus. Secondary classes expand support skills and tool access.
                      </p>
                    </div>
                    <div className={macClassNames('rounded-3xl border border-white/8 bg-white/4 p-4', 'rounded-[24px] border border-white/60 bg-white/72 p-4 dark:border-white/10 dark:bg-white/5', isDesktopMac)}>
                      <div className="text-xs uppercase tracking-[0.24em] text-slate-400">Role Summary</div>
                      <p className="mt-2 text-sm leading-6 text-slate-300">{activeProfile.summary}</p>
                    </div>
                    <div className={macClassNames('rounded-3xl border border-white/8 bg-white/4 p-4', 'rounded-[24px] border border-white/60 bg-white/72 p-4 dark:border-white/10 dark:bg-white/5', isDesktopMac)}>
                      <div className="text-xs uppercase tracking-[0.24em] text-slate-400">Guardrails</div>
                      <ul className="mt-2 space-y-2 text-sm text-slate-300">
                        {activeProfile.guardrails.slice(0, 3).map((item) => (
                          <li key={item}>• {item}</li>
                        ))}
                      </ul>
                    </div>
                    <div className={macClassNames('rounded-3xl border border-amber-300/20 bg-amber-300/10 p-4', 'rounded-[24px] border border-amber-300/35 bg-amber-100/55 p-4 dark:border-amber-300/20 dark:bg-amber-300/10', isDesktopMac)}>
                      <div className="text-xs uppercase tracking-[0.24em] text-amber-100/80">Startup Target</div>
                      <p className="mt-2 text-sm leading-6 text-amber-50/90">
                        The macOS startup service will boot the bot marked for startup. Only one bot can own startup at a time.
                      </p>
                    </div>
                  </div>
                ) : (
                  <p className="mt-4 text-sm text-slate-400">No active profile available yet.</p>
                )}
              </div>
            </div>

            <div className="grid gap-6 lg:grid-cols-[1fr_0.95fr]">
              <div className="rounded-[36px] border border-white/10 bg-white/5 p-6">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-[11px] uppercase tracking-[0.28em] text-slate-400">Roster</div>
                    <h2 className="mt-2 text-2xl font-semibold text-white">Agent Builds</h2>
                  </div>
                </div>
                <div className="mt-6 grid gap-4">
                  {agents.map((agent) => {
                    const active = agent.profile.id === activeAgentId;
                    return (
                      <div
                        key={agent.profile.id}
                        className="rounded-[28px] border border-white/10 bg-[#08111f]/75 p-5"
                      >
                        <div className="flex flex-wrap items-start justify-between gap-4">
                          <div className="flex items-start gap-4">
                            <div className="flex h-20 w-20 shrink-0 items-center justify-center overflow-hidden rounded-[22px] border border-white/10 bg-white/6">
                              {classArtwork(agent.profile.primary_class) ? (
                                <img
                                  src={classArtwork(agent.profile.primary_class) ?? undefined}
                                  alt={`${agent.profile.name} primary class portrait`}
                                  className="h-full w-full object-cover"
                                />
                              ) : (
                                <span className="text-2xl">{agent.identity.emoji}</span>
                              )}
                            </div>
                            <div>
                            <div className="text-[11px] uppercase tracking-[0.24em] text-slate-400">
                              {agent.identity.role_title}
                            </div>
                            <h3 className="mt-2 text-xl font-semibold text-white">
                              {agent.identity.emoji} {agent.profile.name}
                            </h3>
                            <p className="mt-3 text-sm leading-6 text-slate-300">{agent.summary}</p>
                            <p className="mt-3 text-xs uppercase tracking-[0.22em] text-slate-500">
                              {agent.classes
                                .map((item, index) => `${item.name}${index === 0 ? ' (Primary)' : ''}`)
                                .join(' / ')}
                            </p>
                            </div>
                          </div>
                          <div className="flex flex-col items-end gap-3">
                            <span
                              className={[
                                'rounded-full px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em]',
                                active ? 'bg-emerald-400/15 text-emerald-200' : 'bg-white/8 text-white/70',
                              ].join(' ')}
                            >
                              {active ? 'Active' : 'Standby'}
                            </span>
                            {agent.profile.launch_on_startup && (
                              <span className="rounded-full bg-amber-300/15 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-amber-100">
                                Startup
                              </span>
                            )}
                            {!active && (
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => handleActivate(agent.profile.id)}
                                className="rounded-full border border-white/15 px-4 py-2 text-sm font-semibold text-white/90 hover:bg-white/8 disabled:opacity-60"
                              >
                                Activate
                              </button>
                            )}
                          </div>
                        </div>
                        <div className="mt-5 flex items-center justify-between rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
                          <div>
                            <p className="text-sm font-semibold text-white">Launch this bot at startup</p>
                            <p className="mt-1 text-sm text-slate-400">
                              Enabling this makes it the startup target for the local launchd service.
                            </p>
                          </div>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handleStartupToggle(agent, !agent.profile.launch_on_startup)}
                            className={[
                              'relative h-8 w-14 rounded-full border transition disabled:opacity-60',
                              agent.profile.launch_on_startup
                                ? 'border-amber-300/50 bg-amber-300/20'
                                : 'border-white/12 bg-white/8',
                            ].join(' ')}
                          >
                            <span
                              className={[
                                'absolute top-1 h-5 w-5 rounded-full bg-white transition',
                                agent.profile.launch_on_startup ? 'left-8' : 'left-1',
                              ].join(' ')}
                            />
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="rounded-[36px] border border-white/10 bg-white/5 p-6">
                <div className="text-[11px] uppercase tracking-[0.28em] text-slate-400">Class Gallery</div>
                <h2 className="mt-2 text-2xl font-semibold text-white">Current and Upcoming Classes</h2>
                <div className="mt-6 grid gap-4">
                  {classes.map((classItem) => (
                    <ClassCard
                      key={classItem.id}
                      classItem={classItem}
                      selected={Boolean(activeProfile?.classes.some((item) => item.id === classItem.id))}
                      disabled
                    />
                  ))}
                </div>
              </div>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
