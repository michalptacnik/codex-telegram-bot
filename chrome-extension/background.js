const EXTENSION_VERSION = String((chrome.runtime.getManifest() || {}).version || "0.0.0");
const SUPPORTED_COMMANDS = ["open_url", "navigate_url", "run_script", "snapshot", "screenshot"];

const DEFAULT_CONFIG = {
  baseUrl: "http://127.0.0.1:42617",
  token: "",
  enabled: true,
  instanceId: ""
};

const STATE_DEFAULTS = {
  bridgeStatus: "inactive",
  bridgeStatusDetail: "Not connected",
  lastError: "",
  lastSuccessAt: "",
  lastHeartbeatAt: ""
};

const ALARM_NAME = "agenthq-bridge-heartbeat";
const ALARM_PERIOD_MINUTES_PREFERRED = 0.5;
const ALARM_PERIOD_MINUTES_FALLBACK = 1;
const FOLLOWUP_POLL_IDLE_MS = 1500;
const FOLLOWUP_POLL_BUSY_MS = 500;
const SCRIPT_RESULT_MAX_CHARS = 4000;
const SNAPSHOT_RESULT_MAX_CHARS = 80000;
const COMMAND_RESULT_CACHE_TTL_MS = 10 * 60 * 1000;
const COMMAND_RESULT_CACHE_MAX = 200;

let followupTimer = null;
let cycleInFlight = false;
const completedCommandResults = new Map();

function nowIso() {
  return new Date().toISOString();
}

function normalizeBaseUrl(raw) {
  const text = String(raw || "").trim();
  if (!text) {
    return DEFAULT_CONFIG.baseUrl;
  }
  const normalized = text.replace(/\/$/, "");
  if (normalized === "http://127.0.0.1:8765") {
    return DEFAULT_CONFIG.baseUrl;
  }
  return normalized;
}

function installAlarm() {
  try {
    chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD_MINUTES_PREFERRED });
  } catch (_err) {
    chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD_MINUTES_FALLBACK });
  }
}

function scheduleFollowup(ms) {
  const delay = Math.max(500, Number(ms || FOLLOWUP_POLL_IDLE_MS));
  if (followupTimer) {
    clearTimeout(followupTimer);
  }
  followupTimer = setTimeout(() => {
    followupTimer = null;
    runBridgeCycle();
  }, delay);
}

function clipText(value, maxChars) {
  const text = String(value || "");
  if (text.length <= maxChars) {
    return text;
  }
  return `${text.slice(0, maxChars)}…`;
}

function pruneCompletedCommandResults() {
  const cutoff = Date.now() - COMMAND_RESULT_CACHE_TTL_MS;
  for (const [commandId, entry] of completedCommandResults.entries()) {
    const ts = Number(entry && entry.ts ? entry.ts : 0);
    if (!ts || ts < cutoff) {
      completedCommandResults.delete(commandId);
    }
  }
  while (completedCommandResults.size > COMMAND_RESULT_CACHE_MAX) {
    const oldest = completedCommandResults.keys().next();
    if (oldest && !oldest.done) {
      completedCommandResults.delete(oldest.value);
    } else {
      break;
    }
  }
}

function normalizeCommandResult(result) {
  const raw = result || {};
  let data = {};
  try {
    const candidate = raw.data && typeof raw.data === "object" ? raw.data : {};
    data = JSON.parse(JSON.stringify(candidate));
  } catch (_err) {
    data = {};
  }
  return {
    ok: Boolean(raw.ok),
    output: String(raw.output || ""),
    data
  };
}

function getCachedCommandResult(commandId) {
  pruneCompletedCommandResults();
  const key = String(commandId || "").trim();
  if (!key) {
    return null;
  }
  const entry = completedCommandResults.get(key);
  if (!entry || !entry.result) {
    return null;
  }
  return normalizeCommandResult(entry.result);
}

