import { useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  Bot,
  Clock3,
  Pause,
  Play,
  Plus,
  RefreshCcw,
  ShieldCheck,
  Trash2,
  Wrench,
  X,
} from 'lucide-react';
import {
  createAutomation,
  deleteAutomation,
  getAgents,
  getAutomationRuns,
  getAutomations,
  pauseAutomation,
  resumeAutomation,
  runAutomationNow,
  updateAutomation,
} from '@/lib/api';
import type {
  AutomationRecord,
  AutomationRunRecord,
  AutomationSchedule,
  AutomationKind,
  ResolvedAgentProfile,
} from '@/types/api';

type CadencePreset = 'daily' | 'weekly' | 'interval' | 'once' | 'advanced';

type FormState = {
  id: string | null;
  kind: AutomationKind;
  name: string;
  owner_agent_id: string;
  prompt: string;
  command: string;
  cadence: CadencePreset;
  dailyTime: string;
  weeklyDay: string;
  weeklyTime: string;
  intervalHours: string;
  runAt: string;
  cronExpr: string;
  enabled: boolean;
  sessionTarget: 'isolated' | 'main';
  deliveryMode: 'none' | 'announce';
  deliveryChannel: string;
  deliveryTo: string;
};

const emptyForm: FormState = {
  id: null,
  kind: 'scheduled_agent',
  name: '',
  owner_agent_id: '',
  prompt: '',
  command: '',
  cadence: 'daily',
  dailyTime: '09:00',
  weeklyDay: '1',
  weeklyTime: '09:00',
  intervalHours: '24',
  runAt: '',
  cronExpr: '0 9 * * 1-5',
  enabled: true,
  sessionTarget: 'isolated',
  deliveryMode: 'none',
  deliveryChannel: '',
  deliveryTo: '',
};

const weekdayLabels: Record<string, string> = {
  '0': 'Sunday',
  '1': 'Monday',
  '2': 'Tuesday',
  '3': 'Wednesday',
  '4': 'Thursday',
  '5': 'Friday',
  '6': 'Saturday',
};

function formatDate(iso?: string | null): string {
  if (!iso) return '-';
  return new Date(iso).toLocaleString();
}

function statusTone(status?: string | null): string {
  const value = (status ?? '').toLowerCase();
  if (value === 'ok' || value === 'success') return 'text-emerald-300';
  if (value === 'error' || value === 'failed') return 'text-rose-300';
  return 'text-amber-300';
}

function kindLabel(kind: AutomationKind): string {
  switch (kind) {
    case 'scheduled_agent':
      return 'Agent job';
    case 'scheduled_shell':
      return 'Shell job';
    case 'heartbeat_task':
      return 'Heartbeat';
  }
}

function describeSchedule(schedule?: AutomationSchedule | null): string {
  if (!schedule) return 'Heartbeat-managed';
  if (schedule.kind === 'every') {
    const hours = schedule.every_ms / 3_600_000;
    return hours === 1 ? 'Every hour' : `Every ${hours} hours`;
  }
  if (schedule.kind === 'at') {
    return `Once at ${formatDate(schedule.at)}`;
  }
  return schedule.expr;
}

function splitTime(value: string): { hours: string; minutes: string } {
  const [hours = '09', minutes = '00'] = value.split(':');
  return { hours, minutes };
}

function cronToPreset(
  automation: AutomationRecord,
): Pick<
  FormState,
  'cadence' | 'dailyTime' | 'weeklyDay' | 'weeklyTime' | 'intervalHours' | 'runAt' | 'cronExpr'
