import type {
  StatusResponse,
  ToolSpec,
  CronJob,
  AutomationRecord,
  AutomationRunRecord,
  Integration,
  DiagResult,
  MemoryEntry,
  CostSummary,
  CliTool,
  HealthSnapshot,
  AgentSocialAccount,
  AgentXIntegrationStatus,
  AgentClassManifest,
  AgentProfile,
  OnboardingBootstrapResponse,
  ResolvedAgentProfile,
} from '../types/api';
import { clearToken, getToken, setToken } from './auth';

function runtimeBaseUrl(): string {
  const { protocol } = window.location;
  if (protocol === 'http:' || protocol === 'https:') {
    return '';
  }
  return 'http://127.0.0.1:8765';
}

function runtimeUrl(path: string): string {
  const base = runtimeBaseUrl();
  return `${base}${path}`;
}

// ---------------------------------------------------------------------------
// Base fetch wrapper
// ---------------------------------------------------------------------------

export class UnauthorizedError extends Error {
  constructor() {
    super('Unauthorized');
    this.name = 'UnauthorizedError';
  }
}

export async function apiFetch<T = unknown>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers = new Headers(options.headers);

  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  if (
    options.body &&
    typeof options.body === 'string' &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(runtimeUrl(path), { ...options, headers });

  if (response.status === 401) {
    clearToken();
    window.dispatchEvent(new Event('zeroclaw-unauthorized'));
    throw new UnauthorizedError();
  }

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(`API ${response.status}: ${text || response.statusText}`);
  }

  // Some endpoints may return 204 No Content
  if (response.status === 204) {
    return undefined as unknown as T;
  }

  return response.json() as Promise<T>;
}

function unwrapField<T>(value: T | Record<string, T>, key: string): T {
  if (value !== null && typeof value === 'object' && !Array.isArray(value) && key in value) {
    const unwrapped = (value as Record<string, T | undefined>)[key];
    if (unwrapped !== undefined) {
      return unwrapped;
    }
  }
  return value as T;
}

// ---------------------------------------------------------------------------
// Pairing
// ---------------------------------------------------------------------------

export async function pair(code: string): Promise<{ token: string }> {
  const response = await fetch(runtimeUrl('/pair'), {
    method: 'POST',
    headers: { 'X-Pairing-Code': code },
  });

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(`Pairing failed (${response.status}): ${text || response.statusText}`);
  }

  const data = (await response.json()) as { token: string };
  setToken(data.token);
  return data;
}

// ---------------------------------------------------------------------------
// Public health (no auth required)
// ---------------------------------------------------------------------------

export async function getPublicHealth(): Promise<{ require_pairing: boolean; paired: boolean }> {
  const response = await fetch(runtimeUrl('/health'));
  if (!response.ok) {
    throw new Error(`Health check failed (${response.status})`);
  }
  return response.json() as Promise<{ require_pairing: boolean; paired: boolean }>;
}

// ---------------------------------------------------------------------------
// Status / Health
// ---------------------------------------------------------------------------

export function getStatus(): Promise<StatusResponse> {
  return apiFetch<StatusResponse>('/api/status');
}

export function getHealth(): Promise<HealthSnapshot> {
  return apiFetch<HealthSnapshot | { health: HealthSnapshot }>('/api/health').then((data) =>
    unwrapField(data, 'health'),
  );
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export function getConfig(): Promise<string> {
  return apiFetch<string | { format?: string; content: string }>('/api/config').then((data) =>
    typeof data === 'string' ? data : data.content,
  );
}

export function putConfig(toml: string): Promise<void> {
  return apiFetch<void>('/api/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/toml' },
    body: toml,
  });
}

export function getAgentSocialAccounts(): Promise<{
  accounts: AgentSocialAccount[];
  x_status: AgentXIntegrationStatus[];
}> {
  return apiFetch<{ accounts: AgentSocialAccount[]; x_status: AgentXIntegrationStatus[] }>(
    '/api/agents/social-accounts',
  );
}

export function putAgentSocialAccounts(
  accounts: AgentSocialAccount[],
): Promise<{ accounts: AgentSocialAccount[]; x_status: AgentXIntegrationStatus[] }> {
  return apiFetch<{ accounts: AgentSocialAccount[]; x_status: AgentXIntegrationStatus[] }>(
    '/api/agents/social-accounts',
    {
      method: 'PUT',
      body: JSON.stringify({ accounts }),
    },
  );
}

