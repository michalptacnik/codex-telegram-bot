import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from 'react';
import React from 'react';
import {
  getToken as readToken,
  setToken as writeToken,
  clearToken as removeToken,
  isAuthenticated as checkAuth,
} from '../lib/auth';
import {
  pair as apiPair,
  getPublicHealth,
  getAdminPairCode,
  generateAdminPairCode,
} from '../lib/api';

// ---------------------------------------------------------------------------
// Context shape
// ---------------------------------------------------------------------------

export interface AuthState {
  /** The current bearer token, or null if not authenticated. */
  token: string | null;
  /** Whether the user is currently authenticated. */
  isAuthenticated: boolean;
  /** True while the initial auth check or auto-pair is in progress. */
  loading: boolean;
  /** Pair with the agent using a pairing code. Stores the token on success. */
  pair: (code: string) => Promise<void>;
  /** Clear the stored token and sign out. */
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export interface AuthProviderProps {
  children: ReactNode;
}

/**
 * Silently obtain a bearer token by fetching the pairing code from the
 * localhost-only admin endpoint and calling /pair automatically.
 * Only works when the gateway is running on 127.0.0.1.
 */
async function silentAutoPair(): Promise<string> {
  const health = await getPublicHealth();
  if (!health.require_pairing) {
    throw new Error('pairing_not_required');
  }
  // Try existing code first; generate a fresh one if none is available.
  let code: string | null = null;
  const existing = await getAdminPairCode();
  code = existing.pairing_code;
  if (!code) {
    const fresh = await generateAdminPairCode();
    code = fresh.pairing_code;
  }
  if (!code) throw new Error('No pairing code available from gateway');
  const { token } = await apiPair(code);
  return token;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [token, setTokenState] = useState<string | null>(readToken);
  const [authenticated, setAuthenticated] = useState<boolean>(checkAuth);
  // Start in loading state whenever we don't already have a valid token so the
  // "Connecting…" screen is shown while auto-pair runs (never the pairing dialog).
  const [loading, setLoading] = useState<boolean>(!checkAuth());

  const applyToken = useCallback((newToken: string) => {
    writeToken(newToken);
    setTokenState(newToken);
    setAuthenticated(true);
    setLoading(false);
  }, []);

  // On mount: auto-pair if we don't already have a stored token.
  // Retries every second until the gateway is reachable — this handles both
  // the case where the gateway hasn't started yet and the brief window while
  // ShellProvider resolves the real port from the Tauri command.
  useEffect(() => {
    if (checkAuth()) return;
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    async function tryPair(): Promise<void> {
      try {
        const health = await getPublicHealth();
        if (cancelled) return;

        if (!health.require_pairing) {
          setAuthenticated(true);
          setLoading(false);
          return;
        }

        let code: string | null = null;
        const existing = await getAdminPairCode();
        code = existing.pairing_code;
        if (!code) {
          const fresh = await generateAdminPairCode();
          code = fresh.pairing_code;
        }
        if (code && !cancelled) {
          const { token: newToken } = await apiPair(code);
          if (!cancelled) applyToken(newToken);
        }
      } catch {
        // Gateway not reachable yet (starting up or wrong port still loading).
        // Retry in 1 second; keep showing "Connecting…" — never show the dialog.
        if (!cancelled) {
          retryTimer = setTimeout(() => { void tryPair(); }, 1000);
        }
      }
    }

    void tryPair();

    return () => {
      cancelled = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
    };
  }, [applyToken]);

  // Keep state in sync if localStorage is changed in another tab.
  useEffect(() => {
    const handler = (e: StorageEvent) => {
      if (e.key === 'zeroclaw_token') {
        const t = readToken();
        setTokenState(t);
        setAuthenticated(t !== null && t.length > 0);
      }
    };
    window.addEventListener('storage', handler);
    return () => window.removeEventListener('storage', handler);
  }, []);

  // On 401: clear the stale token and auto-re-pair silently.
  // Keep loading=true while re-pairing so the pairing dialog is never shown.
  useEffect(() => {
    const handler = () => {
      removeToken();
      setTokenState(null);
      setAuthenticated(false);
      setLoading(true);
      silentAutoPair()
        .then(applyToken)
        .catch(() => setLoading(false)); // only reveal fallback dialog on failure
    };
    window.addEventListener('zeroclaw-unauthorized', handler);
    return () => window.removeEventListener('zeroclaw-unauthorized', handler);
  }, [applyToken]);

  const pair = useCallback(async (code: string): Promise<void> => {
    const { token: newToken } = await apiPair(code);
    applyToken(newToken);
  }, [applyToken]);

  const logout = useCallback((): void => {
    removeToken();
    setTokenState(null);
    setAuthenticated(false);
  }, []);

  const value: AuthState = {
    token,
    isAuthenticated: authenticated,
    loading,
    pair,
    logout,
  };

  return React.createElement(AuthContext.Provider, { value }, children);
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Access the authentication state from any component inside `<AuthProvider>`.
 * Throws if used outside the provider.
 */
export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an <AuthProvider>');
  }
  return ctx;
}
