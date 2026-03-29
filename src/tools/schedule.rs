use super::traits::{Tool, ToolResult};
use crate::config::Config;
use crate::cron;
use crate::security::SecurityPolicy;
use crate::studio;
use anyhow::Result;
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde_json::json;
use std::sync::Arc;

/// Tool that lets the agent manage recurring and one-shot scheduled tasks.
pub struct ScheduleTool {
    security: Arc<SecurityPolicy>,
    config: Config,
}

impl ScheduleTool {
    pub fn new(security: Arc<SecurityPolicy>, config: Config) -> Self {
        Self { security, config }
    }
}

#[async_trait]
impl Tool for ScheduleTool {
    fn name(&self) -> &str {
        "schedule"
    }

    fn description(&self) -> &str {
        "Manage automations for agents and shell tasks. Actions: create/add/once/list/get/update/cancel/remove/pause/resume/run_now/runs. \
         Agent automations are the default and should usually target the active agent profile unless owner_agent_id is explicitly provided. \
         Supports scheduled agent jobs, scheduled shell jobs, and heartbeat tasks. \
         Use automation_kind='heartbeat_task' for recurring ambient heartbeat work, and scheduled_agent with a delivery config to announce results into channels."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        json!({
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "add", "once", "list", "get", "update", "cancel", "remove", "pause", "resume", "run_now", "runs"],
                    "description": "Action to perform"
                },
                "automation_kind": {
                    "type": "string",
                    "enum": ["scheduled_agent", "scheduled_shell", "heartbeat_task"],
                    "description": "Automation kind. Defaults to scheduled_agent when prompt is present, otherwise scheduled_shell when command is present."
                },
                "job_type": {
                    "type": "string",
                    "enum": ["agent", "shell"],
                    "description": "Legacy alias for automation kind when creating scheduled jobs."
                },
                "schedule": {
                    "type": "object",
                    "description": "Structured schedule: {kind:'cron',expr,tz?} | {kind:'at',at} | {kind:'every',every_ms}"
                },
                "expression": {
                    "type": "string",
                    "description": "Cron expression for recurring tasks (e.g. '0 9 * * 1-5')."
                },
                "delay": {
                    "type": "string",
                    "description": "Delay for one-shot tasks (e.g. '30m', '2h', '1d')."
                },
                "run_at": {
                    "type": "string",
                    "description": "Absolute RFC3339 time for one-shot tasks (e.g. '2030-01-01T00:00:00Z')."
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to execute for scheduled_shell automations."
                },
                "prompt": {
                    "type": "string",
                    "description": "Agent or heartbeat task prompt. Required for scheduled_agent and heartbeat_task."
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable name for the automation."
                },
                "owner_agent_id": {
                    "type": ["string", "null"],
                    "description": "Owning agent profile id. Defaults to the active profile for agent and heartbeat automations."
                },
                "session_target": {
                    "type": "string",
                    "enum": ["isolated", "main"],
                    "description": "Where scheduled agent work should run. Defaults to isolated."
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for scheduled agent jobs."
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether the automation is enabled."
                },
                "delete_after_run": {
                    "type": "boolean",
                    "description": "Delete one-shot jobs after a successful run."
                },
                "delivery": {
                    "type": "object",
                    "description": "Optional delivery config for scheduled agent job output."
                },
                "approved": {
                    "type": "boolean",
                    "description": "Set true to explicitly approve medium/high-risk shell commands in supervised mode",
                    "default": false
                },
                "id": {
                    "type": "string",
                    "description": "Automation ID. Use values from list/get output, e.g. cron:<id> or heartbeat:<id>."
                }
            },
            "required": ["action"]
        })
    }

    async fn execute(&self, args: serde_json::Value) -> Result<ToolResult> {
        let action = args
            .get("action")
            .and_then(|value| value.as_str())
            .ok_or_else(|| anyhow::anyhow!("Missing 'action' parameter"))?;

        match action {
            "list" => self.handle_list(),
            "get" => {
                let id = args
                    .get("id")
                    .and_then(|value| value.as_str())
                    .ok_or_else(|| anyhow::anyhow!("Missing 'id' parameter for get action"))?;
                self.handle_get(id)
            }
            "create" | "add" | "once" => {
                if let Some(blocked) = self.enforce_mutation_allowed(action) {
                    return Ok(blocked);
                }
                let approved = args
                    .get("approved")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false);
                self.handle_create_like(action, &args, approved).await
            }
            "update" => {
                if let Some(blocked) = self.enforce_mutation_allowed(action) {
                    return Ok(blocked);
                }
                let approved = args
                    .get("approved")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false);
                self.handle_update(&args, approved).await
            }
            "cancel" | "remove" => {
                if let Some(blocked) = self.enforce_mutation_allowed(action) {
                    return Ok(blocked);
                }
                let id = args
                    .get("id")
                    .and_then(|value| value.as_str())
                    .ok_or_else(|| anyhow::anyhow!("Missing 'id' parameter for cancel action"))?;
                Ok(self.handle_cancel(id))
            }
            "pause" => {
                if let Some(blocked) = self.enforce_mutation_allowed(action) {
                    return Ok(blocked);
                }
                let id = args
                    .get("id")
                    .and_then(|value| value.as_str())
                    .ok_or_else(|| anyhow::anyhow!("Missing 'id' parameter for pause action"))?;
                Ok(self.handle_pause_resume(id, true))
            }
            "resume" => {
                if let Some(blocked) = self.enforce_mutation_allowed(action) {
                    return Ok(blocked);
                }
                let id = args
                    .get("id")
                    .and_then(|value| value.as_str())
                    .ok_or_else(|| anyhow::anyhow!("Missing 'id' parameter for resume action"))?;
                Ok(self.handle_pause_resume(id, false))
            }
            "run_now" => {
                if let Some(blocked) = self.enforce_mutation_allowed(action) {
                    return Ok(blocked);
                }
                let id = args
                    .get("id")
                    .and_then(|value| value.as_str())
                    .ok_or_else(|| anyhow::anyhow!("Missing 'id' parameter for run_now action"))?;
                self.handle_run_now(id).await
            }
            "runs" => {
                let id = args
                    .get("id")
                    .and_then(|value| value.as_str())
                    .ok_or_else(|| anyhow::anyhow!("Missing 'id' parameter for runs action"))?;
                self.handle_runs(id)
            }
            other => Ok(ToolResult {
                success: false,
                output: String::new(),
                error: Some(format!(
                    "Unknown action '{other}'. Use create/add/once/list/get/update/cancel/remove/pause/resume/run_now/runs."
                )),
                metadata: None,
            }),
        }
    }
}

