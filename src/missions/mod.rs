//! Autonomous Mission Runner — ported from codex-telegram-bot's `services/mission_runner.py`.
//!
//! Provides multi-step autonomous mission execution with:
//! - Goal decomposition into steps via the agent planner
//! - State machine: idle → running → paused → completed/failed
//! - Pause/resume/stop control
//! - Step-level retry with configurable limits
//! - Budget enforcement per mission
//! - Progress callbacks for real-time updates
//! - Checkpoint persistence so re-runs skip completed steps

use anyhow::{bail, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::{watch, Mutex, Notify};
use uuid::Uuid;

// ── Constants ────────────────────────────────────────────────────

const MAX_STEP_RETRIES: u32 = 2;
const DEFAULT_MAX_STEPS: usize = 20;
const DEFAULT_BUDGET_USD: f64 = 5.0;

// ── Types ────────────────────────────────────────────────────────

/// Mission lifecycle states.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MissionState {
    Idle,
    Running,
    Paused,
    Completed,
    Failed,
    Blocked,
}

impl std::fmt::Display for MissionState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Idle => write!(f, "idle"),
            Self::Running => write!(f, "running"),
            Self::Paused => write!(f, "paused"),
            Self::Completed => write!(f, "completed"),
            Self::Failed => write!(f, "failed"),
            Self::Blocked => write!(f, "blocked"),
        }
    }
}

/// A single step within a mission plan.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MissionStep {
    pub index: usize,
    pub description: String,
    pub status: StepStatus,
    pub output: String,
    pub retries: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StepStatus {
    Pending,
    Running,
    Completed,
    Failed,
    Skipped,
}

/// A mission plan: a goal decomposed into ordered steps.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MissionPlan {
    pub goal: String,
    pub steps: Vec<MissionStep>,
}

/// Full mission record with metadata and state.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MissionRecord {
    pub id: String,
    pub goal: String,
    pub state: MissionState,
    pub plan: Option<MissionPlan>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub completed_at: Option<DateTime<Utc>>,
    pub budget_usd: f64,
    pub spent_usd: f64,
    pub max_steps: usize,
    pub error: Option<String>,
}

impl MissionRecord {
    pub fn new(goal: String) -> Self {
        let now = Utc::now();
        Self {
            id: Uuid::new_v4().to_string(),
            goal,
            state: MissionState::Idle,
            plan: None,
            created_at: now,
            updated_at: now,
            completed_at: None,
            budget_usd: DEFAULT_BUDGET_USD,
            spent_usd: 0.0,
            max_steps: DEFAULT_MAX_STEPS,
            error: None,
        }
    }
}

/// Progress event emitted during mission execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MissionEvent {
    pub mission_id: String,
    pub event_type: String,
    pub detail: String,
    pub timestamp: DateTime<Utc>,
}

// ── Control signals ──────────────────────────────────────────────

/// Runtime control handle for a running mission.
pub struct MissionControl {
    pub mission_id: String,
    pause_tx: watch::Sender<bool>,
    pause_rx: watch::Receiver<bool>,
    stop: Arc<Notify>,
    stopped: Arc<std::sync::atomic::AtomicBool>,
}

impl MissionControl {
    fn new(mission_id: String) -> Self {
        let (pause_tx, pause_rx) = watch::channel(false);
        Self {
            mission_id,
            pause_tx,
            pause_rx,
            stop: Arc::new(Notify::new()),
            stopped: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        }
    }

    pub fn pause(&self) {
        let _ = self.pause_tx.send(true);
    }

    pub fn resume(&self) {
        let _ = self.pause_tx.send(false);
    }

    pub fn stop(&self) {
        self.stopped
            .store(true, std::sync::atomic::Ordering::SeqCst);
        self.stop.notify_waiters();
    }

    pub fn is_stopped(&self) -> bool {
        self.stopped.load(std::sync::atomic::Ordering::SeqCst)
    }

    pub fn is_paused(&self) -> bool {
        *self.pause_rx.borrow()
    }

    async fn wait_if_paused(&mut self) {
        let mut rx = self.pause_rx.clone();
        while *rx.borrow() {
            // Wait until the value changes (unpaused)
            if rx.changed().await.is_err() {
                break;
            }
        }
    }
}

