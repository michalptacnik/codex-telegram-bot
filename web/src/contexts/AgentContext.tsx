import { createContext, useContext, useState, useEffect, useCallback } from 'react';
import type { ResolvedAgentProfile } from '@/types/api';
import { getAgents, activateAgent as apiActivateAgent } from '@/lib/api';

interface AgentContextType {
  agents: ResolvedAgentProfile[];
  activeAgentId: string;
  /** The agent being viewed in the scoped layout (from URL param). */
  scopedAgent: ResolvedAgentProfile | null;
  setScopedAgent: (agent: ResolvedAgentProfile | null) => void;
  activateAgent: (id: string) => Promise<void>;
  refreshAgents: () => Promise<void>;
  loading: boolean;
}

const AgentContext = createContext<AgentContextType>({
  agents: [],
  activeAgentId: '',
  scopedAgent: null,
  setScopedAgent: () => {},
  activateAgent: async () => {},
  refreshAgents: async () => {},
  loading: true,
});

export function AgentProvider({ children }: { children: React.ReactNode }) {
  const [agents, setAgents] = useState<ResolvedAgentProfile[]>([]);
  const [activeAgentId, setActiveAgentId] = useState('');
  const [scopedAgent, setScopedAgent] = useState<ResolvedAgentProfile | null>(null);
  const [loading, setLoading] = useState(true);

  const refreshAgents = useCallback(async () => {
    try {
      const data = await getAgents();
      setAgents(data.profiles);
      setActiveAgentId(data.active_agent_id);
    } catch {
      // silently fail — auth might not be ready yet
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshAgents();
  }, [refreshAgents]);

  const activateAgent = useCallback(
    async (id: string) => {
      await apiActivateAgent(id);
      await refreshAgents();
    },
    [refreshAgents],
  );

  return (
    <AgentContext.Provider
      value={{
        agents,
        activeAgentId,
        scopedAgent,
        setScopedAgent,
        activateAgent,
        refreshAgents,
        loading,
      }}
    >
      {children}
    </AgentContext.Provider>
  );
}

export function useAgentContext() {
  return useContext(AgentContext);
}
