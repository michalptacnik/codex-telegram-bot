use crate::multimodal;
use crate::providers::traits::{ChatMessage, Provider, ProviderCapabilities};
use crate::providers::ProviderRuntimeOptions;
use anyhow::{Context, Result};
use async_trait::async_trait;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;
use uuid::Uuid;

const CODEX_CLI_PATH_ENV: &str = "ZEROCLAW_CODEX_CLI_PATH";
const CODEX_SANDBOX_ENV: &str = "ZEROCLAW_CODEX_SANDBOX";
const DEFAULT_CODEX_SANDBOX: &str = "read-only";
const CODEX_HISTORY_HEADER: &str =
    "You are answering on behalf of Agent HQ using the official OpenAI Codex CLI.";
const LOGIN_STATUS_OK_PREFIX: &str = "Logged in";

pub struct OpenAiCodexProvider {
    codex_path: OsString,
    workspace_root: PathBuf,
    sandbox: String,
}

impl OpenAiCodexProvider {
    pub fn new(
        options: &ProviderRuntimeOptions,
        gateway_api_key: Option<&str>,
    ) -> anyhow::Result<Self> {
        if let Some(url) = options
            .provider_api_url
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            anyhow::bail!(
                "openai-codex now uses the official Codex CLI login/session only and does not support api_url overrides ({url})."
            );
        }

        if gateway_api_key.is_some() {
            tracing::warn!(
                "Ignoring Agent HQ API key for openai-codex; the official Codex CLI session controls authentication."
            );
        }

        let codex_path = resolve_codex_cli_path()?;
        let workspace_root = std::env::current_dir().context(
            "Failed to determine the current workspace for the official Codex CLI bridge",
        )?;
        let sandbox = resolve_codex_sandbox()?;

        Ok(Self {
            codex_path,
            workspace_root,
            sandbox,
        })
    }

    async fn ensure_logged_in(&self) -> Result<()> {
        let output = Command::new(&self.codex_path)
            .arg("login")
            .arg("status")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output()
            .await
            .with_context(|| {
                format!(
                    "Failed to run `{}` to verify the official Codex CLI login status",
                    display_program(&self.codex_path)
                )
            })?;

        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);

        if output.status.success() && stdout.trim_start().starts_with(LOGIN_STATUS_OK_PREFIX) {
            return Ok(());
        }

        let details = stdout
            .lines()
            .chain(stderr.lines())
            .find(|line| !line.trim().is_empty())
            .unwrap_or("unknown login status");
        anyhow::bail!(
            "OpenAI Codex requires an official Codex CLI session. Run `codex login` first. Details: {details}"
        );
    }

    async fn run_exec(&self, prompt: &str, model: &str, image_paths: &[PathBuf]) -> Result<String> {
        self.ensure_logged_in().await?;

        let output_file =
            std::env::temp_dir().join(format!("agenthq-codex-output-{}.txt", Uuid::new_v4()));

        let mut command = Command::new(&self.codex_path);
        command
            .arg("exec")
            .arg("-C")
            .arg(&self.workspace_root)
            .arg("--skip-git-repo-check")
            .arg("--sandbox")
            .arg(&self.sandbox)
            .arg("--json")
            .arg("--output-last-message")
            .arg(&output_file)
            .arg("-");

        if !model.trim().is_empty() {
            command.arg("-m").arg(model.trim());
        }

        for image_path in image_paths {
            command.arg("-i").arg(image_path);
        }

        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let mut child = command.spawn().with_context(|| {
            format!(
                "Failed to launch the official Codex CLI (`{}`)",
                display_program(&self.codex_path)
            )
        })?;

        let mut stdin = child
            .stdin
            .take()
            .context("Failed to open stdin for the Codex CLI child process")?;
        stdin
            .write_all(prompt.as_bytes())
            .await
            .context("Failed to send the prompt to the Codex CLI")?;
        drop(stdin);

        let output = child
            .wait_with_output()
            .await
            .context("Failed while waiting for the Codex CLI response")?;

        let stderr = String::from_utf8_lossy(&output.stderr);
        if !output.status.success() {
            let stdout = String::from_utf8_lossy(&output.stdout);
            let details = stderr
                .lines()
                .chain(stdout.lines())
                .find(|line| !line.trim().is_empty())
                .unwrap_or("unknown Codex CLI failure");
            anyhow::bail!("Official Codex CLI execution failed: {details}");
        }

        let message = std::fs::read_to_string(&output_file).with_context(|| {
            format!(
                "Failed to read the Codex CLI output file at {}",
                output_file.display()
            )
        })?;
        let _ = std::fs::remove_file(&output_file);
        let trimmed = message.trim();
        if trimmed.is_empty() {
            anyhow::bail!("Official Codex CLI completed without returning a final message");
        }
        Ok(trimmed.to_string())
    }
}

fn resolve_codex_cli_path() -> Result<OsString> {
    if let Some(override_path) = std::env::var_os(CODEX_CLI_PATH_ENV) {
        if override_path.is_empty() {
            anyhow::bail!("{CODEX_CLI_PATH_ENV} cannot be empty when set");
        }
        return Ok(override_path);
    }

    which::which("codex")
        .map(|path| path.into_os_string())
        .context(
            "Could not find the official `codex` CLI on PATH. Install it and run `codex login`.",
        )
}

