import { Routes, Route, Navigate, useNavigate } from 'react-router-dom';
import { useState, useEffect, createContext, useContext } from 'react';
import { ShellProvider, useShell } from './components/shell/ShellProvider';
import UpdateController from './components/shell/UpdateController';
import badgeArt from './assets/agent-hq-badge.svg';
import { AgentProvider, useAgentContext } from './contexts/AgentContext';
import { AuthProvider, useAuth } from './hooks/useAuth';
import { setLocale, type Locale } from './lib/i18n';

// Pages
import AgentSelector from './pages/AgentSelector';
import AgentCreationWizard from './pages/AgentCreationWizard';
import AgentDashboard from './pages/AgentDashboard';
import AgentChat from './pages/AgentChat';
import AgentAutomations from './pages/AgentAutomations';
import AgentSettings from './pages/AgentSettings';
import SocialAccounts from './pages/SocialAccounts';
import Tools from './pages/Tools';
import Cron from './pages/Cron';
import Integrations from './pages/Integrations';
import Memory from './pages/Memory';
import Config from './pages/Config';
import Cost from './pages/Cost';
import Logs from './pages/Logs';
import Doctor from './pages/Doctor';
import Soul from './pages/Soul';
import Missions from './pages/Missions';
import Plugins from './pages/Plugins';
import Sessions from './pages/Sessions';

// Layouts
import AgentScopedLayout from './components/layout/AgentScopedLayout';
import GlobalLayout from './components/layout/GlobalLayout';

// Locale context
interface LocaleContextType {
  locale: string;
  setAppLocale: (locale: string) => void;
}

export const LocaleContext = createContext<LocaleContextType>({
  locale: 'tr',
  setAppLocale: () => {},
});

export const useLocaleContext = () => useContext(LocaleContext);

