use super::traits::{Tool, ToolResult, ToolResultMetadata};
use anyhow::{anyhow, Context};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, OnceLock};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command};
use tokio::sync::Mutex;
use uuid::Uuid;

const MCP_PROTOCOL_VERSION: &str = "2024-11-05";
const TWITTER_MCP_TIMEOUT: Duration = Duration::from_secs(12);
const TWIKIT_BACKEND_TIMEOUT: Duration = Duration::from_secs(30);

pub struct TwitterMcpTool {
    command: String,
    args: Vec<String>,
    process: Arc<Mutex<Option<McpProcess>>>,
    runtime_dir: PathBuf,
}

struct McpProcess {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    creds_key: String,
    stderr_buffer: Arc<Mutex<String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TwitterHealthStatus {
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backend: Option<String>,
    pub supported_capabilities: TwitterCapabilityMatrix,
}

#[derive(Debug, Clone, Copy)]
enum TwitterBackend {
    McpPrimary,
    TwikitDirect,
}

impl TwitterBackend {
    fn as_str(self) -> &'static str {
        match self {
            Self::McpPrimary => "twitter_client_mcp",
            Self::TwikitDirect => "twikit_direct",
        }
    }
}

#[derive(Debug, Deserialize)]
struct CookieBackendResponse {
    ok: bool,
    #[serde(default)]
    output: String,
    #[serde(default)]
    error: Option<String>,
    #[serde(default)]
    data: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TwitterCapabilityMatrix {
    pub post: bool,
    pub comment: bool,
    pub article: bool,
}

#[derive(Debug, Serialize)]
struct JsonRpcRequest {
    jsonrpc: &'static str,
    id: String,
    method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    params: Option<Value>,
}

#[derive(Debug, Serialize)]
struct JsonRpcNotification {
    jsonrpc: &'static str,
    method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    params: Option<Value>,
}

#[derive(Debug, Deserialize)]
struct JsonRpcResponse {
    #[allow(dead_code)]
    jsonrpc: Option<String>,
    id: Option<Value>,
    result: Option<Value>,
    error: Option<JsonRpcError>,
    method: Option<String>,
}

#[derive(Debug, Deserialize)]
struct JsonRpcError {
    code: i64,
    message: String,
}

impl TwitterMcpTool {
    fn health_cache() -> &'static Mutex<HashMap<String, TwitterHealthStatus>> {
        static CACHE: OnceLock<Mutex<HashMap<String, TwitterHealthStatus>>> = OnceLock::new();
        CACHE.get_or_init(|| Mutex::new(HashMap::new()))
    }

    pub fn new() -> Self {
        Self {
            command: "npx".into(),
            args: vec!["-y".into(), "github:mzkrasner/twitter-client-mcp".into()],
            process: Arc::new(Mutex::new(None)),
            runtime_dir: PathBuf::from(".state/social-direct"),
        }
    }

    fn capabilities() -> TwitterCapabilityMatrix {
        TwitterCapabilityMatrix {
            post: true,
            comment: true,
            article: false,
        }
    }

    fn health(
        status: impl Into<String>,
        detail: Option<String>,
        backend: Option<TwitterBackend>,
    ) -> TwitterHealthStatus {
        TwitterHealthStatus {
            status: status.into(),
            detail,
            backend: backend.map(TwitterBackend::as_str).map(ToString::to_string),
            supported_capabilities: Self::capabilities(),
        }
    }

    async fn cache_health(&self, creds_key: String, status: TwitterHealthStatus) {
        let mut cache = Self::health_cache().lock().await;
        cache.insert(creds_key, status);
    }

    async fn cached_health_for(&self, creds_key: &str) -> Option<TwitterHealthStatus> {
        let cache = Self::health_cache().lock().await;
        cache.get(creds_key).cloned()
    }

    fn classify_startup_failure(detail: &str) -> TwitterHealthStatus {
        let lower = detail.to_ascii_lowercase();
        if lower.contains("twitter basic authentication failed")
            || lower.contains("code\":34")
            || lower.contains("sorry, that page does not exist")
        {
            return Self::health(
                "upstream_login_failed",
                Some(detail.to_string()),
                Some(TwitterBackend::McpPrimary),
            );
        }
        Self::health(
            "mcp_start_failed",
            Some(detail.to_string()),
            Some(TwitterBackend::McpPrimary),
        )
    }

