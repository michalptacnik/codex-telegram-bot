//! Browser Bridge — ported from codex-telegram-bot's `services/browser_bridge.py`.
//!
//! Coordinates communication between the agent and a Chrome extension client.
//! The extension periodically heartbeats and polls pending commands.
//! The agent enqueues commands (browser_open, browser_navigate, browser_snapshot)
//! and can wait for completion.
//!
//! Integrates with ZeroClaw's gateway (axum) for the WebSocket/HTTP bridge.

use chrono::{DateTime, Utc};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{Arc, OnceLock};
use tracing::debug;
use uuid::Uuid;

// ── Global singleton ─────────────────────────────────────────────
//
// A single BrowserBridge instance is shared across the gateway and all channel
// loops so that the Chrome extension (which connects to the gateway's HTTP
// endpoints) and standalone channel agents (Telegram, Discord, Slack…) all
// enqueue into and read from the same command queue.

static GLOBAL_BRIDGE: OnceLock<Arc<BrowserBridge>> = OnceLock::new();

// ── Constants ────────────────────────────────────────────────────

const DEFAULT_HEARTBEAT_TTL_SEC: i64 = 90;
const DEFAULT_COMMAND_RETENTION_SEC: i64 = 900;
const DEFAULT_DISPATCH_LEASE_SEC: i64 = 30;

// ── Types ────────────────────────────────────────────────────────

/// Represents a connected Chrome extension client.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserClient {
    pub instance_id: String,
    pub label: String,
    pub version: String,
    pub platform: String,
    pub user_agent: String,
    pub created_at: DateTime<Utc>,
    pub last_seen_at: DateTime<Utc>,
    pub active_tab_url: String,
    pub active_tab_title: String,
}

/// A command queued for dispatch to a browser extension client.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserCommand {
    pub command_id: String,
    pub client_id: String,
    pub command_type: String,
    pub payload: serde_json::Value,
    pub created_at: DateTime<Utc>,
    pub status: CommandStatus,
    pub dispatched_at: Option<DateTime<Utc>>,
    pub dispatch_count: u32,
    pub completed_at: Option<DateTime<Utc>>,
    pub ok: Option<bool>,
    pub output: String,
    pub data: serde_json::Value,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CommandStatus {
    Queued,
    Dispatched,
    Completed,
    Failed,
}

/// Heartbeat message sent by the Chrome extension.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HeartbeatMessage {
    pub instance_id: String,
    pub label: Option<String>,
    pub version: Option<String>,
    pub platform: Option<String>,
    pub user_agent: Option<String>,
    pub active_tab_url: Option<String>,
    pub active_tab_title: Option<String>,
    pub supported_commands: Option<Vec<String>>,
    pub extension_version: Option<String>,
}

/// Command completion report from the Chrome extension.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandResult {
    pub command_id: String,
    pub ok: bool,
    pub output: Option<String>,
    pub data: Option<serde_json::Value>,
}

/// Status of the browser bridge.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BridgeStatus {
    pub clients: Vec<BrowserClient>,
    pub active_clients: usize,
    pub pending_commands: usize,
    pub completed_commands: usize,
}

// ── Browser Bridge ───────────────────────────────────────────────

/// In-memory bridge between agent tools and Chrome extension clients.
pub struct BrowserBridge {
    heartbeat_ttl_sec: i64,
    command_retention_sec: i64,
    dispatch_lease_sec: i64,
    clients: Arc<Mutex<HashMap<String, BrowserClient>>>,
    commands: Arc<Mutex<HashMap<String, BrowserCommand>>>,
    queue_by_client: Arc<Mutex<HashMap<String, Vec<String>>>>,
    snapshot_ref_map: Arc<Mutex<HashMap<String, String>>>,
    supported_commands: Arc<Mutex<HashMap<String, Vec<String>>>>,
}

static GLOBAL_BROWSER_BRIDGE: OnceLock<Arc<BrowserBridge>> = OnceLock::new();

