import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { ArrowLeft, PanelLeft } from 'lucide-react';
import { useShell } from '@/components/shell/ShellProvider';
import { globalNavSections } from './navigation';
import badgeArt from '@/assets/agent-hq-badge.svg';

function GlobalSidebar() {
  const navigate = useNavigate();

  return (
    <aside className="fixed top-0 left-0 h-screen w-60 bg-gray-900 flex flex-col border-r border-gray-800">
      {/* Logo + back to agents */}
      <div className="px-4 py-4 border-b border-gray-800">
        <button
          type="button"
          onClick={() => navigate('/agents')}
          className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white transition-colors mb-3"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Agents
        </button>
        <div className="flex items-center gap-2">
          <div className="h-8 w-8 rounded-lg bg-blue-600 flex items-center justify-center text-white font-bold text-sm">
            HQ
          </div>
          <div className="flex flex-col">
            <span className="text-lg font-semibold text-white tracking-wide leading-tight">
              Agent HQ
            </span>
            <span className="text-[10px] text-gray-500 leading-tight">
              Global Settings
            </span>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-4 px-3 space-y-1">
        {globalNavSections.map((section) => (
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
    </aside>
  );
}

function MacGlobalSidebar({ sidebarOpen }: { sidebarOpen: boolean }) {
  const navigate = useNavigate();

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
          {sidebarOpen ? 'Agents' : null}
        </button>
        <div className="flex items-center gap-2.5">
          <img src={badgeArt} alt="Agent HQ" className="h-8 w-8" />
          {sidebarOpen ? (
            <div>
              <p className="mac-sidebar-brand-title">Agent HQ</p>
              <p className="mac-sidebar-brand-detail">Global settings &amp; operations</p>
            </div>
          ) : null}
        </div>
      </div>

      <nav className="mac-sidebar-nav">
        {globalNavSections.map((section) => (
          <div key={section.title} className="mac-sidebar-section">
            {sidebarOpen ? <p className="mac-sidebar-section-title">{section.title}</p> : null}
            <div className="mac-sidebar-items">
              {section.items.map(({ to, icon: Icon, title }) => (
                <NavLink
                  key={to}
                  to={to}
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

export default function GlobalLayout() {
  const location = useLocation();
  const { isDesktopMac, sidebarOpen, toggleSidebar } = useShell();
  const titleMap: Record<string, string> = {
    '/automations': 'All Automations',
    '/integrations': 'Integrations',
    '/plugins': 'Plugins',
    '/settings': 'Settings',
    '/cost': 'Cost',
    '/logs': 'Logs',
    '/doctor': 'Doctor',
  };
  const pageTitle = titleMap[location.pathname] ?? 'Agent HQ';

  if (isDesktopMac) {
    return (
      <div className="mac-shell">
        <MacGlobalSidebar sidebarOpen={sidebarOpen} />
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
                <p className="mac-toolbar-overline">Global</p>
                <h1 className="mac-toolbar-title">{pageTitle}</h1>
              </div>
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
      <GlobalSidebar />
      <div className="ml-60 flex flex-col min-h-screen">
        <header className="sticky top-0 z-10 flex items-center h-14 px-6 border-b border-gray-800 bg-gray-950/80 backdrop-blur">
          <div>
            <p className="text-[10px] text-gray-500 uppercase tracking-wider">Global</p>
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
