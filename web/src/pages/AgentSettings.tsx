import { useState } from 'react';
import { Save } from 'lucide-react';
import { updateAgent } from '@/lib/api';
import { useAgentContext } from '@/contexts/AgentContext';
import { useShell } from '@/components/shell/ShellProvider';
import { MacPage, MacPanel, MacStat } from '@/components/macos/MacPrimitives';
import type { AgentProfile } from '@/types/api';

export default function AgentSettings() {
  const { scopedAgent, refreshAgents } = useAgentContext();
  const { isDesktopMac } = useShell();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [draft, setDraft] = useState<AgentProfile | null>(null);

  if (!scopedAgent) return null;

  const profile = draft ?? scopedAgent.profile;

  const fieldClass = isDesktopMac
    ? 'mt-1 w-full rounded-[1rem] border border-[rgba(15,23,42,0.08)] bg-white/80 px-4 py-2.5 text-sm text-slate-900 outline-none focus:border-sky-400 dark:border-white/10 dark:bg-black/20 dark:text-white'
    : 'mt-1 w-full rounded-[1rem] border border-white/10 bg-[#0b1120] px-4 py-2.5 text-sm text-white outline-none focus:border-sky-400';

  const update = (partial: Partial<AgentProfile>) => {
    setDraft((prev) => ({ ...(prev ?? scopedAgent.profile), ...partial }));
  };

  const updateOverrides = (partial: Partial<AgentProfile['overrides']>) => {
    setDraft((prev) => {
      const base = prev ?? scopedAgent.profile;
      return { ...base, overrides: { ...base.overrides, ...partial } };
    });
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      await updateAgent(profile);
      await refreshAgents();
      setDraft(null);
      setSuccess('Agent settings saved.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <MacPage
      eyebrow={scopedAgent.profile.name}
      title="Agent Settings"
      description="Configure identity, model, and behavior overrides."
    >
      {error ? (
        <div className="rounded-[1.4rem] border border-rose-300/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-200 mb-4">
          {error}
        </div>
      ) : null}
      {success ? (
        <div className="rounded-[1.4rem] border border-emerald-300/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-700 dark:text-emerald-200 mb-4">
          {success}
        </div>
      ) : null}

      <div className="grid gap-4 md:grid-cols-4 mb-4">
        <MacStat label="Agent ID" value={profile.id} detail="Unique identifier" />
        <MacStat label="Class" value={scopedAgent.classes[0]?.name ?? 'None'} detail="Primary class" />
        <MacStat label="Tools" value={String(scopedAgent.tool_grants.length)} detail="Granted tools" />
        <MacStat label="Startup" value={profile.launch_on_startup ? 'Yes' : 'No'} detail="Auto-launch" />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <MacPanel title="Identity" detail="Name, avatar, and role summary.">
          <div className="grid gap-4">
            <label className="block">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Name</span>
              <input
                value={profile.name}
                onChange={(e) => update({ name: e.target.value })}
                className={fieldClass}
              />
            </label>
            <label className="block">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Avatar description</span>
              <input
                value={profile.avatar ?? ''}
                onChange={(e) => update({ avatar: e.target.value })}
                className={fieldClass}
              />
            </label>
            <label className="block">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Role summary</span>
              <textarea
                rows={4}
                value={profile.overrides.summary ?? ''}
                onChange={(e) => updateOverrides({ summary: e.target.value })}
                className={fieldClass}
              />
            </label>
          </div>
        </MacPanel>

        <MacPanel title="Model Overrides" detail="Override the global model settings for this agent.">
          <div className="grid gap-4">
            <label className="block">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Provider</span>
              <input
                value={profile.overrides.provider ?? ''}
                onChange={(e) => updateOverrides({ provider: e.target.value || null })}
                placeholder="Use global default"
                className={fieldClass}
              />
            </label>
            <label className="block">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Model</span>
              <input
                value={profile.overrides.model ?? ''}
                onChange={(e) => updateOverrides({ model: e.target.value || null })}
                placeholder="Use global default"
                className={fieldClass}
              />
            </label>
            <label className="block">
              <span className="text-[0.72rem] uppercase tracking-[0.18em] text-[var(--shell-muted)]">Temperature</span>
              <input
                type="number"
                step="0.1"
                min="0"
                max="2"
                value={profile.overrides.temperature ?? ''}
                onChange={(e) => updateOverrides({ temperature: e.target.value ? Number(e.target.value) : null })}
                placeholder="Use global default"
                className={fieldClass}
              />
            </label>
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={profile.overrides.agentic ?? false}
                  onChange={(e) => updateOverrides({ agentic: e.target.checked })}
                  className="rounded"
                />
                Agentic mode
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={profile.launch_on_startup ?? false}
                  onChange={(e) => update({ launch_on_startup: e.target.checked })}
                  className="rounded"
                />
                Launch on startup
              </label>
            </div>
          </div>
        </MacPanel>
      </div>

      <div className="mt-4 flex gap-3">
        <button
          type="button"
          disabled={saving || !draft}
          onClick={handleSave}
          className="inline-flex items-center gap-2 rounded-full bg-[#0a84ff] px-5 py-2.5 text-sm font-semibold text-white shadow-[0_12px_24px_rgba(10,132,255,0.22)] transition hover:brightness-105 disabled:opacity-50"
        >
          <Save className="h-4 w-4" />
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
        {draft ? (
          <button
            type="button"
            onClick={() => setDraft(null)}
            className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-5 py-2.5 text-sm font-medium transition hover:bg-white/10"
          >
            Discard
          </button>
        ) : null}
      </div>
    </MacPage>
  );
}