function cacheCommandResult(commandId, result) {
  const key = String(commandId || "").trim();
  if (!key) {
    return;
  }
  pruneCompletedCommandResults();
  completedCommandResults.set(key, {
    ts: Date.now(),
    result: normalizeCommandResult(result)
  });
}

async function getStored(keys) {
  return await chrome.storage.local.get(keys);
}

async function setStored(values) {
  await chrome.storage.local.set(values);
}

async function loadConfig() {
  const stored = await getStored(["baseUrl", "token", "enabled", "instanceId"]);
  const baseUrl = normalizeBaseUrl(stored.baseUrl || DEFAULT_CONFIG.baseUrl);
  if (baseUrl !== String(stored.baseUrl || "").trim()) {
    await setStored({ baseUrl });
  }
  return {
    baseUrl,
    token: String(stored.token || DEFAULT_CONFIG.token || ""),
    enabled: stored.enabled !== undefined ? Boolean(stored.enabled) : Boolean(DEFAULT_CONFIG.enabled),
    instanceId: String(stored.instanceId || DEFAULT_CONFIG.instanceId || "")
  };
}

async function ensureInstanceId() {
  const cfg = await loadConfig();
  if (cfg.instanceId) {
    return cfg.instanceId;
  }
  const instanceId = crypto.randomUUID();
  await setStored({ instanceId });
  return instanceId;
}

async function loadBridgeState() {
  const stored = await getStored([
    "bridgeStatus",
    "bridgeStatusDetail",
    "lastError",
    "lastSuccessAt",
    "lastHeartbeatAt"
  ]);
  return {
    bridgeStatus: String(stored.bridgeStatus || STATE_DEFAULTS.bridgeStatus),
    bridgeStatusDetail: String(stored.bridgeStatusDetail || STATE_DEFAULTS.bridgeStatusDetail),
    lastError: String(stored.lastError || STATE_DEFAULTS.lastError),
    lastSuccessAt: String(stored.lastSuccessAt || STATE_DEFAULTS.lastSuccessAt),
    lastHeartbeatAt: String(stored.lastHeartbeatAt || STATE_DEFAULTS.lastHeartbeatAt)
  };
}

async function updateBridgeState(patch) {
  await setStored({ ...patch, lastHeartbeatAt: nowIso() });
  await refreshActionBadge();
}

async function refreshActionBadge() {
  try {
    const state = await loadBridgeState();
    const active = String(state.bridgeStatus || "inactive") === "active";
    const text = active ? "ON" : "OFF";
    const color = active ? "#0f9d58" : "#9b1c1c";
    const detail = String(state.bridgeStatusDetail || (active ? "Connected" : "Disconnected"));
    await chrome.action.setBadgeText({ text });
    await chrome.action.setBadgeBackgroundColor({ color });
    await chrome.action.setTitle({ title: `AgentHQ Bridge: ${detail}` });
  } catch (_err) {
    // Ignore badge update failures to keep bridge loop running.
  }
}

function buildHeaders(token) {
  const headers = { "content-type": "application/json" };
  if (token) {
    headers["x-browser-extension-token"] = token;
  }
  return headers;
}

async function getActiveTab() {
  try {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return tabs && tabs.length ? tabs[0] : null;
  } catch (_err) {
    return null;
  }
}

async function activeTabInfo() {
  const tab = await getActiveTab();
  return {
    active_tab_url: tab && tab.url ? String(tab.url) : "",
    active_tab_title: tab && tab.title ? String(tab.title) : ""
  };
}

