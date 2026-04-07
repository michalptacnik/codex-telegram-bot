import { useEffect, useMemo, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { ArrowLeft, ArrowRight, CheckCircle2, Rocket, ShieldCheck, Sparkles } from 'lucide-react';
import {
  MacBadge,
  MacEmptyState,
  MacPanel,
  MacStat,
} from '@/components/macos/MacPrimitives';
import { useShell } from '@/components/shell/ShellProvider';
import socialMediaManagerArt from '@/assets/class-social-media-manager.png';
import salesArt from '@/assets/class-sales.png';
import vaArt from '@/assets/class-va.png';
import type { AgentClassManifest, AgentProfile, OnboardingBootstrapResponse } from '@/types/api';

type WizardStepId = 'role' | 'identity' | 'startup' | 'review';

interface AgentSetupWizardProps {
  classes: AgentClassManifest[];
  draft: AgentProfile;
  setDraft: Dispatch<SetStateAction<AgentProfile>>;
  busy: boolean;
  onSubmit: () => Promise<void> | void;
  onCancel: () => void;
  bootstrap?: OnboardingBootstrapResponse | null;
  mode?: 'setup' | 'studio';
}

const WIZARD_STEPS: Array<{ id: WizardStepId; label: string; title: string; detail: string }> = [
  {
    id: 'role',
    label: 'Role',
    title: 'What should this agent focus on?',
    detail: 'Pick the kind of work this agent should own first. You can add more agents later.',
  },
  {
    id: 'identity',
    label: 'Identity',
    title: 'Name the agent and shape the brief.',
    detail: 'Keep it simple. This should read like the setup screen of a personal computer, not a configuration file.',
  },
  {
    id: 'startup',
    label: 'Startup',
    title: 'Decide how this agent should start.',
    detail: 'Choose whether this agent should become the default profile when Agent HQ opens.',
  },
  {
    id: 'review',
    label: 'Review',
    title: 'Review the setup before creating the agent.',
    detail: 'Confirm the role, startup behavior, and operating style, then finish in one click.',
  },
];

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

function panelFieldClass(isDesktopMac: boolean): string {
  return isDesktopMac
    ? 'mt-2 w-full rounded-[1rem] border border-[rgba(15,23,42,0.08)] bg-white/80 px-4 py-3 text-[0.97rem] text-slate-900 outline-none shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] focus:border-sky-400 dark:border-white/10 dark:bg-black/20 dark:text-white'
    : 'mt-2 w-full rounded-[1rem] border border-white/10 bg-[#0b1120] px-4 py-3 text-[0.97rem] text-white outline-none focus:border-sky-400';
}

function actionButtonClass(primary: boolean, isDesktopMac: boolean): string {
  if (primary) {
    return isDesktopMac
      ? 'inline-flex items-center gap-2 rounded-full bg-[#0a84ff] px-5 py-2.5 text-sm font-semibold text-white shadow-[0_12px_24px_rgba(10,132,255,0.22)] transition hover:brightness-105 disabled:opacity-50'
      : 'inline-flex items-center gap-2 rounded-full bg-sky-500 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-400 disabled:opacity-50';
  }
  return isDesktopMac
    ? 'inline-flex items-center gap-2 rounded-full border border-[rgba(15,23,42,0.08)] bg-white/70 px-5 py-2.5 text-sm font-semibold text-slate-800 transition hover:bg-white dark:border-white/10 dark:bg-white/6 dark:text-white disabled:opacity-50'
    : 'inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-white/10 disabled:opacity-50';
}

function choiceCardClass(selected: boolean, disabled = false): string {
  return [
    'w-full rounded-[1.25rem] border px-4 py-4 text-left transition',
    selected
      ? 'border-sky-400/50 bg-sky-500/10 shadow-[0_12px_26px_rgba(56,189,248,0.12)]'
      : 'border-[var(--shell-border)] bg-[var(--shell-panel)] hover:bg-[var(--shell-selection)]',
    disabled ? 'cursor-not-allowed opacity-55' : 'cursor-pointer',
  ].join(' ');
}

function inferSummaryForClass(classItem?: AgentClassManifest): string {
  return classItem?.default_role_summary ?? 'Handle practical work across the workspace with reliable follow-through.';
}

export default function AgentSetupWizard({
  classes,
  draft,
  setDraft,
  busy,
  onSubmit,
  onCancel,
  bootstrap,
  mode = 'setup',
}: AgentSetupWizardProps) {
  const { isDesktopMac } = useShell();
  const [stepIndex, setStepIndex] = useState(0);
  const activeStep = WIZARD_STEPS[stepIndex]!;

  const activeClass = useMemo(
    () => classes.find((item) => item.id === draft.primary_class),
    [classes, draft.primary_class],
  );

  const readyClasses = useMemo(
    () => classes.filter((item) => item.status === 'active'),
    [classes],
  );

  const startupChoice = draft.launch_on_startup ? 'startup' : 'manual';

  useEffect(() => {
    if (!draft.id?.trim() || draft.id === 'agent') {
      setDraft((prev) => ({
        ...prev,
        id: slugify(prev.name) || prev.id || 'agent',
      }));
    }
  }, [draft.id, draft.name, setDraft]);

  useEffect(() => {
    if ((!draft.overrides.summary || draft.overrides.summary.trim() === '') && activeClass) {
      setDraft((prev) => ({
        ...prev,
        overrides: { ...prev.overrides, summary: inferSummaryForClass(activeClass) },
      }));
    }
  }, [activeClass, draft.overrides.summary, setDraft]);

  const classSignals = useMemo(() => {
    if (!activeClass) {
      return { tools: [], guardrails: [], channels: [] as string[] };
    }
    return {
      tools: activeClass.tool_grants.slice(0, 8),
      guardrails: activeClass.guardrails.slice(0, 3),
      channels: activeClass.channel_affinities.slice(0, 3),
    };
  }, [activeClass]);

  const stepCanContinue = useMemo(() => {
    switch (activeStep.id) {
      case 'role':
        return Boolean(activeClass);
      case 'identity':
        return draft.name.trim().length > 0 && draft.id.trim().length > 0;
      case 'startup':
      case 'review':
        return true;
      default:
        return false;
    }
  }, [activeClass, activeStep.id, draft.id, draft.name]);

  const handleNext = () => {
    if (!stepCanContinue) return;
    setStepIndex((prev) => Math.min(prev + 1, WIZARD_STEPS.length - 1));
  };

  const handleBack = () => {
    if (stepIndex === 0) {
      onCancel();
      return;
    }
    setStepIndex((prev) => Math.max(prev - 1, 0));
  };

  const handleClassSelect = (classItem: AgentClassManifest) => {
    if (classItem.status !== 'active') return;
    setDraft((prev) => ({
      ...prev,
      primary_class: classItem.id,
      overrides: {
        ...prev.overrides,
        summary:
          !prev.overrides.summary || prev.overrides.summary === inferSummaryForClass(activeClass)
            ? classItem.default_role_summary
            : prev.overrides.summary,
      },
    }));
  };

  const handleSubmit = async () => {
    await onSubmit();
  };

  return (
    <div className="grid gap-4 xl:grid-cols-[1.12fr_0.88fr]">
      <MacPanel
        title={activeStep.title}
        detail={activeStep.detail}
        className="min-h-[34rem]"
      >
        <div className="mb-6 grid gap-3 md:grid-cols-4">
          {WIZARD_STEPS.map((step, index) => {
            const state =
              index < stepIndex ? 'done' : index === stepIndex ? 'current' : 'upcoming';
            return (
              <div
                key={step.id}
                className={[
                  'rounded-[1.2rem] border px-4 py-3 transition',
                  state === 'current'
                    ? 'border-sky-400/50 bg-sky-500/10'
                    : 'border-[var(--shell-border)] bg-[var(--shell-panel)]',
                ].join(' ')}
              >
                <div className="flex items-center gap-2">
                  {state === 'done' ? (
                    <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                  ) : (
                    <div className="flex h-5 w-5 items-center justify-center rounded-full bg-[var(--shell-selection)] text-[0.68rem] font-semibold">
                      {index + 1}
                    </div>
                  )}
                  <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                    {step.label}
                  </p>
                </div>
              </div>
            );
          })}
        </div>

        {activeStep.id === 'role' ? (
          <div className="space-y-4">
            <div className="grid gap-3">
              {readyClasses.map((classItem, index) => (
                <button
                  key={classItem.id}
                  type="button"
                  onClick={() => handleClassSelect(classItem)}
                  className={choiceCardClass(draft.primary_class === classItem.id)}
                >
                  <div className="flex items-start gap-4">
                    {classArtwork(classItem.id) ? (
                      <img
                        src={classArtwork(classItem.id) ?? undefined}
                        alt={classItem.name}
                        className="h-16 w-16 shrink-0 rounded-[1rem] object-contain p-2 studio-art-frame"
                      />
                    ) : (
                      <div className="h-16 w-16 shrink-0 rounded-[1rem] bg-[var(--shell-selection)]" />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h3 className="text-sm font-semibold">{classItem.name}</h3>
                        <MacBadge tone="accent">{index === 0 ? 'Recommended' : 'Available now'}</MacBadge>
                        <MacBadge tone="neutral">{classItem.fantasy_theme}</MacBadge>
                      </div>
                      <p className="mt-2 text-sm text-[var(--shell-muted)]">{classItem.description}</p>
                      <p className="mt-3 text-sm font-medium">{classItem.default_role_summary}</p>
                    </div>
                  </div>
                </button>
              ))}
            </div>

            {classes.some((item) => item.status === 'coming_soon') ? (
              <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  More classes later
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {classes
                    .filter((item) => item.status === 'coming_soon')
                    .map((item) => (
                      <MacBadge key={item.id} tone="neutral">{item.name}</MacBadge>
                    ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        {activeStep.id === 'identity' ? (
          <div className="space-y-5">
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
                  placeholder="Noa"
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
                  placeholder="noa"
                />
              </label>
            </div>

            <label className="block text-sm">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                Role summary
              </span>
              <textarea
                rows={5}
                value={draft.overrides.summary ?? ''}
                onChange={(event) =>
                  setDraft((prev) => ({
                    ...prev,
                    overrides: { ...prev.overrides, summary: event.target.value },
                  }))
                }
                className={panelFieldClass(isDesktopMac)}
                placeholder="Describe the type of work this agent should own."
              />
            </label>

            <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                What this controls
              </p>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">
                The name becomes the roster label, and the summary becomes the short brief used across setup, dashboards, and startup ownership.
              </p>
            </div>
          </div>
        ) : null}

        {activeStep.id === 'startup' ? (
          <div className="space-y-4">
            {[
              {
                id: 'startup',
                title: 'Launch this agent by default',
                detail: 'Best for a flagship everyday agent. Agent HQ will treat this profile as the startup owner.',
                icon: Rocket,
              },
              {
                id: 'manual',
                title: 'Keep startup manual',
                detail: 'Best if this is a specialist agent you only want to use sometimes.',
                icon: ShieldCheck,
              },
            ].map(({ id, title, detail, icon: Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() =>
                  setDraft((prev) => ({ ...prev, launch_on_startup: id === 'startup' }))
                }
                className={choiceCardClass(startupChoice === id)}
              >
                <div className="flex items-start gap-4">
                  <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[0.95rem] bg-[var(--shell-selection)]">
                    <Icon className="h-5 w-5 text-[#0a84ff]" />
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold">{title}</h3>
                    <p className="mt-2 text-sm text-[var(--shell-muted)]">{detail}</p>
                  </div>
                </div>
              </button>
            ))}

            {bootstrap?.active_profile ? (
              <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  Current lead profile
                </p>
                <p className="mt-2 text-sm text-[var(--shell-muted)]">
                  {bootstrap.active_profile.profile.name} is currently the default workspace owner.
                </p>
              </div>
            ) : null}
          </div>
        ) : null}

        {activeStep.id === 'review' ? (
          <div className="space-y-4">
            <div className="grid gap-3 md:grid-cols-2">
              <MacStat label="Agent" value={draft.name || 'Unnamed'} detail={draft.id || 'No id yet'} />
              <MacStat
                label="Startup"
                value={draft.launch_on_startup ? 'Default launch' : 'Manual only'}
                detail="Desktop startup behavior"
              />
            </div>

            <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                Role summary
              </p>
              <p className="mt-2 text-sm">{draft.overrides.summary ?? ''}</p>
            </div>

            <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                Selected class
              </p>
              <div className="mt-3 flex items-start gap-4">
                {activeClass && classArtwork(activeClass.id) ? (
                  <img
                    src={classArtwork(activeClass.id) ?? undefined}
                    alt={activeClass.name}
                    className="h-16 w-16 rounded-[1rem] object-contain p-2 studio-art-frame"
                  />
                ) : null}
                <div>
                  <h3 className="text-sm font-semibold">{activeClass?.name ?? 'Unassigned'}</h3>
                  <p className="mt-2 text-sm text-[var(--shell-muted)]">{activeClass?.description}</p>
                </div>
              </div>
            </div>
          </div>
        ) : null}

        <div className="mt-8 flex flex-wrap gap-3">
          <button
            type="button"
            onClick={handleBack}
            className={actionButtonClass(false, isDesktopMac)}
            disabled={busy}
          >
            <ArrowLeft className="h-4 w-4" />
            {stepIndex === 0 ? 'Cancel' : 'Back'}
          </button>

          {activeStep.id === 'review' ? (
            <button
              type="button"
              onClick={() => void handleSubmit()}
              className={actionButtonClass(true, isDesktopMac)}
              disabled={busy}
            >
              {busy ? 'Creating agent...' : mode === 'studio' ? 'Create Agent' : 'Finish Setup'}
              <ArrowRight className="h-4 w-4" />
            </button>
          ) : (
            <button
              type="button"
              onClick={handleNext}
              className={actionButtonClass(true, isDesktopMac)}
              disabled={busy || !stepCanContinue}
            >
              Next
              <ArrowRight className="h-4 w-4" />
            </button>
          )}
        </div>
      </MacPanel>

      <div className="grid gap-4">
        <MacPanel title="Live preview" detail="A setup assistant should explain what each choice changes before you commit.">
          {activeClass ? (
            <div className="space-y-4">
              <MacStat
                label="Primary class"
                value={activeClass.name}
                detail={activeClass.fantasy_theme}
              />

              <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  Soul voice
                </p>
                <p className="mt-2 text-sm text-[var(--shell-muted)]">
                  {activeClass.default_soul_overlay.voice ?? 'Base voice'}
                </p>
              </div>

              <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  Best channels
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {classSignals.channels.length > 0 ? (
                    classSignals.channels.map((channel) => (
                      <MacBadge key={channel} tone="neutral">{channel}</MacBadge>
                    ))
                  ) : (
                    <p className="text-sm text-[var(--shell-muted)]">General workspace usage</p>
                  )}
                </div>
              </div>

              <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  Tool grants
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {classSignals.tools.map((tool) => (
                    <MacBadge key={tool} tone="neutral">{tool}</MacBadge>
                  ))}
                </div>
              </div>

              <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  Guardrails
                </p>
                <div className="mt-3 space-y-2 text-sm text-[var(--shell-muted)]">
                  {classSignals.guardrails.map((guardrail) => (
                    <p key={guardrail}>{guardrail}</p>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <MacEmptyState
              icon={<Sparkles className="h-7 w-7" />}
              title="Pick a role to continue"
              description="Each step will show what changes before you move forward."
            />
          )}
        </MacPanel>

        <MacPanel title="Why this flow" detail="This setup now behaves like a device setup assistant: one decision per screen, with a clear next action.">
          <div className="space-y-3 text-sm text-[var(--shell-muted)]">
            <p>Choose the kind of work first, then give the agent a name, then decide startup ownership.</p>
            <p>The last screen is just a confirmation pass, not another place to discover hidden settings.</p>
          </div>
        </MacPanel>
      </div>
    </div>
  );
}