fn resolve_codex_sandbox() -> Result<String> {
    let value = std::env::var(CODEX_SANDBOX_ENV).unwrap_or_else(|_| DEFAULT_CODEX_SANDBOX.into());
    let normalized = value.trim().to_ascii_lowercase();
    match normalized.as_str() {
        "read-only" | "workspace-write" | "danger-full-access" => Ok(normalized),
        _ => anyhow::bail!(
            "{CODEX_SANDBOX_ENV} must be one of: read-only, workspace-write, danger-full-access"
        ),
    }
}

fn render_message_block(message: &ChatMessage) -> String {
    format!(
        "## {} message\n{}\n",
        message.role.to_ascii_uppercase(),
        message.content.trim()
    )
}

fn build_prompt(messages: &[ChatMessage]) -> (String, Vec<PathBuf>) {
    let mut rendered: Vec<String> = Vec::new();
    let mut image_paths: Vec<PathBuf> = Vec::new();

    rendered.push(CODEX_HISTORY_HEADER.to_string());
    rendered.push(
        "Use the conversation transcript below and reply as the assistant to the latest user request. Preserve any tool-call protocol already present in the prompt."
            .to_string(),
    );

    for message in messages {
        if message.role == "user" {
            let (cleaned_text, refs) = multimodal::parse_image_markers(&message.content);
            rendered.push(render_message_block(&ChatMessage {
                role: message.role.clone(),
                content: cleaned_text,
            }));

            for image in refs {
                let path = PathBuf::from(&image);
                if path.is_file() {
                    image_paths.push(path);
                }
            }
        } else {
            rendered.push(render_message_block(message));
        }
    }

    rendered.push(
        "## Task\nRespond with the next assistant message only. Do not add commentary about using the Codex CLI."
            .to_string(),
    );

    (rendered.join("\n"), dedupe_paths(image_paths))
}

fn dedupe_paths(paths: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut seen = std::collections::BTreeSet::new();
    let mut deduped = Vec::new();

    for path in paths {
        let key = normalize_path_key(&path);
        if seen.insert(key) {
            deduped.push(path);
        }
    }

    deduped
}

fn normalize_path_key(path: &Path) -> String {
    path.to_string_lossy().to_string()
}

fn display_program(program: &OsString) -> String {
    PathBuf::from(program).display().to_string()
}

#[async_trait]
impl Provider for OpenAiCodexProvider {
    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            native_tool_calling: false,
            vision: true,
        }
    }

    async fn chat_with_system(
        &self,
        system_prompt: Option<&str>,
        message: &str,
        model: &str,
        _temperature: f64,
    ) -> Result<String> {
        let mut messages = Vec::new();
        if let Some(system_prompt) = system_prompt.filter(|value| !value.trim().is_empty()) {
            messages.push(ChatMessage::system(system_prompt));
        }
        messages.push(ChatMessage::user(message));
        self.chat_with_history(&messages, model, 0.0).await
    }

    async fn chat_with_history(
        &self,
        messages: &[ChatMessage],
        model: &str,
        _temperature: f64,
    ) -> Result<String> {
        let (prompt, image_paths) = build_prompt(messages);
        self.run_exec(&prompt, model, &image_paths).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_prompt_keeps_message_order_and_roles() {
        let messages = vec![
            ChatMessage::system("Be terse"),
            ChatMessage::user("Hello"),
            ChatMessage::assistant("Hi"),
            ChatMessage::tool("{\"ok\":true}"),
            ChatMessage::user("Do the next step"),
        ];

        let (prompt, image_paths) = build_prompt(&messages);

        assert!(prompt.contains("## SYSTEM message\nBe terse"));
        assert!(prompt.contains("## USER message\nHello"));
        assert!(prompt.contains("## ASSISTANT message\nHi"));
        assert!(prompt.contains("## TOOL message\n{\"ok\":true}"));
        assert!(prompt.contains("## USER message\nDo the next step"));
        assert!(image_paths.is_empty());
    }

    #[test]
    fn build_prompt_collects_only_local_image_files() {
        let dir = tempfile::tempdir().unwrap();
        let image = dir.path().join("screen.png");
        std::fs::write(&image, b"png").unwrap();

        let message = ChatMessage::user(format!(
            "Inspect this [IMAGE:{}] and ignore [IMAGE:https://example.com/a.png]",
            image.display()
        ));

        let (_, image_paths) = build_prompt(&[message]);
        assert_eq!(image_paths, vec![image]);
    }

    #[test]
    fn resolve_codex_sandbox_rejects_invalid_values() {
        let previous = std::env::var(CODEX_SANDBOX_ENV).ok();
        std::env::set_var(CODEX_SANDBOX_ENV, "invalid");
        let err = resolve_codex_sandbox().unwrap_err().to_string();
        match previous {
            Some(value) => std::env::set_var(CODEX_SANDBOX_ENV, value),
            None => std::env::remove_var(CODEX_SANDBOX_ENV),
        }
        assert!(err.contains(CODEX_SANDBOX_ENV));
    }
}