async function apiJson(baseUrl, path, method, body, token) {
  const endpoint = `${normalizeBaseUrl(baseUrl)}${path}`;
  const response = await fetch(endpoint, {
    method: method || "GET",
    headers: buildHeaders(token),
    body: body ? JSON.stringify(body) : undefined
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (_err) {
    payload = {};
  }
  return { ok: response.ok, status: response.status, payload };
}

async function registerClient() {
  const cfg = await loadConfig();
  if (!cfg.enabled) {
    await updateBridgeState({
      bridgeStatus: "inactive",
      bridgeStatusDetail: "Bridge disabled",
      lastError: ""
    });
    return { ok: false, reason: "disabled" };
  }

  const instanceId = await ensureInstanceId();
  const tab = await activeTabInfo();
  const body = {
    instance_id: instanceId,
    label: "AgentHQ Chrome Bridge",
    version: chrome.runtime.getManifest().version,
    extension_version: EXTENSION_VERSION,
    supported_commands: SUPPORTED_COMMANDS,
    platform: navigator.platform || "",
    user_agent: navigator.userAgent || "",
    active_tab_url: tab.active_tab_url,
    active_tab_title: tab.active_tab_title
  };

  try {
    const result = await apiJson(cfg.baseUrl, "/api/browser/extension/register", "POST", body, cfg.token);
    if (!result.ok) {
      const detail = String(result.payload && result.payload.detail ? result.payload.detail : `HTTP ${result.status}`);
      await updateBridgeState({
        bridgeStatus: "inactive",
        bridgeStatusDetail: "Registration failed",
        lastError: detail
      });
      return { ok: false, reason: detail };
    }
    await updateBridgeState({
      bridgeStatus: "active",
      bridgeStatusDetail: "Connected",
      lastError: "",
      lastSuccessAt: nowIso()
    });
    return { ok: true, instanceId };
  } catch (err) {
    await updateBridgeState({
      bridgeStatus: "inactive",
      bridgeStatusDetail: "Connection error",
      lastError: String(err)
    });
    return { ok: false, reason: String(err) };
  }
}

async function heartbeat() {
  const cfg = await loadConfig();
  if (!cfg.enabled) {
    await updateBridgeState({
      bridgeStatus: "inactive",
      bridgeStatusDetail: "Bridge disabled",
      lastError: ""
    });
    return { ok: false, reason: "disabled" };
  }
  const instanceId = await ensureInstanceId();
  const tab = await activeTabInfo();
  const body = {
    instance_id: instanceId,
    extension_version: EXTENSION_VERSION,
    supported_commands: SUPPORTED_COMMANDS,
    active_tab_url: tab.active_tab_url,
    active_tab_title: tab.active_tab_title
  };
  try {
    const result = await apiJson(cfg.baseUrl, "/api/browser/extension/heartbeat", "POST", body, cfg.token);
    if (!result.ok) {
      const detail = String(result.payload && result.payload.detail ? result.payload.detail : `HTTP ${result.status}`);
      await updateBridgeState({
        bridgeStatus: "inactive",
        bridgeStatusDetail: "Heartbeat failed",
        lastError: detail
      });
      return { ok: false, reason: detail };
    }
    await updateBridgeState({
      bridgeStatus: "active",
      bridgeStatusDetail: "Connected",
      lastError: "",
      lastSuccessAt: nowIso()
    });
    return { ok: true, instanceId };
  } catch (err) {
    await updateBridgeState({
      bridgeStatus: "inactive",
      bridgeStatusDetail: "Connection error",
      lastError: String(err)
    });
    return { ok: false, reason: String(err) };
  }
}

async function fetchCommands(instanceId) {
  const cfg = await loadConfig();
  const url = `/api/browser/extension/commands?instance_id=${encodeURIComponent(instanceId)}&limit=5`;
  return await apiJson(cfg.baseUrl, url, "GET", null, cfg.token);
}

async function postCommandResult(instanceId, commandId, ok, output, data) {
  const cfg = await loadConfig();
  const body = {
    instance_id: instanceId,
    ok: Boolean(ok),
    output: String(output || ""),
    data: data || {}
  };
  return await apiJson(
    cfg.baseUrl,
    `/api/browser/extension/commands/${encodeURIComponent(commandId)}/result`,
    "POST",
    body,
    cfg.token
  );
}

// ---------------------------------------------------------------------------
// Navigation helpers
// ---------------------------------------------------------------------------

async function waitForTabComplete(tabId, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 15000);
  return new Promise((resolve) => {
    function listener(updatedTabId, changeInfo) {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(true);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
    const remaining = Math.max(0, deadline - Date.now());
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(false);
    }, remaining);
  });
}

