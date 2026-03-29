export interface StatusResponse {
  provider: string | null;
  model: string;
  temperature: number;
  uptime_seconds: number;
  gateway_port: number;
  locale: string;
  memory_backend: string;
  paired: boolean;
  channels: Record<string, boolean>;
  health: HealthSnapshot;
}

export interface HealthSnapshot {
  pid: number;
  updated_at: string;
  uptime_seconds: number;
  components: Record<string, ComponentHealth>;
}

export interface ComponentHealth {
  status: string;
  updated_at: string;
  last_ok: string | null;
  last_error: string | null;
  restart_count: number;
}

export interface ToolSpec {
  name: string;
  description: string;
  parameters: any;
}

export interface CronJob {
  id: string;
  name: string | null;
  command: string;
  next_run: string;
  last_run: string | null;
  last_status: string | null;
  enabled: boolean;
}

export type AutomationKind =
  | 'scheduled_agent'
  | 'scheduled_shell'
  | 'heartbeat_task';

export type AutomationSchedule =
  | { kind: 'cron'; expr: string; tz?: string | null }
  | { kind: 'at'; at: string }
  | { kind: 'every'; every_ms: number };

export interface DeliveryConfig {
  mode: string;
  channel?: string | null;
  to?: string | null;
  best_effort: boolean;
}

export interface AutomationRecord {
  id: string;
  backend_id: string;
  automation_kind: AutomationKind;
  owner_agent_id?: string | null;
  name?: string | null;
  prompt?: string | null;
  command?: string | null;
  schedule?: AutomationSchedule | null;
  enabled: boolean;
  next_run?: string | null;
  last_run?: string | null;
  last_status?: string | null;
  last_output?: string | null;
  model?: string | null;
  session_target?: string | null;
  delivery?: DeliveryConfig | null;
}

export interface AutomationRunRecord {
  id: string;
  started_at: string;
  finished_at: string;
  status: string;
  output?: string | null;
  duration_ms?: number | null;
}

export interface Integration {
  name: string;
  description: string;
  category: string;
  status: 'Available' | 'Active' | 'ComingSoon';
}

export interface DiagResult {
  severity: 'ok' | 'warn' | 'error';
  category: string;
  message: string;
}

export interface MemoryEntry {
  id: string;
  key: string;
  content: string;
  category: string;
  timestamp: string;
  session_id: string | null;
  score: number | null;
}

export interface CostSummary {
  session_cost_usd: number;
  daily_cost_usd: number;
  monthly_cost_usd: number;
  total_tokens: number;
  /** Total tokens used this month — same period as monthly_cost_usd */
  monthly_tokens: number;
  request_count: number;
  by_model: Record<string, ModelStats>;
  /** False when cost tracking is disabled in config — all values will be 0 */
  tracking_enabled: boolean;
}

export interface ModelStats {
  model: string;
  cost_usd: number;
  total_tokens: number;
  request_count: number;
}

export interface CliTool {
  name: string;
  path: string;
  version: string | null;
  category: string;
}

export interface SSEEvent {
  type: string;
  timestamp?: string;
  [key: string]: any;
}

export interface WsMessage {
  type: 'message' | 'chunk' | 'tool_call' | 'tool_result' | 'done' | 'error';
  content?: string;
  full_response?: string;
  name?: string;
  args?: any;
  output?: string;
  message?: string;
}

export interface SocialTwitterCredentials {
  username?: string | null;
  password?: string | null;
  email?: string | null;
}

export interface AgentSocialAccount {
  agent_name: string;
  twitter?: SocialTwitterCredentials | null;
}

export interface IntegrationCapabilityStatus {
  post: boolean;
  comment: boolean;
  article: boolean;
}

export interface TwitterAdapterStatus {
  status: string;
  detail?: string | null;
  supported_capabilities: IntegrationCapabilityStatus;
}

export interface HeadlessIntegrationStatus {
  status: string;
  authenticated: boolean;
  detail?: string | null;
  session?: string | null;
  url?: string | null;
  required_user_action?: string | null;
  recommended_setup_mode?: string | null;
}

export interface BrowserExtensionIntegrationStatus {
  status: string;
  detail?: string | null;
}

export interface AgentXIntegrationStatus {
  agent_name: string;
  twitter_x: TwitterAdapterStatus;
  browser_headless: HeadlessIntegrationStatus;
  browser_ext: BrowserExtensionIntegrationStatus;
  supported_capabilities: IntegrationCapabilityStatus;
}

export type AgentClassStatus = 'active' | 'coming_soon';

export interface SoulStyle {
  emoji: string;
  emphasis: string;
  brevity: string;
}

export interface SoulProfile {
  name: string;
  voice: string;
  principles: string[];
  boundaries: string[];
  style: SoulStyle;
}

export interface IdentityOverlay {
  creature?: string | null;
  vibe?: string | null;
  emoji?: string | null;
  role_title?: string | null;
  tagline?: string | null;
}

export interface SoulProfileOverlay {
  voice?: string | null;
  principles: string[];
  boundaries: string[];
  style?: SoulStyle | null;
}

export interface AgentClassManifest {
  version: number;
  id: string;
  name: string;
  status: AgentClassStatus;
  description: string;
  fantasy_theme: string;
  default_role_summary: string;
  default_soul_overlay: SoulProfileOverlay;
  default_identity_overlay: IdentityOverlay;
  tool_grants: string[];
  skill_grants: string[];
  channel_affinities: string[];
  integration_affinities: string[];
  guardrails: string[];
  evaluation_scenarios: string[];
}

export interface AgentProfileOverrides {
  summary?: string | null;
  system_prompt_appendix?: string | null;
  provider?: string | null;
  model?: string | null;
  temperature?: number | null;
  max_depth?: number | null;
  agentic?: boolean | null;
  max_iterations?: number | null;
  tool_grants: string[];
  skill_grants: string[];
  soul: SoulProfileOverlay;
  identity: IdentityOverlay;
}

export interface AgentProfile {
  id: string;
  name: string;
  avatar?: string | null;
  launch_on_startup?: boolean;
  primary_class: string;
  secondary_classes: string[];
  social_accounts: {
    twitter?: SocialTwitterCredentials | null;
  };
  overrides: AgentProfileOverrides;
}

export interface ResolvedIdentity {
  name: string;
  creature: string;
  vibe: string;
  emoji: string;
  role_title: string;
  tagline: string;
}

export type DesktopAppearance = 'light' | 'dark' | 'system';

export type DesktopPlatform = 'macos' | 'windows' | 'linux' | 'web';

export interface DesktopShellInfo {
  name: string;
  mode: string;
  runtime_host: string;
  platform: DesktopPlatform;
  appearance: DesktopAppearance;
  menuDriven: boolean;
  supportsTranslucency: boolean;
  windowStyle?: string | null;
  updateConfigured?: boolean;
}

export interface ResolvedAgentProfile {
  profile: AgentProfile;
  classes: AgentClassManifest[];
  summary: string;
  soul: SoulProfile;
  identity: ResolvedIdentity;
  tool_grants: string[];
  skill_grants: string[];
  guardrails: string[];
  evaluation_scenarios: string[];
}

export interface OnboardingState {
  version: number;
  completed: boolean;
  active_agent_id: string;
  startup_agent_id?: string | null;
  has_provider_config: boolean;
  runtime_ready: boolean;
  profile_count: number;
}

export interface OnboardingBootstrapResponse {
  onboarding: OnboardingState;
  active_profile: ResolvedAgentProfile;
}
