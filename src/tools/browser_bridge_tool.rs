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

    fn pick_client(&self) -> Option<String> {
        // Sort by created_at ascending so the oldest registered client (Chrome)
        // is always preferred over newer ones (e.g. Atlas/OpenAI sidecars).
        // HashMap iteration order is non-deterministic, so without sorting
        // commands could silently land on the wrong browser.
        self.bridge
            .active_clients()
            .into_iter()
            .min_by_key(|c| c.created_at)
            .map(|c| c.instance_id)
    }

    async fn dispatch(&self, command_type: &str, payload: Value) -> anyhow::Result<ToolResult> {
        let clients = self.bridge.active_clients();
        tracing::info!(command = %command_type, connected_clients = %clients.len(), "browser_ext dispatch");
        let client_id = self.pick_client().ok_or_else(|| {
            tracing::warn!("browser_ext: no connected extension clients");
            anyhow::anyhow!(
                "No active Chrome extension connected. Install the AgentHQ Chrome extension and make sure it shows ON (green badge)."
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

        let success = cmd.ok.unwrap_or(false);
        let output = if cmd.output.is_empty() {
            if success {
                format!("{command_type} succeeded")
            } else {
                format!("{command_type} failed")
            }
        } else {
            cmd.output.clone()
        };

        Ok(ToolResult {
            success,
            output,
            error: if success { None } else { Some(cmd.output) },
            metadata: None,
        })
    }
}

#[async_trait]
impl Tool for BrowserBridgeTool {
    fn name(&self) -> &str {
        "browser_ext"
    }

    fn description(&self) -> &str {
        "Control the live Chrome browser via the installed AgentHQ extension. \
         Supports navigating to URLs, taking snapshots of the page, clicking elements, \
         typing/filling text, scrolling, pressing keys, and extracting text. \
         Use this for posting on social media, commenting, filling web forms, and any \
         interaction with a real website in the user's browser session. \
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
                    "description": "CSS selector or snapshot ref number for click/fill/type/hover/get_text/wait/select"
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

        // Build payload from args (minus "action" key which is our dispatch field)
        let mut payload = args.clone();
        if let Some(obj) = payload.as_object_mut() {
            obj.remove("action");
        }

        match action.as_str() {
            "open_url" | "navigate_url" | "run_script" | "snapshot" | "screenshot" => {
                self.dispatch(&action, payload).await
            }
            "click" | "fill" | "type" | "hover" | "press" | "scroll" | "select" | "wait"
            | "get_text" => self.dispatch(&action, payload).await,
            "" => anyhow::bail!("Missing required field: action"),
            other => anyhow::bail!(
                "Unknown action '{}'. Use one of: open_url, navigate_url, snapshot, screenshot, click, fill, type, hover, press, scroll, select, wait, get_text, run_script",
                other
            ),
        }
    }
}
