import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Briefcase, MessageSquare, Target } from 'lucide-react';
import { getAutomations } from '@/lib/api';
import {
  MacEmptyState,
  MacPage,
  MacPanel,
  MacStat,
} from '@/components/macos/MacPrimitives';
import type { AutomationRecord, ResolvedAgentProfile } from '@/types/api';

export default function SalesDashboard({ agent }: { agent: ResolvedAgentProfile }) {
  const [automations, setAutomations] = useState<AutomationRecord[]>([]);
  const [, setLoading] = useState(true);
  const agentId = agent.profile.id;

  useEffect(() => {
    getAutomations()
      .then((autos) => setAutomations(autos.filter((a) => a.owner_agent_id === agentId)))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [agentId]);

  return (
    <MacPage
      eyebrow={agent.identity.role_title}
      title={`${agent.profile.name} Dashboard`}
      description="Sales pipeline, outreach, and call preparation."
    >
      <div className="grid gap-4 md:grid-cols-4">
        <MacStat label="Class" value="Sales" detail={agent.classes.map((c) => c.name).join(', ')} />
        <MacStat label="Automations" value={String(automations.length)} detail="Scheduled tasks" />
        <MacStat label="Tools" value={String(agent.tool_grants.length)} detail="Available tools" />
        <MacStat label="Skills" value={String(agent.skill_grants.length)} detail="Active skills" />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <MacPanel title="Quick Actions" detail="Jump into common sales workflows.">
          <div className="grid gap-3 md:grid-cols-2">
            <Link
              to={`/agents/${agentId}/chat`}
              className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4 transition hover:bg-[var(--shell-selection)]"
            >
              <MessageSquare className="h-5 w-5 text-[#0a84ff]" />
              <h3 className="mt-3 text-sm font-semibold">Agent Chat</h3>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">
                Work directly with your sales agent.
              </p>
            </Link>
            <Link
              to={`/agents/${agentId}/missions`}
              className="rounded-[1.25rem] border border-[var(--shell-border)] bg-[var(--shell-panel)] p-4 transition hover:bg-[var(--shell-selection)]"
            >
              <Target className="h-5 w-5 text-[#0a84ff]" />
              <h3 className="mt-3 text-sm font-semibold">Missions</h3>
              <p className="mt-2 text-sm text-[var(--shell-muted)]">
                Long-running sales objectives and tasks.
              </p>
            </Link>
          </div>
        </MacPanel>

        <MacPanel title="Pipeline" detail="Sales pipeline overview.">
          <MacEmptyState
            icon={<Briefcase className="h-7 w-7" />}
            title="Pipeline coming soon"
            description="CRM integration and pipeline tracking will be available in a future update."
          />
        </MacPanel>
      </div>
    </MacPage>
  );
}
