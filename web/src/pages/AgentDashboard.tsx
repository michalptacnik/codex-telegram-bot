import { useAgentContext } from '@/contexts/AgentContext';
import SocialMediaManagerDashboard from './dashboards/SocialMediaManagerDashboard';
import SalesDashboard from './dashboards/SalesDashboard';
import VADashboard from './dashboards/VADashboard';
import { MacEmptyState, MacPage, MacPanel } from '@/components/macos/MacPrimitives';
import { Sparkles } from 'lucide-react';

export default function AgentDashboard() {
  const { scopedAgent } = useAgentContext();

  if (!scopedAgent) {
    return (
      <MacPage eyebrow="Dashboard" title="No agent selected" description="">
        <MacPanel title="Agent not found">
          <MacEmptyState
            icon={<Sparkles className="h-8 w-8" />}
            title="No agent"
            description="Select an agent from the roster to view their dashboard."
          />
        </MacPanel>
      </MacPage>
    );
  }

  switch (scopedAgent.profile.primary_class) {
    case 'social_media_manager':
      return <SocialMediaManagerDashboard agent={scopedAgent} />;
    case 'sales':
      return <SalesDashboard agent={scopedAgent} />;
    case 'va':
      return <VADashboard agent={scopedAgent} />;
    default:
      return <VADashboard agent={scopedAgent} />;
  }
}
