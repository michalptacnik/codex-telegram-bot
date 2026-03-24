//! Browser Extension Bridge Tool.
//!
//! Routes browser actions through the Chrome extension bridge instead of a
//! headless browser or CLI. Commands are enqueued into the in-process
//! BrowserBridge, picked up by the extension's heartbeat poll, executed in
//! the user's real Chrome session, and results returned to the agent.
//!
//! This is the tool that makes "post Hello World on X" work seamlessly —
//! the agent calls `browser_ext_click`, `browser_ext_type`, etc. and the
//! extension carries out the action in the live tab.

use super::traits::{Tool, ToolResult};
use crate::browser_bridge::BrowserBridge;
use async_trait::async_trait;
use chrono::Utc;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

const DEFAULT_TIMEOUT_MS: u64 = 30_000;

pub struct BrowserBridgeTool {
    bridge: Arc<BrowserBridge>,
    timeout_ms: u64,
    last_successful_client_id: Mutex<Option<String>>,
    last_successful_tab_id_by_client: Mutex<HashMap<String, i64>>,
}

impl BrowserBridgeTool {
    pub fn new(bridge: Arc<BrowserBridge>) -> Self {
        Self {
            bridge,
            timeout_ms: DEFAULT_TIMEOUT_MS,
            last_successful_client_id: Mutex::new(None),
            last_successful_tab_id_by_client: Mutex::new(HashMap::new()),
        }
    }

    fn active_client_ids_supporting(&self, required_command: &str) -> Vec<String> {
        self.bridge
            .active_clients()
            .into_iter()
            .filter(|client| {
                self.bridge
                    .supported_commands_for(&client.instance_id)
                    .iter()
                    .any(|cmd| cmd == required_command)
            })
            .map(|client| client.instance_id)
            .collect()
    }

    fn target_host(preferred_url: Option<&str>) -> Option<String> {
        let url = preferred_url?.trim().to_ascii_lowercase();
        let host = url
            .split("//")
            .nth(1)
            .unwrap_or(&url)
            .split('/')
            .next()
            .unwrap_or("")
            .trim();
        if host.is_empty() {
            None
        } else {
            Some(host.to_string())
        }
    }

    fn candidate_client_ids(
        &self,
        preferred_browser: Option<&str>,
        explicit_client_id: Option<&str>,
        required_command: &str,
        preferred_url: Option<&str>,
    ) -> Vec<String> {
        if let Some(client_id) = explicit_client_id
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            return self
                .bridge
                .active_clients()
                .into_iter()
                .find(|client| client.instance_id == client_id)
                .map(|client| vec![client.instance_id])
                .unwrap_or_default();
        }

        let now = Utc::now();
        let active_clients = self.bridge.active_clients();
        let fresh_clients: Vec<_> = active_clients
            .into_iter()
            .filter(|client| (now - client.last_seen_at).num_seconds() <= 20)
            .collect();
        let recency_filtered = if fresh_clients.is_empty() {
            self.bridge.active_clients()
        } else {
            fresh_clients
        };

        let filtered_clients: Vec<_> = recency_filtered
            .into_iter()
            .filter(|client| {
                let url = client.active_tab_url.trim().to_ascii_lowercase();
                !url.starts_with("chrome://")
                    && !url.starts_with("edge://")
                    && !url.starts_with("about:")
                    && !url.starts_with("extension://")
            })
            .collect();
        let mut selectable_clients = if filtered_clients.is_empty() {
            self.bridge.active_clients()
        } else {
            filtered_clients
        };

        let supported_ids = self.active_client_ids_supporting(required_command);
        selectable_clients
            .retain(|client| supported_ids.iter().any(|id| id == &client.instance_id));

        let sticky_client_id = self
            .last_successful_client_id
            .lock()
            .ok()
            .and_then(|guard| guard.clone());
        let preferred_browser = preferred_browser
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(|value| value.to_ascii_lowercase());
        let target_host = Self::target_host(preferred_url);

        if let Some(target_host) = target_host.as_ref() {
            let host_matching: Vec<_> = selectable_clients
                .iter()
                .filter(|client| {
                    client
                        .active_tab_url
                        .trim()
                        .to_ascii_lowercase()
                        .contains(target_host)
                })
                .cloned()
                .collect();
            if !host_matching.is_empty() {
                selectable_clients = host_matching;
            }
        }

        selectable_clients.sort_by_key(|client| {
            let mut score = 0i32;

            if sticky_client_id.as_deref() == Some(client.instance_id.as_str()) {
                score -= 100;
            }

            if let Some(preferred_browser) = preferred_browser.as_ref() {
                let haystacks = [
                    client.label.to_ascii_lowercase(),
                    client.user_agent.to_ascii_lowercase(),
                    client.platform.to_ascii_lowercase(),
                ];
                if haystacks
                    .iter()
                    .any(|value| value.contains(preferred_browser))
                {
                    score -= 20;
                }
            }

            if let Some(target_host) = target_host.as_ref() {
                if client
                    .active_tab_url
                    .trim()
                    .to_ascii_lowercase()
                    .contains(target_host)
                {
                    score -= 100;
                }
            }

            score
        });

