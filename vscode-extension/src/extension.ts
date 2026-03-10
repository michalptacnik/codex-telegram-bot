import * as vscode from "vscode";
import { GatewayClient } from "./gatewayClient";
import { ChatViewProvider } from "./chatViewProvider";
import type { ConnectionState } from "./types";

let client: GatewayClient;
let statusBarItem: vscode.StatusBarItem;

interface ExtensionConfig {
  gatewayUrl: string;
  apiKey: string;
  uiSecret: string;
  defaultChatId: number;
  defaultUserId: number;
  defaultAgentId: string;
}

function readConfig(): ExtensionConfig {
  const cfg = vscode.workspace.getConfiguration("codex-control-center");
  return {
    gatewayUrl: cfg.get<string>("gatewayUrl") || "http://127.0.0.1:8765",
    apiKey: cfg.get<string>("apiKey") || "",
    uiSecret: cfg.get<string>("uiSecret") || "",
    defaultChatId: cfg.get<number>("defaultChatId") || 1,
    defaultUserId: cfg.get<number>("defaultUserId") || 1,
    defaultAgentId: cfg.get<string>("defaultAgentId") || "default",
  };
}

function updateStatusBar(state: ConnectionState): void {
  switch (state) {
    case "connected":
      statusBarItem.text = "$(plug) Codex: Connected";
      statusBarItem.backgroundColor = undefined;
      break;
    case "connecting":
      statusBarItem.text = "$(sync~spin) Codex: Connecting...";
      statusBarItem.backgroundColor = undefined;
      break;
    case "disconnected":
      statusBarItem.text = "$(debug-disconnect) Codex: Disconnected";
      statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
      break;
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const config = readConfig();

  // Create gateway client
  client = new GatewayClient({
    baseUrl: config.gatewayUrl,
    apiKey: config.apiKey,
    uiSecret: config.uiSecret,
    defaultChatId: config.defaultChatId,
    defaultUserId: config.defaultUserId,
    defaultAgentId: config.defaultAgentId,
  });

  // Status bar
  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBarItem.command = "codex-cc.reconnect";
  statusBarItem.tooltip = "Click to reconnect to Codex gateway";
  updateStatusBar("disconnected");
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  client.on("stateChange", updateStatusBar);

  // Chat sidebar
  const chatProvider = new ChatViewProvider(context.extensionUri, client);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, chatProvider),
  );

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand("codex-cc.askAgent", async () => {
      await vscode.commands.executeCommand("codex-cc.chatView.focus");
      const text = await vscode.window.showInputBox({
        prompt: "Ask the Codex agent",
        placeHolder: "What would you like the agent to do?",
      });
      if (text?.trim()) {
        chatProvider.sendFromExtension(text.trim());
      }
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codex-cc.sendSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showWarningMessage("No active editor with selection.");
        return;
      }
      const selection = editor.document.getText(editor.selection);
      if (!selection.trim()) {
        vscode.window.showWarningMessage("No text selected.");
        return;
      }
      const fileName = editor.document.fileName;
      const prompt = `File: ${fileName}\n\n\`\`\`\n${selection}\n\`\`\``;
      await vscode.commands.executeCommand("codex-cc.chatView.focus");
      chatProvider.sendFromExtension(prompt);
      vscode.window.showInformationMessage("Selection sent to Codex agent.");
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codex-cc.reconnect", () => {
      const cfg = readConfig();
      client.disconnect();
      client.reconfigure({
        baseUrl: cfg.gatewayUrl,
        apiKey: cfg.apiKey,
        uiSecret: cfg.uiSecret,
        defaultChatId: cfg.defaultChatId,
        defaultUserId: cfg.defaultUserId,
        defaultAgentId: cfg.defaultAgentId,
      });
      client.connect();
      chatProvider.refreshContext();
      vscode.window.showInformationMessage("Reconnecting to Codex gateway...");
    }),
  );

  // React to config changes
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("codex-control-center")) {
        vscode.commands.executeCommand("codex-cc.reconnect");
      }
    }),
  );

  // Connect
  client.connect();
}

export function deactivate(): void {
  client?.disconnect();
}