async function openOrNavigate(url, opts) {
  const options = opts || {};
  const active = options.active !== false;
  if (options.new_tab === true) {
    const tab = await chrome.tabs.create({ url, active });
    const tabId = tab.id || 0;
    if (tabId) {
      await waitForTabComplete(tabId, 15000);
      try {
        const updated = await chrome.tabs.get(tabId);
        return {
          ok: true,
          output: `Opened new tab ${tabId}`,
          data: { tab_id: tabId, url: updated.url || url, title: updated.title || "" }
        };
      } catch (_e) { /* tab may have closed */ }
    }
    return {
      ok: true,
      output: `Opened new tab ${tabId}`,
      data: { tab_id: tabId, url: tab.url || url, title: tab.title || "" }
    };
  }

  const current = await getActiveTab();
  if (!current || !current.id) {
    const created = await chrome.tabs.create({ url, active });
    const cid = created.id || 0;
    if (cid) {
      await waitForTabComplete(cid, 15000);
      try {
        const updated = await chrome.tabs.get(cid);
        return {
          ok: true,
          output: `Opened tab ${cid}`,
          data: { tab_id: cid, url: updated.url || url, title: updated.title || "" }
        };
      } catch (_e) { /* */ }
    }
    return {
      ok: true,
      output: `Opened tab ${cid}`,
      data: { tab_id: cid, url: created.url || url, title: created.title || "" }
    };
  }
  const updated = await chrome.tabs.update(current.id, { url, active });
  const uid = updated.id || current.id;
  await waitForTabComplete(uid, 15000);
  try {
    const final = await chrome.tabs.get(uid);
    return {
      ok: true,
      output: `Navigated tab ${uid}`,
      data: { tab_id: uid, url: final.url || url, title: final.title || "" }
    };
  } catch (_e) { /* */ }
  return {
    ok: true,
    output: `Navigated tab ${uid}`,
    data: { tab_id: uid, url: updated.url || url, title: updated.title || "" }
  };
}

function toSerializable(value) {
  if (value === undefined) {
    return null;
  }
  if (value === null) {
    return null;
  }
  if (typeof value === "string") {
    return clipText(value, SCRIPT_RESULT_MAX_CHARS);
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  try {
    return JSON.parse(JSON.stringify(value));
  } catch (_err) {
    return clipText(String(value), SCRIPT_RESULT_MAX_CHARS);
  }
}

function asResultText(value) {
  if (value === undefined) return "undefined";
  if (value === null) return "null";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch (_err) {
    return String(value);
  }
}

function shouldUseDebuggerFallback(errorText) {
  const text = String(errorText || "").toLowerCase();
  return (
    text.includes("content security policy")
    || text.includes("unsafe-eval")
    || text.includes("evaluating a string as javascript violates")
  );
}

function _debuggerAttach(tabId) {
  return new Promise((resolve, reject) => {
    chrome.debugger.attach({ tabId }, "1.3", () => {
      const err = chrome.runtime.lastError;
      if (err) {
        reject(new Error(String(err.message || err)));
        return;
      }
      resolve();
    });
  });
}

function _debuggerDetach(tabId) {
  return new Promise((resolve) => {
    chrome.debugger.detach({ tabId }, () => {
      resolve();
    });
  });
}

function _debuggerSendCommand(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params || {}, (result) => {
      const err = chrome.runtime.lastError;
      if (err) {
        reject(new Error(String(err.message || err)));
        return;
      }
      resolve(result || {});
    });
  });
}

