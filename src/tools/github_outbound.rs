//! GitHub Outbound Tool — ported from codex-telegram-bot's `tools/outbound.py`.
//!
//! Provides agent tools for interacting with GitHub:
//! - Create issues
//! - Comment on issues/PRs
//! - List issues
//! - Close issues
//!
//! Requires `GITHUB_TOKEN` environment variable.

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;

const GITHUB_API_BASE: &str = "https://api.github.com";

/// GitHub issue representation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitHubIssue {
    pub number: u64,
    pub title: String,
    pub body: String,
    pub state: String,
    pub html_url: String,
    pub created_at: String,
    pub user: Option<String>,
    pub labels: Vec<String>,
}

/// Create a GitHub issue.
pub async fn create_issue(
    owner: &str,
    repo: &str,
    title: &str,
    body: &str,
    labels: &[String],
) -> Result<GitHubIssue> {
    let token = get_github_token()?;
    let url = format!("{GITHUB_API_BASE}/repos/{owner}/{repo}/issues");

    let mut payload = serde_json::json!({
        "title": title,
        "body": body,
    });
    if !labels.is_empty() {
        payload["labels"] = serde_json::json!(labels);
    }

    let client = reqwest::Client::new();
    let resp = client
        .post(&url)
        .header("Authorization", format!("Bearer {token}"))
        .header("User-Agent", "agent-hq")
        .header("Accept", "application/vnd.github.v3+json")
        .json(&payload)
        .send()
        .await
        .context("Failed to create GitHub issue")?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        bail!("GitHub API error ({status}): {body}");
    }

    let data: Value = resp.json().await?;
    Ok(parse_issue(&data))
}

/// Comment on a GitHub issue or PR.
pub async fn create_comment(
    owner: &str,
    repo: &str,
    issue_number: u64,
    body: &str,
) -> Result<String> {
    let token = get_github_token()?;
    let url = format!("{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/comments");

    let client = reqwest::Client::new();
    let resp = client
        .post(&url)
        .header("Authorization", format!("Bearer {token}"))
        .header("User-Agent", "agent-hq")
        .header("Accept", "application/vnd.github.v3+json")
        .json(&serde_json::json!({"body": body}))
        .send()
        .await
        .context("Failed to create GitHub comment")?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        bail!("GitHub API error ({status}): {body}");
    }

    let data: Value = resp.json().await?;
    Ok(data["html_url"].as_str().unwrap_or("").to_string())
}

/// List issues for a repository.
pub async fn list_issues(
    owner: &str,
    repo: &str,
    state: &str,
    limit: usize,
) -> Result<Vec<GitHubIssue>> {
    let token = get_github_token()?;
    let url = format!("{GITHUB_API_BASE}/repos/{owner}/{repo}/issues?state={state}&per_page={limit}");

    let client = reqwest::Client::new();
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {token}"))
        .header("User-Agent", "agent-hq")
        .header("Accept", "application/vnd.github.v3+json")
        .send()
        .await
        .context("Failed to list GitHub issues")?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        bail!("GitHub API error ({status}): {body}");
    }

    let data: Vec<Value> = resp.json().await?;
    Ok(data.iter().map(parse_issue).collect())
}

/// Close a GitHub issue.
pub async fn close_issue(owner: &str, repo: &str, issue_number: u64) -> Result<GitHubIssue> {
    let token = get_github_token()?;
    let url = format!("{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}");

    let client = reqwest::Client::new();
    let resp = client
        .patch(&url)
        .header("Authorization", format!("Bearer {token}"))
        .header("User-Agent", "agent-hq")
        .header("Accept", "application/vnd.github.v3+json")
        .json(&serde_json::json!({"state": "closed"}))
        .send()
        .await
        .context("Failed to close GitHub issue")?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        bail!("GitHub API error ({status}): {body}");
    }

    let data: Value = resp.json().await?;
    Ok(parse_issue(&data))
}

// ── Helpers ──────────────────────────────────────────────────────

fn get_github_token() -> Result<String> {
    std::env::var("GITHUB_TOKEN")
        .or_else(|_| std::env::var("GH_TOKEN"))
        .context("GITHUB_TOKEN or GH_TOKEN environment variable is required")
}

fn parse_issue(data: &Value) -> GitHubIssue {
    GitHubIssue {
        number: data["number"].as_u64().unwrap_or(0),
        title: data["title"].as_str().unwrap_or("").to_string(),
        body: data["body"].as_str().unwrap_or("").to_string(),
        state: data["state"].as_str().unwrap_or("").to_string(),
        html_url: data["html_url"].as_str().unwrap_or("").to_string(),
        created_at: data["created_at"].as_str().unwrap_or("").to_string(),
        user: data["user"]["login"].as_str().map(String::from),
        labels: data["labels"]
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter_map(|l| l["name"].as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default(),
    }
}
