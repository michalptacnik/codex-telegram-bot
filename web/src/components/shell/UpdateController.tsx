import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { CheckCircle2, Download, LoaderCircle, RefreshCw, Sparkles } from 'lucide-react';
import { useShell } from '@/components/shell/ShellProvider';
import { listenForMenuActions } from '@/lib/desktop';

interface UpdateMetadata {
  version: string;
  currentVersion: string;
  body?: string | null;
  date?: string | null;
}

function describeError(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }

  if (typeof error === 'string' && error.trim()) {
    return error;
  }

  if (error && typeof error === 'object') {
    try {
      const serialized = JSON.stringify(error);
      if (serialized && serialized !== '{}') {
        return serialized;
      }
    } catch {
      // Ignore serialization failures and fall back below.
    }
  }

  return fallback;
}

type UpdateAnnouncement = {
  available: boolean;
  metadata?: UpdateMetadata;
};

type UpdateState =
  | { kind: 'idle' }
  | { kind: 'checking' }
  | { kind: 'available'; metadata: UpdateMetadata }
  | { kind: 'none'; currentVersion?: string }
  | { kind: 'installing'; metadata: UpdateMetadata }
  | { kind: 'installed'; metadata: UpdateMetadata }
  | { kind: 'error'; message: string };

export default function UpdateController() {
  const { shell, isDesktopMac } = useShell();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<UpdateState>({ kind: 'idle' });
  const autoCheckStartedRef = useRef(false);
  const autoOpenedRef = useRef(false);
  const lastCheckedAtRef = useRef(0);

  const updateEnabled = Boolean(shell.updateConfigured && isDesktopMac);

  const currentVersion =
    state.kind === 'available' || state.kind === 'installing' || state.kind === 'installed'
      ? state.metadata.currentVersion
      : state.kind === 'none'
        ? state.currentVersion
        : undefined;

  const publishUpdateStatus = (announcement: UpdateAnnouncement) => {
    window.dispatchEvent(
      new CustomEvent<UpdateAnnouncement>('agenthq:update-status', {
        detail: announcement,
      }),
    );
  };

  const checkForUpdates = async ({
    openSheet = true,
    silent = false,
  }: {
    openSheet?: boolean;
    silent?: boolean;
  } = {}) => {
    if (!updateEnabled) {
      publishUpdateStatus({ available: false });
      if (openSheet) {
        setOpen(true);
        setState({
          kind: 'error',
          message:
            'Updater is not configured yet. Set AGENT_HQ_UPDATER_ENDPOINTS and AGENT_HQ_UPDATER_PUBKEY when building the desktop app.',
        });
      }
      return;
    }

    if (openSheet) {
      setOpen(true);
    }
    if (!silent) {
      setState({ kind: 'checking' });
    }

    try {
      lastCheckedAtRef.current = Date.now();
      const update = await invoke<UpdateMetadata | null>('desktop_check_for_updates');
      if (!update) {
        publishUpdateStatus({ available: false });
        if (!silent) {
          setState({ kind: 'none' });
        }
        return;
      }

      publishUpdateStatus({ available: true, metadata: update });
      setState({ kind: 'available', metadata: update });
      if (silent && !autoOpenedRef.current) {
        setOpen(true);
        autoOpenedRef.current = true;
      }
    } catch (error) {
      if (!silent) {
        setState({
          kind: 'error',
          message: describeError(error, 'Failed to check for updates.'),
        });
      }
    }
  };

  const installUpdate = async () => {
    if (state.kind !== 'available') return;

    try {
      setState({ kind: 'installing', metadata: state.metadata });
      await invoke('desktop_install_update');
      publishUpdateStatus({ available: false });
      setState({ kind: 'installed', metadata: state.metadata });
    } catch (error) {
      setState({
        kind: 'error',
        message: describeError(error, 'Failed to install update.'),
      });
    }
  };

  useEffect(() => {
    let detach = () => {};
    const handleWindowEvent = () => {
      void checkForUpdates();
    };

    void listenForMenuActions((action) => {
      if (action === 'app.check_updates') {
        void checkForUpdates();
      }
    }).then((cleanup) => {
      detach = cleanup;
    });

    window.addEventListener('agenthq:check-updates', handleWindowEvent);

    return () => {
      detach();
      window.removeEventListener('agenthq:check-updates', handleWindowEvent);
    };
  }, [updateEnabled]);

  useEffect(() => {
    if (!updateEnabled || autoCheckStartedRef.current) {
      return;
    }

    autoCheckStartedRef.current = true;
    void checkForUpdates({ openSheet: false, silent: true });

    const handleFocus = () => {
      if (Date.now() - lastCheckedAtRef.current < 15 * 60 * 1000) {
        return;
      }

      void checkForUpdates({ openSheet: false, silent: true });
    };

    window.addEventListener('focus', handleFocus);
    return () => window.removeEventListener('focus', handleFocus);
  }, [updateEnabled]);

  if (!isDesktopMac) {
    return null;
  }

  return open ? (
    <div className="mac-palette-backdrop" onClick={() => setOpen(false)}>
      <div className="mac-update-sheet" onClick={(event) => event.stopPropagation()}>
        <div className="mac-update-header">
          <div className="mac-update-icon">
            {state.kind === 'installed' ? (
              <CheckCircle2 className="h-5 w-5" />
            ) : state.kind === 'installing' || state.kind === 'checking' ? (
              <LoaderCircle className="h-5 w-5 animate-spin" />
            ) : (
              <Sparkles className="h-5 w-5" />
            )}
          </div>
          <div>
            <h2 className="mac-update-title">Software Update</h2>
            <p className="mac-update-subtitle">
              {updateEnabled
                ? 'Check for and install signed Agent HQ desktop updates.'
                : 'Updater support is wired in, but this build still needs a release feed and signing key.'}
            </p>
          </div>
        </div>

        <div className="mac-update-body">
          {currentVersion ? (
            <p className="mac-update-line">Current version: {currentVersion}</p>
          ) : null}

          {state.kind === 'checking' ? (
            <p className="mac-update-line">Checking the configured release feed…</p>
          ) : null}

          {state.kind === 'none' ? (
            <p className="mac-update-line">No newer signed update is currently available.</p>
          ) : null}

          {state.kind === 'available' || state.kind === 'installing' || state.kind === 'installed' ? (
            <>
              <p className="mac-update-line">
                {state.kind === 'installed'
                  ? `Update ${state.metadata.version} was installed.`
                  : `Version ${state.metadata.version} is available.`}
              </p>
              {state.metadata.date ? (
                <p className="mac-update-line">
                  Published {new Date(state.metadata.date).toLocaleString()}
                </p>
              ) : null}
              {state.metadata.body ? (
                <div className="mac-update-notes">{state.metadata.body}</div>
              ) : null}
            </>
          ) : null}

          {state.kind === 'error' ? (
            <div className="mac-update-error">{state.message}</div>
          ) : null}
        </div>

        <div className="mac-update-actions">
          <button type="button" onClick={() => setOpen(false)} className="mac-update-secondary">
            Close
          </button>

          {state.kind === 'idle' || state.kind === 'none' || state.kind === 'error' ? (
            <button type="button" onClick={() => void checkForUpdates()} className="mac-update-primary">
              <RefreshCw className="h-4 w-4" />
              Check Again
            </button>
          ) : null}

          {state.kind === 'available' ? (
            <button type="button" onClick={() => void installUpdate()} className="mac-update-primary">
              <Download className="h-4 w-4" />
              Install Update
            </button>
          ) : null}
        </div>
      </div>
    </div>
  ) : null;
}