> {
  const schedule = automation.schedule;
  if (!schedule) {
    return {
      cadence: 'daily',
      dailyTime: '09:00',
      weeklyDay: '1',
      weeklyTime: '09:00',
      intervalHours: '24',
      runAt: '',
      cronExpr: '0 9 * * 1-5',
    };
  }

  if (schedule.kind === 'every') {
    return {
      cadence: 'interval',
      dailyTime: '09:00',
      weeklyDay: '1',
      weeklyTime: '09:00',
      intervalHours: String(Math.max(1, Math.round(schedule.every_ms / 3_600_000))),
      runAt: '',
      cronExpr: '0 9 * * 1-5',
    };
  }

  if (schedule.kind === 'at') {
    return {
      cadence: 'once',
      dailyTime: '09:00',
      weeklyDay: '1',
      weeklyTime: '09:00',
      intervalHours: '24',
      runAt: new Date(schedule.at).toISOString().slice(0, 16),
      cronExpr: '0 9 * * 1-5',
    };
  }

  const daily = schedule.expr.match(/^(\d{1,2}) (\d{1,2}) \* \* \*$/);
  if (daily) {
    const [, minutes = '0', hours = '0'] = daily;
    return {
      cadence: 'daily',
      dailyTime: `${hours.padStart(2, '0')}:${minutes.padStart(2, '0')}`,
      weeklyDay: '1',
      weeklyTime: '09:00',
      intervalHours: '24',
      runAt: '',
      cronExpr: schedule.expr,
    };
  }

  const weekly = schedule.expr.match(/^(\d{1,2}) (\d{1,2}) \* \* (\d)$/);
  if (weekly) {
    const [, minutes = '0', hours = '0', weekday = '1'] = weekly;
    return {
      cadence: 'weekly',
      dailyTime: '09:00',
      weeklyDay: weekday,
      weeklyTime: `${hours.padStart(2, '0')}:${minutes.padStart(2, '0')}`,
      intervalHours: '24',
      runAt: '',
      cronExpr: schedule.expr,
    };
  }

  return {
    cadence: 'advanced',
    dailyTime: '09:00',
    weeklyDay: '1',
    weeklyTime: '09:00',
    intervalHours: '24',
    runAt: '',
    cronExpr: schedule.expr,
  };
}

function automationToForm(
  automation: AutomationRecord,
  activeAgentId: string,
): FormState {
  const preset = cronToPreset(automation);
  return {
    ...emptyForm,
    ...preset,
    id: automation.id,
    kind: automation.automation_kind,
    name: automation.name ?? '',
    owner_agent_id: automation.owner_agent_id ?? activeAgentId,
    prompt: automation.prompt ?? '',
    command: automation.command ?? '',
    enabled: automation.enabled,
    sessionTarget:
      automation.session_target?.toLowerCase() === 'main' ? 'main' : 'isolated',
    deliveryMode:
      automation.delivery?.mode === 'announce' ? 'announce' : 'none',
    deliveryChannel: automation.delivery?.channel ?? '',
    deliveryTo: automation.delivery?.to ?? '',
  };
}

function buildSchedule(form: FormState): AutomationSchedule | null {
  if (form.kind === 'heartbeat_task') return null;
  switch (form.cadence) {
    case 'daily': {
      const { hours, minutes } = splitTime(form.dailyTime);
      return { kind: 'cron', expr: `${Number(minutes)} ${Number(hours)} * * *` };
    }
    case 'weekly': {
      const { hours, minutes } = splitTime(form.weeklyTime);
      return {
        kind: 'cron',
        expr: `${Number(minutes)} ${Number(hours)} * * ${form.weeklyDay}`,
      };
    }
    case 'interval':
      return {
        kind: 'every',
        every_ms: Math.max(1, Number(form.intervalHours || '1')) * 3_600_000,
      };
    case 'once':
      return {
        kind: 'at',
        at: new Date(form.runAt).toISOString(),
      };
    case 'advanced':
      return { kind: 'cron', expr: form.cronExpr.trim() };
  }
}

function buildAutomationBody(form: FormState, activeAgentId: string) {
  const owner =
    form.kind === 'scheduled_shell'
      ? null
      : form.owner_agent_id || activeAgentId || null;
  return {
    automation_kind: form.kind,
    name: form.name.trim() || null,
    owner_agent_id: owner,
    prompt: form.prompt.trim() || null,
    command: form.command.trim() || null,
    enabled: form.enabled,
    schedule: buildSchedule(form),
    session_target: form.kind === 'scheduled_agent' ? form.sessionTarget : null,
    delivery:
      form.kind === 'scheduled_agent' && form.deliveryMode === 'announce'
        ? {
            mode: 'announce',
            channel: form.deliveryChannel.trim() || null,
            to: form.deliveryTo.trim() || null,
            best_effort: true,
          }
        : null,
  };
}

