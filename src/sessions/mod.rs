//! Session & Workspace Management — ported from codex-telegram-bot's
//! `services/workspace_manager.py` and `services/session_retention.py`.
//!
//! Provides:
//! - Per-(chat, user) isolated workspace directories
//! - Disk quota enforcement per workspace
//! - Session lifecycle (create, archive, prune)
//! - Message history with auto-compaction
//! - Session state persistence

use anyhow::Result;
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

// ── Constants ────────────────────────────────────────────────────

const DEFAULT_MAX_DISK_BYTES: u64 = 100 * 1024 * 1024; // 100 MB
const DEFAULT_MAX_FILE_COUNT: usize = 5000;
const DEFAULT_IDLE_ARCHIVE_HOURS: i64 = 72;
const DEFAULT_MAX_HISTORY_MESSAGES: usize = 200;

// ── Types ────────────────────────────────────────────────────────

/// Session status.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SessionStatus {
    Active,
    Idle,
    Archived,
}

/// A session record.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub session_id: String,
    pub chat_id: String,
    pub user_id: String,
    pub status: SessionStatus,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub last_message_at: Option<DateTime<Utc>>,
    pub summary: String,
    pub message_count: usize,
    pub workspace_path: Option<String>,
}

/// A message within a session.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionMessage {
    pub role: String, // "user" | "assistant" | "system"
    pub content: String,
    pub timestamp: DateTime<Utc>,
}

/// Workspace disk usage statistics.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkspaceStats {
    pub path: String,
    pub disk_bytes: u64,
    pub file_count: usize,
    pub quota_bytes: u64,
    pub quota_files: usize,
}

// ── Workspace Manager ────────────────────────────────────────────

/// Manages isolated workspace directories per session.
pub struct WorkspaceManager {
    root: PathBuf,
    max_disk_bytes: u64,
    max_file_count: usize,
}

impl WorkspaceManager {
    pub fn new(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
            max_disk_bytes: DEFAULT_MAX_DISK_BYTES,
            max_file_count: DEFAULT_MAX_FILE_COUNT,
        }
    }

    pub fn with_quotas(mut self, max_bytes: u64, max_files: usize) -> Self {
        self.max_disk_bytes = max_bytes;
        self.max_file_count = max_files;
        self
    }

    /// Create or get the workspace directory for a session.
    pub fn ensure_workspace(&self, session_id: &str) -> Result<PathBuf> {
        let path = self.root.join(session_id);
        fs::create_dir_all(&path)?;
        Ok(path)
    }

    /// Get workspace statistics.
    pub fn stats(&self, session_id: &str) -> Result<WorkspaceStats> {
        let path = self.root.join(session_id);
        if !path.exists() {
            return Ok(WorkspaceStats {
                path: path.to_string_lossy().to_string(),
                disk_bytes: 0,
                file_count: 0,
                quota_bytes: self.max_disk_bytes,
                quota_files: self.max_file_count,
            });
        }

        let (bytes, count) = dir_size(&path)?;

        Ok(WorkspaceStats {
            path: path.to_string_lossy().to_string(),
            disk_bytes: bytes,
            file_count: count,
            quota_bytes: self.max_disk_bytes,
            quota_files: self.max_file_count,
        })
    }

    /// Check if a workspace is within quota.
    pub fn check_quota(&self, session_id: &str) -> Result<bool> {
        let stats = self.stats(session_id)?;
        Ok(stats.disk_bytes <= self.max_disk_bytes && stats.file_count <= self.max_file_count)
    }

    /// Remove a workspace directory.
    pub fn remove_workspace(&self, session_id: &str) -> Result<()> {
        let path = self.root.join(session_id);
        if path.exists() {
            fs::remove_dir_all(&path)?;
        }
        Ok(())
    }

    /// List all workspace session IDs.
    pub fn list_workspaces(&self) -> Result<Vec<String>> {
        if !self.root.exists() {
            return Ok(Vec::new());
        }
        let mut ids = Vec::new();
        for entry in fs::read_dir(&self.root)? {
            let entry = entry?;
            if entry.path().is_dir() {
                if let Some(name) = entry.file_name().to_str() {
                    ids.push(name.to_string());
                }
            }
        }
        Ok(ids)
    }
}

// ── Session Store ────────────────────────────────────────────────

/// In-memory session store with persistence.
pub struct SessionStore {
    sessions: HashMap<String, Session>,
    messages: HashMap<String, Vec<SessionMessage>>,
    store_path: PathBuf,
    max_history: usize,
}

impl SessionStore {
    pub fn new(store_path: &Path) -> Self {
        let mut store = Self {
            sessions: HashMap::new(),
            messages: HashMap::new(),
            store_path: store_path.to_path_buf(),
            max_history: DEFAULT_MAX_HISTORY_MESSAGES,
        };
        // Load existing sessions if file exists
        if store_path.exists() {
            if let Ok(content) = fs::read_to_string(store_path) {
                if let Ok(data) = serde_json::from_str::<HashMap<String, Session>>(&content) {
                    store.sessions = data;
                }
            }
        }
        store
    }

