use super::traits::{Tool, ToolResult, ToolResultMetadata};
use anyhow::{anyhow, Context};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::sync::Mutex;
use uuid::Uuid;

const DEFAULT_TIMEOUT_MS: u64 = 20_000;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HeadlessRuntimeStatus {
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl HeadlessRuntimeStatus {
    fn new(status: impl Into<String>, detail: Option<String>) -> Self {
        Self {
            status: status.into(),
            detail,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HeadlessXSessionStatus {
    pub session: String,
    pub authenticated: bool,
    pub url: String,
    pub title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
}

fn normalize_profile_name(config: &crate::config::Config, requested: Option<&str>) -> String {
    let trimmed = requested
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("primary");
    if trimmed.eq_ignore_ascii_case("primary") {
        return "primary".into();
    }
    if config.agents.contains_key(trimmed) {
        return trimmed.to_string();
    }
    trimmed.to_string()
}

pub struct BrowserHeadlessTool {
    command: String,
    args: Vec<String>,
    cwd: Option<PathBuf>,
    state_dir: PathBuf,
    default_session: String,
    default_headless: bool,
    default_timeout_ms: u64,
    process: Arc<Mutex<Option<HeadlessSidecarProcess>>>,
    readiness: Arc<Mutex<HeadlessRuntimeStatus>>,
    last_session: Arc<Mutex<Option<String>>>,
}

struct HeadlessSidecarProcess {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

#[derive(Debug, Serialize)]
struct SidecarRequest {
    id: String,
    action: String,
    session: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    selector: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    script: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    wait_until: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    replace: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    full_page: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    headless: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    timeout_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    username: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    password: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    email: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    platform: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    interactive: Option<bool>,
}

#[derive(Debug, Deserialize)]
struct SidecarResponse {
    ok: bool,
    #[allow(dead_code)]
    id: Option<String>,
    #[serde(default)]
    output: String,
    #[serde(default)]
    error: Option<String>,
    #[serde(default)]
    data: Option<Value>,
}

impl BrowserHeadlessTool {
    pub fn new(
        command: String,
        args: Vec<String>,
        cwd: Option<PathBuf>,
        state_dir: PathBuf,
        default_session: Option<String>,
        default_headless: bool,
        default_timeout_ms: u64,
    ) -> Self {
        Self {
            command,
            args,
            cwd,
            state_dir,
            default_session: default_session.unwrap_or_else(|| "default".to_string()),
            default_headless,
            default_timeout_ms,
            process: Arc::new(Mutex::new(None)),
            readiness: Arc::new(Mutex::new(HeadlessRuntimeStatus::new(
                "not_installed",
                Some("Headless sidecar has not been checked yet.".into()),
            ))),
            last_session: Arc::new(Mutex::new(None)),
        }
    }

    async fn ensure_process<'a>(
        &self,
        guard: &'a mut Option<HeadlessSidecarProcess>,
    ) -> anyhow::Result<&'a mut HeadlessSidecarProcess> {
        let needs_spawn = match guard.as_mut() {
            Some(proc) => proc
                .child
                .try_wait()
                .context("checking browser headless sidecar status")?
                .is_some(),
            None => true,
        };

        if needs_spawn {
            *guard = Some(self.spawn_process().await?);
        }

        guard
            .as_mut()
            .ok_or_else(|| anyhow!("browser headless sidecar failed to start"))
    }

    async fn spawn_process(&self) -> anyhow::Result<HeadlessSidecarProcess> {
        std::fs::create_dir_all(&self.state_dir)
            .with_context(|| format!("creating browser headless state dir {:?}", self.state_dir))?;
        self.ensure_sidecar_runtime().await?;

        let mut command = Command::new(&self.command);
        command.args(&self.args);
        command.stdin(Stdio::piped());
        command.stdout(Stdio::piped());
        command.stderr(Stdio::inherit());
        command.env("ZEROCLAW_HEADLESS_STATE_DIR", &self.state_dir);
        if let Some(cwd) = self.cwd.as_ref() {
            command.current_dir(cwd);
        }

        let mut child = command.spawn().with_context(|| {
            format!(
                "spawning browser headless sidecar with command '{}' and args {:?}",
                self.command, self.args
            )
        })?;

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("sidecar stdin unavailable"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow!("sidecar stdout unavailable"))?;

        Ok(HeadlessSidecarProcess {
            child,
            stdin,
            stdout: BufReader::new(stdout),
        })
    }

    fn social_x_session(agent_name: &str) -> String {
        let normalized = agent_name
            .trim()
            .chars()
            .map(|ch| {
                if ch.is_ascii_alphanumeric() {
                    ch.to_ascii_lowercase()
                } else {
                    '-'
                }
            })
            .collect::<String>();
        format!("social-x-{normalized}")
    }

    fn credentials_complete(
        creds: &crate::config::schema::SocialTwitterCredentials,
    ) -> Option<(String, String, String)> {
        let username = creds.username.clone().unwrap_or_default();
        let password = creds.password.clone().unwrap_or_default();
        let email = creds.email.clone().unwrap_or_default();
        if username.trim().is_empty() || password.trim().is_empty() || email.trim().is_empty() {
            None
        } else {
            Some((username, password, email))
        }
    }

    fn normalize_handle(value: &str) -> String {
        value.trim().trim_start_matches('@').to_ascii_lowercase()
    }

    async fn resolve_x_credentials(
        &self,
        agent_name: Option<&str>,
    ) -> anyhow::Result<(String, String, String, String)> {
        let config = crate::config::Config::load_or_init().await?;
        let requested_agent_name = agent_name.map(str::trim).filter(|value| !value.is_empty());
        let target_agent = requested_agent_name.unwrap_or("primary");

        let mut candidates: Vec<(String, crate::config::schema::SocialTwitterCredentials)> =
            Vec::new();
        if let Some(creds) = config.agent.social_accounts.twitter.clone() {
            candidates.push(("primary".into(), creds));
        }
        let mut delegate_names: Vec<_> = config.agents.keys().cloned().collect();
        delegate_names.sort();
        for name in delegate_names {
            if let Some(creds) = config
                .agents
                .get(&name)
                .and_then(|agent| agent.social_accounts.twitter.clone())
            {
                candidates.push((name, creds));
            }
        }

        for (profile_name, creds) in &candidates {
            if profile_name == target_agent {
                if let Some((username, password, email)) = Self::credentials_complete(creds) {
                    return Ok((profile_name.clone(), username, password, email));
                }
                if requested_agent_name.is_some() {
                    return Err(anyhow!(
                        "browser_headless X credentials for agent '{}' are incomplete. Username, password, and email are all required.",
                        profile_name
                    ));
                }
                break;
            }
        }

        let normalized_target = Self::normalize_handle(target_agent);
        for (profile_name, creds) in &candidates {
            let saved_username = creds.username.clone().unwrap_or_default();
            if !saved_username.trim().is_empty()
                && Self::normalize_handle(&saved_username) == normalized_target
            {
                if let Some((username, password, email)) = Self::credentials_complete(creds) {
                    return Ok((profile_name.clone(), username, password, email));
                }
                return Err(anyhow!(
                    "browser_headless found profile '{}' for handle '{}', but the saved X credentials are incomplete.",
                    profile_name,
                    target_agent
                ));
            }
        }

        let complete_profiles: Vec<_> = candidates
            .iter()
            .filter_map(|(profile_name, creds)| {
                Self::credentials_complete(creds).map(|(username, password, email)| {
                    (profile_name.clone(), username, password, email)
                })
            })
            .collect();
        if complete_profiles.len() == 1 {
            let (profile_name, username, password, email) = complete_profiles[0].clone();
            return Ok((profile_name, username, password, email));
        }

        Err(anyhow!(
            "browser_headless requires complete X credentials for agent or handle '{}'. Save them in Agent Accounts first.",
            target_agent
        ))
    }

    fn sidecar_dir(&self) -> anyhow::Result<PathBuf> {
        let base_dir = if let Some(cwd) = self.cwd.clone() {
            cwd
        } else {
            std::env::current_dir().context("resolving current dir for browser headless sidecar")?
        };
        let script_arg = self
            .args
            .iter()
            .find(|arg| arg.ends_with(".mjs") || arg.ends_with(".js"))
            .cloned()
            .unwrap_or_else(|| ".".into());
        let script_path = base_dir.join(script_arg);
        let sidecar_dir = script_path.parent().map(PathBuf::from).unwrap_or(base_dir);
        Ok(sidecar_dir)
    }

    async fn run_bootstrap_command(
        &self,
        program: &str,
        args: &[&str],
        cwd: &PathBuf,
        label: &str,
    ) -> anyhow::Result<()> {
        let output = Command::new(program)
            .args(args)
            .current_dir(cwd)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output()
            .await
            .with_context(|| format!("running {label} for browser headless sidecar"))?;
        if output.status.success() {
            return Ok(());
        }

        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(anyhow!(
            "{label} failed with status {}.\nstdout:\n{}\nstderr:\n{}",
            output.status,
            stdout.trim(),
            stderr.trim()
        ))
    }

    async fn inspect_sidecar_runtime(&self) -> anyhow::Result<HeadlessRuntimeStatus> {
        let sidecar_dir = self.sidecar_dir()?;
        let playwright_dir = sidecar_dir.join("node_modules/playwright");
        let stagehand_dir = sidecar_dir.join("node_modules/@browserbasehq/stagehand");
        if !playwright_dir.exists() || !stagehand_dir.exists() {
            return Ok(HeadlessRuntimeStatus::new(
                "not_installed",
                Some(format!(
                    "Missing sidecar dependencies in {}",
                    sidecar_dir.join("node_modules").display()
                )),
            ));
        }

        let smoke = Command::new("node")
            .args([
                "--input-type=module",
                "-e",
                "import { chromium } from 'playwright'; const browser = await chromium.launch({ headless: true }); await browser.close();",
            ])
            .current_dir(&sidecar_dir)
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .output()
            .await
            .context("checking Playwright Chromium runtime")?;
        if smoke.status.success() {
            return Ok(HeadlessRuntimeStatus::new(
                "ready",
                Some("Playwright sidecar runtime is healthy.".into()),
            ));
        }

        let stderr = String::from_utf8_lossy(&smoke.stderr).trim().to_string();
        Ok(HeadlessRuntimeStatus::new(
            "failed",
            Some(if stderr.is_empty() {
                "Playwright Chromium runtime is not installed.".into()
            } else {
                stderr
            }),
        ))
    }

    async fn ensure_sidecar_runtime(&self) -> anyhow::Result<()> {
        let sidecar_dir = self.sidecar_dir()?;
        let inspected = self.inspect_sidecar_runtime().await?;
        {
            let mut readiness = self.readiness.lock().await;
            *readiness = inspected.clone();
        }
        if inspected.status == "ready" {
            return Ok(());
        }

        let playwright_dir = sidecar_dir.join("node_modules/playwright");
        let stagehand_dir = sidecar_dir.join("node_modules/@browserbasehq/stagehand");
        if !playwright_dir.exists() || !stagehand_dir.exists() {
            {
                let mut readiness = self.readiness.lock().await;
                *readiness = HeadlessRuntimeStatus::new(
                    "installing",
                    Some("Installing headless browser sidecar dependencies.".into()),
                );
            }
            self.run_bootstrap_command(
                "npm",
                &["install", "--no-fund", "--no-audit"],
                &sidecar_dir,
                "npm install",
            )
            .await?;
        }

        {
            let mut readiness = self.readiness.lock().await;
            *readiness = HeadlessRuntimeStatus::new(
                "installing",
                Some("Installing Playwright Chromium runtime.".into()),
            );
        }
        self.run_bootstrap_command(
            "npx",
            &["playwright", "install", "chromium"],
            &sidecar_dir,
            "playwright install chromium",
        )
        .await?;
        let mut readiness = self.readiness.lock().await;
        *readiness = HeadlessRuntimeStatus::new(
            "ready",
            Some("Playwright sidecar runtime is healthy.".into()),
        );
        Ok(())
    }

    async fn fallback_session_name(&self, requested: Option<&str>) -> String {
        if let Some(session) = requested
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(ToString::to_string)
        {
            return session;
        }
        if let Some(session) = self.last_session.lock().await.clone() {
            return session;
        }
        self.default_session.clone()
    }

    fn is_x_url(url: &str) -> bool {
        let lower = url.trim().to_ascii_lowercase();
        lower.contains("://x.com/")
            || lower.ends_with("://x.com")
            || lower.contains("://www.x.com/")
            || lower.ends_with("://www.x.com")
    }

    fn is_social_x_session(session: &str) -> bool {
        session.trim().to_ascii_lowercase().starts_with("social-x-")
    }

    async fn resolve_x_session_name(&self, requested_agent: Option<&str>) -> Option<String> {
        if let Ok((profile_name, _, _, _)) = self.resolve_x_credentials(requested_agent).await {
            return Some(Self::social_x_session(&profile_name));
        }

        let config = crate::config::Config::load_or_init().await.ok()?;
        let requested = requested_agent
            .map(str::trim)
            .filter(|value| !value.is_empty());
        let normalized_requested = requested.map(Self::normalize_handle);

        if let Some(agent_name) = requested {
            if agent_name.eq_ignore_ascii_case("primary") {
                return Some(Self::social_x_session("primary"));
            }
            if config.agents.contains_key(agent_name) {
                return Some(Self::social_x_session(agent_name));
            }
        }

        if let Some(target_handle) = normalized_requested {
            if let Some(saved_username) = config
                .agent
                .social_accounts
                .twitter
                .as_ref()
                .and_then(|creds| creds.username.as_deref())
            {
                if !saved_username.trim().is_empty()
                    && Self::normalize_handle(saved_username) == target_handle
                {
                    return Some(Self::social_x_session("primary"));
                }
            }

            let mut delegate_names: Vec<_> = config.agents.keys().cloned().collect();
            delegate_names.sort();
            for name in delegate_names {
                if let Some(saved_username) = config
                    .agents
                    .get(&name)
                    .and_then(|agent| agent.social_accounts.twitter.as_ref())
                    .and_then(|creds| creds.username.as_deref())
                {
                    if !saved_username.trim().is_empty()
                        && Self::normalize_handle(saved_username) == target_handle
                    {
                        return Some(Self::social_x_session(&name));
                    }
                }
            }
        }

        let mut candidates = Vec::new();
        if config.agent.social_accounts.twitter.is_some() {
            candidates.push("primary".to_string());
        }
        let mut delegate_names: Vec<_> = config
            .agents
            .iter()
            .filter_map(|(name, agent)| {
                agent.social_accounts.twitter.as_ref().map(|_| name.clone())
            })
            .collect();
        delegate_names.sort();
        candidates.extend(delegate_names);
        if candidates.len() == 1 {
            return Some(Self::social_x_session(&candidates[0]));
        }

        None
    }

    async fn resolve_action_session(&self, action: &str, args: &Value) -> String {
        let platform_is_x = args
            .get("platform")
            .and_then(Value::as_str)
            .is_some_and(|value| value.trim().eq_ignore_ascii_case("x"));
        let url_is_x = args
            .get("url")
            .and_then(Value::as_str)
            .is_some_and(Self::is_x_url);
        let x_specific_action = matches!(
            action,
            "bootstrap_x_session"
                | "bootstrap_x_session_interactive"
                | "import_x_session_from_chrome"
        );
        let is_x_target = platform_is_x || url_is_x || x_specific_action;
        let sticky_social_x_session = self
            .last_session
            .lock()
            .await
            .clone()
            .filter(|session| Self::is_social_x_session(session));

        if let Some(explicit_session) = args.get("session").and_then(Value::as_str) {
            let explicit_session = self.fallback_session_name(Some(explicit_session)).await;
            if !Self::is_social_x_session(&explicit_session) {
                if let Some(sticky_session) = sticky_social_x_session.clone() {
                    if is_x_target {
                        return sticky_session;
                    }
                    let action_is_navigation_or_observation = matches!(
                        action,
                        "snapshot"
                            | "get_text"
                            | "run_script"
                            | "wait_for"
                            | "click"
                            | "type"
                            | "save_screenshot"
                            | "save_trace"
                            | "open_url"
                            | "navigate_url"
                    );
                    if action_is_navigation_or_observation {
                        return sticky_session;
                    }
                }
            }
            if is_x_target && !Self::is_social_x_session(&explicit_session) {
                if let Some(session) = self
                    .resolve_x_session_name(args.get("agent_name").and_then(Value::as_str))
                    .await
                {
                    return session;
                }
            }
            return explicit_session;
        }

        if is_x_target {
            if let Some(session) = self
                .resolve_x_session_name(args.get("agent_name").and_then(Value::as_str))
                .await
            {
                return session;
            }
        }

        self.fallback_session_name(None).await
    }

    fn maybe_artifact_path(
        &self,
        requested: Option<&str>,
        prefix: &str,
        extension: &str,
    ) -> String {
        requested
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(|value| value.to_string())
            .unwrap_or_else(|| {
                self.state_dir
                    .join(format!("{prefix}-{}.{}", Uuid::new_v4(), extension))
                    .to_string_lossy()
                    .into_owned()
            })
    }

    async fn dispatch(&self, request: SidecarRequest) -> anyhow::Result<ToolResult> {
        let session_name = request.session.clone();
        {
            let mut last_session = self.last_session.lock().await;
            *last_session = Some(session_name.clone());
        }
        let mut process_guard = self.process.lock().await;
        let process = self.ensure_process(&mut process_guard).await?;

        let mut request_line = serde_json::to_string(&request)?;
        request_line.push('\n');
        process
            .stdin
            .write_all(request_line.as_bytes())
            .await
            .context("writing browser headless request")?;
        process
            .stdin
            .flush()
            .await
            .context("flushing browser headless request")?;

        let mut response_line = String::new();
        let bytes = process
            .stdout
            .read_line(&mut response_line)
            .await
            .context("reading browser headless response")?;
        if bytes == 0 {
            *process_guard = None;
            return Err(anyhow!("browser headless sidecar exited before responding"));
        }

        let response: SidecarResponse = serde_json::from_str(response_line.trim())
            .context("parsing browser headless response JSON")?;
        let metadata = response.data.map(|mut data| {
            if let Value::Object(ref mut object) = data {
                object.insert("session".into(), Value::String(session_name));
            }
            ToolResultMetadata {
                extra: Some(data),
                ..ToolResultMetadata::default()
            }
        });
        Ok(ToolResult {
            success: response.ok,
            output: response.output,
            error: response.error,
            metadata,
        })
    }

    async fn status_result(&self, args: &Value) -> anyhow::Result<ToolResult> {
        let mut runtime = self.inspect_sidecar_runtime().await?;
        {
            let mut readiness = self.readiness.lock().await;
            *readiness = runtime.clone();
        }

        let platform = args
            .get("platform")
            .and_then(Value::as_str)
            .map(|value| value.trim().to_ascii_lowercase());
        if platform.as_deref() != Some("x") {
            let payload = serde_json::to_value(&runtime)?;
            return Ok(ToolResult {
                success: runtime.status == "ready",
                output: runtime
                    .detail
                    .clone()
                    .unwrap_or_else(|| runtime.status.clone()),
                error: (runtime.status != "ready").then(|| runtime.status.clone()),
                metadata: Some(ToolResultMetadata {
                    extra: Some(payload),
                    ..ToolResultMetadata::default()
                }),
            });
        }

        let requested_agent = args.get("agent_name").and_then(Value::as_str);
        let (profile_name, _, _, _) = match self.resolve_x_credentials(requested_agent).await {
            Ok(value) => value,
            Err(error) => {
                let payload = json!({
                    "runtime": runtime,
                    "session": requested_agent
                        .map(Self::social_x_session)
                        .unwrap_or_else(|| Self::social_x_session("primary")),
                    "x": {
                        "authenticated": false,
                        "url": "",
                        "title": "",
                        "detail": error.to_string(),
                    }
                });
                return Ok(ToolResult {
                    success: false,
                    output: error.to_string(),
                    error: Some(error.to_string()),
                    metadata: Some(ToolResultMetadata {
                        extra: Some(payload),
                        ..ToolResultMetadata::default()
                    }),
                });
            }
        };
        let session = Self::social_x_session(&profile_name);
        let result = self
            .dispatch(SidecarRequest {
                id: Uuid::new_v4().to_string(),
                action: "x_status".into(),
                session: session.clone(),
                url: None,
                selector: None,
                text: None,
                script: None,
                path: None,
                wait_until: None,
                ms: None,
                replace: None,
                full_page: None,
                headless: Some(self.default_headless),
                timeout_ms: Some(self.default_timeout_ms.max(30_000)),
                username: None,
                password: None,
                email: None,
                platform: Some("x".into()),
                interactive: None,
            })
            .await?;

        let data = result
            .metadata
            .as_ref()
            .and_then(|meta| meta.extra.clone())
            .unwrap_or_else(|| json!({}));
        runtime.detail = Some(format!(
            "Headless runtime {}, X auth checked for '{}'.",
            runtime.status, profile_name
        ));
        let payload = json!({
            "runtime": runtime,
            "session": session,
            "agent_name": profile_name,
            "x": data,
        });
        Ok(ToolResult {
            success: result.success,
            output: result.output,
            error: result.error,
            metadata: Some(ToolResultMetadata {
                extra: Some(payload),
                ..ToolResultMetadata::default()
            }),
        })
    }

    async fn bootstrap_x_session_result(&self, args: &Value) -> anyhow::Result<ToolResult> {
        let requested_agent = args.get("agent_name").and_then(Value::as_str);
        let (profile_name, username, password, email) =
            self.resolve_x_credentials(requested_agent).await?;
        let session = Self::social_x_session(&profile_name);
        self.ensure_sidecar_runtime().await?;
        let interactive = args
            .get("interactive")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        self.dispatch(SidecarRequest {
            id: Uuid::new_v4().to_string(),
            action: if interactive {
                "bootstrap_x_session_interactive".into()
            } else {
                "bootstrap_x_session".into()
            },
            session,
            url: None,
            selector: None,
            text: None,
            script: None,
            path: None,
            wait_until: None,
            ms: None,
            replace: None,
            full_page: None,
            headless: Some(!interactive),
            timeout_ms: Some(if interactive {
                self.default_timeout_ms.max(300_000)
            } else {
                self.default_timeout_ms.max(60_000)
            }),
            username: Some(username),
            password: Some(password),
            email: Some(email),
            platform: Some("x".into()),
            interactive: Some(interactive),
        })
        .await
    }

    async fn import_x_session_from_chrome_result(
        &self,
        args: &Value,
    ) -> anyhow::Result<ToolResult> {
        let config = crate::config::Config::load_or_init().await?;
        let profile_name =
            normalize_profile_name(&config, args.get("agent_name").and_then(Value::as_str));
        let session = Self::social_x_session(&profile_name);
        self.ensure_sidecar_runtime().await?;
        self.dispatch(SidecarRequest {
            id: Uuid::new_v4().to_string(),
            action: "import_x_session_from_chrome".into(),
            session,
            url: None,
            selector: None,
            text: None,
            script: None,
            path: None,
            wait_until: None,
            ms: None,
            replace: None,
            full_page: None,
            headless: Some(true),
            timeout_ms: Some(self.default_timeout_ms.max(60_000)),
            username: None,
            password: None,
            email: None,
            platform: Some("x".into()),
            interactive: None,
        })
        .await
    }
}

#[async_trait]
impl Tool for BrowserHeadlessTool {
    fn name(&self) -> &str {
        "browser_headless"
    }

    fn description(&self) -> &str {
        "Primary headless browser tool for normal web automation. Uses a local Playwright sidecar with a Stagehand-ready interface, persistent profiles, screenshots, and trace capture. Prefer this before browser_ext for browser work unless the task explicitly needs the user's live logged-in browser session."
    }

    fn parameters_schema(&self) -> Value {
        json!({
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "open_url",
                        "navigate_url",
                        "snapshot",
                        "click",
                        "type",
                        "wait_for",
                        "get_text",
                        "run_script",
                        "save_screenshot",
                        "save_trace",
                        "export_x_cookies",
                        "status",
                        "bootstrap_x_session",
                        "bootstrap_x_session_interactive",
                        "import_x_session_from_chrome",
                        "act",
                        "extract",
                        "observe"
                    ]
                },
                "session": {
                    "type": "string",
                    "description": "Named persistent browser session. Reuse the same session across related calls in one task."
                },
                "url": {
                    "type": "string",
                    "description": "URL for open_url or navigate_url"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector for click, type, get_text, or wait_for"
                },
                "text": {
                    "type": "string",
                    "description": "Text for type or wait_for"
                },
                "script": {
                    "type": "string",
                    "description": "JavaScript expression or snippet for run_script"
                },
                "ms": {
                    "type": "integer",
                    "description": "Milliseconds for wait_for"
                },
                "replace": {
                    "type": "boolean",
                    "description": "Clear existing text before typing"
                },
                "path": {
                    "type": "string",
                    "description": "Optional output path for save_screenshot or save_trace"
                },
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the full page when saving a screenshot"
                },
                "headless": {
                    "type": "boolean",
                    "description": "Override the default headless setting for this session"
                },
                "agent_name": {
                    "type": "string",
                    "description": "Optional AgentHQ agent name or saved X handle for social session status/bootstrap."
                },
                "platform": {
                    "type": "string",
                    "enum": ["x"],
                    "description": "Social platform for status checks or exported cookies. Currently only 'x' is supported."
                },
                "interactive": {
                    "type": "boolean",
                    "description": "For X bootstrap. When true, launch a headed persistent browser and wait for authentication instead of attempting true headless login."
                },
                "mode": {
                    "type": "string",
                    "enum": ["headless", "interactive", "import_chrome"],
                    "description": "Optional X setup mode hint for callers or GUI flows."
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Per-call timeout in milliseconds"
                },
                "wait_until": {
                    "type": "string",
                    "description": "Navigation wait strategy for open_url",
                    "enum": ["load", "domcontentloaded", "networkidle", "commit"]
                }
            },
            "required": ["action"]
        })
    }

    async fn execute(&self, args: Value) -> anyhow::Result<ToolResult> {
        let action = args
            .get("action")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("Missing required field: action"))?
            .trim()
            .to_string();
        let headless = args.get("headless").and_then(Value::as_bool);
        let timeout_ms = args
            .get("timeout_ms")
            .and_then(Value::as_u64)
            .or(Some(self.default_timeout_ms));

        if action == "status" {
            return self.status_result(&args).await;
        }
        if action == "bootstrap_x_session" || action == "bootstrap_x_session_interactive" {
            return self.bootstrap_x_session_result(&args).await;
        }
        if action == "import_x_session_from_chrome" {
            return self.import_x_session_from_chrome_result(&args).await;
        }

        let session = self.resolve_action_session(&action, &args).await;

        let request = SidecarRequest {
            id: Uuid::new_v4().to_string(),
            action: action.clone(),
            session,
            url: args
                .get("url")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            selector: args
                .get("selector")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            text: args
                .get("text")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            script: args
                .get("script")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            path: match action.as_str() {
                "save_screenshot" => Some(self.maybe_artifact_path(
                    args.get("path").and_then(Value::as_str),
                    "headless-screenshot",
                    "png",
                )),
                "save_trace" => Some(self.maybe_artifact_path(
                    args.get("path").and_then(Value::as_str),
                    "headless-trace",
                    "zip",
                )),
                _ => args
                    .get("path")
                    .and_then(Value::as_str)
                    .map(ToString::to_string),
            },
            wait_until: args
                .get("wait_until")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            ms: args.get("ms").and_then(Value::as_u64),
            replace: args.get("replace").and_then(Value::as_bool),
            full_page: args.get("full_page").and_then(Value::as_bool),
            headless: Some(headless.unwrap_or(self.default_headless)),
            timeout_ms,
            username: None,
            password: None,
            email: None,
            platform: args
                .get("platform")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            interactive: args.get("interactive").and_then(Value::as_bool),
        };

        self.dispatch(request).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_exposes_primary_actions() {
        let tool = BrowserHeadlessTool::new(
            "node".into(),
            vec!["sidecars/browser-headless/server.mjs".into()],
            None,
            PathBuf::from("/tmp/browser-headless"),
            Some("default".into()),
            true,
            DEFAULT_TIMEOUT_MS,
        );
        let schema = tool.parameters_schema();
        let actions = schema["properties"]["action"]["enum"]
            .as_array()
            .expect("enum array");
        assert!(actions.iter().any(|value| value == "open_url"));
        assert!(actions.iter().any(|value| value == "save_trace"));
    }

    #[test]
    fn artifact_paths_are_generated_under_state_dir() {
        let tool = BrowserHeadlessTool::new(
            "node".into(),
            vec!["sidecars/browser-headless/server.mjs".into()],
            None,
            PathBuf::from("/tmp/browser-headless"),
            Some("default".into()),
            true,
            DEFAULT_TIMEOUT_MS,
        );
        let screenshot = tool.maybe_artifact_path(None, "headless-screenshot", "png");
        assert!(screenshot.contains("/tmp/browser-headless/"));
        assert!(screenshot.ends_with(".png"));
    }
}
