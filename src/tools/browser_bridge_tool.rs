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
use serde_json::{json, Value};
use std::sync::Arc;

const DEFAULT_TIMEOUT_MS: u64 = 30_000;

pub struct BrowserBridgeTool {
    bridge: Arc<BrowserBridge>,
    timeout_ms: u64,
}

impl BrowserBridgeTool {
    pub fn new(bridge: Arc<BrowserBridge>) -> Self {
        Self {
            bridge,
            timeout_ms: DEFAULT_TIMEOUT_MS,
        }
    }

    fn pick_client(
        &self,
        preferred_browser: Option<&str>,
        explicit_client_id: Option<&str>,
        required_command: &str,
    ) -> Option<String> {
        if let Some(client_id) = explicit_client_id.map(str::trim).filter(|value| !value.is_empty()) {
            return self
                .bridge
                .active_clients()
                .into_iter()
                .find(|client| client.instance_id == client_id)
                .map(|client| client.instance_id);
        }

        let preferred_browser = preferred_browser
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(|value| value.to_ascii_lowercase());

        if let Some(preferred_browser) = preferred_browser {
            let supported = self
                .bridge
                .active_clients()
                .into_iter()
                .find(|client| {
                    let haystacks = [
                        client.label.to_ascii_lowercase(),
                        client.user_agent.to_ascii_lowercase(),
                        client.platform.to_ascii_lowercase(),
                    ];
                    haystacks.iter().any(|value| value.contains(&preferred_browser))
                        && self
                            .bridge
                            .supported_commands_for(&client.instance_id)
                            .iter()
                            .any(|cmd| cmd == required_command)
                })
                .map(|client| client.instance_id);
            if supported.is_some() {
                return supported;
            }
        }

        self.bridge
            .pick_client(Some(required_command))
            .or_else(|| self.bridge.pick_client(None))
            .map(|client| client.instance_id)
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
        let client_id = self
            .pick_client(preferred_browser, explicit_client_id, command_type)
            .ok_or_else(|| {
            anyhow::anyhow!(
                "No active browser extension client connected. Install the AgentHQ browser extension and make sure it shows ON (green badge)."
            )
        })?;

        let command_id = self
            .bridge
            .enqueue_command(&client_id, command_type, payload);

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

        if command_type == "snapshot" && cmd.ok.unwrap_or(false) {
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
                            value.as_str().map(|selector| (key.clone(), selector.to_string()))
                        })
                        .collect(),
                );
            }
        }

        Ok(self.to_tool_result(cmd))
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
         Use this for posting on social media, commenting, filling web forms, and any \
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