// ── Step executor trait ──────────────────────────────────────────

/// Trait for executing a single mission step.  The agent loop implements this.
#[async_trait::async_trait]
pub trait StepExecutor: Send + Sync {
    /// Execute a step description and return the output.
    async fn execute_step(&self, step_description: &str) -> Result<String>;

    /// Estimate the cost of executing a step (for budget checks).
    fn estimated_step_cost(&self) -> f64 {
        0.01 // default conservative estimate
    }
}

/// Trait for decomposing a goal into steps.
#[async_trait::async_trait]
pub trait MissionPlanner: Send + Sync {
    /// Decompose a goal into an ordered list of step descriptions.
    async fn plan(&self, goal: &str) -> Result<Vec<String>>;
}

// ── Mission Runner ───────────────────────────────────────────────

/// The autonomous mission runner.
pub struct MissionRunner {
    missions: Arc<Mutex<HashMap<String, MissionRecord>>>,
    controls: Arc<Mutex<HashMap<String, MissionControl>>>,
    events: Arc<Mutex<Vec<MissionEvent>>>,
}

impl MissionRunner {
    pub fn new() -> Self {
        Self {
            missions: Arc::new(Mutex::new(HashMap::new())),
            controls: Arc::new(Mutex::new(HashMap::new())),
            events: Arc::new(Mutex::new(Vec::new())),
        }
    }

    /// Create a new mission from a goal string.
    pub async fn create_mission(&self, goal: String) -> MissionRecord {
        let record = MissionRecord::new(goal);
        let id = record.id.clone();
        self.missions.lock().await.insert(id, record.clone());
        record
    }

    /// List all missions.
    pub async fn list_missions(&self) -> Vec<MissionRecord> {
        self.missions.lock().await.values().cloned().collect()
    }

    /// Get a mission by ID.
    pub async fn get_mission(&self, id: &str) -> Option<MissionRecord> {
        self.missions.lock().await.get(id).cloned()
    }

    /// Pause a running mission.
    pub async fn pause(&self, id: &str) -> bool {
        if let Some(ctrl) = self.controls.lock().await.get(id) {
            ctrl.pause();
            if let Some(m) = self.missions.lock().await.get_mut(id) {
                m.state = MissionState::Paused;
                m.updated_at = Utc::now();
            }
            true
        } else {
            false
        }
    }

    /// Resume a paused mission.
    pub async fn resume(&self, id: &str) -> bool {
        if let Some(ctrl) = self.controls.lock().await.get(id) {
            ctrl.resume();
            if let Some(m) = self.missions.lock().await.get_mut(id) {
                m.state = MissionState::Running;
                m.updated_at = Utc::now();
            }
            true
        } else {
            false
        }
    }

    /// Stop a running mission.
    pub async fn stop(&self, id: &str) -> bool {
        if let Some(ctrl) = self.controls.lock().await.get(id) {
            ctrl.stop();
            if let Some(m) = self.missions.lock().await.get_mut(id) {
                m.state = MissionState::Failed;
                m.error = Some("Stopped by user".into());
                m.updated_at = Utc::now();
            }
            true
        } else {
            false
        }
    }

