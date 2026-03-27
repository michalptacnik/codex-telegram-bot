import type { DesktopShellInfo } from '@/types/api';

const FALLBACK_DESKTOP_INFO: DesktopShellInfo = {
  name: 'Agent HQ Desktop',
  mode: 'browser',
  runtime_host: 'http://127.0.0.1:8765',
  platform: 'web',
  appearance: 'system',
  menuDriven: false,
  supportsTranslucency: false,
  windowStyle: 'browser',
  updateConfigured: false,
};

function hasTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

export function isDesktopRuntime(): boolean {
  return hasTauriRuntime();
}

export async function getDesktopShellInfo(): Promise<DesktopShellInfo> {
  if (!hasTauriRuntime()) {
    return FALLBACK_DESKTOP_INFO;
  }

  try {
    const { invoke } = await import('@tauri-apps/api/core');
    return await invoke<DesktopShellInfo>('desktop_shell_info');
  } catch {
    return {
      ...FALLBACK_DESKTOP_INFO,
      mode: 'local_control_center',
      platform: navigator.platform.toLowerCase().includes('mac') ? 'macos' : 'web',
      menuDriven: true,
      supportsTranslucency: navigator.platform.toLowerCase().includes('mac'),
      windowStyle: navigator.platform.toLowerCase().includes('mac')
        ? 'transparent'
        : FALLBACK_DESKTOP_INFO.windowStyle,
      updateConfigured: false,
    };
  }
}

export async function listenForMenuActions(
  onAction: (action: string) => void,
): Promise<() => void> {
  if (!hasTauriRuntime()) {
    return () => {};
  }

  const { listen } = await import('@tauri-apps/api/event');
  const unlisten = await listen<string>('menu-action', (event) => {
    if (typeof event.payload === 'string') {
      onAction(event.payload);
    }
  });
  return () => {
    void unlisten();
  };
}
