import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { getDesktopShellInfo } from '@/lib/desktop';
import type { DesktopAppearance, DesktopShellInfo } from '@/types/api';

interface ShellContextValue {
  shell: DesktopShellInfo;
  isDesktop: boolean;
  isDesktopMac: boolean;
  resolvedAppearance: Exclude<DesktopAppearance, 'system'>;
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
  toggleSidebar: () => void;
}

const DEFAULT_SHELL: DesktopShellInfo = {
  name: 'Agent HQ',
  mode: 'browser',
  runtime_host: 'http://127.0.0.1:8765',
  platform: 'web',
  appearance: 'system',
  menuDriven: false,
  supportsTranslucency: false,
  windowStyle: 'browser',
  updateConfigured: false,
};

const ShellContext = createContext<ShellContextValue>({
  shell: DEFAULT_SHELL,
  isDesktop: false,
  isDesktopMac: false,
  resolvedAppearance: 'dark',
  sidebarOpen: true,
  setSidebarOpen: () => {},
  toggleSidebar: () => {},
});

function resolveAppearance(preference: DesktopAppearance): 'light' | 'dark' {
  if (preference === 'light' || preference === 'dark') {
    return preference;
  }

  if (
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-color-scheme: light)').matches
  ) {
    return 'light';
  }

  return 'dark';
}

export function ShellProvider({ children }: { children: ReactNode }) {
  const [shell, setShell] = useState<DesktopShellInfo>(DEFAULT_SHELL);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [resolvedAppearance, setResolvedAppearance] = useState<'light' | 'dark'>(
    resolveAppearance(DEFAULT_SHELL.appearance),
  );

  useEffect(() => {
    let mounted = true;

    void getDesktopShellInfo().then((info) => {
      if (!mounted) return;
      setShell(info);
      setResolvedAppearance(resolveAppearance(info.appearance));
    });

    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: light)');
    const update = () => setResolvedAppearance(resolveAppearance(shell.appearance));
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [shell.appearance]);

  useEffect(() => {
    const root = document.documentElement;
    const body = document.body;
    const desktopClass = shell.platform === 'macos' ? 'desktop-macos' : 'desktop-browser';

    root.dataset.shellPlatform = shell.platform;
    root.dataset.shellMode = shell.mode;
    root.dataset.appearance = resolvedAppearance;
    body.classList.toggle('desktop-shell', shell.platform !== 'web');
    body.classList.toggle('desktop-macos', shell.platform === 'macos');
    body.classList.remove(desktopClass === 'desktop-macos' ? 'desktop-browser' : 'desktop-macos');
    body.classList.add(desktopClass);

    return () => {
      delete root.dataset.shellPlatform;
      delete root.dataset.shellMode;
      delete root.dataset.appearance;
      body.classList.remove('desktop-shell', 'desktop-macos', 'desktop-browser');
    };
  }, [resolvedAppearance, shell.mode, shell.platform]);

  const value = useMemo<ShellContextValue>(
    () => ({
      shell,
      isDesktop: shell.platform !== 'web',
      isDesktopMac: shell.platform === 'macos',
      resolvedAppearance,
      sidebarOpen,
      setSidebarOpen,
      toggleSidebar: () => setSidebarOpen((open) => !open),
    }),
    [resolvedAppearance, shell, sidebarOpen],
  );

  return <ShellContext.Provider value={value}>{children}</ShellContext.Provider>;
}

export function useShell() {
  return useContext(ShellContext);
}