    /// Run a mission to completion.
    pub async fn run(
        &self,
        mission_id: &str,
        planner: &dyn MissionPlanner,
        executor: &dyn StepExecutor,
    ) -> Result<MissionRecord> {
        // Create control handle
        let mut control = MissionControl::new(mission_id.to_string());
        self.controls.lock().await.insert(
            mission_id.to_string(),
            MissionControl::new(mission_id.to_string()),
        );

        // Set state to running
        {
            let mut missions = self.missions.lock().await;
            let mission = missions
                .get_mut(mission_id)
                .ok_or_else(|| anyhow::anyhow!("Mission not found"))?;
            mission.state = MissionState::Running;
            mission.updated_at = Utc::now();
        }

        self.emit_event(mission_id, "started", "Mission execution started")
            .await;

        // Plan phase
        let step_descriptions = match planner.plan(&self.get_goal(mission_id).await).await {
            Ok(steps) => steps,
            Err(e) => {
                self.fail_mission(mission_id, &format!("Planning failed: {e}"))
                    .await;
                bail!("Planning failed: {e}");
            }
        };

        // Store plan
        {
            let mut missions = self.missions.lock().await;
            if let Some(mission) = missions.get_mut(mission_id) {
                let steps: Vec<MissionStep> = step_descriptions
                    .iter()
                    .enumerate()
                    .map(|(i, desc)| MissionStep {
                        index: i,
                        description: desc.clone(),
                        status: StepStatus::Pending,
                        output: String::new(),
                        retries: 0,
                    })
                    .collect();

                if steps.len() > mission.max_steps {
                    self.emit_event(
                        mission_id,
                        "warning",
                        &format!(
                            "Plan has {} steps, truncating to {}",
                            steps.len(),
                            mission.max_steps
                        ),
                    )
                    .await;
                }

                mission.plan = Some(MissionPlan {
                    goal: mission.goal.clone(),
                    steps: steps.into_iter().take(mission.max_steps).collect(),
                });
                mission.updated_at = Utc::now();
            }
        }

        self.emit_event(
            mission_id,
            "planned",
            &format!("{} steps planned", step_descriptions.len()),
        )
        .await;

        // Execute steps
        let step_count = {
            let missions = self.missions.lock().await;
            missions
                .get(mission_id)
                .and_then(|m| m.plan.as_ref())
                .map(|p| p.steps.len())
                .unwrap_or(0)
        };

        for step_idx in 0..step_count {
            // Check stop
            if self.is_stopped(mission_id).await {
                break;
            }

            // Check pause — wait until resumed
            control.wait_if_paused().await;

            // Check budget
            let est_cost = executor.estimated_step_cost();
            if !self.check_budget(mission_id, est_cost).await {
                self.fail_mission(mission_id, "Budget exceeded").await;
                bail!("Budget exceeded");
            }

            // Get step description
            let step_desc = {
                let missions = self.missions.lock().await;
                missions
                    .get(mission_id)
                    .and_then(|m| m.plan.as_ref())
                    .and_then(|p| p.steps.get(step_idx))
                    .map(|s| s.description.clone())
                    .unwrap_or_default()
            };

            // Skip already completed steps (checkpoint resume)
            {
                let missions = self.missions.lock().await;
                if let Some(step) = missions
                    .get(mission_id)
                    .and_then(|m| m.plan.as_ref())
                    .and_then(|p| p.steps.get(step_idx))
                {
                    if step.status == StepStatus::Completed {
                        continue;
                    }
                }
            }

            self.set_step_status(mission_id, step_idx, StepStatus::Running, "")
                .await;
            self.emit_event(
                mission_id,
                "step_start",
                &format!("Step {}: {}", step_idx + 1, step_desc),
            )
            .await;

            // Execute with retries
            let mut last_err = String::new();
            let mut success = false;
            for retry in 0..=MAX_STEP_RETRIES {
                match executor.execute_step(&step_desc).await {
                    Ok(output) => {
                        self.set_step_status(mission_id, step_idx, StepStatus::Completed, &output)
                            .await;
                        self.emit_event(
                            mission_id,
                            "step_done",
                            &format!("Step {} completed", step_idx + 1),
                        )
                        .await;

                        // Track cost
                        {
                            let mut missions = self.missions.lock().await;
                            if let Some(m) = missions.get_mut(mission_id) {
                                m.spent_usd += est_cost;
                                m.updated_at = Utc::now();
                            }
                        }

                        success = true;
                        break;
                    }
                    Err(e) => {
                        last_err = e.to_string();
                        if retry < MAX_STEP_RETRIES {
                            self.emit_event(
                                mission_id,
                                "step_retry",
                                &format!(
                                    "Step {} retry {}/{}: {}",
                                    step_idx + 1,
                                    retry + 1,
                                    MAX_STEP_RETRIES,
                                    last_err
                                ),
                            )
                            .await;
                        }
                    }
                }
            }

            if !success {
                self.set_step_status(mission_id, step_idx, StepStatus::Failed, &last_err)
                    .await;
                self.fail_mission(
                    mission_id,
                    &format!(
                        "Step {} failed after {} retries: {}",
                        step_idx + 1,
                        MAX_STEP_RETRIES,
                        last_err
                    ),
                )
                .await;
                bail!("Step {} failed: {}", step_idx + 1, last_err);
            }
        }

        // Complete mission
        {
            let mut missions = self.missions.lock().await;
            if let Some(m) = missions.get_mut(mission_id) {
                if m.state == MissionState::Running {
                    m.state = MissionState::Completed;
                    m.completed_at = Some(Utc::now());
                    m.updated_at = Utc::now();
                }
            }
        }

        self.emit_event(mission_id, "completed", "Mission completed successfully")
            .await;
        self.controls.lock().await.remove(mission_id);

        self.get_mission(mission_id)
            .await
            .ok_or_else(|| anyhow::anyhow!("Mission record lost"))
    }