impl ScheduleTool {
    fn parse_delay(input: &str) -> Result<chrono::Duration> {
        let input = input.trim();
        if input.is_empty() {
            anyhow::bail!("delay must not be empty");
        }
        let split = input
            .find(|c: char| !c.is_ascii_digit())
            .unwrap_or(input.len());
        let (num, unit) = input.split_at(split);
        let amount: i64 = num.parse()?;
        let unit = if unit.is_empty() { "m" } else { unit };
        Ok(match unit {
            "s" => chrono::Duration::seconds(amount),
            "m" => chrono::Duration::minutes(amount),
            "h" => chrono::Duration::hours(amount),
            "d" => chrono::Duration::days(amount),
            _ => anyhow::bail!("unsupported delay unit '{unit}', use s/m/h/d"),
        })
    }

    fn parse_automation_id<'a>(&self, raw: &'a str) -> (&'a str, &'a str) {
        raw.split_once(':').unwrap_or(("cron", raw))
    }

    async fn default_owner_agent_id(&self) -> Option<String> {
        let state = studio::load_or_bootstrap(&self.config).await.ok()?;
        Some(state.active_agent_id)
    }

    fn schedule_from_args(
        &self,
        action: &str,
        args: &serde_json::Value,
    ) -> Result<Option<cron::Schedule>> {
        if let Some(schedule) = args.get("schedule") {
            let schedule = serde_json::from_value::<cron::Schedule>(schedule.clone())
                .map_err(|error| anyhow::anyhow!("Invalid schedule payload: {error}"))?;
            return Ok(Some(schedule));
        }

        let expression = args.get("expression").and_then(|value| value.as_str());
        let delay = args.get("delay").and_then(|value| value.as_str());
        let run_at = args.get("run_at").and_then(|value| value.as_str());

        match action {
            "add" => {
                if let Some(expr) = expression {
                    return Ok(Some(cron::Schedule::Cron {
                        expr: expr.to_string(),
                        tz: None,
                    }));
                }
                anyhow::bail!("'add' requires either 'schedule' or 'expression'");
            }
            "once" => {
                if let Some(value) = delay {
                    let when = chrono::Utc::now() + Self::parse_delay(value)?;
                    return Ok(Some(cron::Schedule::At { at: when }));
                }
                if let Some(value) = run_at {
                    let at = DateTime::parse_from_rfc3339(value)
                        .map_err(|error| anyhow::anyhow!("Invalid run_at timestamp: {error}"))?
                        .with_timezone(&Utc);
                    return Ok(Some(cron::Schedule::At { at }));
                }
                anyhow::bail!("'once' requires 'schedule', 'delay', or 'run_at'");
            }
            _ => {
                if let Some(expr) = expression {
                    return Ok(Some(cron::Schedule::Cron {
                        expr: expr.to_string(),
                        tz: None,
                    }));
                }
                if let Some(value) = delay {
                    let when = chrono::Utc::now() + Self::parse_delay(value)?;
                    return Ok(Some(cron::Schedule::At { at: when }));
                }
                if let Some(value) = run_at {
                    let at = DateTime::parse_from_rfc3339(value)
                        .map_err(|error| anyhow::anyhow!("Invalid run_at timestamp: {error}"))?
                        .with_timezone(&Utc);
                    return Ok(Some(cron::Schedule::At { at }));
                }
            }
        }

        Ok(None)
    }

    fn infer_automation_kind(&self, args: &serde_json::Value) -> &'static str {
        match args.get("automation_kind").and_then(|value| value.as_str()) {
            Some("heartbeat_task") => "heartbeat_task",
            Some("scheduled_shell") => "scheduled_shell",
            Some("scheduled_agent") => "scheduled_agent",
            Some(_) => "scheduled_agent",
            None => match args.get("job_type").and_then(|value| value.as_str()) {
                Some("shell") => "scheduled_shell",
                Some("agent") => "scheduled_agent",
                _ if args.get("prompt").is_some() => "scheduled_agent",
                _ => "scheduled_shell",
            },
        }
    }

    fn enforce_mutation_allowed(&self, action: &str) -> Option<ToolResult> {
        if !self.config.cron.enabled {
            return Some(ToolResult {
                success: false,
                output: String::new(),
                error: Some(format!(
                    "cron is disabled by config (cron.enabled=false); cannot perform '{action}'"
                )),
                metadata: None,
            });
        }

        if !self.security.can_act() {
            return Some(ToolResult {
                success: false,
                output: String::new(),
                error: Some(format!(
                    "Security policy: read-only mode, cannot perform '{action}'"
                )),
                metadata: None,
            });
        }

        if !self.security.record_action() {
            return Some(ToolResult {
                success: false,
                output: String::new(),
                error: Some("Rate limit exceeded: action budget exhausted".to_string()),
                metadata: None,
            });
        }

        None
    }

    fn handle_list(&self) -> Result<ToolResult> {
        let jobs = cron::list_jobs(&self.config)?;
        let heartbeat_tasks = crate::heartbeat::tasks::list_tasks(&self.config.workspace_dir)?;

        if jobs.is_empty() && heartbeat_tasks.is_empty() {
            return Ok(ToolResult {
                success: true,
                output: "No scheduled jobs or heartbeat automations.".to_string(),
                error: None,
                metadata: None,
            });
        }

        let mut lines = Vec::with_capacity(jobs.len() + heartbeat_tasks.len());
        for job in jobs {
            let paused = !job.enabled;
            let one_shot = matches!(job.schedule, cron::Schedule::At { .. });
            let flags = match (paused, one_shot) {
                (true, true) => " [disabled, one-shot]",
                (true, false) => " [disabled]",
                (false, true) => " [one-shot]",
                (false, false) => "",
            };
            let last_run = job
                .last_run
                .map_or_else(|| "never".to_string(), |value| value.to_rfc3339());
            let last_status = job.last_status.unwrap_or_else(|| "n/a".to_string());
            let job_kind = match job.job_type {
                cron::JobType::Agent => "scheduled_agent",
                cron::JobType::Shell => "scheduled_shell",
            };
            lines.push(format!(
                "- cron:{} | {} | owner={} | next={} | last={} ({}){} | {}",
                job.id,
                job_kind,
                job.owner_agent_id.unwrap_or_else(|| "global".to_string()),
                job.next_run.to_rfc3339(),
                last_run,
                last_status,
                flags,
                job.prompt.unwrap_or(job.command),
            ));
        }

        for task in heartbeat_tasks {
            let last_run = task
                .last_run
                .map_or_else(|| "never".to_string(), |value| value.to_rfc3339());
            let last_status = task.last_status.unwrap_or_else(|| "n/a".to_string());
            lines.push(format!(
                "- heartbeat:{} | heartbeat_task | owner={} | last={} ({}){} | {}",
                task.id,
                task.owner_agent_id.unwrap_or_else(|| "active".to_string()),
                last_run,
                last_status,
                if task.enabled { "" } else { " [disabled]" },
                task.prompt
            ));
        }

        Ok(ToolResult {
            success: true,
            output: format!("Automations ({}):\n{}", lines.len(), lines.join("\n")),
            error: None,
            metadata: None,
        })
    }

    fn handle_get(&self, id: &str) -> Result<ToolResult> {
        let (kind, backend_id) = self.parse_automation_id(id);
        if kind == "heartbeat" {
            match crate::heartbeat::tasks::get_task(&self.config.workspace_dir, backend_id) {
                Ok(task) => {
                    let detail = json!({
                        "id": format!("heartbeat:{}", task.id),
                        "automation_kind": "heartbeat_task",
                        "name": task.name,
                        "owner_agent_id": task.owner_agent_id,
                        "prompt": task.prompt,
                        "enabled": task.enabled,
                        "created_at": task.created_at.to_rfc3339(),
                        "last_run": task.last_run.map(|value| value.to_rfc3339()),
                        "last_status": task.last_status,
                        "last_output": task.last_output,
                    });
                    return Ok(ToolResult {
                        success: true,
                        output: serde_json::to_string_pretty(&detail)?,
                        error: None,
                        metadata: None,
                    });
                }
                Err(_) => {
                    return Ok(ToolResult {
                        success: false,
                        output: String::new(),
                        error: Some(format!("Automation '{id}' not found")),
                        metadata: None,
                    });
                }
            }
        }

        match cron::get_job(&self.config, backend_id) {
            Ok(job) => {
                let detail = json!({
                    "id": format!("cron:{}", job.id),
                    "automation_kind": match job.job_type {
                        cron::JobType::Agent => "scheduled_agent",
                        cron::JobType::Shell => "scheduled_shell",
                    },
                    "expression": job.expression,
                    "schedule": job.schedule,
                    "command": job.command,
                    "prompt": job.prompt,
                    "owner_agent_id": job.owner_agent_id,
                    "next_run": job.next_run.to_rfc3339(),
                    "last_run": job.last_run.map(|value| value.to_rfc3339()),
                    "last_status": job.last_status,
                    "enabled": job.enabled,
                    "one_shot": matches!(job.schedule, cron::Schedule::At { .. }),
                });
                Ok(ToolResult {
                    success: true,
                    output: serde_json::to_string_pretty(&detail)?,
                    error: None,
                    metadata: None,
                })
            }
            Err(_) => Ok(ToolResult {
                success: false,
                output: String::new(),
                error: Some(format!("Job '{id}' not found")),
                metadata: None,
            }),
        }
    }

    async fn handle_create_like(
        &self,
        action: &str,
        args: &serde_json::Value,
        approved: bool,
    ) -> Result<ToolResult> {
        let automation_kind = self.infer_automation_kind(args);
        let name = args
            .get("name")
            .and_then(|value| value.as_str())
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty());
        let enabled = args
            .get("enabled")
            .and_then(|value| value.as_bool())
            .unwrap_or(true);

        if automation_kind == "heartbeat_task" {
            let prompt = args
                .get("prompt")
                .and_then(|value| value.as_str())
                .filter(|value| !value.trim().is_empty())
                .ok_or_else(|| anyhow::anyhow!("Missing or empty 'prompt' parameter"))?;
            let owner_agent_id = args
                .get("owner_agent_id")
                .and_then(|value| value.as_str())
                .map(str::to_string)
                .or(self.default_owner_agent_id().await);
            let task = crate::heartbeat::tasks::create_task(
                &self.config.workspace_dir,
                name.clone(),
                prompt.trim().to_string(),
                owner_agent_id.clone(),
                enabled,
            )?;
            return Ok(ToolResult {
                success: true,
                output: format!(
                    "Created heartbeat automation heartbeat:{}{} for {}",
                    task.id,
                    name.map(|value| format!(" ({value})")).unwrap_or_default(),
                    owner_agent_id.unwrap_or_else(|| "the active agent".to_string())
                ),
                error: None,
                metadata: None,
            });
        }

        let schedule = self.schedule_from_args(action, args)?.ok_or_else(|| {
            anyhow::anyhow!(
                "Missing schedule. Provide 'schedule', 'expression', 'delay', or 'run_at'"
            )
        })?;
        let default_delete_after_run = matches!(schedule, cron::Schedule::At { .. });
        let delete_after_run = args
            .get("delete_after_run")
            .and_then(|value| value.as_bool())
            .unwrap_or(default_delete_after_run);

        if automation_kind == "scheduled_shell" {
            let command = args
                .get("command")
                .and_then(|value| value.as_str())
                .filter(|value| !value.trim().is_empty())
                .ok_or_else(|| anyhow::anyhow!("Missing or empty 'command' parameter"))?;
            let is_one_shot = matches!(schedule, cron::Schedule::At { .. });
            let job = match match schedule {
                cron::Schedule::At { at } => {
                    cron::add_once_at_validated(&self.config, at, command, approved)
                }
                _ => cron::add_shell_job_with_approval(
                    &self.config,
                    name,
                    schedule,
                    command,
                    approved,
                ),
            } {
                Ok(job) => job,
                Err(error) => {
                    return Ok(ToolResult {
                        success: false,
                        output: String::new(),
                        error: Some(error.to_string()),
                        metadata: None,
                    });
                }
            };
            if !enabled {
                let _ = cron::pause_job(&self.config, &job.id)?;
            }
            return Ok(ToolResult {
                success: true,
                output: if is_one_shot {
                    format!(
                        "Created one-shot job {} (runs at: {}, cmd: {})",
                        job.id,
                        job.next_run.to_rfc3339(),
                        job.command
                    )
                } else {
                    format!(
                        "Created recurring job {} (expr: {}, next: {}, cmd: {})",
                        job.id,
                        job.expression,
                        job.next_run.to_rfc3339(),
                        job.command
                    )
                },
                error: None,
                metadata: None,
            });
        }

        let prompt = args
            .get("prompt")
            .and_then(|value| value.as_str())
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| anyhow::anyhow!("Missing or empty 'prompt' parameter"))?;
        let owner_agent_id = args
            .get("owner_agent_id")
            .and_then(|value| value.as_str())
            .map(str::to_string)
            .or(self.default_owner_agent_id().await);
        let session_target = match args.get("session_target").and_then(|value| value.as_str()) {
            Some("main") => cron::SessionTarget::Main,
            _ => cron::SessionTarget::Isolated,
        };
        let model = args
            .get("model")
            .and_then(|value| value.as_str())
            .map(str::to_string);
        let delivery = args
            .get("delivery")
            .cloned()
            .map(serde_json::from_value)
            .transpose()?;
        let job = cron::add_agent_job(
            &self.config,
            name,
            schedule,
            owner_agent_id.clone(),
            prompt,
            session_target,
            model,
            delivery,
            delete_after_run,
        )?;
        if !enabled {
            let _ = cron::pause_job(&self.config, &job.id)?;
        }
        Ok(ToolResult {
            success: true,
            output: format!(
                "Created scheduled agent automation cron:{} for {} (next: {})",
                job.id,
                owner_agent_id.unwrap_or_else(|| "the active agent".to_string()),
                job.next_run.to_rfc3339()
            ),
            error: None,
            metadata: None,
        })
    }

    async fn handle_update(&self, args: &serde_json::Value, approved: bool) -> Result<ToolResult> {
        let id = args
            .get("id")
            .and_then(|value| value.as_str())
            .ok_or_else(|| anyhow::anyhow!("Missing 'id' parameter for update action"))?;
        let (kind, backend_id) = self.parse_automation_id(id);

        if kind == "heartbeat" {
            let patch = crate::heartbeat::tasks::ManagedHeartbeatTaskPatch {
                name: args
                    .get("name")
                    .and_then(|value| value.as_str())
                    .map(str::to_string),
                prompt: args
                    .get("prompt")
                    .and_then(|value| value.as_str())
                    .map(str::to_string),
                owner_agent_id: args.get("owner_agent_id").map(|value| {
                    if value.is_null() {
                        None
                    } else {
                        value.as_str().map(str::to_string)
                    }
                }),
                enabled: args.get("enabled").and_then(|value| value.as_bool()),
            };
            let task = crate::heartbeat::tasks::update_task(
                &self.config.workspace_dir,
                backend_id,
                patch,
            )?;
            return Ok(ToolResult {
                success: true,
                output: format!("Updated heartbeat automation heartbeat:{}", task.id),
                error: None,
                metadata: None,
            });
        }

        let job = cron::get_job(&self.config, backend_id)?;
        let patch = cron::CronJobPatch {
            schedule: self.schedule_from_args("update", args)?,
            command: args
                .get("command")
                .and_then(|value| value.as_str())
                .map(str::to_string),
            owner_agent_id: args.get("owner_agent_id").map(|value| {
                if value.is_null() {
                    None
                } else {
                    value.as_str().map(str::to_string)
                }
            }),
            prompt: args
                .get("prompt")
                .and_then(|value| value.as_str())
                .map(str::to_string),
            name: args
                .get("name")
                .and_then(|value| value.as_str())
                .map(str::to_string),
            enabled: args.get("enabled").and_then(|value| value.as_bool()),
            delivery: args
                .get("delivery")
                .cloned()
                .map(serde_json::from_value)
                .transpose()?,
            model: args
                .get("model")
                .and_then(|value| value.as_str())
                .map(str::to_string),
            session_target: args
                .get("session_target")
                .and_then(|value| value.as_str())
                .map(|value| {
                    if value.eq_ignore_ascii_case("main") {
                        cron::SessionTarget::Main
                    } else {
                        cron::SessionTarget::Isolated
                    }
                }),
            delete_after_run: args
                .get("delete_after_run")
                .and_then(|value| value.as_bool()),
        };
        let updated = if matches!(job.job_type, cron::JobType::Shell) {
            cron::update_shell_job_with_approval(&self.config, backend_id, patch, approved)?
        } else {
            cron::update_job(&self.config, backend_id, patch)?
        };
        Ok(ToolResult {
            success: true,
            output: format!("Updated automation cron:{}", updated.id),
            error: None,
            metadata: None,
        })
    }

    fn handle_cancel(&self, id: &str) -> ToolResult {
        let (kind, backend_id) = self.parse_automation_id(id);
        let result = if kind == "heartbeat" {
            crate::heartbeat::tasks::remove_task(&self.config.workspace_dir, backend_id)
        } else {
            cron::remove_job(&self.config, backend_id)
        };
        match result {
            Ok(()) => ToolResult {
                success: true,
                output: format!("Removed automation {id}"),
                error: None,
                metadata: None,
            },
            Err(error) => ToolResult {
                success: false,
                output: String::new(),
                error: Some(error.to_string()),
                metadata: None,
            },
        }
    }

    fn handle_pause_resume(&self, id: &str, pause: bool) -> ToolResult {
        let (kind, backend_id) = self.parse_automation_id(id);
        let operation = if kind == "heartbeat" {
            crate::heartbeat::tasks::update_task(
                &self.config.workspace_dir,
                backend_id,
                crate::heartbeat::tasks::ManagedHeartbeatTaskPatch {
                    enabled: Some(!pause),
                    ..Default::default()
                },
            )
            .map(|_| ())
        } else {
            if pause {
                cron::pause_job(&self.config, backend_id)
            } else {
                cron::resume_job(&self.config, backend_id)
            }
            .map(|_| ())
        };

        match operation {
            Ok(_) => ToolResult {
                success: true,
                output: if pause {
                    format!("Paused job {id}")
                } else {
                    format!("Resumed job {id}")
                },
                error: None,
                metadata: None,
            },
            Err(error) => ToolResult {
                success: false,
                output: String::new(),
                error: Some(error.to_string()),
                metadata: None,
            },
        }
    }

    async fn handle_run_now(&self, id: &str) -> Result<ToolResult> {
        let (kind, backend_id) = self.parse_automation_id(id);
        if kind == "heartbeat" {
            let task = crate::heartbeat::tasks::get_task(&self.config.workspace_dir, backend_id)?;
            let output = crate::daemon::run_owned_heartbeat_task(
                &self.config,
                task.owner_agent_id.as_deref(),
                format!("[Heartbeat Task] {}", task.prompt),
            )
            .await?;
            let _ = crate::heartbeat::tasks::record_task_run(
                &self.config.workspace_dir,
                backend_id,
                true,
                &output,
            )?;
            return Ok(ToolResult {
                success: true,
                output,
                error: None,
                metadata: None,
            });
        }

        let job = cron::get_job(&self.config, backend_id)?;
        let started_at = Utc::now();
        let (success, output) = cron::scheduler::execute_job_now(&self.config, &job).await;
        let finished_at = Utc::now();
        let duration_ms = (finished_at - started_at).num_milliseconds();
        let status = if success { "ok" } else { "error" };
        let _ = cron::record_run(
            &self.config,
            &job.id,
            started_at,
            finished_at,
            status,
            Some(&output),
            duration_ms,
        );
        let _ = cron::record_last_run(&self.config, &job.id, finished_at, success, &output);
        Ok(ToolResult {
            success,
            output,
            error: (!success).then(|| "Automation run failed".to_string()),
            metadata: None,
        })
    }

    fn handle_runs(&self, id: &str) -> Result<ToolResult> {
        let (kind, backend_id) = self.parse_automation_id(id);
        if kind == "heartbeat" {
            let task = crate::heartbeat::tasks::get_task(&self.config.workspace_dir, backend_id)?;
            let runs = task
                .last_run
                .map(|last_run| {
                    json!([{
                        "id": format!("heartbeat-run-{}", task.id),
                        "started_at": last_run.to_rfc3339(),
                        "finished_at": last_run.to_rfc3339(),
                        "status": task.last_status.unwrap_or_else(|| "ok".to_string()),
                        "output": task.last_output,
                    }])
                })
                .unwrap_or_else(|| json!([]));
            return Ok(ToolResult {
                success: true,
                output: serde_json::to_string_pretty(&runs)?,
                error: None,
                metadata: None,
            });
        }

        let runs = cron::list_runs(&self.config, backend_id, 20)?;
        Ok(ToolResult {
            success: true,
            output: serde_json::to_string_pretty(&runs)?,
            error: None,
            metadata: None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::security::AutonomyLevel;
    use tempfile::TempDir;

    async fn test_setup() -> (TempDir, Config, Arc<SecurityPolicy>) {
        let tmp = TempDir::new().unwrap();
        let config = Config {
            workspace_dir: tmp.path().join("workspace"),
            config_path: tmp.path().join("config.toml"),
            ..Config::default()
        };
        tokio::fs::create_dir_all(&config.workspace_dir)
            .await
            .unwrap();
        let security = Arc::new(SecurityPolicy::from_config(
            &config.autonomy,
            &config.workspace_dir,
        ));
        (tmp, config, security)
    }

    #[tokio::test]
    async fn tool_name_and_schema() {
        let (_tmp, config, security) = test_setup().await;
        let tool = ScheduleTool::new(security, config);
        assert_eq!(tool.name(), "schedule");
        let schema = tool.parameters_schema();
        assert!(schema["properties"]["action"].is_object());
    }

    #[tokio::test]
    async fn list_empty() {
        let (_tmp, config, security) = test_setup().await;
        let tool = ScheduleTool::new(security, config);

        let result = tool.execute(json!({"action": "list"})).await.unwrap();
        assert!(result.success);
        assert!(result.output.contains("No scheduled jobs"));
    }

    #[tokio::test]
    async fn create_get_and_cancel_roundtrip() {
        let (_tmp, config, security) = test_setup().await;
        let tool = ScheduleTool::new(security, config);

        let create = tool
            .execute(json!({
                "action": "create",
                "expression": "*/5 * * * *",
                "command": "echo hello"
            }))
            .await
            .unwrap();
        assert!(create.success);
        assert!(create.output.contains("Created recurring job"));

        let list = tool.execute(json!({"action": "list"})).await.unwrap();
        assert!(list.success);
        assert!(list.output.contains("echo hello"));

        let id = create.output.split_whitespace().nth(3).unwrap();

        let get = tool
            .execute(json!({"action": "get", "id": id}))
            .await
            .unwrap();
        assert!(get.success);
        assert!(get.output.contains("echo hello"));

        let cancel = tool
            .execute(json!({"action": "cancel", "id": id}))
            .await
            .unwrap();
        assert!(cancel.success);
    }

    #[tokio::test]
    async fn once_and_pause_resume_aliases_work() {
        let (_tmp, config, security) = test_setup().await;
        let tool = ScheduleTool::new(security, config);

        let once = tool
            .execute(json!({
                "action": "once",
                "delay": "30m",
                "command": "echo delayed"
            }))
            .await
            .unwrap();
        assert!(once.success);

        let add = tool
            .execute(json!({
                "action": "add",
                "expression": "*/10 * * * *",
                "command": "echo recurring"
            }))
            .await
            .unwrap();
        assert!(add.success);

        let id = add.output.split_whitespace().nth(3).unwrap();
        let pause = tool
            .execute(json!({"action": "pause", "id": id}))
            .await
            .unwrap();
        assert!(pause.success);

        let resume = tool
            .execute(json!({"action": "resume", "id": id}))
            .await
            .unwrap();
        assert!(resume.success);
    }

    #[tokio::test]
    async fn readonly_blocks_mutating_actions() {
        let tmp = TempDir::new().unwrap();
        let config = Config {
            workspace_dir: tmp.path().join("workspace"),
            config_path: tmp.path().join("config.toml"),
            autonomy: crate::config::AutonomyConfig {
                level: AutonomyLevel::ReadOnly,
                ..Default::default()
            },
            ..Config::default()
        };
        tokio::fs::create_dir_all(&config.workspace_dir)
            .await
            .unwrap();
        let security = Arc::new(SecurityPolicy::from_config(
            &config.autonomy,
            &config.workspace_dir,
        ));

        let tool = ScheduleTool::new(security, config);

        let blocked = tool
            .execute(json!({
                "action": "create",
                "expression": "* * * * *",
                "command": "echo blocked"
            }))
            .await
            .unwrap();
        assert!(!blocked.success);
        assert!(blocked.error.as_deref().unwrap().contains("read-only"));

        let list = tool.execute(json!({"action": "list"})).await.unwrap();
        assert!(list.success);
    }

    #[tokio::test]
    async fn rate_limit_blocks_create_action() {
        let tmp = TempDir::new().unwrap();
        let config = Config {
            workspace_dir: tmp.path().join("workspace"),
            config_path: tmp.path().join("config.toml"),
            autonomy: crate::config::AutonomyConfig {
                level: AutonomyLevel::Full,
                max_actions_per_hour: 0,
                ..Default::default()
            },
            ..Config::default()
        };
        tokio::fs::create_dir_all(&config.workspace_dir)
            .await
            .unwrap();
        let security = Arc::new(SecurityPolicy::from_config(
            &config.autonomy,
            &config.workspace_dir,
        ));
        let tool = ScheduleTool::new(security, config);

        let blocked = tool
            .execute(json!({
                "action": "create",
                "expression": "*/5 * * * *",
                "command": "echo blocked-by-rate-limit"
            }))
            .await
            .unwrap();
        assert!(!blocked.success);
        assert!(blocked
            .error
            .as_deref()
            .unwrap_or_default()
            .contains("Rate limit exceeded"));

        let list = tool.execute(json!({"action": "list"})).await.unwrap();
        assert!(list.success);
        assert!(list.output.contains("No scheduled jobs"));
    }

    #[tokio::test]
    async fn rate_limit_blocks_cancel_and_keeps_job() {
        let tmp = TempDir::new().unwrap();
        let config = Config {
            workspace_dir: tmp.path().join("workspace"),
            config_path: tmp.path().join("config.toml"),
            autonomy: crate::config::AutonomyConfig {
                level: AutonomyLevel::Full,
                max_actions_per_hour: 1,
                ..Default::default()
            },
            ..Config::default()
        };
        tokio::fs::create_dir_all(&config.workspace_dir)
            .await
            .unwrap();
        let security = Arc::new(SecurityPolicy::from_config(
            &config.autonomy,
            &config.workspace_dir,
        ));
        let tool = ScheduleTool::new(security, config);

        let create = tool
            .execute(json!({
                "action": "create",
                "expression": "*/5 * * * *",
                "command": "echo keep-me"
            }))
            .await
            .unwrap();
        assert!(create.success);
        let id = create.output.split_whitespace().nth(3).unwrap();

        let cancel = tool
            .execute(json!({"action": "cancel", "id": id}))
            .await
            .unwrap();
        assert!(!cancel.success);
        assert!(cancel
            .error
            .as_deref()
            .unwrap_or_default()
            .contains("Rate limit exceeded"));

        let get = tool
            .execute(json!({"action": "get", "id": id}))
            .await
            .unwrap();
        assert!(get.success);
        assert!(get.output.contains("echo keep-me"));
    }

    #[tokio::test]
    async fn unknown_action_returns_failure() {
        let (_tmp, config, security) = test_setup().await;
        let tool = ScheduleTool::new(security, config);

        let result = tool.execute(json!({"action": "explode"})).await.unwrap();
        assert!(!result.success);
        assert!(result.error.as_deref().unwrap().contains("Unknown action"));
    }

    #[tokio::test]
    async fn mutating_actions_fail_when_cron_disabled() {
        let tmp = TempDir::new().unwrap();
        let mut config = Config {
            workspace_dir: tmp.path().join("workspace"),
            config_path: tmp.path().join("config.toml"),
            ..Config::default()
        };
        config.cron.enabled = false;
        std::fs::create_dir_all(&config.workspace_dir).unwrap();
        let security = Arc::new(SecurityPolicy::from_config(
            &config.autonomy,
            &config.workspace_dir,
        ));
        let tool = ScheduleTool::new(security, config);

        let create = tool
            .execute(json!({
                "action": "create",
                "expression": "*/5 * * * *",
                "command": "echo hello"
            }))
            .await
            .unwrap();

        assert!(!create.success);
        assert!(create
            .error
            .as_deref()
            .unwrap_or_default()
            .contains("cron is disabled"));
    }

    #[tokio::test]
    async fn create_blocks_disallowed_command() {
        let tmp = TempDir::new().unwrap();
        let mut config = Config {
            workspace_dir: tmp.path().join("workspace"),
            config_path: tmp.path().join("config.toml"),
            ..Config::default()
        };
        config.autonomy.level = AutonomyLevel::Supervised;
        config.autonomy.allowed_commands = vec!["echo".into()];
        std::fs::create_dir_all(&config.workspace_dir).unwrap();
        let security = Arc::new(SecurityPolicy::from_config(
            &config.autonomy,
            &config.workspace_dir,
        ));
        let tool = ScheduleTool::new(security, config);

        let result = tool
            .execute(json!({
                "action": "create",
                "expression": "*/5 * * * *",
                "command": "curl https://example.com"
            }))
            .await
            .unwrap();

        assert!(!result.success);
        assert!(result
            .error
            .as_deref()
            .unwrap_or_default()
            .contains("not allowed"));
    }

    #[tokio::test]
    async fn medium_risk_create_requires_approval() {
        let tmp = TempDir::new().unwrap();
        let mut config = Config {
            workspace_dir: tmp.path().join("workspace"),
            config_path: tmp.path().join("config.toml"),
            ..Config::default()
        };
        config.autonomy.level = AutonomyLevel::Supervised;
        config.autonomy.allowed_commands = vec!["touch".into()];
        std::fs::create_dir_all(&config.workspace_dir).unwrap();
        let security = Arc::new(SecurityPolicy::from_config(
            &config.autonomy,
            &config.workspace_dir,
        ));
        let tool = ScheduleTool::new(security, config);

        let denied = tool
            .execute(json!({
                "action": "create",
                "expression": "*/5 * * * *",
                "command": "touch schedule-policy-test"
            }))
            .await
            .unwrap();
        assert!(!denied.success);
        assert!(denied
            .error
            .as_deref()
            .unwrap_or_default()
            .contains("explicit approval"));

        let approved = tool
            .execute(json!({
                "action": "create",
                "expression": "*/5 * * * *",
                "command": "touch schedule-policy-test",
                "approved": true
            }))
            .await
            .unwrap();
        assert!(approved.success, "{:?}", approved.error);
    }
}
