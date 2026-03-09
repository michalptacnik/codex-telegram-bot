import * as vscode from "vscode";
import * as path from "path";
import type { GatewayClient } from "./gatewayClient";
import type {
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
      switch (msg.command) {
        case "sendMessage":
          this.client.sendMessage(msg.text);
          break;
        case "approve":
          this.client.approve(msg.approvalId, msg.sessionId, msg.chatId, msg.userId);
          break;
        case "deny":
          this.client.deny(msg.approvalId, msg.sessionId, msg.chatId, msg.userId);
          break;
      }
    });

    // Push current state
    this.postMessage({ type: "connectionState", state: this.client.state });
  }

  /** Send a message from the extension side (e.g. from the sendSelection command). */
  sendFromExtension(text: string): void {
    this.client.sendMessage(text);
    this.postMessage({ type: "userEcho", text });
  }

  private bindClientEvents(): void {
    this.client.on("stateChange", (state: ConnectionState) => {
      this.postMessage({ type: "connectionState", state });
    });
    this.client.on("session", (msg: SessionResponse) => {
      this.postMessage(msg);
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
