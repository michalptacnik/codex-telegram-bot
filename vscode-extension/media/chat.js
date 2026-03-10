// @ts-nocheck
(function () {
  const vscode = acquireVsCodeApi();

  const connDot = document.getElementById("conn-dot");
  const connLabel = document.getElementById("conn-label");
  const agentSelect = document.getElementById("agent-select");
  const sessionSelect = document.getElementById("session-select");
  const chatIdInput = document.getElementById("chat-id");
  const userIdInput = document.getElementById("user-id");
  const refreshBtn = document.getElementById("refresh-btn");
  const identityBtn = document.getElementById("identity-btn");
  const chatLog = document.getElementById("chat-log");
  const chatForm = document.getElementById("chat-form");
  const chatInput = document.getElementById("chat-input");

  let activeSessionId = "";
  let activeChatId = 0;
  let activeUserId = 0;
  let activeAgentId = "default";
  let knownSessions = [];
  let currentAssistantBubble = null;

  // --- Rendering helpers ---

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function inlineMarkdown(text) {
    let out = escapeHtml(text);
    out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/\*(.+?)\*/g, "<em>$1</em>");
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    return out;
  }

  function renderMarkdownLite(text) {
    const lines = String(text || "").split("\n");
    const parts = [];
    let inList = false;
    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) {
        if (inList) { parts.push("</ul>"); inList = false; }
        parts.push("<br/>");
        continue;
      }
      if (line.startsWith("- ")) {
        if (!inList) { parts.push("<ul>"); inList = true; }
        parts.push("<li>" + inlineMarkdown(line.slice(2)) + "</li>");
        continue;
      }
      if (inList) { parts.push("</ul>"); inList = false; }
      parts.push("<p>" + inlineMarkdown(line) + "</p>");
    }
    if (inList) { parts.push("</ul>"); }
    return parts.join("");
  }

  function appendMessage(role, text) {
    const row = document.createElement("div");
    row.className = "msg-row " + role;
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.dataset.raw = text || "";
    if (role === "assistant") {
      bubble.innerHTML = renderMarkdownLite(text);
    } else {
      bubble.textContent = text;
    }
    row.appendChild(bubble);
    chatLog.appendChild(row);
    chatLog.scrollTop = chatLog.scrollHeight;
    return bubble;
  }

  function appendToolEvent(ev) {
    if (!currentAssistantBubble) {
      currentAssistantBubble = appendMessage("assistant", "");
    }
    const block = document.createElement("div");
    block.className = "tool-event tool-" + (ev.status || "result");
    const detail = ev.detail || {};
    const title = (ev.name || "tool") + " \u00b7 " + (ev.status || "");
    const info = [];
    if (detail.step) { info.push("step=" + detail.step); }
    if (detail.returncode !== undefined) { info.push("rc=" + detail.returncode); }
    if (detail.approval_id) { info.push("approval=" + String(detail.approval_id).slice(0, 8)); }
    if (detail.message) { info.push(detail.message); }
    if (detail.output) { info.push(String(detail.output).slice(0, 200)); }
    block.textContent = info.length ? title + " (" + info.join(", ") + ")" : title;
    currentAssistantBubble.appendChild(block);

    if (ev.status === "awaiting_approval" && detail.approval_id) {
      const actions = document.createElement("div");
      actions.className = "approval-actions";

      const approveBtn = document.createElement("button");
      approveBtn.className = "btn-approve";
      approveBtn.textContent = "Approve";
      approveBtn.onclick = function () {
        vscode.postMessage({
          command: "approve",
          approvalId: detail.approval_id,
          sessionId: activeSessionId,
          chatId: detail.chat_id || activeChatId,
          userId: detail.user_id || activeUserId,
        });
        actions.remove();
      };

      const denyBtn = document.createElement("button");
      denyBtn.className = "btn-deny";
      denyBtn.textContent = "Deny";
      denyBtn.onclick = function () {
        vscode.postMessage({
          command: "deny",
          approvalId: detail.approval_id,
          sessionId: activeSessionId,
          chatId: detail.chat_id || activeChatId,
          userId: detail.user_id || activeUserId,
        });
        actions.remove();
      };

      actions.appendChild(approveBtn);
      actions.appendChild(denyBtn);
      currentAssistantBubble.appendChild(actions);
    }

    chatLog.scrollTop = chatLog.scrollHeight;
  }

  // --- Connection state ---

  function setConnectionState(state) {
    connDot.className = state;
    const labels = { connected: "Connected", connecting: "Connecting...", disconnected: "Disconnected" };
    connLabel.textContent = labels[state] || state;
  }

  function setNumericInput(input, value) {
    if (!input) { return; }
    const n = Number(value);
    input.value = Number.isFinite(n) && n > 0 ? String(Math.floor(n)) : "";
  }

  function parsePositiveInt(value, fallback) {
    const n = Number(value);
    if (Number.isFinite(n) && n > 0) {
      return Math.floor(n);
    }
    return fallback > 0 ? fallback : 1;
  }

  function renderAgentOptions(agents) {
    if (!agentSelect) { return; }
    const list = Array.isArray(agents) ? agents.slice() : [];
    if (list.length === 0) {
      list.push({ agent_id: "default", name: "default", enabled: true });
    }
    agentSelect.innerHTML = "";
    for (const item of list) {
      const id = String(item.agent_id || "");
      if (!id) { continue; }
      const opt = document.createElement("option");
      const disabledSuffix = item.enabled === false ? " (disabled)" : "";
      opt.value = id;
      opt.textContent = String(item.name || id) + disabledSuffix;
      agentSelect.appendChild(opt);
    }
    if (activeAgentId) {
      agentSelect.value = activeAgentId;
    }
    if (!agentSelect.value && agentSelect.options.length > 0) {
      agentSelect.selectedIndex = 0;
      activeAgentId = agentSelect.value;
    }
  }

  function renderSessionOptions(sessions) {
    if (!sessionSelect) { return; }
    knownSessions = Array.isArray(sessions) ? sessions.slice() : [];
    sessionSelect.innerHTML = "";
    const currentOpt = document.createElement("option");
    currentOpt.value = "";
    currentOpt.textContent = "(current/new session)";
    sessionSelect.appendChild(currentOpt);
    for (const item of knownSessions) {
      const sid = String(item.session_id || "");
      if (!sid) { continue; }
      const opt = document.createElement("option");
      const shortId = sid.length > 10 ? sid.slice(0, 10) : sid;
      const status = String(item.status || "");
      const sessionAgent = String(item.current_agent_id || "");
      opt.value = sid;
      opt.textContent = `${shortId}  [${status}]  (${sessionAgent || "default"})`;
      sessionSelect.appendChild(opt);
    }
    sessionSelect.value = activeSessionId || "";
  }

  function applyContext(msg) {
    activeChatId = parsePositiveInt(msg.chatId, activeChatId || 1);
    activeUserId = parsePositiveInt(msg.userId, activeUserId || 1);
    activeAgentId = String(msg.agentId || activeAgentId || "default");
    activeSessionId = String(msg.sessionId || activeSessionId || "");
    setNumericInput(chatIdInput, activeChatId);
    setNumericInput(userIdInput, activeUserId);
    renderAgentOptions(msg.agents);
    renderSessionOptions(msg.sessions);
  }

  // --- Inbound messages from extension host ---

  window.addEventListener("message", function (event) {
    const msg = event.data;
    if (!msg || !msg.type) { return; }

    switch (msg.type) {
      case "connectionState":
        setConnectionState(msg.state);
        break;

      case "session":
        activeSessionId = msg.session_id || "";
        activeChatId = msg.chat_id || 0;
        activeUserId = msg.user_id || 0;
        setNumericInput(chatIdInput, activeChatId);
        setNumericInput(userIdInput, activeUserId);
        if (sessionSelect) {
          sessionSelect.value = activeSessionId || "";
        }
        break;

      case "assistant_chunk":
        if (!currentAssistantBubble) {
          currentAssistantBubble = appendMessage("assistant", "");
        }
        currentAssistantBubble.dataset.raw =
          (currentAssistantBubble.dataset.raw || "") + (msg.text || "");
        currentAssistantBubble.innerHTML = renderMarkdownLite(
          currentAssistantBubble.dataset.raw,
        );
        chatLog.scrollTop = chatLog.scrollHeight;
        break;

      case "tool_event":
        appendToolEvent(msg);
        break;

      case "done":
        currentAssistantBubble = null;
        break;

      case "error":
        appendMessage("assistant", "Error: " + (msg.detail || "unknown"));
        currentAssistantBubble = null;
        break;

      case "context":
        applyContext(msg);
        break;

      case "contextError":
        appendMessage("assistant", "Context error: " + (msg.detail || "unknown"));
        break;

      case "userEcho":
        appendMessage("user", msg.text || "");
        currentAssistantBubble = null;
        break;
    }
  });

  // --- Form submit ---

  chatForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    const text = (chatInput.value || "").trim();
    if (!text) { return; }
    appendMessage("user", text);
    currentAssistantBubble = null;
    const chatId = parsePositiveInt(chatIdInput && chatIdInput.value, activeChatId || 1);
    const userId = parsePositiveInt(userIdInput && userIdInput.value, activeUserId || 1);
    const sessionId = sessionSelect ? String(sessionSelect.value || "") : activeSessionId;
    const agentId = agentSelect ? String(agentSelect.value || "default") : activeAgentId || "default";
    activeChatId = chatId;
    activeUserId = userId;
    activeSessionId = sessionId;
    activeAgentId = agentId;
    vscode.postMessage({
      command: "sendMessage",
      text: text,
      sessionId: sessionId,
      chatId: chatId,
      userId: userId,
      agentId: agentId,
    });
    chatInput.value = "";
  });

  // Submit on Ctrl/Cmd+Enter
  chatInput.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
      ev.preventDefault();
      chatForm.dispatchEvent(new Event("submit"));
    }
  });

  if (agentSelect) {
    agentSelect.addEventListener("change", function () {
      const id = String(agentSelect.value || "").trim();
      if (!id) { return; }
      activeAgentId = id;
      vscode.postMessage({ command: "setAgent", agentId: id });
    });
  }

  if (sessionSelect) {
    sessionSelect.addEventListener("change", function () {
      const sid = String(sessionSelect.value || "").trim();
      activeSessionId = sid;
      if (!sid) { return; }
      const session = knownSessions.find((item) => String(item.session_id || "") === sid);
      if (session) {
        activeChatId = parsePositiveInt(session.chat_id, activeChatId || 1);
        activeUserId = parsePositiveInt(session.user_id, activeUserId || 1);
        setNumericInput(chatIdInput, activeChatId);
        setNumericInput(userIdInput, activeUserId);
      }
      vscode.postMessage({ command: "selectSession", sessionId: sid });
    });
  }

  if (identityBtn) {
    identityBtn.addEventListener("click", function () {
      const chatId = parsePositiveInt(chatIdInput && chatIdInput.value, activeChatId || 1);
      const userId = parsePositiveInt(userIdInput && userIdInput.value, activeUserId || 1);
      activeChatId = chatId;
      activeUserId = userId;
      vscode.postMessage({ command: "setIdentity", chatId: chatId, userId: userId });
    });
  }

  if (refreshBtn) {
    refreshBtn.addEventListener("click", function () {
      vscode.postMessage({ command: "refreshContext" });
    });
  }

  vscode.postMessage({ command: "ready" });
})();