impl BrowserBridge {
    pub fn new() -> Self {
        Self {
            heartbeat_ttl_sec: DEFAULT_HEARTBEAT_TTL_SEC,
            command_retention_sec: DEFAULT_COMMAND_RETENTION_SEC,
            dispatch_lease_sec: DEFAULT_DISPATCH_LEASE_SEC,
            clients: Arc::new(Mutex::new(HashMap::new())),
            commands: Arc::new(Mutex::new(HashMap::new())),
            queue_by_client: Arc::new(Mutex::new(HashMap::new())),
            snapshot_ref_map: Arc::new(Mutex::new(HashMap::new())),
            supported_commands: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Return the process-global shared bridge instance.
    ///
    /// Both the gateway (which hosts the extension HTTP endpoints) and the
    /// channel loops (Telegram, Discord, Slack…) call this so they all share
    /// one command queue.  The Chrome extension connects to the gateway; its
    /// commands flow into this singleton and are visible to every agent.
    pub fn global() -> Arc<Self> {
        GLOBAL_BRIDGE
            .get_or_init(|| Arc::new(Self::new()))
            .clone()
    }

    /// Process a heartbeat from a Chrome extension client.
    pub fn heartbeat(&self, msg: HeartbeatMessage) -> String {
        let now = Utc::now();
        let instance_id = msg.instance_id.clone();

        let mut clients = self.clients.lock();
        let client = clients
            .entry(instance_id.clone())
            .or_insert_with(|| BrowserClient {
                instance_id: instance_id.clone(),
                label: msg.label.clone().unwrap_or_default(),
                version: msg.version.clone().unwrap_or_default(),
                platform: msg.platform.clone().unwrap_or_default(),
                user_agent: msg.user_agent.clone().unwrap_or_default(),
                created_at: now,
                last_seen_at: now,
                active_tab_url: String::new(),
                active_tab_title: String::new(),
            });

        client.last_seen_at = now;
        if let Some(url) = &msg.active_tab_url {
            client.active_tab_url = url.clone();
        }
        if let Some(title) = &msg.active_tab_title {
            client.active_tab_title = title.clone();
        }
        if let Some(version) = &msg.version {
            client.version = version.clone();
        }

        // Update supported commands
        if let Some(cmds) = msg.supported_commands {
            self.supported_commands
                .lock()
                .insert(instance_id.clone(), cmds);
        }
        let stored_clients = clients.len();
        drop(clients);
        debug!(
            instance_id = %instance_id,
            active_clients = self.active_clients().len(),
            stored_clients,
            "browser bridge heartbeat recorded"
        );

        instance_id
    }

    /// Enqueue a command for dispatch to a browser client.
    pub fn enqueue_command(
        &self,
        client_id: &str,
        command_type: &str,
        payload: serde_json::Value,
    ) -> String {
        let command_id = Uuid::new_v4().to_string();
        let now = Utc::now();

        let cmd = BrowserCommand {
            command_id: command_id.clone(),
            client_id: client_id.to_string(),
            command_type: command_type.to_string(),
            payload,
            created_at: now,
            status: CommandStatus::Queued,
            dispatched_at: None,
            dispatch_count: 0,
            completed_at: None,
            ok: None,
            output: String::new(),
            data: serde_json::Value::Null,
        };

        self.commands.lock().insert(command_id.clone(), cmd);
        self.queue_by_client
            .lock()
            .entry(client_id.to_string())
            .or_default()
            .push(command_id.clone());

        command_id
    }

    /// Poll for pending commands for a client (called by the extension).
    pub fn poll_commands(&self, client_id: &str) -> Vec<BrowserCommand> {
        let now = Utc::now();
        let mut result = Vec::new();

        let queue = self.queue_by_client.lock();
        let command_ids = match queue.get(client_id) {
            Some(ids) => ids.clone(),
            None => return result,
        };
        drop(queue);

        let mut commands = self.commands.lock();
        for cmd_id in &command_ids {
            if let Some(cmd) = commands.get_mut(cmd_id) {
                if cmd.status == CommandStatus::Queued {
                    cmd.status = CommandStatus::Dispatched;
                    cmd.dispatched_at = Some(now);
                    cmd.dispatch_count += 1;
                    result.push(cmd.clone());
                }
            }
        }

        result
    }

    /// Report command completion from the extension.
    pub fn complete_command(&self, result: CommandResult) {
        let now = Utc::now();
        let mut commands = self.commands.lock();
        if let Some(cmd) = commands.get_mut(&result.command_id) {
            cmd.status = if result.ok {
                CommandStatus::Completed
            } else {
                CommandStatus::Failed
            };
            cmd.completed_at = Some(now);
            cmd.ok = Some(result.ok);
            if let Some(output) = result.output {
                cmd.output = output;
            }
            if let Some(data) = result.data {
                cmd.data = data;
            }
        }
    }

    /// Get the status of a command.
    pub fn get_command(&self, command_id: &str) -> Option<BrowserCommand> {
        self.commands.lock().get(command_id).cloned()
    }

    /// Wait for a command to complete (with timeout).
    pub async fn wait_for_command(
        &self,
        command_id: &str,
        timeout_ms: u64,
    ) -> Option<BrowserCommand> {
        let deadline = tokio::time::Instant::now() + tokio::time::Duration::from_millis(timeout_ms);

        loop {
            if let Some(cmd) = self.get_command(command_id) {
                if cmd.status == CommandStatus::Completed || cmd.status == CommandStatus::Failed {
                    return Some(cmd);
                }
            }

            if tokio::time::Instant::now() >= deadline {
                return None;
            }

            tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
        }
    }

    /// Get active clients (those that heartbeated within TTL).
    pub fn active_clients(&self) -> Vec<BrowserClient> {
        let now = Utc::now();
        let clients = self.clients.lock();
        let mut active: Vec<_> = clients
            .values()
            .filter(|c| (now - c.last_seen_at).num_seconds() < self.heartbeat_ttl_sec)
            .cloned()
            .collect();
        active.sort_by(|a, b| b.last_seen_at.cmp(&a.last_seen_at));
        active
    }

    /// Pick the most recently active client, optionally requiring support for a command.
    pub fn pick_client(&self, required_command: Option<&str>) -> Option<BrowserClient> {
        let want = required_command.map(str::trim).filter(|s| !s.is_empty());
        let supported = self.supported_commands.lock();

        self.active_clients().into_iter().find(|client| {
            let Some(command) = want else {
                return true;
            };
            supported
                .get(&client.instance_id)
                .is_some_and(|cmds| cmds.iter().any(|item| item == command))
        })
    }

    /// Return the supported command list for a given client.
    pub fn supported_commands_for(&self, client_id: &str) -> Vec<String> {
        self.supported_commands
            .lock()
            .get(client_id)
            .cloned()
            .unwrap_or_default()
    }

    /// Check if any active extension supports a given command type.
    pub fn supports_command(&self, command: &str) -> bool {
        let active = self.active_clients();
        let supported = self.supported_commands.lock();
        for client in &active {
            if let Some(cmds) = supported.get(&client.instance_id) {
                if cmds.iter().any(|c| c == command) {
                    return true;
                }
            }
        }
        false
    }

    /// Get bridge status summary.
    pub fn status(&self) -> BridgeStatus {
        let active = self.active_clients();
        let commands = self.commands.lock();
        let pending = commands
            .values()
            .filter(|c| c.status == CommandStatus::Queued || c.status == CommandStatus::Dispatched)
            .count();
        let completed = commands
            .values()
            .filter(|c| c.status == CommandStatus::Completed)
            .count();

        let status = BridgeStatus {
            active_clients: active.len(),
            clients: active,
            pending_commands: pending,
            completed_commands: completed,
        };
        debug!(
            active_clients = status.active_clients,
            pending_commands = status.pending_commands,
            completed_commands = status.completed_commands,
            "browser bridge status requested"
        );
        status
    }

    /// Set the snapshot ref map (CSS selector mappings from last DOM snapshot).
    pub fn set_snapshot_ref_map(&self, ref_map: HashMap<String, String>) {
        *self.snapshot_ref_map.lock() = ref_map;
    }

    /// Get the snapshot ref map.
    pub fn get_snapshot_ref_map(&self) -> HashMap<String, String> {
        self.snapshot_ref_map.lock().clone()
    }

    /// Garbage-collect expired clients and old commands.
    pub fn gc(&self) {
        let now = Utc::now();

        // Remove expired clients
        self.clients
            .lock()
            .retain(|_, c| (now - c.last_seen_at).num_seconds() < self.heartbeat_ttl_sec * 3);

        // Remove old completed/failed commands
        self.commands
            .lock()
            .retain(|_, c| (now - c.created_at).num_seconds() < self.command_retention_sec);

        // Clean up empty queues
        self.queue_by_client.lock().retain(|_, q| !q.is_empty());
    }
}

impl Default for BrowserBridge {
    fn default() -> Self {
        Self::new()
    }
}

/// Return the process-global browser bridge instance.
pub fn global_bridge() -> Arc<BrowserBridge> {
    Arc::clone(GLOBAL_BROWSER_BRIDGE.get_or_init(|| Arc::new(BrowserBridge::new())))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_heartbeat_and_enqueue() {
        let bridge = BrowserBridge::new();

        // Heartbeat
        let id = bridge.heartbeat(HeartbeatMessage {
            instance_id: "ext-1".into(),
            label: Some("Test Chrome".into()),
            version: Some("1.0".into()),
            platform: Some("linux".into()),
            user_agent: Some("Chrome/120".into()),
            active_tab_url: Some("https://example.com".into()),
            active_tab_title: Some("Example".into()),
            supported_commands: Some(vec!["browser_open".into(), "browser_navigate".into()]),
            extension_version: Some("1.0.0".into()),
        });
        assert_eq!(id, "ext-1");
        assert_eq!(bridge.active_clients().len(), 1);

        // Enqueue command
        let cmd_id = bridge.enqueue_command(
            "ext-1",
            "browser_open",
            serde_json::json!({"url": "https://test.com"}),
        );
        assert!(!cmd_id.is_empty());

        // Poll
        let cmds = bridge.poll_commands("ext-1");
        assert_eq!(cmds.len(), 1);
        assert_eq!(cmds[0].command_type, "browser_open");

        // Complete
        bridge.complete_command(CommandResult {
            command_id: cmd_id.clone(),
            ok: true,
            output: Some("Opened".into()),
            data: None,
        });

        let cmd = bridge.get_command(&cmd_id).unwrap();
        assert_eq!(cmd.status, CommandStatus::Completed);
        assert!(cmd.ok.unwrap());
    }
}