async function executeScriptViaDebugger(tabId, script) {
  let attached = false;
  try {
    await _debuggerAttach(tabId);
    attached = true;
    await _debuggerSendCommand(tabId, "Runtime.enable", {});
    const wrappedExpression = `(async () => {\n${String(script || "")}\n})()`;
    const evalResult = await _debuggerSendCommand(
      tabId,
      "Runtime.evaluate",
      {
        expression: wrappedExpression,
        awaitPromise: true,
        returnByValue: true
      }
    );
    const exception = evalResult && evalResult.exceptionDetails ? evalResult.exceptionDetails : null;
    if (exception) {
      const exceptionObj = exception.exception || {};
      const detail = String(exceptionObj.description || exceptionObj.value || exception.text || "script exception");
      return {
        ok: false,
        output: `Script failed on tab ${tabId}: ${detail}`,
        data: { tab_id: tabId, error: detail, engine: "debugger" }
      };
    }
    const resultObj = evalResult && evalResult.result ? evalResult.result : {};
    let value = null;
    if (Object.prototype.hasOwnProperty.call(resultObj, "value")) {
      value = resultObj.value;
    } else if (Object.prototype.hasOwnProperty.call(resultObj, "description")) {
      value = resultObj.description;
    } else {
      value = null;
    }
    return {
      ok: true,
      output: `Script executed on tab ${tabId}`,
      data: {
        tab_id: tabId,
        result: toSerializable(value),
        result_text: clipText(asResultText(value), SCRIPT_RESULT_MAX_CHARS),
        engine: "debugger"
      }
    };
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    return {
      ok: false,
      output: `Script failed on tab ${tabId}: ${msg}`,
      data: { tab_id: tabId, error: msg, engine: "debugger" }
    };
  } finally {
    if (attached) {
      try {
        await _debuggerDetach(tabId);
      } catch (_err) {
        // Ignore detach failures.
      }
    }
  }
}

async function resolveTargetTabId(payload) {
  const explicit = Number(payload.tab_id || payload.tabId || 0);
  if (Number.isInteger(explicit) && explicit > 0) {
    return explicit;
  }
  const activeTab = await getActiveTab();
  if (activeTab && activeTab.id) {
    return Number(activeTab.id);
  }
  return 0;
}

async function executeScript(payload) {
  const script = String(payload.script || payload.js || payload.code || "").trim();
  if (!script) {
    return { ok: false, output: "Missing script.", data: {} };
  }
  const tabId = await resolveTargetTabId(payload || {});
  if (!tabId) {
    return { ok: false, output: "No target tab available.", data: {} };
  }
  const allFrames = payload && payload.all_frames === true;

  try {
    const injections = await chrome.scripting.executeScript({
      target: { tabId, allFrames },
      func: async (source) => {
        const asText = (value) => {
          if (value === undefined) return "undefined";
          if (value === null) return "null";
          if (typeof value === "string") return value;
          try {
            return JSON.stringify(value);
          } catch (_err) {
            return String(value);
          }
        };
        try {
          const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
          const fn = new AsyncFunction(source);
          const value = await fn();
          return { ok: true, value: value, value_text: asText(value) };
        } catch (err) {
          const msg = err && err.message ? String(err.message) : String(err);
          return { ok: false, error: msg };
        }
      },
      args: [script]
    });

    const first = Array.isArray(injections) && injections.length ? injections[0] : null;
    const result = first && first.result ? first.result : { ok: false, error: "No execution result" };
    if (result.ok) {
      return {
        ok: true,
        output: `Script executed on tab ${tabId}`,
        data: {
          tab_id: tabId,
          result: toSerializable(result.value),
          result_text: clipText(String(result.value_text || ""), SCRIPT_RESULT_MAX_CHARS),
          engine: "scripting"
        }
      };
    }
    const primaryError = String(result.error || "unknown");
    if (!shouldUseDebuggerFallback(primaryError)) {
      return {
        ok: false,
        output: `Script failed on tab ${tabId}: ${primaryError}`,
        data: {
          tab_id: tabId,
          error: primaryError,
          engine: "scripting"
        }
      };
    }
  } catch (err) {
    const primaryError = String(err && err.message ? err.message : err);
    if (!shouldUseDebuggerFallback(primaryError)) {
      return {
        ok: false,
        output: `Script failed on tab ${tabId}: ${primaryError}`,
        data: {
          tab_id: tabId,
          error: primaryError,
          engine: "scripting"
        }
      };
    }
  }

  return await executeScriptViaDebugger(tabId, script);
}

