import { Routes, Route, Navigate } from 'react-router-dom';
import { useState, useEffect, createContext, useContext } from 'react';
import Layout from './components/layout/Layout';
import { ShellProvider, useShell } from './components/shell/ShellProvider';
import UpdateController from './components/shell/UpdateController';
import badgeArt from './assets/agent-hq-badge.svg';
import Dashboard from './pages/Dashboard';
import AgentChat from './pages/AgentChat';
import Studio from './pages/Studio';
import Tools from './pages/Tools';
import Cron from './pages/Cron';
import Integrations from './pages/Integrations';
import Memory from './pages/Memory';
import Config from './pages/Config';
import Cost from './pages/Cost';
import Logs from './pages/Logs';
import Doctor from './pages/Doctor';
// Agent HQ pages
import Soul from './pages/Soul';
import Missions from './pages/Missions';
import Plugins from './pages/Plugins';
import Sessions from './pages/Sessions';
import { AuthProvider, useAuth } from './hooks/useAuth';
import { setLocale, type Locale } from './lib/i18n';

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

function AppContent() {
  const { isAuthenticated, loading, pair, logout } = useAuth();
  const [locale, setLocaleState] = useState('tr');
  const { isDesktopMac } = useShell();

  const setAppLocale = (newLocale: string) => {
    setLocaleState(newLocale);
    setLocale(newLocale as Locale);
  };

  // Listen for 401 events to force logout
  useEffect(() => {
    const handler = () => {
      logout();
    };
    window.addEventListener('zeroclaw-unauthorized', handler);
    return () => window.removeEventListener('zeroclaw-unauthorized', handler);
  }, [logout]);

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
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Studio />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/agent" element={<AgentChat />} />
          <Route path="/tools" element={<Tools />} />
          <Route path="/cron" element={<Cron />} />
          <Route path="/integrations" element={<Integrations />} />
          <Route path="/memory" element={<Memory />} />
          <Route path="/config" element={<Config />} />
          <Route path="/cost" element={<Cost />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/doctor" element={<Doctor />} />
          {/* Agent HQ routes */}
          <Route path="/soul" element={<Soul />} />
          <Route path="/missions" element={<Missions />} />
          <Route path="/plugins" element={<Plugins />} />
          <Route path="/sessions" element={<Sessions />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </LocaleContext.Provider>
  );
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
