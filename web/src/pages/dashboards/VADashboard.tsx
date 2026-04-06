import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Brain, Clock, MessageSquare, Target } from 'lucide-react';
import { getAutomations } from '@/lib/api';
import {
  MacPage,
  MacPanel,
  MacStat,
  MacBadge,
} from '@/components/macos/MacPrimitives';
import type { AutomationRecord, ResolvedAgentProfile } from '@/types/api';

export default function VADashboard({ agent }: { agent: ResolvedAgentProfile }) {
  const [automations, setAutomations] = useState<AutomationRecord[]>([]);
  const [, setLoading] = useState(true);
  const agentId = agent.profile.id;

  useEffect(() => {
    getAutomations()
      .then((autos) => setAutomations(autos.filter((a) => a.owner_agent_id === agentId)))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [agentId]);

  const activeAutomations = automations.filter((a) => a.enabled);

  return (
    <MacPage
      eyebrow={agent.identity.role_title}
      title={`${agent.profile.name} Dashboard`}
      description="Tasks, inbox triage, and scheduled operations."
    >
      <div className="grid gap-4 md:grid-cols-4">
        <MacStat label="Class" value="VA" detail={agent.classes.map((c) => c.name).join(', ')} />
        <MacStat label="Automations" value={String(activeAutomations.length)} detail="Active tasks" />
        <MacStat label="Tools" value={String(agent.tool_grants.length)} detail="Available tools" />
        <MacStat label="Skills" value={String(agent.skill_grants.length)} detail="Active skills" />
      </div>

      <div className="grid gap-4 xl:grid-cols-[1fr_1fr]">
        <MacPanel title="Quick Actions" detail="Jump into common VA workflows.">
          <div className="grid gap-3 md:grid-cols-2">
            <Link
              to={`/agents/${agentId}/chat`}
              className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4 transition hover:bg-[var(--shell-selection)]"
            >
              <MessageSquare className="h-5 w-5 text-[#0a84ff]" />
              <h3 className="mt-3 text-sm font-semibold">Agent Chat</h3>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">Work directly with your VA.</p>
            </Link>
            <Link
              to={`/agents/${agentId}/missions`}
              className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4 transition hover:bg-[var(--shell-selection)]"
            >
              <Target className="h-5 w-5 text-[#0a84ff]" />
              <h3 className="mt-3 text-sm font-semibold">Missions</h3>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">Long-running objectives.</p>
            </Link>
            <Link
              to={`/agents/${agentId}/memory`}
              className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4 transition hover:bg-[var(--shell-selection)]"
            >
              <Brain className="h-5 w-5 text-[#0a84ff]" />
              <h3 className="mt-3 text-sm font-semibold">Memory</h3>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">Agent knowledge base.</p>
            </Link>
            <Link
              to={`/agents/${agentId}/automations`}
              className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4 transition hover:bg-[var(--shell-selection)]"
            >
              <Clock className="h-5 w-5 text-[#0a84ff]" />
              <h3 className="mt-3 text-sm font-semibold">Automations</h3>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">Scheduled tasks and reminders.</p>
            </Link>
          </div>
        </MacPanel>

        <MacPanel title="Agent Details" detail="Role, guardrails, and tool access.">
          <div className="space-y-4">
            <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Summary</p>
              <p className="mt-2 text-sm">{agent.summary}</p>
            </div>
            <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Guardrails</p>
              <div className="mt-3 grid gap-2">
                {agent.guardrails.slice(0, 4).map((g) => (
                  <div key={g} className="rounded-[1rem] bg-[var(--shell-selection)] px-3 py-2 text-sm">
                    {g}
                  </div>
                ))}
              </div>
            </div>
            <div className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4">
              <p className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Tool grants</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {agent.tool_grants.slice(0, 8).map((tool) => (
                  <MacBadge key={tool} tone="neutral">{tool}</MacBadge>
                ))}
              </div>
            </div>
          </div>
        </MacPanel>
      </div>
    </MacPage>
  );
}
