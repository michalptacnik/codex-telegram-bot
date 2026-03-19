const els = {
  enabled: document.getElementById("enabled"),
  baseUrl: document.getElementById("base-url"),
  token: document.getElementById("token"),
  instanceId: document.getElementById("instance-id"),
  statusPill: document.getElementById("status-pill"),
  meta: document.getElementById("meta"),
  error: document.getElementById("error"),
  save: document.getElementById("save"),
  ping: document.getElementById("ping")
};

function normalizeBaseUrl(raw) {
  const text = String(raw || "").trim();
  if (!text) return "http://127.0.0.1:42617";
  const normalized = text.replace(/\/$/, "");
  if (normalized === "http://127.0.0.1:8765") return "http://127.0.0.1:42617";
  return normalized;
}

function setStatus(active, detail) {
  els.statusPill.textContent = active ? "active" : "inactive";
  els.statusPill.classList.toggle("active", active);
  els.statusPill.classList.toggle("inactive", !active);
  if (detail) {
    els.meta.textContent = detail;
  }
}

function setError(text) {
  els.error.textContent = String(text || "");
}

async function getStorage(keys) {
  return await chrome.storage.local.get(keys);
}

async function setStorage(values) {
  await chrome.storage.local.set(values);
}

function askBackground(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message || "message error" });
        return;
      }
      resolve(response || { ok: false, error: "no response" });
    });
  });
}

function renderStatus(response) {
  const cfg = (response && response.config) || {};
  const state = (response && response.state) || {};

  els.enabled.checked = cfg.enabled !== false;
  els.baseUrl.value = normalizeBaseUrl(cfg.baseUrl || "");
  els.token.value = String(cfg.token || "");
  els.instanceId.value = String(cfg.instanceId || "");

  const active = String(state.bridgeStatus || "inactive") === "active";
  const detail = [
    `state: ${state.bridgeStatus || "inactive"}`,
    state.bridgeStatusDetail ? `detail: ${state.bridgeStatusDetail}` : "",
    state.lastSuccessAt ? `last success: ${state.lastSuccessAt}` : ""
  ]
    .filter(Boolean)
    .join("\n");

  setStatus(active, detail || "state: inactive");
  setError(state.lastError || "");
}

async function refresh() {
  const response = await askBackground({ type: "bridge_status" });
  if (!response || !response.ok) {
    setStatus(false, "state: inactive");
    setError(response && response.error ? response.error : "Failed to read bridge state.");
    return;
  }
  renderStatus(response);
}

async function saveSettings() {
  const baseUrl = normalizeBaseUrl(els.baseUrl.value);
  const token = String(els.token.value || "").trim();
  const enabled = Boolean(els.enabled.checked);

  await setStorage({ baseUrl, token, enabled });
  const response = await askBackground({ type: "bridge_config_updated" });
  if (!response || !response.ok) {
    setError(response && response.error ? response.error : "Failed to apply settings.");
    return;
  }
  renderStatus(response);
}

async function pingNow() {
  const response = await askBackground({ type: "bridge_ping" });
  if (!response || !response.ok) {
    setError(response && response.error ? response.error : "Ping failed.");
    return;
  }
  renderStatus(response);
}

els.save.addEventListener("click", () => {
  saveSettings();
});

els.ping.addEventListener("click", () => {
  pingNow();
});

refresh();
setInterval(refresh, 2500);
