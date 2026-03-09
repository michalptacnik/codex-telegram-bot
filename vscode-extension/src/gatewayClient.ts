import { EventEmitter } from "events";
import * as http from "http";
import * as https from "https";
import WebSocket from "ws";
import type {
  ConnectionState,
  InboundMessage,
  PromptResponse,
  JobStatusResponse,
  RunsResponse,
} from "./types";

export class GatewayClient extends EventEmitter {
  private baseUrl: string;
  private apiKey: string;
  private uiSecret: string;
  private ws: WebSocket | null = null;
  private pingInterval: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectDelay = 1000;
  private _state: ConnectionState = "disconnected";
  private shouldReconnect = true;
  private activeSessionId = "";

  constructor(baseUrl: string, apiKey: string, uiSecret: string) {
    super();
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
    this.uiSecret = uiSecret;
  }

  get state(): ConnectionState {
    return this._state;
  }

  get sessionId(): string {
    return this.activeSessionId;
  }

  // --- REST helpers ---

  private request(method: string, path: string, body?: unknown): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const url = new URL(path, this.baseUrl);
      const isHttps = url.protocol === "https:";
      const mod = isHttps ? https : http;
      const payload = body !== undefined ? JSON.stringify(body) : undefined;

      const req = mod.request(
        {
          hostname: url.hostname,
          port: url.port || (isHttps ? 443 : 80),
          path: url.pathname + url.search,
          method,
          headers: {
            ...(this.apiKey ? { "X-Local-Api-Key": this.apiKey } : {}),
            ...(payload ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) } : {}),
          },
          timeout: 30_000,
        },
        (res) => {
          const chunks: Buffer[] = [];
          res.on("data", (c: Buffer) => chunks.push(c));
          res.on("end", () => {
            const raw = Buffer.concat(chunks).toString("utf-8");
            if (res.statusCode && res.statusCode >= 400) {
              reject(new Error(`HTTP ${res.statusCode}: ${raw.slice(0, 300)}`));
              return;
            }
            try {
              resolve(JSON.parse(raw));
            } catch {
              resolve(raw);
            }
          });
        },
      );
      req.on("error", reject);
      req.on("timeout", () => {
        req.destroy();
        reject(new Error("Request timeout"));
      });
      if (payload) {
        req.write(payload);
      }
      req.end();
    });
  }

  async submitPrompt(prompt: string, agentId = "default"): Promise<PromptResponse> {
    return (await this.request("POST", "/api/v1/prompts", { prompt, agent_id: agentId })) as PromptResponse;
  }

  async getRuns(limit = 20): Promise<RunsResponse> {
    return (await this.request("GET", `/api/v1/runs?limit=${limit}`)) as RunsResponse;
  }

  async getJobStatus(jobId: string): Promise<JobStatusResponse> {
    return (await this.request("GET", `/api/v1/jobs/${encodeURIComponent(jobId)}`)) as JobStatusResponse;
  }

  async cancelJob(jobId: string): Promise<unknown> {
    return this.request("POST", `/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`);
  }

  // --- WebSocket ---

  connect(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      return;
    }
    this.shouldReconnect = true;
    this.setState("connecting");

    const wsBase = this.baseUrl.replace(/^http/, "ws");
    const tokenParam = this.uiSecret ? `?token=${encodeURIComponent(this.uiSecret)}` : "";
    const wsUrl = `${wsBase}/ws/chat${tokenParam}`;

    const ws = new WebSocket(wsUrl);
    this.ws = ws;

    ws.on("open", () => {
      this.reconnectDelay = 1000;
      this.setState("connected");
      this.startPing();
      if (this.activeSessionId) {
        this.subscribe(this.activeSessionId);
      }
    });

    ws.on("message", (data: WebSocket.Data) => {
      try {
        const msg = JSON.parse(data.toString()) as InboundMessage;
        if (msg.type === "session") {
          this.activeSessionId = msg.session_id;
        }
        this.emit("message", msg);
        this.emit(msg.type, msg);
      } catch {
        // ignore malformed messages
      }
    });

    ws.on("close", () => {
      this.stopPing();
      this.setState("disconnected");
      this.scheduleReconnect();
    });

    ws.on("error", () => {
      // close event will follow
    });
  }

  disconnect(): void {
    this.shouldReconnect = false;
    this.clearReconnect();
    this.stopPing();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.setState("disconnected");
  }

  subscribe(sessionId: string): void {
    this.activeSessionId = sessionId;
    this.send({ type: "subscribe", session_id: sessionId });
  }

  sendMessage(text: string, opts?: { session_id?: string; chat_id?: number; user_id?: number; agent_id?: string }): void {
    const payload: Record<string, unknown> = { type: "user_message", text };
    if (opts?.session_id || this.activeSessionId) {
      payload.session_id = opts?.session_id || this.activeSessionId;
    }
    if (opts?.chat_id) { payload.chat_id = opts.chat_id; }
    if (opts?.user_id) { payload.user_id = opts.user_id; }
    if (opts?.agent_id) { payload.agent_id = opts.agent_id; }
    // Default chat_id/user_id for new sessions
    if (!payload.session_id && !payload.chat_id) {
      payload.chat_id = 1;
      payload.user_id = 1;
    }
    this.send(payload);
  }

  approve(approvalId: string, sessionId?: string, chatId?: number, userId?: number): void {
    this.send({
      type: "approve",
      approval_id: approvalId,
      session_id: sessionId || this.activeSessionId,
      chat_id: chatId,
      user_id: userId,
    });
  }

  deny(approvalId: string, sessionId?: string, chatId?: number, userId?: number): void {
    this.send({
      type: "deny",
      approval_id: approvalId,
      session_id: sessionId || this.activeSessionId,
      chat_id: chatId,
      user_id: userId,
    });
  }

  private send(data: unknown): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  private setState(state: ConnectionState): void {
    if (this._state !== state) {
      this._state = state;
      this.emit("stateChange", state);
    }
  }

  private startPing(): void {
    this.stopPing();
    this.pingInterval = setInterval(() => {
      this.send({ type: "ping" });
    }, 30_000);
  }

  private stopPing(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }

  private scheduleReconnect(): void {
    if (!this.shouldReconnect) { return; }
    this.clearReconnect();
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30_000);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}
