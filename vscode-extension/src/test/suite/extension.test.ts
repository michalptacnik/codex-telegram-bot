import * as assert from "assert";
import * as vscode from "vscode";

describe("Codex Control Center Extension", () => {
  it("activates successfully", async () => {
    const extension = vscode.extensions.getExtension("codex.codex-control-center");
    assert.ok(extension, "extension not found");
    await extension?.activate();
    assert.strictEqual(extension?.isActive, true);
  });

  it("registers commands", async () => {
    const commands = await vscode.commands.getCommands(true);
    assert.ok(commands.includes("codex-cc.askAgent"), "codex-cc.askAgent command not registered");
    assert.ok(commands.includes("codex-cc.sendSelection"), "codex-cc.sendSelection command not registered");
    assert.ok(commands.includes("codex-cc.reconnect"), "codex-cc.reconnect command not registered");
  });

  it("exposes expected default configuration", () => {
    const cfg = vscode.workspace.getConfiguration("codex-control-center");
    assert.strictEqual(cfg.get("gatewayUrl"), "http://127.0.0.1:8765");
    assert.strictEqual(cfg.get("defaultChatId"), 1);
    assert.strictEqual(cfg.get("defaultUserId"), 1);
    assert.strictEqual(cfg.get("defaultAgentId"), "default");
  });

  it("reconnect command executes", async () => {
    await vscode.commands.executeCommand("codex-cc.reconnect");
  });
});
