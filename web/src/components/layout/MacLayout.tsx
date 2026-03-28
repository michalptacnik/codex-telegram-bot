import { useEffect, useMemo, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  ChevronRight,
  Command,
  Download,
  Globe,
  LogOut,
  PanelLeft,
  Search,
} from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';
import { useLocaleContext } from '@/App';
import { listenForMenuActions } from '@/lib/desktop';
import { useShell } from '@/components/shell/ShellProvider';
import badgeArt from '@/assets/agent-hq-badge.svg';
import { menuActionRoutes, navItems, primaryNavSections, routeTitles } from './navigation';

type PaletteAction = {
  id: string;
  title: string;
  subtitle: string;
  keywords: string;
  run: () => void;
};

type UpdateStatusDetail = {
  available: boolean;
  metadata?: {
    version: string;
  };
};

export default function MacLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { logout } = useAuth();
  const { locale, setAppLocale } = useLocaleContext();
  const { shell, sidebarOpen, toggleSidebar } = useShell();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [availableUpdateVersion, setAvailableUpdateVersion] = useState<string | null>(null);

  const pageTitle = routeTitles[location.pathname] ?? 'Agent HQ';
  const pageSubtitle =
    shell.platform === 'macos'
      ? 'Native macOS workspace'
      : 'Desktop workspace';

  const openPalette = () => setPaletteOpen(true);
  const closePalette = () => {
    setPaletteOpen(false);
    setQuery('');
  };

  const actions = useMemo<PaletteAction[]>(
    () => [
      ...navItems.map((item) => ({
        id: `route:${item.to}`,
        title: item.title,
        subtitle: 'Navigate to section',
        keywords: `${item.title} ${item.to}`,
        run: () => navigate(item.to),
      })),
      {
        id: 'action:toggle_sidebar',
        title: sidebarOpen ? 'Hide Sidebar' : 'Show Sidebar',
        subtitle: 'Change navigation density',
        keywords: 'sidebar navigation toggle',
        run: toggleSidebar,
      },
      {
        id: 'action:toggle_language',
        title: `Switch language to ${locale === 'en' ? 'Turkish' : 'English'}`,
        subtitle: 'Change interface language',
        keywords: 'language locale english turkish',
        run: () => setAppLocale(locale === 'en' ? 'tr' : 'en'),
      },
      {
        id: 'action:new_agent',
        title: 'New Agent',
        subtitle: 'Open the setup flow for another roster member',
        keywords: 'new create agent wizard roster',
        run: () => navigate('/?new=1'),
      },
      {
        id: 'action:new_mission',
        title: 'New Mission',
        subtitle: 'Jump to Missions and start a new objective',
        keywords: 'new create mission objective',
        run: () => navigate('/missions?new=1'),
      },
      {
        id: 'action:new_session',
        title: 'New Session',
        subtitle: 'Open the sessions workspace',
        keywords: 'new session chat',
        run: () => navigate('/sessions?new=1'),
      },
      {
        id: 'action:doctor',
        title: 'Run Diagnostics',
        subtitle: 'Open Doctor tools',
        keywords: 'doctor diagnostics health',
        run: () => navigate('/doctor'),
      },
      {
        id: 'action:updates',
        title: 'Check for Updates',
        subtitle: 'Check the signed release feed for a newer desktop build',
        keywords: 'update software release install upgrade',
        run: () => window.dispatchEvent(new CustomEvent('agenthq:check-updates')),
      },
      {
        id: 'action:logout',
        title: 'Log Out',
        subtitle: 'Clear the current pairing session',
        keywords: 'logout sign out auth',
        run: logout,
      },
    ],
    [locale, logout, navigate, setAppLocale, sidebarOpen, toggleSidebar],
  );

  const filteredActions = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) {
      return actions;
    }

    return actions.filter((action) =>
      `${action.title} ${action.subtitle} ${action.keywords}`.toLowerCase().includes(normalized),
    );
  }, [actions, query]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen(true);
        return;
      }

      if (event.key === 'Escape') {
        closePalette();
      }
    };

    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    let detach = () => {};

    void listenForMenuActions((action) => {
      if (action === 'view.toggle_sidebar') {
        toggleSidebar();
        return;
      }

      if (action === 'view.refresh') {
        window.location.reload();
        return;
      }

      if (action === 'view.command_palette' || action === 'view.search') {
        openPalette();
        return;
      }

      if (action === 'app.toggle_language') {
        setAppLocale(locale === 'en' ? 'tr' : 'en');
        return;
      }

      if (action === 'app.check_updates') {
        window.dispatchEvent(new CustomEvent('agenthq:check-updates'));
        return;
      }

      if (action === 'session.logout') {
        logout();
        return;
      }

      const route = menuActionRoutes[action];
      if (route) {
        navigate(route);
      }
    }).then((cleanup) => {
      detach = cleanup;
    });

    return () => detach();
  }, [locale, logout, navigate, setAppLocale, toggleSidebar]);

  useEffect(() => {
    const handleUpdateStatus = (event: Event) => {
      const detail = (event as CustomEvent<UpdateStatusDetail>).detail;
      if (!detail?.available) {
        setAvailableUpdateVersion(null);
        return;
      }

      setAvailableUpdateVersion(detail.metadata?.version ?? 'available');
    };

    window.addEventListener('agenthq:update-status', handleUpdateStatus as EventListener);
    return () =>
      window.removeEventListener('agenthq:update-status', handleUpdateStatus as EventListener);
  }, []);

  return (
    <>
      <div className="mac-shell">
        <aside className={sidebarOpen ? 'mac-sidebar' : 'mac-sidebar mac-sidebar-collapsed'}>
          <div className="mac-sidebar-brand">
            <img src={badgeArt} alt="Agent HQ" className="mac-sidebar-logo-image" />
            {sidebarOpen ? (
              <div>
                <p className="mac-sidebar-brand-title">Agent HQ</p>
                <p className="mac-sidebar-brand-detail">Local runtime, agents, and channels</p>
              </div>
            ) : null}
          </div>

          <nav className="mac-sidebar-nav">
            {primaryNavSections.map((section) => (
              <div key={section.title} className="mac-sidebar-section">
                {sidebarOpen ? <p className="mac-sidebar-section-title">{section.title}</p> : null}
                <div className="mac-sidebar-items">
                  {section.items.map(({ to, icon: Icon, title }) => (
                    <NavLink
                      key={to}
                      to={to}
                      end={to === '/' || to === '/dashboard'}
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
                <p className="mac-toolbar-overline">{pageSubtitle}</p>
                <h1 className="mac-toolbar-title">{pageTitle}</h1>
              </div>
            </div>

            <div className="mac-toolbar-trailing">
              {availableUpdateVersion ? (
                <button
                  type="button"
                  onClick={() => window.dispatchEvent(new CustomEvent('agenthq:check-updates'))}
                  className="mac-toolbar-pill mac-toolbar-pill-attention"
                  title={`Update ${availableUpdateVersion} is ready to install`}
                >
                  <Download className="h-4 w-4" />
                  <span>Update {availableUpdateVersion}</span>
                </button>
              ) : null}
              <button type="button" onClick={openPalette} className="mac-toolbar-search">
                <Search className="h-4 w-4" />
                <span>Jump to anything</span>
                <span className="mac-kbd">
                  <Command className="h-3.5 w-3.5" />
                  K
                </span>
              </button>
              <button
                type="button"
                onClick={() => setAppLocale(locale === 'en' ? 'tr' : 'en')}
                className="mac-toolbar-pill"
                title="Toggle language"
              >
                <Globe className="h-4 w-4" />
                <span>{locale === 'en' ? 'TR' : 'EN'}</span>
              </button>
              <button type="button" onClick={logout} className="mac-toolbar-pill">
                <LogOut className="h-4 w-4" />
                <span>Log Out</span>
              </button>
            </div>
          </header>

          <main className="mac-main">
            <div className="mac-main-scroll">
              <Outlet />
            </div>
          </main>
        </div>
      </div>

      {paletteOpen ? (
        <div className="mac-palette-backdrop" onClick={closePalette}>
          <div className="mac-palette" onClick={(event) => event.stopPropagation()}>
            <div className="mac-palette-header">
              <Search className="h-4 w-4 text-[var(--shell-muted)]" />
              <input
                autoFocus
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="mac-palette-input"
                placeholder="Search sections, actions, and diagnostics"
              />
            </div>
            <div className="mac-palette-list">
              {filteredActions.slice(0, 12).map((action) => (
                <button
                  key={action.id}
                  type="button"
                  className="mac-palette-item"
                  onClick={() => {
                    action.run();
                    closePalette();
                  }}
                >
                  <div>
                    <div className="mac-palette-title">{action.title}</div>
                    <div className="mac-palette-detail">{action.subtitle}</div>
                  </div>
                  <ChevronRight className="h-4 w-4 text-[var(--shell-muted)]" />
                </button>
              ))}
              {filteredActions.length === 0 ? (
                <div className="mac-palette-empty">No matching actions.</div>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