export function bootstrapAgentXHeadlessSession(
  agent_name: string,
  mode?: 'headless' | 'interactive' | 'import_chrome',
): Promise<{
  status: string;
  message?: string;
  error?: string | null;
  metadata?: unknown;
}> {
  return apiFetch<{
    status: string;
    message?: string;
    error?: string | null;
    metadata?: unknown;
  }>('/api/agents/social-accounts/bootstrap/x', {
    method: 'POST',
    body: JSON.stringify({ agent_name, mode }),
  });
}

export function getClasses(): Promise<AgentClassManifest[]> {
  return apiFetch<{ classes: AgentClassManifest[] } | AgentClassManifest[]>('/api/classes').then(
    (data) => unwrapField(data, 'classes'),
  );
}

export function getClass(id: string): Promise<AgentClassManifest> {
  return apiFetch<AgentClassManifest>(`/api/classes/${encodeURIComponent(id)}`);
}

export function getAgents(): Promise<{
  active_agent_id: string;
  profiles: ResolvedAgentProfile[];
}> {
  return apiFetch<{ active_agent_id: string; profiles: ResolvedAgentProfile[] }>('/api/agents');
}

export function getAgent(id: string): Promise<ResolvedAgentProfile> {
  return apiFetch<ResolvedAgentProfile>(`/api/agents/${encodeURIComponent(id)}`);
}

export function createAgent(
  profile: AgentProfile,
  activate = false,
): Promise<ResolvedAgentProfile> {
  return apiFetch<ResolvedAgentProfile>('/api/agents', {
    method: 'POST',
    body: JSON.stringify({ profile, activate }),
  });
}

export function updateAgent(
  profile: AgentProfile,
  activate = false,
): Promise<ResolvedAgentProfile> {
  return apiFetch<ResolvedAgentProfile>(`/api/agents/${encodeURIComponent(profile.id)}`, {
    method: 'PUT',
    body: JSON.stringify({ profile, activate }),
  });
}