// ---------------------------------------------------------------------------
// Snapshot — accessibility tree with numeric element refs
// ---------------------------------------------------------------------------

async function executeSnapshot(payload) {
  const tabId = await resolveTargetTabId(payload || {});
  if (!tabId) {
    return { ok: false, output: "No target tab available.", data: {} };
  }
  const maxElements = Math.min(Math.max(10, Number(payload.max_elements || 200)), 500);

  try {
    const injections = await chrome.scripting.executeScript({
      target: { tabId, allFrames: false },
      func: (budget) => {
        const INTERACTIVE = new Set([
          "A","BUTTON","INPUT","TEXTAREA","SELECT","SUMMARY","DETAILS"
        ]);
        const STRUCTURAL = new Set([
          "MAIN","NAV","HEADER","FOOTER","ASIDE","SECTION","ARTICLE","FORM","TABLE",
          "H1","H2","H3","H4","H5","H6"
        ]);
        const ROLE_INTERACTIVE = new Set([
          "button","link","tab","checkbox","radio","menuitem","option","switch",
          "combobox","listbox","slider","spinbutton","searchbox","textbox"
        ]);

        function isVisible(el) {
          if (!el || !el.getBoundingClientRect) return false;
          const r = el.getBoundingClientRect();
          if (r.width === 0 && r.height === 0) return false;
          const s = window.getComputedStyle(el);
          if (s.display === "none" || s.visibility === "hidden") return false;
          return true;
        }

        function uniqueSelector(el) {
          if (el.id) return "#" + CSS.escape(el.id);
          const parts = [];
          let cur = el;
          while (cur && cur !== document.body && cur !== document.documentElement) {
            let seg = cur.tagName.toLowerCase();
            if (cur.id) { parts.unshift("#" + CSS.escape(cur.id)); break; }
            const parent = cur.parentElement;
            if (parent) {
              const siblings = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
              if (siblings.length > 1) {
                seg += ":nth-of-type(" + (siblings.indexOf(cur) + 1) + ")";
              }
            }
            parts.unshift(seg);
            cur = cur.parentElement;
          }
          return parts.join(" > ");
        }

        function clipStr(s, n) { return s && s.length > n ? s.slice(0, n) : (s || ""); }

        const elements = [];
        const refMap = {};
        let refCounter = 0;
        let totalOnPage = 0;

        const walker = document.createTreeWalker(
          document.body || document.documentElement,
          NodeFilter.SHOW_ELEMENT,
          null
        );
        let node = walker.currentNode;
        while (node) {
          if (node.nodeType === Node.ELEMENT_NODE) {
            const tag = node.tagName || "";
            const role = (node.getAttribute("role") || "").toLowerCase();
            const ce = node.hasAttribute("contenteditable") && node.getAttribute("contenteditable") !== "false";
            const isInteractive = INTERACTIVE.has(tag) || ROLE_INTERACTIVE.has(role) || ce;
            const isStructural = STRUCTURAL.has(tag);

            if ((isInteractive || isStructural) && isVisible(node)) {
              totalOnPage++;
              if (elements.length < budget) {
                refCounter++;
                const ref = refCounter;
                const sel = uniqueSelector(node);
                const entry = { ref: ref, tag: tag.toLowerCase(), role: role };
                const ariaLabel = node.getAttribute("aria-label") || "";
                const name = ariaLabel || node.getAttribute("name") || node.getAttribute("title") || "";
                if (name) entry.name = clipStr(name, 120);
                const text = clipStr((node.innerText || node.textContent || "").replace(/\s+/g, " ").trim(), 120);
                if (text && text !== name) entry.text = text;
                if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
                  const t = node.getAttribute("type") || "";
                  if (t) entry.type = t;
                  const ph = node.getAttribute("placeholder") || "";
                  if (ph) entry.placeholder = clipStr(ph, 80);
                  if (node.value) entry.value = clipStr(String(node.value), 80);
                }
                if (tag === "A") {
                  const href = node.getAttribute("href") || "";
                  if (href) entry.href = clipStr(href, 200);
                }
                if (node.checked !== undefined) entry.checked = Boolean(node.checked);
                if (node.selected !== undefined) entry.selected = Boolean(node.selected);
                entry.selector_path = sel;
                elements.push(entry);
                refMap[String(ref)] = sel;
              }
            }
          }
          node = walker.nextNode();
        }

        return {
          url: String(location.href || ""),
          title: String(document.title || ""),
          elements: elements,
          ref_map: refMap,
          total_elements_on_page: totalOnPage,
          truncated: totalOnPage > budget
        };
      },
      args: [maxElements]
    });

    const first = Array.isArray(injections) && injections.length ? injections[0] : null;
    const result = first && first.result ? first.result : null;
    if (!result) {
      return { ok: false, output: "Snapshot returned no result.", data: {} };
    }

    const output = clipText(JSON.stringify(result), SNAPSHOT_RESULT_MAX_CHARS);
    return {
      ok: true,
      output: `Snapshot captured for tab ${tabId}: ${(result.elements || []).length} elements`,
      data: {
        tab_id: tabId,
        result: result
      }
    };
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    return {
      ok: false,
      output: `Snapshot failed on tab ${tabId}: ${msg}`,
      data: { tab_id: tabId, error: msg }
    };
  }
}

