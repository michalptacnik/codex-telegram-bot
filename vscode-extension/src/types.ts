// --- Outbound (client -> server) ---

export interface PingMessage {
  type: "ping";
}

export interface SubscribeMessage {
  type: "subscribe";
  session_id: string;
}

export interface UserMessage {
  type: "user_message";
  text: string;
  session_id?: string;
  chat_id?: number;
  user_id?: number;
  agent_id?: string;
}

export interface ApproveMessage {
  type: "approve";
  approval_id: string;
  session_id?: string;
  chat_id?: number;
  user_id?: number;
}

export interface DenyMessage {
  type: "deny";
  approval_id: string;
  session_id?: string;
  chat_id?: number;
  user_id?: number;
}

export type OutboundMessage =
  | PingMessage
  | SubscribeMessage
  | UserMessage
  | ApproveMessage
  | DenyMessage;

// --- Inbound (server -> client) ---

export interface CostSummary {
  total_tokens?: number;
  total_cost_usd?: number;
  updated_at?: string;
}

export interface SessionResponse {
  type: "session";
  session_id: string;
  chat_id: number;
  user_id: number;
  cost_summary?: CostSummary;
}

export interface AssistantChunkResponse {
  type: "assistant_chunk";
  session_id: string;
  text: string;
}

export interface ToolEventResponse {
  type: "tool_event";
  name: string;
  status: string;
  session_id?: string;
  detail?: Record<string, unknown>;
}

export interface DoneResponse {
  type: "done";
  session_id: string;
  kind?: string;
  cost_summary?: CostSummary;
}

export interface ErrorResponse {
  type: "error";
  detail: string;
}

export interface PongResponse {
  type: "pong";
}

export type InboundMessage =
  | SessionResponse
  | AssistantChunkResponse
  | ToolEventResponse
  | DoneResponse
  | ErrorResponse
  | PongResponse;

// --- REST ---

export interface PromptRequest {
  prompt: string;
  agent_id?: string;
}

export interface PromptResponse {
  job_id: string;
  status: string;
  agent_id?: string;
}

export interface JobStatusResponse {
  job_id: string;
  status: string;
}

export interface RunItem {
  run_id: string;
  status: string;
  created_at?: string;
  completed_at?: string;
  error?: string;
  [key: string]: unknown;
}

export interface RunsResponse {
  items: RunItem[];
  limit: number;
}

export interface AgentInfo {
  agent_id: string;
  name: string;
  provider: string;
  policy_profile: string;
  max_concurrency: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface SessionInfo {
  session_id: string;
  chat_id: number;
  user_id: number;
  status: string;
  current_agent_id: string;
  summary?: string;
  last_run_id?: string;
  created_at: string;
  updated_at: string;
}

export type AgentsResponse = AgentInfo[];
export type SessionsResponse = SessionInfo[];

export type ConnectionState = "connected" | "connecting" | "disconnected";