export function activateAgent(id: string): Promise<{
  status: string;
  active_agent_id: string;
  profile: ResolvedAgentProfile;
}> {
  return apiFetch<{
    status: string;
    active_agent_id: string;
    profile: ResolvedAgentProfile;
  }>(`/api/agents/${encodeURIComponent(id)}/activate`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export function getOnboardingState(): Promise<OnboardingBootstrapResponse> {
  return apiFetch<OnboardingBootstrapResponse>('/api/onboarding/state');
}

export function bootstrapOnboarding(): Promise<OnboardingBootstrapResponse> {
  return apiFetch<OnboardingBootstrapResponse>('/api/onboarding/bootstrap', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export function completeOnboarding(active_agent_id?: string): Promise<OnboardingBootstrapResponse> {
  return apiFetch<OnboardingBootstrapResponse>('/api/onboarding/complete', {
    method: 'POST',
    body: JSON.stringify({ active_agent_id }),
  });
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

export function getTools(): Promise<ToolSpec[]> {
  return apiFetch<ToolSpec[] | { tools: ToolSpec[] }>('/api/tools').then((data) =>
    unwrapField(data, 'tools'),
  );
}

// ---------------------------------------------------------------------------
// Cron
// ---------------------------------------------------------------------------

export function getCronJobs(): Promise<CronJob[]> {
  return apiFetch<CronJob[] | { jobs: CronJob[] }>('/api/cron').then((data) =>
    unwrapField(data, 'jobs'),
  );
}

export function getAutomations(): Promise<AutomationRecord[]> {
  return apiFetch<AutomationRecord[] | { automations: AutomationRecord[] }>('/api/automations').then(
    (data) => unwrapField(data, 'automations'),
  );
}

export function createAutomation(body: Record<string, unknown>): Promise<AutomationRecord> {
  return apiFetch<AutomationRecord | { automation: AutomationRecord }>('/api/automations', {
    method: 'POST',
    body: JSON.stringify(body),
  }).then((data) => unwrapField(data, 'automation'));
}

export function updateAutomation(
  id: string,
  body: Record<string, unknown>,
): Promise<AutomationRecord> {
  return apiFetch<AutomationRecord | { automation: AutomationRecord }>(
    `/api/automations/${encodeURIComponent(id)}`,
    {
      method: 'PUT',
      body: JSON.stringify(body),
    },
  ).then((data) => unwrapField(data, 'automation'));
}

export function deleteAutomation(id: string): Promise<void> {
  return apiFetch<void>(`/api/automations/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}

export function pauseAutomation(id: string): Promise<AutomationRecord> {
  return apiFetch<AutomationRecord | { automation: AutomationRecord }>(
    `/api/automations/${encodeURIComponent(id)}/pause`,
    {
      method: 'POST',
      body: JSON.stringify({}),
    },
  ).then((data) => unwrapField(data, 'automation'));
}

export function resumeAutomation(id: string): Promise<AutomationRecord> {
  return apiFetch<AutomationRecord | { automation: AutomationRecord }>(
    `/api/automations/${encodeURIComponent(id)}/resume`,
    {
      method: 'POST',
      body: JSON.stringify({}),
    },
  ).then((data) => unwrapField(data, 'automation'));
}

export function runAutomationNow(id: string): Promise<{ status: string; output?: string }> {
  return apiFetch<{ status: string; output?: string }>(
    `/api/automations/${encodeURIComponent(id)}/run`,
    {
      method: 'POST',
      body: JSON.stringify({}),
    },
  );
}

export function getAutomationRuns(id: string): Promise<AutomationRunRecord[]> {
  return apiFetch<AutomationRunRecord[] | { runs: AutomationRunRecord[] }>(
    `/api/automations/${encodeURIComponent(id)}/runs`,
  ).then((data) => unwrapField(data, 'runs'));
}

export function addCronJob(body: {
  name?: string;
  command: string;
  schedule: string;
  enabled?: boolean;
}): Promise<CronJob> {
  return apiFetch<CronJob | { status: string; job: CronJob }>('/api/cron', {
    method: 'POST',
    body: JSON.stringify(body),
  }).then((data) => (typeof (data as { job?: CronJob }).job === 'object' ? (data as { job: CronJob }).job : (data as CronJob)));
}

export function deleteCronJob(id: string): Promise<void> {
  return apiFetch<void>(`/api/cron/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}

// ---------------------------------------------------------------------------
// Integrations
// ---------------------------------------------------------------------------

export function getIntegrations(): Promise<Integration[]> {
  return apiFetch<Integration[] | { integrations: Integration[] }>('/api/integrations').then(
    (data) => unwrapField(data, 'integrations'),
  );
}

// ---------------------------------------------------------------------------
// Doctor / Diagnostics
// ---------------------------------------------------------------------------

export function runDoctor(): Promise<DiagResult[]> {
  return apiFetch<DiagResult[] | { results: DiagResult[]; summary?: unknown }>('/api/doctor', {
    method: 'POST',
    body: JSON.stringify({}),
  }).then((data) => (Array.isArray(data) ? data : data.results));
}

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------

export function getMemory(
  query?: string,
  category?: string,
): Promise<MemoryEntry[]> {
  const params = new URLSearchParams();
  if (query) params.set('query', query);
  if (category) params.set('category', category);
  const qs = params.toString();
  return apiFetch<MemoryEntry[] | { entries: MemoryEntry[] }>(`/api/memory${qs ? `?${qs}` : ''}`).then(
    (data) => unwrapField(data, 'entries'),
  );
}

export function storeMemory(
  key: string,
  content: string,
  category?: string,
): Promise<void> {
  return apiFetch<unknown>('/api/memory', {
    method: 'POST',
    body: JSON.stringify({ key, content, category }),
  }).then(() => undefined);
}

export function deleteMemory(key: string): Promise<void> {
  return apiFetch<void>(`/api/memory/${encodeURIComponent(key)}`, {
    method: 'DELETE',
  });
}

// ---------------------------------------------------------------------------
// Cost
// ---------------------------------------------------------------------------

export function getCost(): Promise<CostSummary> {
  return apiFetch<CostSummary | { cost: CostSummary }>('/api/cost').then((data) =>
    unwrapField(data, 'cost'),
  );
}

// ---------------------------------------------------------------------------
// CLI Tools
// ---------------------------------------------------------------------------

export function getCliTools(): Promise<CliTool[]> {
  return apiFetch<CliTool[] | { cli_tools: CliTool[] }>('/api/cli-tools').then((data) =>
    unwrapField(data, 'cli_tools'),
  );
}