// ---------------------------------------------------------------------------
// Screenshot — capture visible tab as base64 image
// ---------------------------------------------------------------------------

async function executeScreenshot(payload) {
  const fmt = String(payload.format || "png").toLowerCase();
  const format = fmt === "jpeg" ? "jpeg" : "png";
  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(null, { format: format });
    const tabId = await resolveTargetTabId(payload || {});
    return {
      ok: true,
      output: `Screenshot captured (${format})`,
      data: {
        tab_id: tabId || 0,
        image_data: dataUrl,
        format: format
      }
    };
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    return {
      ok: false,
      output: `Screenshot failed: ${msg}`,
      data: { error: msg }
    };
  }
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------

async function executeCommand(command) {
  const type = String(command.command_type || "");
  const payload = command.payload || {};
  if (type === "open_url") {
    const url = String(payload.url || "").trim();
    if (!url) {
      return { ok: false, output: "Missing URL.", data: {} };
    }
    return await openOrNavigate(url, {
      new_tab: payload.new_tab !== false,
      active: payload.active !== false
    });
  }
  if (type === "navigate_url") {
    const url = String(payload.url || "").trim();
    if (!url) {
      return { ok: false, output: "Missing URL.", data: {} };
    }
    return await openOrNavigate(url, {
      new_tab: false,
      active: payload.active !== false
    });
  }
  if (type === "run_script") {
    return await executeScript(payload);
  }
  if (type === "snapshot") {
    return await executeSnapshot(payload);
  }
  if (type === "screenshot") {
    return await executeScreenshot(payload);
  }
  return {
    ok: false,
    output: `Unsupported command type: ${type}`,
    data: {}
  };
}

