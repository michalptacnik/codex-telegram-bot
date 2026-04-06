import { useEffect } from 'react';
import { NavLink, Outlet, useNavigate, useParams, useLocation } from 'react-router-dom';
import { ArrowLeft, PanelLeft } from 'lucide-react';
import { useAgentContext } from '@/contexts/AgentContext';
import { useShell } from '@/components/shell/ShellProvider';
import { getAgentNavSections, type NavSection } from './navigation';
import { MacBadge } from '@/components/macos/MacPrimitives';
import socialMediaManagerArt from '@/assets/class-social-media-manager.png';
import salesArt from '@/assets/class-sales.png';
import vaArt from '@/assets/class-va.png';

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

function AgentScopedSidebar({ sections, agentName, agentClass, agentEmoji }: {
  sections: NavSection[];
  agentName: string;
  agentClass: string;
  agentEmoji: string;
}) {
  const navigate = useNavigate();
  const artwork = classArtwork(agentClass);

  return (
    <aside className="fixed top-0 left-0 h-screen w-60 bg-gray-900 flex flex-col border-r border-gray-800">
      {/* Back + Agent identity */}
      <div className="px-4 py-4 border-b border-gray-800">
        <button
          type="button"
          onClick={() => navigate('/agents')}
          className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white transition-colors mb-3"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          All Agents
        </button>
        <div className="flex items-center gap-3">
          {artwork ? (
            <img src={artwork} alt={agentName} className="h-10 w-10 rounded-xl object-contain" />
          ) : (
            <div className="h-10 w-10 rounded-xl bg-gray-800 flex items-center justify-center text-lg">
              {agentEmoji}
            </div>
          )}
          <div className="min-w-0">
            <p className="text-sm font-semibold text-white truncate">{agentName}</p>
            <p className="text-[10px] text-gray-500 truncate">{agentClass.replace(/_/g, ' ')}</p>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-4 px-3 space-y-1">
        {sections.map((section) => (
          <div key={section.title}>
            <div className="pt-4 pb-2 px-3">
              <span className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
                {section.title}
              </span>
            </div>
            {section.items.map(({ to, icon: Icon, title }) => (
              <NavLink
                key={to}
                to={to}
                end={to.endsWith(`/agents/`) || !to.includes('/', to.lastIndexOf('/agents/') + 9)}
                className={({ isActive }) =>
                  [
                    'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-blue-600 text-white'
                      : 'text-gray-300 hover:bg-gray-800 hover:text-white',
                  ].join(' ')
                }
              >
                <Icon className="h-5 w-5 flex-shrink-0" />
                <span>{title}</span>
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      {/* Quick global links */}
      <div className="px-4 py-3 border-t border-gray-800">
        <div className="flex gap-2">
          <NavLink to="/settings" className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
            Settings
          </NavLink>
          <NavLink to="/cost" className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
            Cost
          </NavLink>
          <NavLink to="/logs" className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
            Logs
          </NavLink>
        </div>
      </div>
    </aside>
  );
}

function MacAgentScopedSidebar({ sections, agentName, agentClass, agentEmoji, sidebarOpen }: {
  sections: NavSection[];
  agentName: string;
  agentClass: string;
  agentEmoji: string;
  sidebarOpen: boolean;
}) {
  const navigate = useNavigate();
  const artwork = classArtwork(agentClass);

  return (
    <aside className={sidebarOpen ? 'mac-sidebar' : 'mac-sidebar mac-sidebar-collapsed'}>
      <div className="mac-sidebar-brand" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: '0.5rem' }}>
        <button
          type="button"
          onClick={() => navigate('/agents')}
          className="flex items-center gap-1.5 text-[0.7rem] opacity-60 hover:opacity-100 transition-opacity"
          style={{ color: 'var(--shell-muted)' }}
        >
          <ArrowLeft className="h-3 w-3" />
          {sidebarOpen ? 'All Agents' : null}
        </button>
        <div className="flex items-center gap-2.5">
          {artwork ? (
            <img src={artwork} alt={agentName} className="h-8 w-8 rounded-[10px] object-contain" />
          ) : (
            <span className="text-lg">{agentEmoji}</span>
          )}
          {sidebarOpen ? (
            <div>
              <p className="mac-sidebar-brand-title">{agentName}</p>
              <p className="mac-sidebar-brand-detail">{agentClass.replace(/_/g, ' ')}</p>
            </div>
          ) : null}
        </div>
      </div>

      <nav className="mac-sidebar-nav">
        {sections.map((section) => (
          <div key={section.title} className="mac-sidebar-section">
            {sidebarOpen ? <p className="mac-sidebar-section-title">{section.title}</p> : null}
            <div className="mac-sidebar-items">
              {section.items.map(({ to, icon: Icon, title }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={!to.includes('/', to.lastIndexOf('/agents/') + 9)}
                  className={({ isActive }) =>
                    [
                      'mac-sidebar-link',
                      isActive ? 'mac-sidebar-link-active' : '',
                      sidebarOpen ? '' : 'mac-sidebar-link-icon-only',
                    ].join(' ')
                  }
                  title={title}
                >
                  <Icon className="h-4 w-4" />
                  {sidebarOpen ? <span>{title}</span> : null}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>
    </aside>
  );
}

export default function AgentScopedLayout() {
  const { agentId } = useParams<{ agentId: string }>();
  const { agents, setScopedAgent, loading } = useAgentContext();
  const { isDesktopMac, sidebarOpen, toggleSidebar } = useShell();
  const navigate = useNavigate();
  const location = useLocation();

  const agent = agents.find((a) => a.profile.id === agentId) ?? null;

  useEffect(() => {
    setScopedAgent(agent);
    return () => setScopedAgent(null);
  }, [agent, setScopedAgent]);

  // Redirect if agent not found (after loading completes)
  useEffect(() => {
    if (!loading && !agent && agentId) {
      navigate('/agents', { replace: true });
    }
  }, [loading, agent, agentId, navigate]);

  if (loading || !agent) {
    return null;
  }

  const sections = getAgentNavSections(agent.profile.id, agent.profile.primary_class);

  // Derive page title from current path
  const pathSuffix = location.pathname.replace(`/agents/${agentId}`, '') || '/';
  const currentItem = sections.flatMap((s) => s.items).find((item) => {
    const itemSuffix = item.to.replace(`/agents/${agentId}`, '') || '/';
    return itemSuffix === pathSuffix;
  });
  const pageTitle = currentItem?.title ?? agent.profile.name;

  if (isDesktopMac) {
    return (
      <div className="mac-shell">
        <MacAgentScopedSidebar
          sections={sections}
          agentName={agent.profile.name}
          agentClass={agent.profile.primary_class}
          agentEmoji={agent.identity.emoji}
          sidebarOpen={sidebarOpen}
        />
        <div className="mac-content">
          <header className="mac-toolbar">
            <div className="mac-toolbar-leading">
              <button
                type="button"
                onClick={toggleSidebar}
                className="mac-toolbar-button"
                aria-label="Toggle sidebar"
              >
                <PanelLeft className="h-4 w-4" />
              </button>
              <div className="mac-toolbar-titles" data-tauri-drag-region>
                <p className="mac-toolbar-overline">{agent.identity.role_title}</p>
                <h1 className="mac-toolbar-title">{pageTitle}</h1>
              </div>
            </div>
            <div className="mac-toolbar-trailing">
              <MacBadge tone="accent">{agent.profile.primary_class.replace(/_/g, ' ')}</MacBadge>
            </div>
          </header>
          <main className="mac-main">
            <div className="mac-main-scroll">
              <Outlet />
            </div>
          </main>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <AgentScopedSidebar
        sections={sections}
        agentName={agent.profile.name}
        agentClass={agent.profile.primary_class}
        agentEmoji={agent.identity.emoji}
      />
      <div className="ml-60 flex flex-col min-h-screen">
        <header className="sticky top-0 z-10 flex items-center justify-between h-14 px-6 border-b border-gray-800 bg-gray-950/80 backdrop-blur">
          <div>
            <p className="text-[10px] text-gray-500 uppercase tracking-wider">{agent.identity.role_title}</p>
            <h1 className="text-sm font-semibold text-white">{pageTitle}</h1>
          </div>
        </header>
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