    /// Create or get a session for a (chat_id, user_id) pair.
    pub fn get_or_create(&mut self, chat_id: &str, user_id: &str) -> &Session {
        let session_id = format!("{chat_id}:{user_id}");
        self.sessions.entry(session_id.clone()).or_insert_with(|| {
            let now = Utc::now();
            Session {
                session_id: session_id.clone(),
                chat_id: chat_id.to_string(),
                user_id: user_id.to_string(),
                status: SessionStatus::Active,
                created_at: now,
                updated_at: now,
                last_message_at: None,
                summary: String::new(),
                message_count: 0,
                workspace_path: None,
            }
        })
    }

    /// Add a message to a session.
    pub fn add_message(&mut self, session_id: &str, message: SessionMessage) {
        let messages = self.messages.entry(session_id.to_string()).or_default();
        messages.push(message);

        // Auto-compact if over limit
        if messages.len() > self.max_history {
            let drain_count = messages.len() - self.max_history;
            messages.drain(..drain_count);
        }

        // Update session
        if let Some(session) = self.sessions.get_mut(session_id) {
            session.last_message_at = Some(Utc::now());
            session.updated_at = Utc::now();
            session.message_count += 1;
        }
    }

    /// Get messages for a session.
    pub fn get_messages(&self, session_id: &str) -> Vec<SessionMessage> {
        self.messages.get(session_id).cloned().unwrap_or_default()
    }

    /// List all sessions.
    pub fn list_sessions(&self) -> Vec<Session> {
        let mut sessions: Vec<Session> = self.sessions.values().cloned().collect();
        sessions.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
        sessions
    }

    /// Get a session by ID.
    pub fn get_session(&self, session_id: &str) -> Option<&Session> {
        self.sessions.get(session_id)
    }

    /// Archive idle sessions (no messages for N hours).
    pub fn archive_idle_sessions(&mut self, idle_hours: Option<i64>) -> Vec<String> {
        let hours = idle_hours.unwrap_or(DEFAULT_IDLE_ARCHIVE_HOURS);
        let cutoff = Utc::now() - Duration::hours(hours);
        let mut archived = Vec::new();

        for session in self.sessions.values_mut() {
            if session.status == SessionStatus::Active {
                let last_activity = session.last_message_at.unwrap_or(session.created_at);
                if last_activity < cutoff {
                    session.status = SessionStatus::Archived;
                    session.updated_at = Utc::now();
                    archived.push(session.session_id.clone());
                }
            }
        }

        archived
    }

    /// Persist sessions to disk.
    pub fn persist(&self) -> Result<()> {
        if let Some(parent) = self.store_path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(
            &self.store_path,
            serde_json::to_string_pretty(&self.sessions)?,
        )?;
        Ok(())
    }
}

// ── Helpers ──────────────────────────────────────────────────────

/// Calculate total size and file count of a directory.
fn dir_size(path: &Path) -> Result<(u64, usize)> {
    let mut total_bytes: u64 = 0;
    let mut file_count: usize = 0;

    if !path.exists() {
        return Ok((0, 0));
    }

    for entry in walkdir(path)? {
        let metadata = entry.metadata()?;
        if metadata.is_file() {
            total_bytes += metadata.len();
            file_count += 1;
        }
    }

    Ok((total_bytes, file_count))
}

/// Simple recursive directory walker.
fn walkdir(path: &Path) -> Result<Vec<fs::DirEntry>> {
    let mut entries = Vec::new();
    if !path.is_dir() {
        return Ok(entries);
    }
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        entries.push(entry);
        let path = entries.last().unwrap().path();
        if path.is_dir() {
            entries.extend(walkdir(&path)?);
        }
    }
    Ok(entries)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_workspace_manager() {
        let tmp = TempDir::new().unwrap();
        let mgr = WorkspaceManager::new(tmp.path());

        let path = mgr.ensure_workspace("test-session").unwrap();
        assert!(path.exists());

        let stats = mgr.stats("test-session").unwrap();
        assert_eq!(stats.disk_bytes, 0);

        assert!(mgr.check_quota("test-session").unwrap());

        mgr.remove_workspace("test-session").unwrap();
        assert!(!path.exists());
    }

    #[test]
    fn test_session_store() {
        let tmp = TempDir::new().unwrap();
        let store_path = tmp.path().join("sessions.json");
        let mut store = SessionStore::new(&store_path);

        let session = store.get_or_create("chat1", "user1");
        assert_eq!(session.status, SessionStatus::Active);

        store.add_message(
            "chat1:user1",
            SessionMessage {
                role: "user".into(),
                content: "Hello".into(),
                timestamp: Utc::now(),
            },
        );

        let messages = store.get_messages("chat1:user1");
        assert_eq!(messages.len(), 1);

        store.persist().unwrap();
        assert!(store_path.exists());
    }
}