async function heartbeatAndPoll() {
  const reg = await registerClient();
  if (!reg.ok) {
    return { ok: false, polled: 0, processed: 0 };
  }
  const hb = await heartbeat();
  if (!hb.ok) {
    return { ok: false, polled: 0, processed: 0 };
  }
  const instanceId = hb.instanceId || reg.instanceId;
  const commandsResp = await fetchCommands(instanceId);
  if (!commandsResp.ok) {
    const detail = String(commandsResp.payload && commandsResp.payload.detail ? commandsResp.payload.detail : `HTTP ${commandsResp.status}`);
    await updateBridgeState({
      bridgeStatus: "inactive",
      bridgeStatusDetail: "Command poll failed",
      lastError: detail
    });
    return { ok: false, polled: 0, processed: 0 };
  }

  const items = Array.isArray(commandsResp.payload.items) ? commandsResp.payload.items : [];
  let processed = 0;
  for (const item of items) {
    const commandId = String(item && item.command_id ? item.command_id : "").trim();
    if (!commandId) {
      continue;
    }

    let result = getCachedCommandResult(commandId);
    if (!result) {
      result = { ok: false, output: "Unhandled command", data: {} };
      try {
        result = await executeCommand(item);
      } catch (err) {
        result = { ok: false, output: String(err), data: {} };
      }
      result = normalizeCommandResult(result);
      cacheCommandResult(commandId, result);
    }

    const postResp = await postCommandResult(
      instanceId,
      commandId,
      result.ok,
      result.output,
      result.data || {}
    );
    if (!postResp.ok) {
      const detail = String(postResp.payload && postResp.payload.detail ? postResp.payload.detail : `HTTP ${postResp.status}`);
      await updateBridgeState({
        bridgeStatus: "inactive",
        bridgeStatusDetail: "Result post failed",
        lastError: detail
      });
    }
    processed += 1;
  }

  return { ok: true, polled: items.length, processed };
}

async function runBridgeCycle() {
  if (cycleInFlight) {
    return;
  }
  cycleInFlight = true;
  try {
    const summary = await heartbeatAndPoll();
    if (summary && summary.ok) {
      scheduleFollowup(summary.polled > 0 ? FOLLOWUP_POLL_BUSY_MS : FOLLOWUP_POLL_IDLE_MS);
    } else {
      scheduleFollowup(FOLLOWUP_POLL_IDLE_MS);
    }
  } catch (err) {
    await updateBridgeState({
      bridgeStatus: "inactive",
      bridgeStatusDetail: "Bridge cycle failed",
      lastError: String(err)
    });
    scheduleFollowup(FOLLOWUP_POLL_IDLE_MS);
  } finally {
    cycleInFlight = false;
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await ensureInstanceId();
  installAlarm();
  await refreshActionBadge();
  await runBridgeCycle();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureInstanceId();
  installAlarm();
  await refreshActionBadge();
  await runBridgeCycle();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (!alarm || alarm.name !== ALARM_NAME) {
    return;
  }
  await runBridgeCycle();
});

chrome.tabs.onActivated.addListener(async () => {
  await runBridgeCycle();
});

chrome.tabs.onUpdated.addListener(async (_tabId, changeInfo) => {
  if (changeInfo.status === "complete") {
    await runBridgeCycle();
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const type = message && message.type ? String(message.type) : "";
  if (type === "bridge_ping") {
    runBridgeCycle()
      .then(async () => {
        const cfg = await loadConfig();
        const state = await loadBridgeState();
        sendResponse({ ok: true, config: cfg, state });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err) });
      });
    return true;
  }
  if (type === "bridge_config_updated") {
    runBridgeCycle()
      .then(async () => {
        const cfg = await loadConfig();
        const state = await loadBridgeState();
        sendResponse({ ok: true, config: cfg, state });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err) });
      });
    return true;
  }
  if (type === "bridge_status") {
    Promise.all([loadConfig(), loadBridgeState()])
      .then(([cfg, state]) => {
        sendResponse({ ok: true, config: cfg, state });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err) });
      });
    return true;
  }
  return false;
});

// Keep alarm installed even if extension was loaded while browser was already running.
installAlarm();
refreshActionBadge();
runBridgeCycle();
