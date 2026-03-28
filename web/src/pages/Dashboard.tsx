import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Activity,
  Cpu,
  Database,
  DollarSign,
  Globe,
  Radio,
} from 'lucide-react';
import { MacBadge, MacEmptyState, MacPage, MacPanel, MacStat } from '@/components/macos/MacPrimitives';
import { useShell } from '@/components/shell/ShellProvider';
import type { CostSummary, StatusResponse } from '@/types/api';
import { getCost, getStatus } from '@/lib/api';

function formatUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatUSD(value: number): string {
  return `$${value.toFixed(4)}`;
}

function healthTone(status: string): 'success' | 'warning' | 'danger' {
  switch (status.toLowerCase()) {
    case 'ok':
    case 'healthy':
      return 'success';
    case 'warn':
    case 'warning':
    case 'degraded':
      return 'warning';
    default:
      return 'danger';
  }
}

export default function Dashboard() {
  const { isDesktopMac } = useShell();
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [cost, setCost] = useState<CostSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getStatus(), getCost()])
      .then(([statusResponse, costSummary]) => {
        setStatus(statusResponse);
        setCost(costSummary);
      })
      .catch((err: Error) => setError(err.message));
  }, []);

  const topModels = useMemo(() => {
    if (!cost) return [];
    return Object.values(cost.by_model)
      .sort((a, b) => b.cost_usd - a.cost_usd)
      .slice(0, 3);
  }, [cost]);

  if (!isDesktopMac) {
    if (error) {
      return (
        <div className="p-6">
          <div className="rounded-lg bg-red-900/30 border border-red-700 p-4 text-red-300">
            Failed to load dashboard: {error}
          </div>
        </div>
      );
    }

    if (!status || !cost) {
      return (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-2 border-blue-500 border-t-transparent" />
        </div>
      );
    }
  }

  if (error && isDesktopMac) {
    return (
      <MacPage
        eyebrow="Overview"
        title="Operations overview"
        description="A macOS-style operational overview of the local runtime."
      >
        <MacPanel title="Unavailable" detail={error}>
          <MacEmptyState
            icon={<Activity className="h-8 w-8" />}
            title="Dashboard unavailable"
            description="The runtime did not return dashboard data. Check diagnostics or logs for more detail."
          />
        </MacPanel>
      </MacPage>
    );
  }

  if (!status || !cost) {
    return (
      <MacPage
        eyebrow="Overview"
        title="Operations overview"
        description="A macOS-style operational overview of the local runtime."
      >
        <MacPanel title="Loading">
          <MacEmptyState
            icon={<Activity className="h-8 w-8 animate-pulse" />}
            title="Connecting to runtime"
            description="Gathering status, memory, channel, and cost information."
          />
        </MacPanel>
      </MacPage>
    );
  }

  const overallStatus =
    Object.values(status.health.components).find((component) => component.status !== 'ok')?.status ??
    'ok';

  return (
    <MacPage
      eyebrow="Overview"
      title="Operations overview"
      description="Health, spend, and live channel posture arranged for quick scanning on macOS."
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <MacBadge tone={healthTone(overallStatus)}>{overallStatus}</MacBadge>
          <Link className="mac-badge mac-badge-neutral" to="/logs">
            Logs
          </Link>
          <Link className="mac-badge mac-badge-neutral" to="/doctor">
            Diagnostics
          </Link>
          <Link className="mac-badge mac-badge-neutral" to="/cost">
            Cost
          </Link>
        </div>
      }
    >
      <div className="grid gap-4 lg:grid-cols-4">
        <MacStat label="Provider" value={status.provider ?? 'Unknown'} detail={status.model} />
        <MacStat label="Uptime" value={formatUptime(status.uptime_seconds)} detail="Since last restart" />
        <MacStat label="Gateway" value={`:${status.gateway_port}`} detail={`Locale ${status.locale}`} />
        <MacStat
          label="Memory"
          value={status.memory_backend}
          detail={status.paired ? 'Pairing complete' : 'Awaiting pairing'}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        <MacPanel title="Runtime posture" detail="Core runtime, model, memory, and channel signals.">
          <div className="grid gap-3 md:grid-cols-2">
            {[
              {
                icon: Cpu,
                label: 'Model stack',
                value: `${status.provider ?? 'Unknown'} / ${status.model}`,
              },
              {
                icon: Globe,
                label: 'Locale and listener',
                value: `${status.locale} on port ${status.gateway_port}`,
              },
              {
                icon: Database,
                label: 'Memory backend',
                value: status.memory_backend,
              },
              {
                icon: Radio,
                label: 'Paired channels',
                value: `${Object.values(status.channels).filter(Boolean).length} active`,
              },
            ].map(({ icon: Icon, label, value }) => (
              <div key={label} className="mac-stat">
                <div className="flex items-center gap-2 text-[0.82rem] text-[var(--shell-muted)]">
                  <Icon className="h-4 w-4" />
                  <span>{label}</span>
                </div>
                <p className="mt-3 text-lg font-semibold tracking-[-0.03em]">{value}</p>
              </div>
            ))}
          </div>
        </MacPanel>

        <MacPanel title="Cost overview" detail="Current spend and token activity.">
          <div className="grid gap-3">
            <MacStat label="Session spend" value={formatUSD(cost.session_cost_usd)} detail={`${cost.request_count.toLocaleString()} requests`} />
            <MacStat label="Daily spend" value={formatUSD(cost.daily_cost_usd)} detail={`${cost.total_tokens.toLocaleString()} total tokens`} />
            <MacStat label="Monthly spend" value={formatUSD(cost.monthly_cost_usd)} detail={`${cost.monthly_tokens.toLocaleString()} monthly tokens`} />
          </div>
        </MacPanel>
      </div>

      <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
        <MacPanel title="Active channels" detail="Configured channels and their current activity state.">
          <div className="grid gap-3">
            {Object.entries(status.channels).length === 0 ? (
              <MacEmptyState
                icon={<Radio className="h-7 w-7" />}
                title="No active channels"
                description="Channel activity appears here once integrations are configured."
              />
            ) : (
              Object.entries(status.channels).map(([name, active]) => (
                <div
                  key={name}
                  className="flex items-center justify-between rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] px-4 py-3"
                >
                  <div>
                    <p className="text-sm font-semibold capitalize">{name}</p>
                    <p className="text-sm text-[var(--shell-muted)]">
                      {active ? 'Receiving events' : 'Configured but idle'}
                    </p>
                  </div>
                  <MacBadge tone={active ? 'success' : 'neutral'}>
                    {active ? 'Active' : 'Idle'}
                  </MacBadge>
                </div>
              ))
            )}
          </div>
        </MacPanel>

        <MacPanel title="Component health" detail="Service health from the runtime heartbeat.">
          <div className="grid gap-3 md:grid-cols-2">
            {Object.entries(status.health.components).length === 0 ? (
              <MacEmptyState
                icon={<Activity className="h-7 w-7" />}
                title="No health reporters"
                description="Health summaries appear here once components start publishing heartbeat data."
              />
            ) : (
              Object.entries(status.health.components).map(([name, component]) => (
                <div key={name} className="mac-stat">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-semibold capitalize">{name}</p>
                    <MacBadge tone={healthTone(component.status)}>{component.status}</MacBadge>
                  </div>
                  <p className="mt-3 text-sm text-[var(--shell-muted)]">
                    Updated {new Date(component.updated_at).toLocaleString()}
                  </p>
                  {component.restart_count > 0 ? (
                    <p className="mt-2 text-sm text-[var(--shell-muted)]">
                      Restarts: {component.restart_count}
                    </p>
                  ) : null}
                </div>
              ))
            )}
          </div>
        </MacPanel>
      </div>

      <MacPanel title="Top model spend" detail="The models contributing most to current tracked cost.">
        {topModels.length === 0 ? (
          <MacEmptyState
            icon={<DollarSign className="h-7 w-7" />}
            title="No model cost data"
            description="Tracked model spend will populate here once requests have been recorded."
          />
        ) : (
          <div className="grid gap-3 md:grid-cols-3">
            {topModels.map((model) => (
              <div key={model.model} className="mac-stat">
                <p className="mac-stat-label">{model.model}</p>
                <p className="mac-stat-value">{formatUSD(model.cost_usd)}</p>
                <p className="mac-stat-detail">
                  {model.total_tokens.toLocaleString()} tokens across {model.request_count.toLocaleString()} requests
                </p>
              </div>
            ))}
          </div>
        )}
      </MacPanel>
    </MacPage>
  );
}
