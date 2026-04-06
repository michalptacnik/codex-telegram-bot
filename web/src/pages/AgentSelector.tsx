import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, Search } from 'lucide-react';
import { useAgentContext } from '@/contexts/AgentContext';
import { useShell } from '@/components/shell/ShellProvider';
import { MacBadge, MacPage, MacSearchField, MacEmptyState } from '@/components/macos/MacPrimitives';
import socialMediaManagerArt from '@/assets/class-social-media-manager.png';
import salesArt from '@/assets/class-sales.png';
import vaArt from '@/assets/class-va.png';
import type { ResolvedAgentProfile } from '@/types/api';

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

function AgentCard({
  agent,
  isActive,
  onClick,
  isDesktopMac,
}: {
  agent: ResolvedAgentProfile;
  isActive: boolean;
  onClick: () => void;
  isDesktopMac: boolean;
}) {
  const artwork = classArtwork(agent.profile.primary_class);

  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'group flex items-start gap-4 rounded-[1.25rem] border p-5 text-left transition',
        isActive
          ? 'border-sky-400/50 bg-sky-500/10'
          : isDesktopMac
            ? 'border-[var(--shell-border)] bg-[var(--shell-panel)] hover:bg-[var(--shell-selection)]'
            : 'border-gray-800 bg-gray-900 hover:bg-gray-800',
      ].join(' ')}
    >
      {artwork ? (
        <img
          src={artwork}
          alt={agent.profile.name}
          className="h-16 w-16 shrink-0 rounded-[1rem] object-contain p-1 studio-art-frame"
        />
      ) : (
        <div className={`h-16 w-16 shrink-0 rounded-[1rem] flex items-center justify-center text-2xl ${isDesktopMac ? 'bg-[var(--shell-selection)]' : 'bg-gray-800'}`}>
          {agent.identity.emoji}
        </div>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className={`text-base font-semibold ${isDesktopMac ? '' : 'text-white'}`}>{agent.profile.name}</h3>
          {isActive ? <MacBadge tone="accent">Active</MacBadge> : null}
        </div>
        <p className={`mt-0.5 text-xs uppercase tracking-wider ${isDesktopMac ? 'text-[var(--shell-muted)]' : 'text-gray-500'}`}>
          {agent.identity.role_title}
        </p>
        <p className={`mt-2 text-sm line-clamp-2 ${isDesktopMac ? 'text-[var(--shell-muted)]' : 'text-gray-400'}`}>
          {agent.summary}
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {agent.classes.map((c) => (
            <MacBadge key={c.id} tone="neutral">{c.name}</MacBadge>
          ))}
        </div>
      </div>
    </button>
  );
}

export default function AgentSelector() {
  const { agents, activeAgentId, loading } = useAgentContext();
  const { isDesktopMac } = useShell();
  const navigate = useNavigate();
  const [query, setQuery] = useState('');

  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return agents;
    return agents.filter((a) => {
      const haystack = [
        a.profile.name,
        a.summary,
        a.identity.role_title,
        ...a.classes.map((c) => c.name),
      ].join(' ').toLowerCase();
      return haystack.includes(normalized);
    });
  }, [agents, query]);

  if (loading) {
    return (
      <MacPage
        eyebrow="Agent HQ"
        title="Loading agents..."
        description="Fetching your agent roster."
      >
        <div />
      </MacPage>
    );
  }

  return (
    <MacPage
      eyebrow="Agent HQ"
      title="Choose an Agent"
      description="Select an agent to enter its workspace, or create a new one."
    >
      <div className="flex items-center gap-3 mb-6">
        <div className="flex-1">
          <MacSearchField
            value={query}
            onChange={setQuery}
            placeholder="Search agents, roles, and classes"
          />
        </div>
        <button
          type="button"
          onClick={() => navigate('/setup')}
          className={
            isDesktopMac
              ? 'inline-flex items-center gap-2 rounded-full bg-[#0a84ff] px-5 py-2.5 text-sm font-semibold text-white shadow-[0_12px_24px_rgba(10,132,255,0.22)] transition hover:brightness-105'
              : 'inline-flex items-center gap-2 rounded-full bg-sky-500 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-400'
          }
        >
          <Plus className="h-4 w-4" />
          New Agent
        </button>
      </div>

      {filtered.length === 0 && agents.length === 0 ? (
        <MacEmptyState
          icon={<Plus className="h-8 w-8" />}
          title="No agents yet"
          description="Create your first agent to get started."
        />
      ) : filtered.length === 0 ? (
        <MacEmptyState
          icon={<Search className="h-7 w-7" />}
          title="No matching agents"
          description="Try a different search term."
        />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((agent) => (
            <AgentCard
              key={agent.profile.id}
              agent={agent}
              isActive={agent.profile.id === activeAgentId}
              onClick={() => navigate(`/agents/${agent.profile.id}`)}
              isDesktopMac={isDesktopMac}
            />
          ))}
        </div>
      )}

      {/* Global shortcuts */}
      <div className={`mt-8 flex flex-wrap gap-3 pt-6 border-t ${isDesktopMac ? 'border-[var(--shell-border)]' : 'border-gray-800'}`}>
        <button
          type="button"
          onClick={() => navigate('/automations')}
          className={`text-sm ${isDesktopMac ? 'text-[var(--shell-muted)] hover:text-[var(--shell-fg)]' : 'text-gray-500 hover:text-gray-300'} transition-colors`}
        >
          All Automations
        </button>
        <button
          type="button"
          onClick={() => navigate('/settings')}
          className={`text-sm ${isDesktopMac ? 'text-[var(--shell-muted)] hover:text-[var(--shell-fg)]' : 'text-gray-500 hover:text-gray-300'} transition-colors`}
        >
          Global Settings
        </button>
        <button
          type="button"
          onClick={() => navigate('/cost')}
          className={`text-sm ${isDesktopMac ? 'text-[var(--shell-muted)] hover:text-[var(--shell-fg)]' : 'text-gray-500 hover:text-gray-300'} transition-colors`}
        >
          Cost
        </button>
      </div>
    </MacPage>
  );
}
