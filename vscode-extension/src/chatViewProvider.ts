import * as vscode from "vscode";
import type { GatewayClient } from "./gatewayClient";
import type {
  AgentInfo,
  SessionInfo,
  SessionResponse,
  AssistantChunkResponse,
  ToolEventResponse,
  DoneResponse,
  ErrorResponse,
  ConnectionState,
} from "./types";

export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "codex-cc.chatView";

  private view?: vscode.WebviewView;
  private client: GatewayClient;
  private extensionUri: vscode.Uri;
  private agents: AgentInfo[] = [];
  private sessions: SessionInfo[] = [];

  constructor(extensionUri: vscode.Uri, client: GatewayClient) {
    this.extensionUri = extensionUri;
    this.client = client;
    this.bindClientEvents();
  }

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken,
  ): void {
    this.view = webviewView;
    const mediaUri = vscode.Uri.joinPath(this.extensionUri, "media");

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [mediaUri],
    };

    webviewView.webview.html = this.getHtml(webviewView.webview, mediaUri);

    webviewView.webview.onDidReceiveMessage((msg) => {
      void this.handleWebviewMessage(msg);
    });

    // Push current state
    this.postMessage({ type: "connectionState", state: this.client.state });
    this.postContext();
    void this.refreshContext();
  }

  /** Send a message from the extension side (e.g. from the sendSelection command). */
  sendFromExtension(text: string): void {
    this.client.sendMessage(text, { agent_id: this.client.agentId });
    this.postMessage({ type: "userEcho", text });
  }

  async refreshContext(): Promise<void> {
    try {
      const [agents, sessions] = await Promise.all([
        this.client.listAgents(),
        this.client.listSessions(100),
      ]);
      this.agents = Array.isArray(agents) ? agents : [];
      this.sessions = Array.isArray(sessions) ? sessions : [];
    } catch (error) {
      this.postMessage({
        type: "contextError",
        detail: (error as Error)?.message || "Failed to load context.",
      });
    }
    this.postContext();
  }

  private async handleWebviewMessage(msg: unknown): Promise<void> {
    if (!msg || typeof msg !== "object") {
      return;
    }
    const payload = msg as Record<string, unknown>;
    const command = String(payload.command || "").trim();
    if (!command) {
      return;
    }

    switch (command) {
      case "ready":
        this.postContext();
        await this.refreshContext();
        return;
      case "refreshContext":
        await this.refreshContext();
        return;
      case "setAgent": {
        const agentId = String(payload.agentId || "").trim();
        if (agentId) {
          this.client.setDefaultAgent(agentId);
          this.postContext();
        }
        return;
      }
      case "setIdentity": {
        const chatId = sanitizePositiveInt(payload.chatId, this.client.chatId);
        const userId = sanitizePositiveInt(payload.userId, this.client.userId);
        this.client.setIdentity(chatId, userId);
        this.postContext();
        return;
      }
      case "selectSession": {
        const sessionId = String(payload.sessionId || "").trim();
        if (sessionId) {
          this.client.subscribe(sessionId);
          this.postContext();
        }
        return;
      }
      case "sendMessage": {
        const text = String(payload.text || "").trim();
        if (!text) {
          return;
        }
        this.client.sendMessage(text, {
          session_id: String(payload.sessionId || "").trim() || undefined,
          chat_id: sanitizePositiveInt(payload.chatId, this.client.chatId),
          user_id: sanitizePositiveInt(payload.userId, this.client.userId),
          agent_id: String(payload.agentId || this.client.agentId).trim() || this.client.agentId,
        });
        return;
      }
      case "approve":
        this.client.approve(
          String(payload.approvalId || ""),
          String(payload.sessionId || "") || undefined,
          sanitizePositiveInt(payload.chatId, this.client.chatId),
          sanitizePositiveInt(payload.userId, this.client.userId),
        );
        return;
      case "deny":
        this.client.deny(
          String(payload.approvalId || ""),
          String(payload.sessionId || "") || undefined,
          sanitizePositiveInt(payload.chatId, this.client.chatId),
          sanitizePositiveInt(payload.userId, this.client.userId),
        );
        return;
      default:
        return;
    }
  }

  private bindClientEvents(): void {
    this.client.on("stateChange", (state: ConnectionState) => {
      this.postMessage({ type: "connectionState", state });
      if (state === "connected") {
        void this.refreshContext();
      }
    });
    this.client.on("session", (msg: SessionResponse) => {
      this.client.setIdentity(msg.chat_id, msg.user_id);
      this.postMessage(msg);
      this.postContext();
    });
    this.client.on("assistant_chunk", (msg: AssistantChunkResponse) => {
      this.postMessage(msg);
    });
    this.client.on("tool_event", (msg: ToolEventResponse) => {
      this.postMessage(msg);
    });
    this.client.on("done", (msg: DoneResponse) => {
      this.postMessage(msg);
    });
    this.client.on("error", (msg: ErrorResponse) => {
      this.postMessage(msg);
    });
  }

  private postContext(): void {
    this.postMessage({
      type: "context",
      chatId: this.client.chatId,
      userId: this.client.userId,
      agentId: this.client.agentId,
      sessionId: this.client.sessionId,
      sessions: this.sessions,
      agents: this.agents,
    });
  }

  private postMessage(data: unknown): void {
    this.view?.webview.postMessage(data);
  }

  private getHtml(webview: vscode.Webview, mediaUri: vscode.Uri): string {
    const cssUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaUri, "chat.css"));
    const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaUri, "chat.js"));
    const nonce = getNonce();

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
  <link rel="stylesheet" href="${cssUri}">
</head>
<body>
  <div id="connection-bar">
    <span id="conn-dot"></span>
    <span id="conn-label">Disconnected</span>
  </div>
  <section id="context-panel">
    <div class="context-row">
      <label for="agent-select">Agent</label>
      <select id="agent-select"></select>
      <button id="refresh-btn" type="button" title="Refresh sessions and agents">Refresh</button>
    </div>
    <div class="context-row">
      <label for="session-select">Session</label>
      <select id="session-select"></select>
    </div>
    <div class="context-row" id="identity-row">
      <label for="chat-id">Chat</label>
      <input id="chat-id" type="number" min="1" step="1" />
      <label for="user-id">User</label>
      <input id="user-id" type="number" min="1" step="1" />
      <button id="identity-btn" type="button" title="Apply chat/user IDs">Apply</button>
    </div>
  </section>
  <div id="chat-log"></div>
  <form id="chat-form">
    <textarea id="chat-input" rows="2" placeholder="Ask the agent..."></textarea>
    <button type="submit">Send</button>
  </form>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}

function getNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let result = "";
  for (let i = 0; i < 32; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

function sanitizePositiveInt(value: unknown, fallback: number): number {
  const n = Number(value);
  if (Number.isFinite(n) && n > 0) {
    return Math.floor(n);
  }
  return fallback > 0 ? fallback : 1;
}