    fn classify_credential_error(detail: &str) -> TwitterHealthStatus {
        if detail.contains("requires credentials") {
            return Self::health("credentials_missing", Some(detail.to_string()), None);
        }
        if detail.contains("incomplete") {
            return Self::health("credentials_incomplete", Some(detail.to_string()), None);
        }
        Self::health("mcp_start_failed", Some(detail.to_string()), None)
    }

    async fn ensure_process<'a>(
        &self,
        guard: &'a mut Option<McpProcess>,
        creds: &ResolvedTwitterCredentials,
    ) -> anyhow::Result<&'a mut McpProcess> {
        let needs_spawn = match guard.as_mut() {
            Some(proc) => {
                proc.child
                    .try_wait()
                    .context("checking twitter MCP process status")?
                    .is_some()
                    || proc.creds_key != creds.cache_key()
            }
            None => true,
        };

        if needs_spawn {
            match self.spawn_process(creds).await {
                Ok(process) => {
                    self.cache_health(
                        creds.cache_key(),
                        Self::health(
                            "ready",
                            Some("twitter_x adapter is healthy.".into()),
                            Some(TwitterBackend::McpPrimary),
                        ),
                    )
                    .await;
                    *guard = Some(process);
                }
                Err(error) => {
                    let status = Self::classify_startup_failure(&error.to_string());
                    self.cache_health(creds.cache_key(), status.clone()).await;
                    return Err(anyhow!(
                        "{}",
                        status
                            .detail
                            .clone()
                            .unwrap_or_else(|| "twitter_x failed to start".into())
                    ));
                }
            }
        }

        guard
            .as_mut()
            .ok_or_else(|| anyhow!("twitter MCP process failed to start"))
    }

    async fn spawn_process(
        &self,
        creds: &ResolvedTwitterCredentials,
    ) -> anyhow::Result<McpProcess> {
        let mut command = Command::new(&self.command);
        command.args(&self.args);
        command.stdin(Stdio::piped());
        command.stdout(Stdio::piped());
        command.stderr(Stdio::piped());
        command.env("TWITTER_USERNAME", &creds.username);
        command.env("TWITTER_PASSWORD", &creds.password);
        command.env("TWITTER_EMAIL", &creds.email);

        let mut child = command.spawn().with_context(|| {
            format!(
                "spawning twitter MCP server with command '{}' and args {:?}",
                self.command, self.args
            )
        })?;

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("twitter MCP stdin unavailable"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow!("twitter MCP stdout unavailable"))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| anyhow!("twitter MCP stderr unavailable"))?;
        let stderr_buffer = Arc::new(Mutex::new(String::new()));
        Self::spawn_stderr_reader(stderr, Arc::clone(&stderr_buffer));

        let mut process = McpProcess {
            child,
            stdin,
            stdout: BufReader::new(stdout),
            creds_key: creds.cache_key(),
            stderr_buffer,
        };

        self.initialize(&mut process).await?;
        Ok(process)
    }

    fn spawn_stderr_reader(stderr: ChildStderr, buffer: Arc<Mutex<String>>) {
        tokio::spawn(async move {
            let mut reader = BufReader::new(stderr);
            loop {
                let mut line = String::new();
                match reader.read_line(&mut line).await {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        let mut collected = buffer.lock().await;
                        collected.push_str(&line);
                    }
                }
            }
        });
    }

    fn social_direct_dir(&self) -> anyhow::Result<PathBuf> {
        let cwd =
            std::env::current_dir().context("resolving current dir for twitter_x fallback")?;
        Ok(cwd.join("sidecars/social-direct"))
    }

    fn social_direct_runtime_dir(&self) -> anyhow::Result<PathBuf> {
        let cwd = std::env::current_dir().context("resolving current dir for twitter_x runtime")?;
        Ok(cwd.join(&self.runtime_dir))
    }

    fn twikit_cookies_path(&self, creds: &ResolvedTwitterCredentials) -> anyhow::Result<PathBuf> {
        let runtime_dir = self.social_direct_runtime_dir()?;
        let slug = creds
            .source
            .chars()
            .map(|ch| {
                if ch.is_ascii_alphanumeric() {
                    ch.to_ascii_lowercase()
                } else {
                    '_'
                }
            })
            .collect::<String>();
        Ok(runtime_dir
            .join("cookies")
            .join(format!("{slug}_x_cookies.json")))
    }

    async fn ensure_cookie_backend_runtime(&self) -> anyhow::Result<PathBuf> {
        let runtime_dir = self.social_direct_runtime_dir()?;
        let venv_dir = runtime_dir.join("venv");
        let python_bin = venv_dir.join("bin/python");
        if python_bin.exists() {
            return Ok(python_bin);
        }

        tokio::fs::create_dir_all(&runtime_dir)
            .await
            .with_context(|| format!("creating twitter_x runtime dir {}", runtime_dir.display()))?;

        let status = Command::new("python3")
            .args(["-m", "venv", venv_dir.to_string_lossy().as_ref()])
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .status()
            .await
            .context("creating twitter_x fallback venv")?;
        if !status.success() {
            return Err(anyhow!(
                "creating twitter_x fallback venv failed with status {status}"
            ));
        }

        let install = Command::new(&python_bin)
            .args(["-m", "pip", "install", "-q", "twikit", "browser-cookie3"])
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .status()
            .await
            .context("installing twitter_x fallback dependencies")?;
        if !install.success() {
            return Err(anyhow!(
                "installing twitter_x fallback dependencies failed with status {install}"
            ));
        }

        Ok(python_bin)
    }

    async fn call_twikit_backend(
        &self,
        action: &str,
        args: Value,
        creds: &ResolvedTwitterCredentials,
        agent_name: Option<&str>,
    ) -> anyhow::Result<ToolResult> {
        let python_bin = self.ensure_cookie_backend_runtime().await?;
        let script = self.social_direct_dir()?.join("x_twikit_client.py");
        let cookies_file = self.twikit_cookies_path(creds)?;
        let exported_cookies = self.headless_x_cookies(agent_name).await.unwrap_or(None);
        if let Some(parent) = cookies_file.parent() {
            tokio::fs::create_dir_all(parent)
                .await
                .with_context(|| format!("creating twikit cookie dir {}", parent.display()))?;
        }
        let request = json!({
            "action": action,
            "account_username": creds.username,
            "password": creds.password,
            "email": creds.email,
            "cookies_file": cookies_file,
            "cookies": exported_cookies,
            "username": args.get("username").and_then(Value::as_str),
            "tweet_id": args.get("tweet_id").and_then(Value::as_str),
            "text": args.get("text").and_then(Value::as_str),
            "in_reply_to_id": args.get("in_reply_to_id").and_then(Value::as_str),
            "query": args.get("query").and_then(Value::as_str),
            "count": args.get("count").and_then(Value::as_u64),
            "search_mode": args.get("search_mode").and_then(Value::as_str),
        });

        let mut child = Command::new(&python_bin)
            .arg(&script)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .with_context(|| format!("spawning twikit_direct backend at {}", script.display()))?;

        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("twikit_direct stdin unavailable"))?;
        let request_line = serde_json::to_vec(&request)?;
        stdin
            .write_all(&request_line)
            .await
            .context("writing twikit_direct request")?;
        stdin.shutdown().await.ok();

        let output = tokio::time::timeout(TWIKIT_BACKEND_TIMEOUT, child.wait_with_output())
            .await
            .map_err(|_| anyhow!("twikit_direct backend timed out after 30 seconds"))?
            .context("waiting for twikit_direct response")?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            return Err(anyhow!(if stderr.is_empty() {
                format!("twikit_direct backend failed with status {}", output.status)
            } else {
                stderr
            }));
        }

        let response: CookieBackendResponse =
            serde_json::from_slice(&output.stdout).context("parsing twikit_direct response")?;
        let mut extra = response.data.unwrap_or_else(|| json!({}));
        if let Value::Object(ref mut object) = extra {
            object.insert(
                "backend".into(),
                Value::String(TwitterBackend::TwikitDirect.as_str().into()),
            );
        }
        Ok(ToolResult {
            success: response.ok,
            output: response.output,
            error: response.error,
            metadata: Some(ToolResultMetadata {
                extra: Some(extra),
                ..ToolResultMetadata::default()
            }),
        })
    }

    async fn headless_x_cookies(&self, agent_name: Option<&str>) -> anyhow::Result<Option<Value>> {
        let config = crate::config::Config::load_or_init().await?;
        let tool = crate::tools::BrowserHeadlessTool::new(
            config.browser.headless.command.clone(),
            config.browser.headless.args.clone(),
            config
                .browser
                .headless
                .cwd
                .as_ref()
                .map(std::path::PathBuf::from),
            config
                .browser
                .headless
                .state_dir
                .as_ref()
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|| config.workspace_dir.join("state/browser-headless")),
            config.browser.session_name.clone(),
            config.browser.headless.headless,
            config.browser.headless.timeout_ms,
        );
        let result = tool
            .execute(json!({
                "action": "export_x_cookies",
                "platform": "x",
                "agent_name": agent_name.unwrap_or("primary"),
                "timeout_ms": 10_000,
            }))
            .await?;
        if !result.success {
            return Ok(None);
        }
        Ok(result
            .metadata
            .and_then(|meta| meta.extra)
            .and_then(|extra| extra.get("cookies").cloned()))
    }

    async fn initialize(&self, process: &mut McpProcess) -> anyhow::Result<()> {
        let request = JsonRpcRequest {
            jsonrpc: "2.0",
            id: "initialize".into(),
            method: "initialize".into(),
            params: Some(json!({
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "AgentHQ",
                    "version": env!("CARGO_PKG_VERSION"),
                }
            })),
        };
        self.send_request(process, &request).await?;
        let response = self.read_response(process, "initialize").await?;
        if let Some(error) = response.error {
            return Err(anyhow!(
                "twitter MCP initialize failed ({}): {}",
                error.code,
                error.message
            ));
        }
        let notification = JsonRpcNotification {
            jsonrpc: "2.0",
            method: "notifications/initialized".into(),
            params: None,
        };
        let mut line = serde_json::to_string(&notification)?;
        line.push('\n');
        process.stdin.write_all(line.as_bytes()).await?;
        process.stdin.flush().await?;
        Ok(())
    }

    async fn send_request(
        &self,
        process: &mut McpProcess,
        request: &JsonRpcRequest,
    ) -> anyhow::Result<()> {
        let mut line = serde_json::to_string(request)?;
        line.push('\n');
        process.stdin.write_all(line.as_bytes()).await?;
        process.stdin.flush().await?;
        Ok(())
    }

    async fn read_response(
        &self,
        process: &mut McpProcess,
        request_id: &str,
    ) -> anyhow::Result<JsonRpcResponse> {
        loop {
            let mut line = String::new();
            let bytes = process.stdout.read_line(&mut line).await?;
            if bytes == 0 {
                let stderr = process.stderr_buffer.lock().await.clone();
                if stderr.trim().is_empty() {
                    return Err(anyhow!("twitter MCP server exited before responding"));
                }
                return Err(anyhow!(stderr.trim().to_string()));
            }

            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }

            let response: JsonRpcResponse = serde_json::from_str(trimmed)
                .with_context(|| format!("parsing twitter MCP response: {trimmed}"))?;

            if response.method.is_some() && response.id.is_none() {
                continue;
            }

            let matches_id = response.id.as_ref().is_some_and(|value| match value {
                Value::String(id) => id == request_id,
                Value::Number(id) => id.to_string() == request_id,
                _ => false,
            });

            if matches_id {
                return Ok(response);
            }
        }
    }

    fn fallback_action_supported(action: &str) -> bool {
        matches!(
            action,
            "profile_by_username"
                | "my_profile"
                | "get_tweet"
                | "get_user_tweets"
                | "send_tweet"
                | "search_tweets"
                | "get_followers"
                | "get_following"
                | "follow_user"
        )
    }

    fn with_backend_metadata(
        mut result: ToolResult,
        backend: TwitterBackend,
    ) -> anyhow::Result<ToolResult> {
        let mut extra = result
            .metadata
            .as_ref()
            .and_then(|meta| meta.extra.clone())
            .unwrap_or_else(|| json!({}));
        if let Value::Object(ref mut object) = extra {
            object.insert("backend".into(), Value::String(backend.as_str().into()));
        }
        result.metadata = Some(ToolResultMetadata {
            extra: Some(extra),
            ..ToolResultMetadata::default()
        });
        Ok(result)
    }

    async fn call_tool(
        &self,
        action: &str,
        tool_name: &str,
        arguments: Value,
        agent_name: Option<&str>,
    ) -> anyhow::Result<ToolResult> {
        let creds = self.resolve_credentials(agent_name).await?;
        let fallback_arguments = arguments.clone();
        let cached = self.cached_health_for(&creds.cache_key()).await;
        let primary_blocked = cached.as_ref().is_some_and(|status| {
            status.status != "ready"
                && status.backend.as_deref() != Some(TwitterBackend::TwikitDirect.as_str())
        });

        let primary_error: Option<String>;
        if !primary_blocked {
            let primary_attempt = tokio::time::timeout(TWITTER_MCP_TIMEOUT, async {
                let mut process_guard = self.process.lock().await;
                let process = self.ensure_process(&mut process_guard, &creds).await?;
                let request_id = Uuid::new_v4().to_string();
                let request = JsonRpcRequest {
                    jsonrpc: "2.0",
                    id: request_id.clone(),
                    method: "tools/call".into(),
                    params: Some(json!({
                        "name": tool_name,
                        "arguments": arguments.clone(),
                    })),
                };
                self.send_request(process, &request).await?;
                let response = self.read_response(process, &request_id).await?;

                if let Some(error) = response.error {
                    return Ok::<ToolResult, anyhow::Error>(ToolResult {
                        success: false,
                        output: String::new(),
                        error: Some(format!(
                            "twitter MCP protocol error ({}): {}",
                            error.code, error.message
                        )),
                        metadata: None,
                    });
                }

                let result = response
                    .result
                    .ok_or_else(|| anyhow!("twitter MCP response missing result"))?;
                let is_error = result
                    .get("isError")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                let texts: Vec<String> = result
                    .get("content")
                    .and_then(Value::as_array)
                    .map(|items| {
                        items
                            .iter()
                            .filter_map(|item| {
                                item.get("text")
                                    .and_then(Value::as_str)
                                    .map(ToString::to_string)
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let output = texts.join("\n");
                let metadata = ToolResultMetadata {
                    extra: Some(result),
                    ..ToolResultMetadata::default()
                };

                Ok(ToolResult {
                    success: !is_error,
                    output,
                    error: if is_error {
                        Some("twitter MCP tool call failed".into())
                    } else {
                        None
                    },
                    metadata: Some(metadata),
                })
            })
            .await;

            match primary_attempt {
                Ok(Ok(result)) if result.success => {
                    let ready = Self::health(
                        "ready",
                        Some("twitter_x adapter is healthy.".into()),
                        Some(TwitterBackend::McpPrimary),
                    );
                    self.cache_health(creds.cache_key(), ready).await;
                    return Self::with_backend_metadata(result, TwitterBackend::McpPrimary);
                }
                Ok(Ok(result)) => {
                    primary_error = result.error.clone().or_else(|| Some(result.output.clone()));
                }
                Ok(Err(error)) => {
                    let status = Self::classify_startup_failure(&error.to_string());
                    self.cache_health(creds.cache_key(), status.clone()).await;
                    primary_error = status.detail.clone();
                }
                Err(_) => {
                    let status = Self::health(
                        "mcp_start_failed",
                        Some("twitter-client-mcp timed out while starting or responding.".into()),
                        Some(TwitterBackend::McpPrimary),
                    );
                    self.cache_health(creds.cache_key(), status.clone()).await;
                    primary_error = status.detail.clone();
                }
            }
        } else {
            primary_error = cached.and_then(|status| status.detail);
        }

        if Self::fallback_action_supported(action) {
            match self
                .call_twikit_backend(action, fallback_arguments, &creds, agent_name)
                .await
            {
                Ok(result) if result.success => {
                    let detail = primary_error
                        .map(|error| format!("Primary twitter-client-mcp failed; using twikit direct backend instead. Primary detail: {error}"))
                        .or_else(|| Some("Using twikit direct backend.".into()));
                    let ready = Self::health("ready", detail, Some(TwitterBackend::TwikitDirect));
                    self.cache_health(creds.cache_key(), ready).await;
                    return Self::with_backend_metadata(result, TwitterBackend::TwikitDirect);
                }
                Ok(result) => {
                    let fallback_error = result
                        .error
                        .clone()
                        .unwrap_or_else(|| "twikit_direct backend failed".into());
                    let detail = match primary_error {
                        Some(primary) => format!(
                            "Primary twitter-client-mcp failed: {primary}\nFallback twikit_direct failed: {fallback_error}"
                        ),
                        None => fallback_error,
                    };
                    return Ok(ToolResult {
                        success: false,
                        output: String::new(),
                        error: Some(detail),
                        metadata: result.metadata,
                    });
                }
                Err(error) => {
                    let detail = match primary_error {
                        Some(primary) => format!(
                            "Primary twitter-client-mcp failed: {primary}\nFallback twikit_direct failed: {error}"
                        ),
                        None => error.to_string(),
                    };
                    return Ok(ToolResult {
                        success: false,
                        output: String::new(),
                        error: Some(detail),
                        metadata: None,
                    });
                }
            }
        }

        Ok(ToolResult {
            success: false,
            output: String::new(),
            error: Some(primary_error.unwrap_or_else(|| "twitter_x backend unavailable".into())),
            metadata: None,
        })
    }

    async fn health_result(&self, agent_name: Option<&str>) -> anyhow::Result<ToolResult> {
        let creds = match self.resolve_credentials(agent_name).await {
            Ok(creds) => creds,
            Err(error) => {
                let status = Self::classify_credential_error(&error.to_string());
                return Ok(ToolResult {
                    success: false,
                    output: status.status.clone(),
                    error: status.detail.clone(),
                    metadata: Some(ToolResultMetadata {
                        extra: Some(serde_json::to_value(&status)?),
                        ..ToolResultMetadata::default()
                    }),
                });
            }
        };

        if let Some(cached) = self.cached_health_for(&creds.cache_key()).await {
            if cached.status != "ready" {
                return Ok(ToolResult {
                    success: false,
                    output: cached.status.clone(),
                    error: cached.detail.clone(),
                    metadata: Some(ToolResultMetadata {
                        extra: Some(serde_json::to_value(&cached)?),
                        ..ToolResultMetadata::default()
                    }),
                });
            }
        }

        let primary_attempt = tokio::time::timeout(TWITTER_MCP_TIMEOUT, async {
            let mut guard = self.process.lock().await;
            self.ensure_process(&mut guard, &creds).await.map(|_| ())
        })
        .await;
        let primary_failure: Option<TwitterHealthStatus> = match primary_attempt {
            Ok(Ok(_)) => {
                let status = Self::health(
                    "ready",
                    Some("twitter_x adapter is healthy.".into()),
                    Some(TwitterBackend::McpPrimary),
                );
                self.cache_health(creds.cache_key(), status.clone()).await;
                return Ok(ToolResult {
                    success: true,
                    output: status.status.clone(),
                    error: None,
                    metadata: Some(ToolResultMetadata {
                        extra: Some(serde_json::to_value(&status)?),
                        ..ToolResultMetadata::default()
                    }),
                });
            }
            Ok(Err(error)) => Some(Self::classify_startup_failure(&error.to_string())),
            Err(_) => Some(Self::health(
                "mcp_start_failed",
                Some("twitter-client-mcp timed out while starting or responding.".into()),
                Some(TwitterBackend::McpPrimary),
            )),
        };

        match self
            .call_twikit_backend("status", json!({}), &creds, agent_name)
            .await
        {
            Ok(result) if result.success => {
                let detail = primary_failure
                    .as_ref()
                    .and_then(|status| status.detail.clone())
                    .map(|detail| {
                        format!(
                            "Primary twitter-client-mcp failed; twikit direct backend is healthy. Primary detail: {detail}"
                        )
                    })
                    .or_else(|| Some("twikit direct backend is healthy.".into()));
                let status = Self::health("ready", detail, Some(TwitterBackend::TwikitDirect));
                self.cache_health(creds.cache_key(), status.clone()).await;
                Ok(ToolResult {
                    success: true,
                    output: status.status.clone(),
                    error: None,
                    metadata: Some(ToolResultMetadata {
                        extra: Some(serde_json::to_value(&status)?),
                        ..ToolResultMetadata::default()
                    }),
                })
            }
            Ok(result) => {
                let fallback_error = result
                    .error
                    .clone()
                    .unwrap_or_else(|| "twikit_direct backend failed".into());
                let detail = match primary_failure.and_then(|status| status.detail) {
                    Some(primary) => format!(
                        "Primary twitter-client-mcp failed: {primary}\nFallback twikit_direct failed: {fallback_error}"
                    ),
                    None => fallback_error,
                };
                let status = Self::health(
                    "mcp_start_failed",
                    Some(detail),
                    Some(TwitterBackend::TwikitDirect),
                );
                self.cache_health(creds.cache_key(), status.clone()).await;
                Ok(ToolResult {
                    success: false,
                    output: status.status.clone(),
                    error: status.detail.clone(),
                    metadata: Some(ToolResultMetadata {
                        extra: Some(serde_json::to_value(&status)?),
                        ..ToolResultMetadata::default()
                    }),
                })
            }
            Err(error) => {
                let detail = match primary_failure.and_then(|status| status.detail) {
                    Some(primary) => format!(
                        "Primary twitter-client-mcp failed: {primary}\nFallback twikit_direct failed: {error}"
                    ),
                    None => error.to_string(),
                };
                let status = Self::health(
                    "mcp_start_failed",
                    Some(detail),
                    Some(TwitterBackend::TwikitDirect),
                );
                self.cache_health(creds.cache_key(), status.clone()).await;
                Ok(ToolResult {
                    success: false,
                    output: status.status.clone(),
                    error: status.detail.clone(),
                    metadata: Some(ToolResultMetadata {
                        extra: Some(serde_json::to_value(&status)?),
                        ..ToolResultMetadata::default()
                    }),
                })
            }
        }
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

    async fn resolve_credentials(
        &self,
        agent_name: Option<&str>,
    ) -> anyhow::Result<ResolvedTwitterCredentials> {
        let env_username = std::env::var("TWITTER_USERNAME").ok();
        let env_password = std::env::var("TWITTER_PASSWORD").ok();
        let env_email = std::env::var("TWITTER_EMAIL").ok();
        if let (Some(username), Some(password), Some(email)) =
            (env_username, env_password, env_email)
        {
            if !username.trim().is_empty()
                && !password.trim().is_empty()
                && !email.trim().is_empty()
            {
                return Ok(ResolvedTwitterCredentials {
                    username,
                    password,
                    email,
                    source: "env".into(),
                });
            }
        }

        let config = crate::config::Config::load_or_init().await?;
        let requested_agent_name = agent_name.map(str::trim).filter(|value| !value.is_empty());
        let target_agent = agent_name
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or("primary");

        let mut candidates: Vec<(String, crate::config::schema::SocialTwitterCredentials)> =
            Vec::new();
        if let Some(creds) = config.agent.social_accounts.twitter.clone() {
            candidates.push(("primary".into(), creds));
        }
        // Also check the generic accounts map for platform == "twitter"
        for (label, account) in &config.agent.social_accounts.accounts {
            if account.platform == "twitter" {
                candidates.push((
                    format!("primary:{label}"),
                    crate::config::schema::SocialTwitterCredentials {
                        username: account.username.clone(),
                        password: account.password.clone(),
                        email: account.email.clone(),
                    },
                ));
            }
        }
        let mut delegate_names: Vec<_> = config.agents.keys().cloned().collect();
        delegate_names.sort();
        for name in &delegate_names {
            if let Some(creds) = config
                .agents
                .get(name)
                .and_then(|agent| agent.social_accounts.twitter.clone())
            {
                candidates.push((name.clone(), creds));
            }
        }
        // Also check delegate agents' generic accounts maps
        for name in &delegate_names {
            if let Some(agent) = config.agents.get(name) {
                for (label, account) in &agent.social_accounts.accounts {
                    if account.platform == "twitter" {
                        candidates.push((
                            format!("{name}:{label}"),
                            crate::config::schema::SocialTwitterCredentials {
                                username: account.username.clone(),
                                password: account.password.clone(),
                                email: account.email.clone(),
                            },
                        ));
                    }
                }
            }
        }

        for (profile_name, creds) in &candidates {
            if profile_name == target_agent {
                if let Some((username, password, email)) = Self::credentials_complete(creds) {
                    return Ok(ResolvedTwitterCredentials {
                        username,
                        password,
                        email,
                        source: format!("agent:{profile_name}"),
                    });
                }
                if requested_agent_name.is_some() {
                    return Err(anyhow!(
                        "twitter_x credentials for agent '{}' are incomplete. Username, password, and email are all required.",
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
                    return Ok(ResolvedTwitterCredentials {
                        username,
                        password,
                        email,
                        source: format!("agent:{profile_name}:twitter-handle"),
                    });
                }
                return Err(anyhow!(
                    "twitter_x found profile '{}' for handle '{}', but the saved X credentials are incomplete. Username, password, and email are all required.",
                    profile_name,
                    target_agent
                ));
            }
        }

        if let Some(creds) = config.agent.social_accounts.twitter.as_ref() {
            if let Some((username, password, email)) = Self::credentials_complete(creds) {
                return Ok(ResolvedTwitterCredentials {
                    username,
                    password,
                    email,
                    source: "agent:primary:fallback".into(),
                });
            }
        }

        let complete_profiles: Vec<_> = candidates
            .iter()
            .filter_map(|(profile_name, creds)| {
                Self::credentials_complete(creds).map(|(username, password, email)| {
                    ResolvedTwitterCredentials {
                        username,
                        password,
                        email,
                        source: format!("agent:{profile_name}:only-complete-profile"),
                    }
                })
            })
            .collect();
        if complete_profiles.len() == 1 {
            return Ok(complete_profiles[0].clone());
        }

        Err(anyhow!(
            "twitter_x requires credentials for agent or handle '{}'. Save complete X credentials in the GUI under Agent Accounts for primary or a delegate agent.",
            target_agent
        ))
    }
}

#[derive(Debug, Clone)]
struct ResolvedTwitterCredentials {
    username: String,
    password: String,
    email: String,
    source: String,
}

impl ResolvedTwitterCredentials {
    fn cache_key(&self) -> String {
        format!(
            "{}:{}:{}:{}",
            self.source, self.username, self.password, self.email
        )
    }
}

#[async_trait]
impl Tool for TwitterMcpTool {
    fn name(&self) -> &str {
        "twitter_x"
    }

    fn description(&self) -> &str {
        "Primary X/Twitter adapter backed by twitter-client-mcp. Use this before browser_headless or browser_ext for tweet posting, replies, search, profile lookup, and relationship actions. Credentials can come from environment variables or from saved Agent Accounts in config."
    }

    fn parameters_schema(&self) -> Value {
        json!({
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "status",
                        "profile_by_username",
                        "my_profile",
                        "get_tweet",
                        "get_user_tweets",
                        "send_tweet",
                        "like_tweet",
                        "retweet",
                        "search_tweets",
                        "search_profiles",
                        "get_followers",
                        "get_following",
                        "follow_user"
                    ]
                },
                "agent_name": {
                    "type": "string",
                    "description": "Optional agent whose saved X credentials should be used. Defaults to 'primary'."
                },
                "username": { "type": "string" },
                "tweet_id": { "type": "string" },
                "text": { "type": "string" },
                "in_reply_to_id": { "type": "string" },
                "query": { "type": "string" },
                "count": { "type": "integer" },
                "search_mode": {
                    "type": "string",
                    "enum": ["top", "latest", "photos", "videos"]
                },
                "check": { "type": "boolean" }
            },
            "required": ["action"]
        })
    }

    async fn execute(&self, args: Value) -> anyhow::Result<ToolResult> {
        let action = args
            .get("action")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("Missing required field: action"))?;
        let agent_name = args.get("agent_name").and_then(Value::as_str);

        if action == "status" {
            return self.health_result(agent_name).await;
        }

        let (tool_name, tool_args) = match action {
            "profile_by_username" => (
                "profileByUsername",
                json!({ "username": args.get("username").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing username"))? }),
            ),
            "my_profile" => (
                "myProfile",
                json!({ "check": args.get("check").and_then(Value::as_bool).unwrap_or(false) }),
            ),
            "get_tweet" => (
                "getTweet",
                json!({ "tweetId": args.get("tweet_id").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing tweet_id"))? }),
            ),
            "get_user_tweets" => (
                "getUserTweets",
                json!({
                    "username": args.get("username").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing username"))?,
                    "count": args.get("count").and_then(Value::as_u64),
                }),
            ),
            "send_tweet" => (
                "sendTweet",
                json!({
                    "text": args.get("text").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing text"))?,
                    "inReplyToId": args.get("in_reply_to_id").and_then(Value::as_str),
                }),
            ),
            "like_tweet" => (
                "likeTweet",
                json!({ "tweetId": args.get("tweet_id").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing tweet_id"))? }),
            ),
            "retweet" => (
                "retweet",
                json!({ "tweetId": args.get("tweet_id").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing tweet_id"))? }),
            ),
            "search_tweets" => (
                "searchTweets",
                json!({
                    "query": args.get("query").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing query"))?,
                    "count": args.get("count").and_then(Value::as_u64),
                    "searchMode": args.get("search_mode").and_then(Value::as_str),
                }),
            ),
            "search_profiles" => (
                "searchProfiles",
                json!({
                    "query": args.get("query").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing query"))?,
                    "count": args.get("count").and_then(Value::as_u64),
                }),
            ),
            "get_followers" => (
                "getFollowers",
                json!({
                    "username": args.get("username").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing username"))?,
                    "count": args.get("count").and_then(Value::as_u64),
                }),
            ),
            "get_following" => (
                "getFollowing",
                json!({
                    "username": args.get("username").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing username"))?,
                    "count": args.get("count").and_then(Value::as_u64),
                }),
            ),
            "follow_user" => (
                "followUser",
                json!({ "username": args.get("username").and_then(Value::as_str).ok_or_else(|| anyhow!("Missing username"))? }),
            ),
            other => {
                return Err(anyhow!(
                    "Unknown action '{}'. Use one of: profile_by_username, my_profile, get_tweet, get_user_tweets, send_tweet, like_tweet, retweet, search_tweets, search_profiles, get_followers, get_following, follow_user",
                    other
                ))
            }
        };

        self.call_tool(action, tool_name, tool_args, agent_name)
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_includes_send_tweet() {
        let tool = TwitterMcpTool::new();
        let schema = tool.parameters_schema();
        let actions = schema["properties"]["action"]["enum"]
            .as_array()
            .expect("enum array");
        assert!(actions.iter().any(|value| value == "send_tweet"));
        assert!(actions.iter().any(|value| value == "search_tweets"));
    }
}