        let mut ordered: Vec<String> = selectable_clients
            .into_iter()
            .map(|client| client.instance_id)
            .collect();

        if ordered.is_empty() {
            return self
                .bridge
                .pick_client(Some(required_command))
                .or_else(|| self.bridge.pick_client(None))
                .map(|client| vec![client.instance_id])
                .unwrap_or_default();
        }

        ordered.dedup();
        ordered
    }

    fn remember_successful_client(&self, client_id: &str) {
        if let Ok(mut guard) = self.last_successful_client_id.lock() {
            *guard = Some(client_id.to_string());
        }
    }

    fn remembered_tab_id_for_client(&self, client_id: &str) -> Option<i64> {
        self.last_successful_tab_id_by_client
            .lock()
            .ok()
            .and_then(|guard| guard.get(client_id).copied())
    }

    fn remember_successful_tab_id(
        &self,
        client_id: &str,
        cmd: &crate::browser_bridge::BrowserCommand,
    ) {
        let tab_id = cmd.data.get("tab_id").and_then(Value::as_i64).or_else(|| {
            cmd.data
                .get("result")
                .and_then(|value| value.get("tab_id"))
                .and_then(Value::as_i64)
        });

        if let Some(tab_id) = tab_id {
            if let Ok(mut guard) = self.last_successful_tab_id_by_client.lock() {
                guard.insert(client_id.to_string(), tab_id);
            }
        }
    }

    fn should_retry_with_another_client(
        command_type: &str,
        cmd: &crate::browser_bridge::BrowserCommand,
    ) -> bool {
        if cmd.ok.unwrap_or(false) {
            return false;
        }

        let output = cmd.output.trim().to_ascii_lowercase();
        (command_type == "open_url" || command_type == "navigate_url")
            && output.contains("blocked by embedder")
    }

    fn resolve_selector(&self, selector: &str) -> anyhow::Result<String> {
        let trimmed = selector.trim();
        if let Some(ref_id) = trimmed.strip_prefix('@') {
            let ref_id = ref_id.trim_start_matches('e');
            let selector = self
                .bridge
                .get_snapshot_ref_map()
                .get(ref_id)
                .cloned()
                .ok_or_else(|| anyhow::anyhow!("Unknown snapshot ref '@{ref_id}'"))?;
            return Ok(selector);
        }
        if trimmed.chars().all(|ch| ch.is_ascii_digit()) {
            let selector = self
                .bridge
                .get_snapshot_ref_map()
                .get(trimmed)
                .cloned()
                .ok_or_else(|| anyhow::anyhow!("Unknown snapshot ref '{trimmed}'"))?;
            return Ok(selector);
        }
        Ok(trimmed.to_string())
    }

    fn to_tool_result(&self, cmd: crate::browser_bridge::BrowserCommand) -> ToolResult {
        let success = cmd.ok.unwrap_or(false);
        if success {
            let output = if cmd.data.is_null() {
                if cmd.output.is_empty() {
                    serde_json::to_string_pretty(&json!({ "ok": true })).unwrap_or_default()
                } else {
                    serde_json::to_string_pretty(&json!({ "ok": true, "output": cmd.output }))
                        .unwrap_or_default()
                }
            } else {
                serde_json::to_string_pretty(&cmd.data).unwrap_or_default()
            };
            return ToolResult {
                success: true,
                output,
                error: None,
                metadata: None,
            };
        }

        ToolResult {
            success: false,
            output: String::new(),
            error: Some(if cmd.output.trim().is_empty() {
                format!("Browser command '{}' failed", cmd.command_type)
            } else {
                cmd.output
            }),
            metadata: None,
        }
    }

    async fn dispatch(
        &self,
        command_type: &str,
        payload: Value,
        preferred_browser: Option<&str>,
        explicit_client_id: Option<&str>,
    ) -> anyhow::Result<ToolResult> {
        let preferred_url = payload.get("url").and_then(Value::as_str);
        let candidate_client_ids = self.candidate_client_ids(
            preferred_browser,
            explicit_client_id,
            command_type,
            preferred_url,
        );
        if candidate_client_ids.is_empty() {
            anyhow::bail!(
                "No active browser extension client connected. Install the AgentHQ browser extension and make sure it shows ON (green badge)."
            );
        }

        let mut last_command = None;
        for client_id in candidate_client_ids {
            let mut payload_for_client = payload.clone();
            if payload_for_client.get("tab_id").is_none() && command_type != "open_url" {
                if let Some(tab_id) = self.remembered_tab_id_for_client(&client_id) {
                    if let Some(obj) = payload_for_client.as_object_mut() {
                        obj.insert("tab_id".into(), Value::Number(tab_id.into()));
                    }
                }
            }

            let command_id =
                self.bridge
                    .enqueue_command(&client_id, command_type, payload_for_client);

            let cmd = self
                .bridge
                .wait_for_command(&command_id, self.timeout_ms)
                .await
                .ok_or_else(|| {
                    anyhow::anyhow!(
                        "Browser command '{command_type}' timed out after {}s. The extension may be busy or disconnected.",
                        self.timeout_ms / 1000
                    )
                })?;

            if cmd.ok.unwrap_or(false) {
                self.remember_successful_client(&client_id);
                self.remember_successful_tab_id(&client_id, &cmd);
                if command_type == "snapshot" {
                    if let Some(ref_map) = cmd
                        .data
                        .get("result")
                        .and_then(|value| value.get("ref_map"))
                        .and_then(Value::as_object)
                    {
                        self.bridge.set_snapshot_ref_map(
                            ref_map
                                .iter()
                                .filter_map(|(key, value)| {
                                    value
                                        .as_str()
                                        .map(|selector| (key.clone(), selector.to_string()))
                                })
                                .collect(),
                        );
                    }
                }
                return Ok(self.to_tool_result(cmd));
            }

            let retryable = Self::should_retry_with_another_client(command_type, &cmd);
            last_command = Some(cmd);
            if !retryable {
                break;
            }
        }

        Ok(self.to_tool_result(last_command.expect("candidate_client_ids was not empty")))
    }
}