// Pairing dialog component
function PairingDialog({ onPair }: { onPair: (code: string) => Promise<void> }) {
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { isDesktopMac } = useShell();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      await onPair(code);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Pairing failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className={
        isDesktopMac
          ? 'min-h-screen bg-[radial-gradient(circle_at_top,#f6f8fc_0%,#edf1f8_42%,#dce4f0_100%)] dark:bg-[radial-gradient(circle_at_top,#1c2534_0%,#121a27_42%,#0c1119_100%)] flex items-center justify-center p-6'
          : 'min-h-screen bg-gray-950 flex items-center justify-center'
      }
    >
      <div
        className={
          isDesktopMac
            ? 'w-full max-w-lg rounded-[32px] border border-white/55 bg-white/72 p-8 shadow-[0_28px_80px_rgba(15,23,42,0.18)] backdrop-blur-2xl dark:border-white/12 dark:bg-white/8'
            : 'bg-gray-900 rounded-xl p-8 w-full max-w-md border border-gray-800'
        }
      >
        <div className="text-center mb-6">
          <div
            className={
              isDesktopMac
                ? 'mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-[20px] bg-[linear-gradient(180deg,rgba(151,186,255,0.35),rgba(255,255,255,0.9))] shadow-[inset_0_1px_0_rgba(255,255,255,0.75)] dark:bg-[linear-gradient(180deg,rgba(94,145,255,0.45),rgba(255,255,255,0.12))]'
                : 'mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-amber-300/15'
            }
          >
            <img src={badgeArt} alt="Agent HQ" className="h-12 w-12" />
          </div>
          <h1 className={isDesktopMac ? 'text-3xl font-semibold text-slate-900 dark:text-white mb-2' : 'text-2xl font-bold text-white mb-2'}>
            Agent HQ
          </h1>
          <p className={isDesktopMac ? 'text-sm text-slate-500 dark:text-slate-400 mb-2' : 'text-sm text-gray-500 mb-2'}>
            Local operator console for your Rust runtime
          </p>
          <p className={isDesktopMac ? 'text-slate-600 dark:text-slate-300' : 'text-gray-400'}>
            Enter the pairing code from your local Agent HQ instance
          </p>
        </div>
        <form onSubmit={handleSubmit}>
          <input
            type="text"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="6-digit code"
            className={
              isDesktopMac
                ? 'w-full px-4 py-3 rounded-2xl border border-white/65 bg-white/85 text-slate-900 text-center text-2xl tracking-[0.35em] shadow-[inset_0_1px_0_rgba(255,255,255,0.8)] focus:outline-none focus:border-sky-400 mb-4 dark:border-white/12 dark:bg-black/18 dark:text-white'
                : 'w-full px-4 py-3 bg-gray-800 border border-gray-700 rounded-lg text-white text-center text-2xl tracking-widest focus:outline-none focus:border-blue-500 mb-4'
            }
            maxLength={6}
            autoFocus
          />
          {error && (
            <p className={isDesktopMac ? 'text-rose-600 dark:text-rose-300 text-sm mb-4 text-center' : 'text-red-400 text-sm mb-4 text-center'}>
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={loading || code.length < 6}
            className={
              isDesktopMac
                ? 'w-full py-3 rounded-2xl bg-[linear-gradient(180deg,#0a84ff,#006ae6)] text-white font-medium shadow-[0_18px_32px_rgba(0,106,230,0.28)] transition hover:brightness-105 disabled:bg-slate-300 disabled:text-slate-500 disabled:shadow-none dark:disabled:bg-white/10 dark:disabled:text-slate-500'
                : 'w-full py-3 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 disabled:text-gray-500 text-white rounded-lg font-medium transition-colors'
            }
          >
            {loading ? 'Pairing...' : 'Pair'}
          </button>
        </form>
      </div>
    </div>
  );
}

/**
 * Redirects `/` based on agent count:
 * - 0 agents → /setup (wizard)
 * - 1+ agents → /agents (selector)
 */
function StartupRedirector() {
  const { agents, loading } = useAgentContext();
  const navigate = useNavigate();

  useEffect(() => {
    if (!loading) {
      navigate(agents.length === 0 ? '/setup' : '/agents', { replace: true });
    }
  }, [loading, agents.length, navigate]);

  return null;
}

function AppContent() {
  const { isAuthenticated, loading, pair } = useAuth();
  const [locale, setLocaleState] = useState('tr');
  const { isDesktopMac } = useShell();

  const setAppLocale = (newLocale: string) => {
    setLocaleState(newLocale);
    setLocale(newLocale as Locale);
  };

  if (loading) {
    return (
      <div
        className={
          isDesktopMac
            ? 'min-h-screen bg-[radial-gradient(circle_at_top,#f6f8fc_0%,#edf1f8_42%,#dce4f0_100%)] dark:bg-[radial-gradient(circle_at_top,#1c2534_0%,#121a27_42%,#0c1119_100%)] flex items-center justify-center'
            : 'min-h-screen bg-gray-950 flex items-center justify-center'
        }
      >
        <p className={isDesktopMac ? 'text-slate-600 dark:text-slate-300' : 'text-gray-400'}>
          Connecting...
        </p>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <PairingDialog onPair={pair} />;
  }

  return (
    <LocaleContext.Provider value={{ locale, setAppLocale }}>
      <AgentProvider>
        <Routes>
          {/* Root redirect based on agent count */}
          <Route path="/" element={<StartupRedirector />} />

          {/* Agent selector (fullscreen, no sidebar) */}
          <Route path="/agents" element={<AgentSelectorWrapper />} />

          {/* Agent creation wizard (fullscreen) */}
          <Route path="/setup" element={<AgentCreationWizardWrapper />} />

          {/* Agent-scoped routes */}
          <Route path="/agents/:agentId" element={<AgentScopedLayout />}>
            <Route index element={<AgentDashboard />} />
            <Route path="chat" element={<AgentChat />} />
            <Route path="missions" element={<Missions />} />
            <Route path="sessions" element={<Sessions />} />
            <Route path="automations" element={<AgentAutomations />} />
            <Route path="tools" element={<Tools />} />
            <Route path="memory" element={<Memory />} />
            <Route path="social-accounts" element={<SocialAccounts />} />
            <Route path="settings" element={<AgentSettings />} />
            <Route path="soul" element={<Soul />} />
          </Route>

          {/* Global routes */}
          <Route element={<GlobalLayout />}>
            <Route path="/automations" element={<Cron />} />
            <Route path="/integrations" element={<Integrations />} />
            <Route path="/plugins" element={<Plugins />} />
            <Route path="/settings" element={<Config />} />
            <Route path="/cost" element={<Cost />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/doctor" element={<Doctor />} />
          </Route>

          {/* Legacy redirects — old flat routes to new structure */}
          <Route path="/dashboard" element={<LegacyRedirect to="/" />} />
          <Route path="/agent" element={<LegacyRedirect to="/chat" />} />
          <Route path="/cron" element={<Navigate to="/automations" replace />} />
          <Route path="/config" element={<Navigate to="/settings" replace />} />
          <Route path="/soul" element={<LegacyRedirect to="/soul" />} />
          <Route path="/missions" element={<LegacyRedirect to="/missions" />} />
          <Route path="/sessions" element={<LegacyRedirect to="/sessions" />} />
          <Route path="/tools" element={<LegacyRedirect to="/tools" />} />
          <Route path="/memory" element={<LegacyRedirect to="/memory" />} />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AgentProvider>
    </LocaleContext.Provider>
  );
}

/** Wraps AgentSelector in its own shell layout (fullscreen with mac chrome) */
function AgentSelectorWrapper() {
  const { isDesktopMac } = useShell();

  if (isDesktopMac) {
    return (
      <div style={{ minHeight: '100vh', paddingTop: '52px' }}>
        <div className="mac-main-scroll" style={{ maxWidth: '1200px', margin: '0 auto' }}>
          <AgentSelector />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <main className="max-w-6xl mx-auto px-6 py-8">
        <AgentSelector />
      </main>
    </div>
  );
}

/** Wraps AgentCreationWizard similarly */
function AgentCreationWizardWrapper() {
  const { isDesktopMac } = useShell();

  if (isDesktopMac) {
    return (
      <div style={{ minHeight: '100vh', paddingTop: '52px' }}>
        <div className="mac-main-scroll" style={{ maxWidth: '1200px', margin: '0 auto' }}>
          <AgentCreationWizard />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <main className="max-w-6xl mx-auto px-6 py-8">
        <AgentCreationWizard />
      </main>
    </div>
  );
}

/**
 * Legacy redirect: maps old flat routes (e.g. /dashboard, /agent) to agent-scoped routes.
 * Uses the active agent ID from context.
 */
function LegacyRedirect({ to }: { to: string }) {
  const { activeAgentId, loading } = useAgentContext();

  if (loading) return null;

  if (!activeAgentId) {
    return <Navigate to="/agents" replace />;
  }

  // `to` is relative (e.g. "/chat" → "/agents/{id}/chat", "/" → "/agents/{id}")
  const target = to === '/' ? `/agents/${activeAgentId}` : `/agents/${activeAgentId}${to}`;
  return <Navigate to={target} replace />;
}

export default function App() {
  return (
    <ShellProvider>
      <UpdateController />
      <AuthProvider>
        <AppContent />
      </AuthProvider>
    </ShellProvider>
  );
}
