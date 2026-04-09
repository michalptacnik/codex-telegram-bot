import { useEffect, useMemo, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { ArrowLeft, ArrowRight, CheckCircle2, Rocket, ShieldCheck, Sparkles } from 'lucide-react';
import { MacBadge, MacEmptyState, MacPanel, MacStat } from '@/components/macos/MacPrimitives';
import { useShell } from '@/components/shell/ShellProvider';
import { applySetupConfig, parseSetupConfig, type SetupConfigDraft } from '@/lib/setupConfig';
import socialMediaManagerArt from '@/assets/class-social-media-manager.png';
import salesArt from '@/assets/class-sales.png';
import vaArt from '@/assets/class-va.png';
import type { AgentClassManifest, AgentProfile, OnboardingBootstrapResponse } from '@/types/api';

type WizardStepId = 'focus' | 'access' | 'channels' | 'name' | 'launch' | 'confirm';

interface AgentSetupWizardProps {
  classes: AgentClassManifest[];
  draft: AgentProfile;
  setDraft: Dispatch<SetStateAction<AgentProfile>>;
  busy: boolean;
  onSubmit: () => Promise<void> | void;
  onCancel: () => void;
  bootstrap?: OnboardingBootstrapResponse | null;
  mode?: 'setup' | 'studio';
  configText?: string | null;
  onSaveConfig?: (nextConfig: string) => Promise<void>;
}

const WIZARD_STEPS: Array<{ id: WizardStepId; label: string; title: string; detail: string }> = [
  {
    id: 'focus',
    label: 'Focus',
    title: 'What should this assistant help with first?',
    detail: 'Pick the kind of work you want covered from day one. You can always add more agents later.',
  },
  {
    id: 'access',
    label: 'Access',
    title: 'How should this assistant access a model?',
    detail: 'Choose whether to use your existing setup, a DeepSeek API key, or an official Codex session.',
  },
  {
    id: 'channels',
    label: 'Channels',
    title: 'Where should this assistant be reachable?',
    detail: 'Turn on the channels you actually plan to use. Telegram belongs here, not buried in raw config.',
  },
  {
    id: 'name',
    label: 'Name',
    title: 'Give the assistant a name and a short brief.',
    detail: 'This is the part that should feel personal: what to call the assistant and how to describe her job.',
  },
  {
    id: 'launch',
    label: 'Launch',
    title: 'Should she open as your default assistant?',
    detail: 'Choose whether this should be your everyday default or a specialist you open only when needed.',
  },
  {
    id: 'confirm',
    label: 'Confirm',
    title: 'Review the setup and create the assistant.',
    detail: 'One last pass so you can confirm the starter setup before Agent HQ creates the profile.',
  },
];

const FOCUS_COPY: Record<string, { headline: string; prompt: string }> = {
  va: {
    headline: 'Stay on top of admin and follow-through',
    prompt: 'Best for inboxes, scheduling, reminders, practical research, and daily organization.',
  },
  social_media_manager: {
    headline: 'Run content and social momentum',
    prompt: 'Best for planning posts, writing drafts, managing campaigns, and keeping channels active.',
  },
  sales: {
    headline: 'Build and qualify pipeline',
    prompt: 'Best for prospecting, target research, outreach prep, and keeping handoffs organized.',
  },
};

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

function choiceCardClass(selected: boolean): string {
  return [
    'w-full rounded-[1.25rem] border px-4 py-4 text-left transition',
    selected
      ? 'border-sky-400/50 bg-sky-500/10 shadow-[0_12px_26px_rgba(56,189,248,0.12)]'
      : 'border-[var(--shell-border)] bg-[var(--shell-panel)] hover:bg-[var(--shell-selection)]',
    'cursor-pointer',
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
  configText,
  onSaveConfig,
}: AgentSetupWizardProps) {
  const { isDesktopMac } = useShell();
  const [stepIndex, setStepIndex] = useState(0);
  const [setupError, setSetupError] = useState<string | null>(null);
  const activeStep = WIZARD_STEPS[stepIndex]!;
  const [configDraft, setConfigDraft] = useState<SetupConfigDraft | null>(null);

  const readyClasses = useMemo(
    () => classes.filter((item) => item.status === 'active'),
    [classes],
  );

  const activeClass = useMemo(() => {
    const preferred = readyClasses.find((item) => item.id === draft.primary_class);
    return preferred ?? readyClasses[0] ?? null;
  }, [draft.primary_class, readyClasses]);

  const startupChoice = draft.launch_on_startup ? 'default' : 'manual';

  useEffect(() => {
    if (!configText) return;
    setConfigDraft(parseSetupConfig(configText));
  }, [configText]);

  useEffect(() => {
    if (!draft.id?.trim() || draft.id === 'agent') {
      setDraft((prev) => ({
        ...prev,
        id: slugify(prev.name) || prev.id || 'agent',
      }));
    }
  }, [draft.id, draft.name, setDraft]);

  useEffect(() => {
    if (activeClass && draft.primary_class !== activeClass.id) {
      setDraft((prev) => ({
        ...prev,
        primary_class: activeClass.id,
      }));
    }
  }, [activeClass, draft.primary_class, setDraft]);

  useEffect(() => {
    if ((!draft.overrides.summary || draft.overrides.summary.trim() === '') && activeClass) {
      setDraft((prev) => ({
        ...prev,
        overrides: { ...prev.overrides, summary: inferSummaryForClass(activeClass) },
      }));
    }
  }, [activeClass, draft.overrides.summary, setDraft]);

  const stepCanContinue = useMemo(() => {
    switch (activeStep.id) {
      case 'focus':
        return readyClasses.length > 0 && Boolean(activeClass);
      case 'access':
        if (!configDraft) return false;
        if (configDraft.accessMode === 'existing') {
          return Boolean(configDraft.provider && configDraft.model);
        }
        if (configDraft.accessMode === 'deepseek') {
          return Boolean(
            configDraft.model.trim() &&
            (configDraft.apiKey.trim() || configDraft.hasExistingApiKey),
          );
        }
        if (configDraft.accessMode === 'codex') {
          return Boolean(configDraft.model.trim());
        }
        return false;
      case 'channels':
        if (!configDraft) return false;
        return !configDraft.telegramEnabled || Boolean(
          (configDraft.telegramBotToken.trim() || configDraft.hasExistingTelegramToken) &&
          configDraft.telegramAllowedUsers.trim(),
        );
      case 'name':
        return draft.name.trim().length > 0 && draft.id.trim().length > 0;
      case 'launch':
      case 'confirm':
        return true;
      default:
        return false;
    }
  }, [activeClass, activeStep.id, configDraft, draft.id, draft.name, readyClasses.length]);

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
    setDraft((prev) => ({
      ...prev,
      primary_class: classItem.id,
      overrides: {
        ...prev.overrides,
        summary:
          !prev.overrides.summary || prev.overrides.summary === inferSummaryForClass(activeClass ?? undefined)
            ? classItem.default_role_summary
            : prev.overrides.summary,
      },
    }));
  };

  const handleSubmit = async () => {
    setSetupError(null);
    if (configDraft && configText && onSaveConfig) {
      const nextConfig = applySetupConfig(configText, configDraft);
      await onSaveConfig(nextConfig);
    }
    await onSubmit();
  };

  const selectedFocus = activeClass ? FOCUS_COPY[activeClass.id] : null;

  return (
    <div className="grid gap-4 xl:grid-cols-[1.12fr_0.88fr]">
      <MacPanel title={activeStep.title} detail={activeStep.detail} className="min-h-[34rem]">
        {setupError ? (
          <div className="mb-4 rounded-[1.4rem] border border-rose-300/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-200">
            {setupError}
          </div>
        ) : null}
        <div className="mb-6 grid gap-3 md:grid-cols-3 xl:grid-cols-6">
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

        {activeStep.id === 'focus' ? (
          readyClasses.length > 0 ? (
            <div className="grid gap-3">
              {readyClasses.map((classItem) => {
                const copy = FOCUS_COPY[classItem.id];
                return (
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
                          <h3 className="text-base font-semibold">
                            {copy?.headline ?? classItem.name}
                          </h3>
                          <MacBadge tone="accent">
                            {classItem.id === 'va' ? 'Best first assistant' : 'Starter option'}
                          </MacBadge>
                        </div>
                        <p className="mt-2 text-sm text-[var(--shell-muted)]">
                          {copy?.prompt ?? classItem.description}
                        </p>
                        <p className="mt-3 text-sm font-medium">{classItem.default_role_summary}</p>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          ) : (
            <MacEmptyState
              icon={<Sparkles className="h-7 w-7" />}
              title="No starter options available"
              description="Agent HQ could not load any starter assistant templates, so setup cannot continue yet."
            />
          )
        ) : null}

        {activeStep.id === 'access' && configDraft ? (
          <div className="space-y-4">
            {[
              {
                id: 'existing',
                title: 'Use current setup',
                detail: 'Keep the model access that Agent HQ already has configured.',
              },
              {
                id: 'deepseek',
                title: 'Use a DeepSeek API key',
                detail: 'Good when you want to paste a key directly and run this assistant on DeepSeek.',
              },
              {
                id: 'codex',
                title: 'Use my Codex session',
                detail: 'Uses the official Codex session on this Mac. Agent HQ does not do direct ChatGPT OAuth itself.',
              },
            ].map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() =>
                  setConfigDraft((prev) =>
                    prev
                      ? {
                          ...prev,
                          accessMode: option.id as SetupConfigDraft['accessMode'],
                          provider:
                            option.id === 'codex'
                              ? 'openai-codex'
                              : option.id === 'deepseek'
                                ? 'deepseek'
                                : prev.provider,
                          model:
                            option.id === 'codex'
                              ? prev.model || 'gpt-5-codex'
                              : option.id === 'deepseek'
                                ? prev.model || 'deepseek-chat'
                                : prev.model,
                        }
                      : prev,
                  )
                }
                className={choiceCardClass(configDraft.accessMode === option.id)}
              >
                <h3 className="text-sm font-semibold">{option.title}</h3>
                <p className="mt-2 text-sm text-[var(--shell-muted)]">{option.detail}</p>
              </button>
            ))}

            {configDraft.accessMode !== 'existing' ? (
              <div className="grid gap-4 md:grid-cols-2">
                <label className="block text-sm">
                  <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                    Provider
                  </span>
                  <input
                    value={configDraft.provider}
                    onChange={(event) =>
                      setConfigDraft((prev) => (prev ? { ...prev, provider: event.target.value } : prev))
                    }
                    className={panelFieldClass(isDesktopMac)}
                    placeholder="deepseek or openai-codex"
                  />
                </label>

                <label className="block text-sm">
                  <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                    Model
                  </span>
                  <input
                    value={configDraft.model}
                    onChange={(event) =>
                      setConfigDraft((prev) => (prev ? { ...prev, model: event.target.value } : prev))
                    }
                    className={panelFieldClass(isDesktopMac)}
                    placeholder={configDraft.accessMode === 'codex' ? 'gpt-5-codex' : 'deepseek-chat'}
                  />
                </label>
              </div>
            ) : null}

            {configDraft.accessMode === 'deepseek' ? (
              <label className="block text-sm">
                <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  DeepSeek API key
                </span>
                <input
                  value={configDraft.apiKey}
                  onChange={(event) =>
                    setConfigDraft((prev) => (prev ? { ...prev, apiKey: event.target.value } : prev))
                  }
                  className={panelFieldClass(isDesktopMac)}
                  placeholder={configDraft.hasExistingApiKey ? 'Already configured' : 'sk-...'}
                />
              </label>
            ) : null}

            {configDraft.accessMode === 'codex' ? (
              <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  Codex session
                </p>
                <p className="mt-2 text-sm text-[var(--shell-muted)]">
                  This path expects the official `codex` client to be installed and already logged in on this Mac.
                </p>
              </div>
            ) : null}
          </div>
        ) : null}

        {activeStep.id === 'channels' && configDraft ? (
          <div className="space-y-4">
            <button
              type="button"
              onClick={() =>
                setConfigDraft((prev) =>
                  prev ? { ...prev, telegramEnabled: !prev.telegramEnabled } : prev,
                )
              }
              className={choiceCardClass(configDraft.telegramEnabled)}
            >
              <h3 className="text-sm font-semibold">Telegram</h3>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">
                Connect this assistant to a Telegram bot and whitelist who can talk to her.
              </p>
            </button>

            {configDraft.telegramEnabled ? (
              <div className="grid gap-4">
                <label className="block text-sm">
                  <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                    Bot token
                  </span>
                  <input
                    value={configDraft.telegramBotToken}
                    onChange={(event) =>
                      setConfigDraft((prev) =>
                        prev ? { ...prev, telegramBotToken: event.target.value } : prev,
                      )
                    }
                    className={panelFieldClass(isDesktopMac)}
                    placeholder={configDraft.hasExistingTelegramToken ? 'Already configured' : '123456:ABC...'}
                  />
                </label>

                <label className="block text-sm">
                  <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                    Allowed users
                  </span>
                  <input
                    value={configDraft.telegramAllowedUsers}
                    onChange={(event) =>
                      setConfigDraft((prev) =>
                        prev ? { ...prev, telegramAllowedUsers: event.target.value } : prev,
                      )
                    }
                    className={panelFieldClass(isDesktopMac)}
                    placeholder="1703898290, 123456789"
                  />
                </label>
              </div>
            ) : null}
          </div>
        ) : null}

        {activeStep.id === 'name' ? (
          <div className="space-y-5">
            <div className="grid gap-4 md:grid-cols-2">
              <label className="block text-sm">
                <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  Assistant name
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
                  Internal id
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
                What should she help with?
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
                placeholder="Describe the work this assistant should own."
              />
            </label>

            <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                Setup note
              </p>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">
                Keep the brief practical. It should read like something you would say out loud to a new assistant.
              </p>
            </div>
          </div>
        ) : null}

        {activeStep.id === 'launch' ? (
          <div className="space-y-4">
            {[
              {
                id: 'default',
                title: 'Yes, make her my default assistant',
                detail: 'Agent HQ will open this profile first and treat it as the main everyday assistant.',
                icon: Rocket,
              },
              {
                id: 'manual',
                title: 'No, keep her as a specialist',
                detail: 'Good when this assistant is focused on one area and should not replace the main default profile.',
                icon: ShieldCheck,
              },
            ].map(({ id, title, detail, icon: Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() =>
                  setDraft((prev) => ({ ...prev, launch_on_startup: id === 'default' }))
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
                  Current default
                </p>
                <p className="mt-2 text-sm text-[var(--shell-muted)]">
                  {bootstrap.active_profile.profile.name} is currently the assistant that opens first.
                </p>
              </div>
            ) : null}
          </div>
        ) : null}

        {activeStep.id === 'confirm' ? (
          <div className="space-y-4">
            <div className="grid gap-3 md:grid-cols-2">
              <MacStat label="Assistant" value={draft.name || 'Unnamed'} detail={draft.id || 'No id yet'} />
              <MacStat
                label="Launch behavior"
                value={draft.launch_on_startup ? 'Default assistant' : 'Specialist only'}
                detail="How Agent HQ should open"
              />
            </div>

            <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                Focus
              </p>
              <p className="mt-2 text-sm font-semibold">
                {selectedFocus?.headline ?? activeClass?.name ?? 'Not selected'}
              </p>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">
                {draft.overrides.summary ?? ''}
              </p>
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

          {activeStep.id === 'confirm' ? (
            <button
              type="button"
              onClick={() => {
                void handleSubmit().catch((error) => {
                  setSetupError(error instanceof Error ? error.message : 'Failed to finish setup');
                });
              }}
              className={actionButtonClass(true, isDesktopMac)}
              disabled={busy || readyClasses.length === 0}
            >
              {busy ? 'Creating assistant...' : mode === 'studio' ? 'Create Assistant' : 'Finish Setup'}
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
        <MacPanel
          title="Preview"
          detail="This panel should tell you what will change before you move forward."
        >
          {activeClass ? (
            <div className="space-y-4">
              <MacStat
                label="Starter"
                value={selectedFocus?.headline ?? activeClass.name}
                detail={activeClass.default_identity_overlay.role_title ?? activeClass.fantasy_theme}
              />

              {configDraft ? (
                <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                  <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                    Model access
                  </p>
                  <p className="mt-2 text-sm text-[var(--shell-muted)]">
                    {configDraft.accessMode === 'deepseek'
                      ? `DeepSeek ${configDraft.model || 'deepseek-chat'}`
                      : configDraft.accessMode === 'codex'
                        ? `Codex session ${configDraft.model || 'gpt-5-codex'}`
                        : `${configDraft.provider || 'Existing provider'} ${configDraft.model || ''}`.trim()}
                  </p>
                </div>
              ) : null}

              {configDraft?.telegramEnabled ? (
                <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                  <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                    Channels
                  </p>
                  <p className="mt-2 text-sm text-[var(--shell-muted)]">
                    Telegram for {configDraft.telegramAllowedUsers || 'configured users'}
                  </p>
                </div>
              ) : null}

              <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
                <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">
                  What you are setting up
                </p>
                <p className="mt-2 text-sm text-[var(--shell-muted)]">
                  {selectedFocus?.prompt ?? activeClass.description}
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
                  Enabled tools
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
              title="Choose a starter to continue"
              description="The first screen should always give you at least one real starter option to choose from."
            />
          )}
        </MacPanel>

        <MacPanel
          title="What changed"
          detail="This setup should now include access and channel setup, not just agent labeling."
        >
          <div className="space-y-3 text-sm text-[var(--shell-muted)]">
            <p>You choose the type of help, then how the assistant gets model access, then which channels should be connected, then her identity and default behavior.</p>
            <p>The setup no longer pretends the assistant is ready before provider access and Telegram are configured.</p>
          </div>
        </MacPanel>
      </div>
    </div>
  );
}
