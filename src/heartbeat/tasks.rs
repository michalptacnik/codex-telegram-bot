use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use uuid::Uuid;

const HEARTBEAT_TASKS_FILE: &str = "state/heartbeat_tasks.json";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManagedHeartbeatTask {
    pub id: String,
    #[serde(default)]
    pub name: Option<String>,
    pub prompt: String,
    pub owner_agent_id: Option<String>,
    pub enabled: bool,
    pub created_at: DateTime<Utc>,
    pub last_run: Option<DateTime<Utc>>,
    pub last_status: Option<String>,
    pub last_output: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ManagedHeartbeatTaskPatch {
    pub name: Option<String>,
    pub prompt: Option<String>,
    pub owner_agent_id: Option<Option<String>>,
    pub enabled: Option<bool>,
}

fn tasks_path(workspace_dir: &Path) -> PathBuf {
    workspace_dir.join(HEARTBEAT_TASKS_FILE)
}

pub fn list_tasks(workspace_dir: &Path) -> Result<Vec<ManagedHeartbeatTask>> {
    let path = tasks_path(workspace_dir);
    if !path.exists() {
        return Ok(Vec::new());
    }
    let raw = std::fs::read_to_string(&path)
        .with_context(|| format!("Failed to read {}", path.display()))?;
    let mut tasks: Vec<ManagedHeartbeatTask> = serde_json::from_str(&raw)
        .with_context(|| format!("Failed to parse {}", path.display()))?;
    tasks.sort_by(|a, b| a.prompt.cmp(&b.prompt));
    Ok(tasks)
}

fn write_tasks(workspace_dir: &Path, tasks: &[ManagedHeartbeatTask]) -> Result<()> {
    let path = tasks_path(workspace_dir);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("Failed to create {}", parent.display()))?;
    }
    let payload = serde_json::to_string_pretty(tasks)?;
    std::fs::write(&path, payload).with_context(|| format!("Failed to write {}", path.display()))
}

pub fn create_task(
    workspace_dir: &Path,
    name: Option<String>,
    prompt: String,
    owner_agent_id: Option<String>,
    enabled: bool,
) -> Result<ManagedHeartbeatTask> {
    let mut tasks = list_tasks(workspace_dir)?;
    let task = ManagedHeartbeatTask {
        id: Uuid::new_v4().to_string(),
        name,
        prompt,
        owner_agent_id,
        enabled,
        created_at: Utc::now(),
        last_run: None,
        last_status: None,
        last_output: None,
    };
    tasks.push(task.clone());
    write_tasks(workspace_dir, &tasks)?;
    Ok(task)
}

pub fn get_task(workspace_dir: &Path, id: &str) -> Result<ManagedHeartbeatTask> {
    list_tasks(workspace_dir)?
        .into_iter()
        .find(|task| task.id == id)
        .with_context(|| format!("Heartbeat task '{id}' not found"))
}

pub fn update_task(
    workspace_dir: &Path,
    id: &str,
    patch: ManagedHeartbeatTaskPatch,
) -> Result<ManagedHeartbeatTask> {
    let mut tasks = list_tasks(workspace_dir)?;
    let Some(task) = tasks.iter_mut().find(|task| task.id == id) else {
        anyhow::bail!("Heartbeat task '{id}' not found");
    };
    if let Some(name) = patch.name {
        task.name = Some(name);
    }
    if let Some(prompt) = patch.prompt {
        task.prompt = prompt;
    }
    if let Some(owner_agent_id) = patch.owner_agent_id {
        task.owner_agent_id = owner_agent_id;
    }
    if let Some(enabled) = patch.enabled {
        task.enabled = enabled;
    }
    let updated = task.clone();
    write_tasks(workspace_dir, &tasks)?;
    Ok(updated)
}

pub fn remove_task(workspace_dir: &Path, id: &str) -> Result<()> {
    let mut tasks = list_tasks(workspace_dir)?;
    let before = tasks.len();
    tasks.retain(|task| task.id != id);
    if tasks.len() == before {
        anyhow::bail!("Heartbeat task '{id}' not found");
    }
    write_tasks(workspace_dir, &tasks)
}

pub fn record_task_run(
    workspace_dir: &Path,
    id: &str,
    success: bool,
    output: &str,
) -> Result<ManagedHeartbeatTask> {
    let mut tasks = list_tasks(workspace_dir)?;
    let Some(task) = tasks.iter_mut().find(|task| task.id == id) else {
        anyhow::bail!("Heartbeat task '{id}' not found");
    };
    task.last_run = Some(Utc::now());
    task.last_status = Some(if success { "ok" } else { "error" }.to_string());
    task.last_output = Some(output.to_string());
    let updated = task.clone();
    write_tasks(workspace_dir, &tasks)?;
    Ok(updated)
}