    /// Get mission events.
    pub async fn get_events(&self, mission_id: &str) -> Vec<MissionEvent> {
        self.events
            .lock()
            .await
            .iter()
            .filter(|e| e.mission_id == mission_id)
            .cloned()
            .collect()
    }

    // ── Private helpers ──────────────────────────────────────────

    async fn get_goal(&self, id: &str) -> String {
        self.missions
            .lock()
            .await
            .get(id)
            .map(|m| m.goal.clone())
            .unwrap_or_default()
    }

    async fn is_stopped(&self, id: &str) -> bool {
        self.controls
            .lock()
            .await
            .get(id)
            .map(|c| c.is_stopped())
            .unwrap_or(true)
    }

    async fn check_budget(&self, id: &str, est_cost: f64) -> bool {
        let missions = self.missions.lock().await;
        if let Some(m) = missions.get(id) {
            m.spent_usd + est_cost <= m.budget_usd
        } else {
            false
        }
    }

    async fn set_step_status(
        &self,
        mission_id: &str,
        step_idx: usize,
        status: StepStatus,
        output: &str,
    ) {
        let mut missions = self.missions.lock().await;
        if let Some(mission) = missions.get_mut(mission_id) {
            if let Some(plan) = &mut mission.plan {
                if let Some(step) = plan.steps.get_mut(step_idx) {
                    step.status = status;
                    if !output.is_empty() {
                        step.output = output.to_string();
                    }
                }
            }
            mission.updated_at = Utc::now();
        }
    }

    async fn fail_mission(&self, id: &str, reason: &str) {
        let mut missions = self.missions.lock().await;
        if let Some(m) = missions.get_mut(id) {
            m.state = MissionState::Failed;
            m.error = Some(reason.to_string());
            m.updated_at = Utc::now();
        }
        drop(missions);
        self.emit_event(id, "failed", reason).await;
    }

    async fn emit_event(&self, mission_id: &str, event_type: &str, detail: &str) {
        let event = MissionEvent {
            mission_id: mission_id.to_string(),
            event_type: event_type.to_string(),
            detail: detail.to_string(),
            timestamp: Utc::now(),
        };
        self.events.lock().await.push(event);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct MockPlanner;

    #[async_trait::async_trait]
    impl MissionPlanner for MockPlanner {
        async fn plan(&self, _goal: &str) -> Result<Vec<String>> {
            Ok(vec![
                "Step 1: Research".into(),
                "Step 2: Implement".into(),
                "Step 3: Test".into(),
            ])
        }
    }

    struct MockExecutor;

    #[async_trait::async_trait]
    impl StepExecutor for MockExecutor {
        async fn execute_step(&self, _desc: &str) -> Result<String> {
            Ok("done".into())
        }
    }

    #[tokio::test]
    async fn test_mission_lifecycle() {
        let runner = MissionRunner::new();
        let mission = runner.create_mission("Test goal".into()).await;
        assert_eq!(mission.state, MissionState::Idle);

        let result = runner
            .run(&mission.id, &MockPlanner, &MockExecutor)
            .await
            .unwrap();
        assert_eq!(result.state, MissionState::Completed);
        assert!(result.plan.is_some());
        assert_eq!(result.plan.unwrap().steps.len(), 3);
    }
}