function statCard(title: string, value: string, note: string, icon: ReactNode) {
  return (
    <div className="rounded-2xl border border-white/8 bg-[rgba(12,18,28,0.88)] p-4 shadow-[0_18px_40px_rgba(0,0,0,0.16)]">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-[0.68rem] uppercase tracking-[0.18em] text-slate-400">
            {title}
          </p>
          <p className="mt-2 text-2xl font-semibold text-white">{value}</p>
          <p className="mt-1 text-sm text-slate-400">{note}</p>
        </div>
        <div className="rounded-2xl border border-white/8 bg-white/5 p-3 text-sky-300">
          {icon}
        </div>
      </div>
    </div>
  );
}

export default function Cron({ ownerAgentFilter }: { ownerAgentFilter?: string } = {}) {
  const [automations, setAutomations] = useState<AutomationRecord[]>([]);
  const [profiles, setProfiles] = useState<ResolvedAgentProfile[]>([]);
  const [activeAgentId, setActiveAgentId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [formError, setFormError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<'all' | AutomationKind>('all');
  const [ownerFilter, setOwnerFilter] = useState(ownerAgentFilter ?? 'all');
  const [statusFilter, setStatusFilter] = useState<'all' | 'enabled' | 'disabled' | 'failed'>(
    'all',
  );
  const [query, setQuery] = useState('');
  const [selectedAutomationId, setSelectedAutomationId] = useState<string | null>(null);
  const [runs, setRuns] = useState<AutomationRunRecord[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [automationList, agentList] = await Promise.all([
        getAutomations(),
        getAgents(),
      ]);
      setAutomations(automationList);
      setProfiles(agentList.profiles);
      setActiveAgentId(agentList.active_agent_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load automation manager');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, []);

  useEffect(() => {
    if (!selectedAutomationId) {
      setRuns([]);
      return;
    }
    setRunsLoading(true);
    getAutomationRuns(selectedAutomationId)
      .then(setRuns)
      .catch(() => setRuns([]))
      .finally(() => setRunsLoading(false));
  }, [selectedAutomationId]);

  const filteredAutomations = useMemo(() => {
    const lowered = query.trim().toLowerCase();
    return automations.filter((automation) => {
      if (typeFilter !== 'all' && automation.automation_kind !== typeFilter) return false;
      if (
        ownerFilter !== 'all' &&
        (automation.owner_agent_id ?? 'global') !== ownerFilter
      )
        return false;
      if (statusFilter === 'enabled' && !automation.enabled) return false;
      if (statusFilter === 'disabled' && automation.enabled) return false;
      if (
        statusFilter === 'failed' &&
        (automation.last_status ?? '').toLowerCase() !== 'error'
      )
        return false;
      if (!lowered) return true;
      const haystack = [
        automation.name,
        automation.prompt,
        automation.command,
        automation.owner_agent_id,
        automation.id,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      return haystack.includes(lowered);
    });
  }, [automations, ownerFilter, query, statusFilter, typeFilter]);

  const nextUpcoming = useMemo(() => {
    return [...automations]
      .filter((item) => item.enabled && item.next_run)
      .sort((a, b) => (a.next_run! < b.next_run! ? -1 : 1))[0];
  }, [automations]);

  const failedCount = automations.filter(
    (item) => (item.last_status ?? '').toLowerCase() === 'error',
  ).length;
  const heartbeatCount = automations.filter(
    (item) => item.automation_kind === 'heartbeat_task',
  ).length;

  const openCreate = () => {
    setForm({
      ...emptyForm,
      owner_agent_id: ownerAgentFilter ?? activeAgentId,
    });
    setFormError(null);
    setShowForm(true);
  };

  const openEdit = (automation: AutomationRecord) => {
    setForm(automationToForm(automation, activeAgentId));
    setFormError(null);
    setShowForm(true);
  };

  const submitForm = async () => {
    if (form.kind === 'heartbeat_task' || form.kind === 'scheduled_agent') {
      if (!form.prompt.trim()) {
        setFormError('Prompt is required for agent and heartbeat automations.');
        return;
      }
    }
    if (form.kind === 'scheduled_shell' && !form.command.trim()) {
      setFormError('Command is required for shell automations.');
      return;
    }
    if (form.kind !== 'heartbeat_task' && !buildSchedule(form)) {
      setFormError('A schedule is required.');
      return;
    }

    try {
      setFormError(null);
      const body = buildAutomationBody(form, activeAgentId);
      if (form.id) {
        const updated = await updateAutomation(form.id, body);
        setAutomations((current) =>
          current.map((item) => (item.id === updated.id ? updated : item)),
        );
      } else {
        const created = await createAutomation(body);
        setAutomations((current) => [created, ...current]);
      }
      setShowForm(false);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to save automation');
    }
  };

  const mutateAutomation = async (
    id: string,
    action: 'pause' | 'resume' | 'delete' | 'run',
  ) => {
    setBusyId(id);
    setError(null);
    try {
      if (action === 'pause') {
        const updated = await pauseAutomation(id);
        setAutomations((current) =>
          current.map((item) => (item.id === updated.id ? updated : item)),
        );
      } else if (action === 'resume') {
        const updated = await resumeAutomation(id);
        setAutomations((current) =>
          current.map((item) => (item.id === updated.id ? updated : item)),
        );
      } else if (action === 'delete') {
        await deleteAutomation(id);
        setAutomations((current) => current.filter((item) => item.id !== id));
        if (selectedAutomationId === id) setSelectedAutomationId(null);
      } else {
        await runAutomationNow(id);
        await loadData();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Automation action failed');
    } finally {
      setBusyId(null);
    }
  };

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-9 w-9 animate-spin rounded-full border-2 border-sky-500 border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="min-h-full bg-[radial-gradient(circle_at_top_left,rgba(56,189,248,0.12),transparent_36%),radial-gradient(circle_at_top_right,rgba(34,197,94,0.10),transparent_28%),linear-gradient(180deg,#07111b_0%,#09131f_100%)] p-6 text-white">
      <div className="mx-auto max-w-7xl space-y-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <p className="text-[0.72rem] uppercase tracking-[0.22em] text-sky-300">
              Automation Manager
            </p>
            <h1 className="mt-2 text-3xl font-semibold tracking-tight">
              Schedule work for every agent profile
            </h1>
            <p className="mt-2 max-w-3xl text-sm text-slate-300">
              Build repeated daily or weekly jobs, interval automations, one-off tasks,
              and heartbeat work from the same control surface.
            </p>
          </div>
          <button
            onClick={openCreate}
            className="inline-flex items-center gap-2 rounded-full bg-sky-500 px-5 py-3 text-sm font-semibold text-slate-950 transition hover:bg-sky-400"
          >
            <Plus className="h-4 w-4" />
            New Automation
          </button>
        </div>

        <div className="grid gap-4 md:grid-cols-3">
          {statCard(
            'Upcoming',
            nextUpcoming ? formatDate(nextUpcoming.next_run) : 'Nothing queued',
            nextUpcoming ? nextUpcoming.name ?? kindLabel(nextUpcoming.automation_kind) : 'No enabled automation has a next run yet.',
            <Clock3 className="h-6 w-6" />,
          )}
          {statCard(
            'Failures',
            String(failedCount),
            failedCount === 0 ? 'No recent failed runs.' : 'Review failed jobs and rerun after fixing prompts or tools.',
            <ShieldCheck className="h-6 w-6" />,
          )}
          {statCard(
            'Heartbeat',
            String(heartbeatCount),
            heartbeatCount === 0 ? 'No managed heartbeat tasks yet.' : 'Heartbeat work is managed here alongside scheduled jobs.',
            <RefreshCcw className="h-6 w-6" />,
          )}
        </div>

        {error ? (
          <div className="rounded-2xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
            {error}
          </div>
        ) : null}

        <div className="grid gap-6 xl:grid-cols-[1.65fr_0.95fr]">
          <section className="rounded-[1.7rem] border border-white/8 bg-[rgba(9,16,26,0.88)] p-5 shadow-[0_24px_60px_rgba(0,0,0,0.24)]">
            <div className="grid gap-3 md:grid-cols-4">
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search automations"
                className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none placeholder:text-slate-500"
              />
              <select
                value={typeFilter}
                onChange={(event) => setTypeFilter(event.target.value as typeof typeFilter)}
                className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
              >
                <option value="all">All types</option>
                <option value="scheduled_agent">Agent jobs</option>
                <option value="scheduled_shell">Shell jobs</option>
                <option value="heartbeat_task">Heartbeat</option>
              </select>
              <select
                value={ownerFilter}
                onChange={(event) => setOwnerFilter(event.target.value)}
                className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
              >
                <option value="all">All owners</option>
                <option value="global">Global or shell</option>
                {profiles.map((profile) => (
                  <option key={profile.profile.id} value={profile.profile.id}>
                    {profile.profile.name}
                  </option>
                ))}
              </select>
              <select
                value={statusFilter}
                onChange={(event) =>
                  setStatusFilter(event.target.value as typeof statusFilter)
                }
                className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
              >
                <option value="all">Any status</option>
                <option value="enabled">Enabled</option>
                <option value="disabled">Disabled</option>
                <option value="failed">Failed</option>
              </select>
            </div>

            <div className="mt-5 space-y-3">
              {filteredAutomations.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-white/10 bg-white/[0.03] px-6 py-10 text-center text-slate-400">
                  No automations match the current filters.
                </div>
              ) : (
                filteredAutomations.map((automation) => {
                  const ownerName =
                    profiles.find((item) => item.profile.id === automation.owner_agent_id)
                      ?.profile.name ??
                    (automation.owner_agent_id ? automation.owner_agent_id : 'Global');
                  const summary =
                    automation.prompt ??
                    automation.command ??
                    'No task description';
                  return (
                    <article
                      key={automation.id}
                      className="rounded-[1.45rem] border border-white/8 bg-white/[0.03] p-4 transition hover:border-sky-400/30 hover:bg-white/[0.05]"
                    >
                      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="rounded-full border border-sky-400/25 bg-sky-400/10 px-3 py-1 text-[0.7rem] uppercase tracking-[0.16em] text-sky-200">
                              {kindLabel(automation.automation_kind)}
                            </span>
                            <span
                              className={`rounded-full border px-3 py-1 text-[0.7rem] uppercase tracking-[0.16em] ${
                                automation.enabled
                                  ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200'
                                  : 'border-white/10 bg-white/5 text-slate-400'
                              }`}
                            >
                              {automation.enabled ? 'Enabled' : 'Paused'}
                            </span>
                            <span
                              className={`text-xs ${statusTone(automation.last_status)}`}
                            >
                              Last status: {automation.last_status ?? 'never run'}
                            </span>
                          </div>

                          <h2 className="mt-3 text-lg font-semibold text-white">
                            {automation.name || summary.slice(0, 72)}
                          </h2>
                          <p className="mt-2 line-clamp-2 text-sm text-slate-300">
                            {summary}
                          </p>

                          <div className="mt-4 flex flex-wrap gap-4 text-xs text-slate-400">
                            <span>Owner: {ownerName}</span>
                            <span>Schedule: {describeSchedule(automation.schedule)}</span>
                            <span>Next: {formatDate(automation.next_run)}</span>
                          </div>
                        </div>

                        <div className="flex flex-wrap gap-2 lg:justify-end">
                          <button
                            onClick={() =>
                              setSelectedAutomationId((current) =>
                                current === automation.id ? null : automation.id,
                              )
                            }
                            className="rounded-full border border-white/10 bg-white/5 px-3 py-2 text-xs font-medium text-slate-200 transition hover:bg-white/10"
                          >
                            Runs
                          </button>
                          <button
                            onClick={() => openEdit(automation)}
                            className="rounded-full border border-white/10 bg-white/5 px-3 py-2 text-xs font-medium text-slate-200 transition hover:bg-white/10"
                          >
                            Edit
                          </button>
                          <button
                            disabled={busyId === automation.id}
                            onClick={() => void mutateAutomation(automation.id, 'run')}
                            className="rounded-full border border-sky-400/25 bg-sky-400/10 px-3 py-2 text-xs font-medium text-sky-100 transition hover:bg-sky-400/20 disabled:opacity-50"
                          >
                            {busyId === automation.id ? 'Running…' : 'Run now'}
                          </button>
                          <button
                            disabled={busyId === automation.id}
                            onClick={() =>
                              void mutateAutomation(
                                automation.id,
                                automation.enabled ? 'pause' : 'resume',
                              )
                            }
                            className="rounded-full border border-white/10 bg-white/5 px-3 py-2 text-xs font-medium text-slate-200 transition hover:bg-white/10 disabled:opacity-50"
                          >
                            {automation.enabled ? (
                              <span className="inline-flex items-center gap-1">
                                <Pause className="h-3.5 w-3.5" />
                                Pause
                              </span>
                            ) : (
                              <span className="inline-flex items-center gap-1">
                                <Play className="h-3.5 w-3.5" />
                                Resume
                              </span>
                            )}
                          </button>
                          <button
                            disabled={busyId === automation.id}
                            onClick={() => void mutateAutomation(automation.id, 'delete')}
                            className="rounded-full border border-rose-400/20 bg-rose-400/10 px-3 py-2 text-xs font-medium text-rose-100 transition hover:bg-rose-400/20 disabled:opacity-50"
                          >
                            <span className="inline-flex items-center gap-1">
                              <Trash2 className="h-3.5 w-3.5" />
                              Delete
                            </span>
                          </button>
                        </div>
                      </div>

                      {selectedAutomationId === automation.id ? (
                        <div className="mt-4 rounded-2xl border border-white/8 bg-slate-950/50 p-4">
                          <div className="mb-3 flex items-center justify-between">
                            <h3 className="text-sm font-semibold text-white">Recent runs</h3>
                            {runsLoading ? (
                              <span className="text-xs text-slate-400">Loading…</span>
                            ) : null}
                          </div>
                          {runs.length === 0 ? (
                            <p className="text-sm text-slate-400">No runs recorded yet.</p>
                          ) : (
                            <div className="space-y-2">
                              {runs.map((run) => (
                                <div
                                  key={run.id}
                                  className="rounded-2xl border border-white/6 bg-white/[0.03] p-3"
                                >
                                  <div className="flex flex-wrap items-center justify-between gap-2">
                                    <span className={`text-xs font-medium ${statusTone(run.status)}`}>
                                      {run.status}
                                    </span>
                                    <span className="text-xs text-slate-400">
                                      {formatDate(run.finished_at)}
                                    </span>
                                  </div>
                                  {run.output ? (
                                    <p className="mt-2 whitespace-pre-wrap text-sm text-slate-300">
                                      {run.output}
                                    </p>
                                  ) : null}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      ) : null}
                    </article>
                  );
                })
              )}
            </div>
          </section>

          <aside className="space-y-4">
            <div className="rounded-[1.7rem] border border-white/8 bg-[rgba(9,16,26,0.88)] p-5">
              <h2 className="text-sm font-semibold uppercase tracking-[0.18em] text-slate-300">
                Quick Patterns
              </h2>
              <div className="mt-4 space-y-3 text-sm text-slate-300">
                <div className="rounded-2xl border border-white/8 bg-white/[0.03] p-4">
                  <div className="flex items-center gap-2 text-white">
                    <Bot className="h-4 w-4 text-sky-300" />
                    Daily agent review
                  </div>
                  <p className="mt-2 text-slate-400">
                    Schedule an agent to run a morning review every weekday with browser access and the right profile context.
                  </p>
                </div>
                <div className="rounded-2xl border border-white/8 bg-white/[0.03] p-4">
                  <div className="flex items-center gap-2 text-white">
                    <Bot className="h-4 w-4 text-emerald-300" />
                    Sales pipeline sweep
                  </div>
                  <p className="mt-2 text-slate-400">
                    For Sales profiles, queue daily prospect discovery, reply triage, or weekly pipeline summaries without losing owner context.
                  </p>
                </div>
                <div className="rounded-2xl border border-white/8 bg-white/[0.03] p-4">
                  <div className="flex items-center gap-2 text-white">
                    <Wrench className="h-4 w-4 text-emerald-300" />
                    Repeated operator work
                  </div>
                  <p className="mt-2 text-slate-400">
                    Use shell jobs for maintenance tasks, but keep assistant work in agent automations whenever possible.
                  </p>
                </div>
                <div className="rounded-2xl border border-white/8 bg-white/[0.03] p-4">
                  <div className="flex items-center gap-2 text-white">
                    <RefreshCcw className="h-4 w-4 text-amber-300" />
                    Heartbeat coverage
                  </div>
                  <p className="mt-2 text-slate-400">
                    Heartbeat tasks live here now, so you can see ownership, state, and last output without editing files by hand.
                  </p>
                </div>
              </div>
            </div>
          </aside>
        </div>
      </div>

      {showForm ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
          <div className="max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-[1.8rem] border border-white/10 bg-[#08121d] p-6 shadow-[0_32px_90px_rgba(0,0,0,0.48)]">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-[0.68rem] uppercase tracking-[0.18em] text-sky-300">
                  {form.id ? 'Edit automation' : 'Create automation'}
                </p>
                <h2 className="mt-2 text-2xl font-semibold text-white">
                  {form.id ? 'Refine this automation' : 'Build a new automation'}
                </h2>
              </div>
              <button
                onClick={() => setShowForm(false)}
                className="rounded-full border border-white/10 bg-white/5 p-2 text-slate-300 transition hover:bg-white/10"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {formError ? (
              <div className="mt-4 rounded-2xl border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
                {formError}
              </div>
            ) : null}

            <div className="mt-6 grid gap-4 md:grid-cols-2">
              <label className="block">
                <span className="text-sm text-slate-300">Automation type</span>
                <select
                  value={form.kind}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      kind: event.target.value as AutomationKind,
                    }))
                  }
                  className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                >
                  <option value="scheduled_agent">Scheduled agent job</option>
                  <option value="heartbeat_task">Heartbeat task</option>
                  <option value="scheduled_shell">Scheduled shell job</option>
                </select>
              </label>

              <label className="block">
                <span className="text-sm text-slate-300">Name</span>
                <input
                  value={form.name}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, name: event.target.value }))
                  }
                  className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                  placeholder="Morning review"
                />
              </label>

              {form.kind !== 'scheduled_shell' ? (
                <label className="block">
                  <span className="text-sm text-slate-300">Owner agent</span>
                  <select
                    value={form.owner_agent_id}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        owner_agent_id: event.target.value,
                      }))
                    }
                    className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                  >
                    {profiles.map((profile) => (
                      <option key={profile.profile.id} value={profile.profile.id}>
                        {profile.profile.name}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <div className="rounded-2xl border border-dashed border-white/10 bg-white/[0.03] px-4 py-3 text-sm text-slate-400">
                  Shell jobs are global and do not bind to a specific agent profile.
                </div>
              )}

              {form.kind === 'scheduled_agent' ? (
                <label className="block">
                  <span className="text-sm text-slate-300">Run target</span>
                  <select
                    value={form.sessionTarget}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        sessionTarget: event.target.value as 'isolated' | 'main',
                      }))
                    }
                    className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                  >
                    <option value="isolated">Isolated session</option>
                    <option value="main">Main session</option>
                  </select>
                </label>
              ) : null}

              {form.kind !== 'heartbeat_task' ? (
                <label className="block">
                  <span className="text-sm text-slate-300">Cadence</span>
                  <select
                    value={form.cadence}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        cadence: event.target.value as CadencePreset,
                      }))
                    }
                    className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                  >
                    <option value="daily">Daily</option>
                    <option value="weekly">Weekly</option>
                    <option value="interval">Every N hours</option>
                    <option value="once">One time</option>
                    <option value="advanced">Advanced cron</option>
                  </select>
                </label>
              ) : null}

              {form.kind === 'scheduled_agent' || form.kind === 'heartbeat_task' ? (
                <label className="block md:col-span-2">
                  <span className="text-sm text-slate-300">Task prompt</span>
                  <textarea
                    value={form.prompt}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, prompt: event.target.value }))
                    }
                    rows={5}
                    className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                    placeholder="Review replies, qualify which prospects need follow-up, and prepare concise next-step drafts."
                  />
                </label>
              ) : (
                <label className="block md:col-span-2">
                  <span className="text-sm text-slate-300">Shell command</span>
                  <textarea
                    value={form.command}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, command: event.target.value }))
                    }
                    rows={4}
                    className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                    placeholder="echo cleanup"
                  />
                </label>
              )}

              {form.kind !== 'heartbeat_task' && form.cadence === 'daily' ? (
                <label className="block">
                  <span className="text-sm text-slate-300">Daily time</span>
                  <input
                    type="time"
                    value={form.dailyTime}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, dailyTime: event.target.value }))
                    }
                    className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                  />
                </label>
              ) : null}

              {form.kind !== 'heartbeat_task' && form.cadence === 'weekly' ? (
                <>
                  <label className="block">
                    <span className="text-sm text-slate-300">Weekday</span>
                    <select
                      value={form.weeklyDay}
                      onChange={(event) =>
                        setForm((current) => ({ ...current, weeklyDay: event.target.value }))
                      }
                      className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                    >
                      {Object.entries(weekdayLabels).map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="block">
                    <span className="text-sm text-slate-300">Weekly time</span>
                    <input
                      type="time"
                      value={form.weeklyTime}
                      onChange={(event) =>
                        setForm((current) => ({ ...current, weeklyTime: event.target.value }))
                      }
                      className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                    />
                  </label>
                </>
              ) : null}

              {form.kind !== 'heartbeat_task' && form.cadence === 'interval' ? (
                <label className="block">
                  <span className="text-sm text-slate-300">Repeat every N hours</span>
                  <input
                    type="number"
                    min={1}
                    value={form.intervalHours}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        intervalHours: event.target.value,
                      }))
                    }
                    className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                  />
                </label>
              ) : null}

              {form.kind !== 'heartbeat_task' && form.cadence === 'once' ? (
                <label className="block">
                  <span className="text-sm text-slate-300">Run at</span>
                  <input
                    type="datetime-local"
                    value={form.runAt}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, runAt: event.target.value }))
                    }
                    className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                  />
                </label>
              ) : null}

              {form.kind !== 'heartbeat_task' && form.cadence === 'advanced' ? (
                <label className="block md:col-span-2">
                  <span className="text-sm text-slate-300">Cron expression</span>
                  <input
                    value={form.cronExpr}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, cronExpr: event.target.value }))
                    }
                    className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                  />
                </label>
              ) : null}

              {form.kind === 'scheduled_agent' ? (
                <>
                  <label className="block">
                    <span className="text-sm text-slate-300">Delivery mode</span>
                    <select
                      value={form.deliveryMode}
                      onChange={(event) =>
                        setForm((current) => ({
                          ...current,
                          deliveryMode: event.target.value as 'none' | 'announce',
                        }))
                      }
                      className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                    >
                      <option value="none">No delivery</option>
                      <option value="announce">Announce to channel</option>
                    </select>
                  </label>
                  {form.deliveryMode === 'announce' ? (
                    <>
                      <label className="block">
                        <span className="text-sm text-slate-300">Channel</span>
                        <input
                          value={form.deliveryChannel}
                          onChange={(event) =>
                            setForm((current) => ({
                              ...current,
                              deliveryChannel: event.target.value,
                            }))
                          }
                          placeholder="telegram"
                          className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                        />
                      </label>
                      <label className="block">
                        <span className="text-sm text-slate-300">Target</span>
                        <input
                          value={form.deliveryTo}
                          onChange={(event) =>
                            setForm((current) => ({
                              ...current,
                              deliveryTo: event.target.value,
                            }))
                          }
                          placeholder="chat id or channel id"
                          className="mt-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm outline-none"
                        />
                      </label>
                    </>
                  ) : null}
                </>
              ) : null}

              <label className="flex items-center gap-3 rounded-2xl border border-white/8 bg-white/[0.03] px-4 py-3 text-sm text-slate-200">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, enabled: event.target.checked }))
                  }
                />
                Start enabled
              </label>
            </div>

            <div className="mt-6 flex justify-end gap-3">
              <button
                onClick={() => setShowForm(false)}
                className="rounded-full border border-white/10 bg-white/5 px-5 py-3 text-sm font-medium text-slate-200 transition hover:bg-white/10"
              >
                Cancel
              </button>
              <button
                onClick={() => void submitForm()}
                className="rounded-full bg-sky-500 px-5 py-3 text-sm font-semibold text-slate-950 transition hover:bg-sky-400"
              >
                {form.id ? 'Save automation' : 'Create automation'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