#[async_trait]
impl Tool for BrowserBridgeTool {
    fn name(&self) -> &str {
        "browser_ext"
    }

    fn description(&self) -> &str {
        "Control a live browser session via the installed AgentHQ extension client. \
         Supports navigating to URLs, taking snapshots of the page, clicking elements, \
         typing/filling text, scrolling, pressing keys, and extracting text. \
         Prefer this over `browser` for posting on social media, commenting, filling web forms, and any \
         interaction with a real website in the user's logged-in browser session. \
         When multiple browser clients are connected, pass `browser` or `client_id` to pick the right one. \
         The extension must be installed and show a green ON badge."
    }

    fn parameters_schema(&self) -> Value {
        json!({
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action to perform",
                    "enum": [
                        "open_url",
                        "navigate_url",
                        "snapshot",
                        "screenshot",
                        "click",
                        "fill",
                        "type",
                        "hover",
                        "press",
                        "scroll",
                        "select",
                        "wait",
                        "get_text",
                        "run_script"
                    ]
                },
                "url": {
                    "type": "string",
                    "description": "URL for open_url or navigate_url"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector or snapshot ref (@e1 or 1) for click/fill/type/hover/get_text/wait/select"
                },
                "value": {
                    "type": "string",
                    "description": "Text value for fill or select"
                },
                "text": {
                    "type": "string",
                    "description": "Text for type action or text to wait for"
                },
                "replace": {
                    "type": "boolean",
                    "description": "When true, clear existing text in the target before typing. Use this for exact social-media draft text instead of append behavior."
                },
                "key": {
                    "type": "string",
                    "description": "Key name for press (e.g. Enter, Tab, Escape, ArrowDown)"
                },
                "direction": {
                    "type": "string",
                    "description": "Scroll direction: up, down, left, right",
                    "enum": ["up", "down", "left", "right"]
                },
                "pixels": {
                    "type": "integer",
                    "description": "Pixels to scroll (default 400)"
                },
                "script": {
                    "type": "string",
                    "description": "JavaScript to run for run_script action"
                },
                "ms": {
                    "type": "integer",
                    "description": "Milliseconds to wait for the wait action"
                },
                "new_tab": {
                    "type": "boolean",
                    "description": "Open URL in a new tab (default true for open_url)"
                },
                "tab_id": {
                    "type": "integer",
                    "description": "Specific tab ID to target (defaults to active tab)"
                },
                "browser": {
                    "type": "string",
                    "description": "Preferred browser/client label to use when multiple extension clients are connected (for example: Google Chrome)"
                },
                "client_id": {
                    "type": "string",
                    "description": "Exact extension client instance ID to target"
                }
            },
            "required": ["action"]
        })
    }

    async fn execute(&self, args: Value) -> anyhow::Result<ToolResult> {
        let action = args
            .get("action")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let preferred_browser = args.get("browser").and_then(|v| v.as_str());
        let explicit_client_id = args.get("client_id").and_then(|v| v.as_str());

        // Build payload from args (minus "action" key which is our dispatch field)
        let mut payload = args.clone();
        if let Some(obj) = payload.as_object_mut() {
            obj.remove("action");
            obj.remove("browser");
            obj.remove("client_id");
            if let Some(selector) = obj.get("selector").and_then(|value| value.as_str()) {
                obj.insert(
                    "selector".into(),
                    Value::String(self.resolve_selector(selector)?),
                );
            }
        }

        match action.as_str() {
            "open_url" | "navigate_url" | "run_script" | "snapshot" | "screenshot" => {
                self.dispatch(&action, payload, preferred_browser, explicit_client_id)
                    .await
            }
            "click" | "fill" | "type" | "hover" | "press" | "scroll" | "select" | "wait"
            | "get_text" => {
                self.dispatch(&action, payload, preferred_browser, explicit_client_id)
                    .await
            }
            "" => anyhow::bail!("Missing required field: action"),
            other => anyhow::bail!(
                "Unknown action '{}'. Use one of: open_url, navigate_url, snapshot, screenshot, click, fill, type, hover, press, scroll, select, wait, get_text, run_script",
                other
            ),
        }
    }
}
