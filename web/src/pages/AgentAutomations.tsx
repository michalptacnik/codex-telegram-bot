import { useAgentContext } from '@/contexts/AgentContext';
import Cron from './Cron';

/**
 * Agent-scoped automation view.
 * Renders the standard Cron page but pre-scoped to the current agent.
 */
export default function AgentAutomations() {
  const { scopedAgent } = useAgentContext();
  // Pass the scoped agent ID as a prop so Cron can pre-filter
  return <Cron ownerAgentFilter={scopedAgent?.profile.id ?? undefined} />;
}
