use super::traits::{Tool, ToolResult};
use anyhow::anyhow;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::time::Duration;

const LINKEDIN_API_BASE: &str = "https://api.linkedin.com/v2";
const LINKEDIN_TIMEOUT: Duration = Duration::from_secs(15);

pub struct LinkedInTool;

impl LinkedInTool {
    pub fn new() -> Self {
        Self
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LinkedInHealthStatus {
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    pub supported_capabilities: LinkedInCapabilityMatrix,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct LinkedInCapabilityMatrix {
    pub post: bool,
    pub comment: bool,
}

#[derive(Debug, Clone)]
struct ResolvedLinkedInCredentials {
    access_token: String,
    source: String,
}

impl LinkedInTool {
    fn credentials_complete(
        account: &crate::config::schema::SocialAccountConfig,
    ) -> Option<String> {
        let token = account.access_token.as_deref()?.trim();
        if token.is_empty() {
            None
        } else {
            Some(token.to_string())
        }
    }

    async fn resolve_credentials(
        &self,
        agent_name: Option<&str>,
    ) -> anyhow::Result<ResolvedLinkedInCredentials> {
        // 1. Environment variable
        if let Ok(token) = std::env::var("LINKEDIN_ACCESS_TOKEN") {
            if !token.trim().is_empty() {
                return Ok(ResolvedLinkedInCredentials {
                    access_token: token,
                    source: "env".into(),
                });
            }
        }

        let config = crate::config::Config::load_or_init().await?;
        let target_agent = agent_name
            .map(str::trim)
            .filter(|v| !v.is_empty())
            .unwrap_or("primary");
        let requested_agent_name = agent_name.map(str::trim).filter(|v| !v.is_empty());

        // Build candidate list from accounts maps
        let mut candidates: Vec<(String, crate::config::schema::SocialAccountConfig)> = Vec::new();

        // Primary agent's accounts
        for (label, account) in &config.agent.social_accounts.accounts {
            if account.platform == "linkedin" {
                candidates.push((format!("primary:{label}"), account.clone()));
            }
        }

        // Delegate agents' accounts
        let mut delegate_names: Vec<_> = config.agents.keys().cloned().collect();
        delegate_names.sort();
        for name in delegate_names {
            if let Some(agent) = config.agents.get(&name) {
                for (label, account) in &agent.social_accounts.accounts {
                    if account.platform == "linkedin" {
                        candidates.push((format!("{name}:{label}"), account.clone()));
                    }
                }
            }
        }

        // Match by agent name prefix
        for (profile_name, account) in &candidates {
            let agent_part = profile_name.split(':').next().unwrap_or("");
            if agent_part == target_agent || profile_name == target_agent {
                if let Some(token) = Self::credentials_complete(account) {
                    return Ok(ResolvedLinkedInCredentials {
                        access_token: token,
                        source: format!("agent:{profile_name}"),
                    });
                }
                if requested_agent_name.is_some() {
                    return Err(anyhow!(
                        "linkedin credentials for agent '{}' are incomplete. An access_token is required.",
                        target_agent
                    ));
                }
                break;
            }
        }

        // Fallback: first complete credential
        let complete: Vec<_> = candidates
            .iter()
            .filter_map(|(name, account)| {
                Self::credentials_complete(account).map(|token| ResolvedLinkedInCredentials {
                    access_token: token,
                    source: format!("agent:{name}:only-complete-profile"),
                })
            })
            .collect();
        if complete.len() == 1 {
            return Ok(complete[0].clone());
        }

        Err(anyhow!(
            "linkedin requires an access_token for agent or label '{}'. Save LinkedIn credentials in config under social_accounts.accounts with platform = \"linkedin\".",
            target_agent
        ))
    }

    async fn api_request(
        &self,
        method: &str,
        path: &str,
        token: &str,
        body: Option<Value>,
    ) -> anyhow::Result<Value> {
        let client = reqwest::Client::builder()
            .timeout(LINKEDIN_TIMEOUT)
            .build()?;

        let url = format!("{LINKEDIN_API_BASE}{path}");
        let mut req = match method {
            "GET" => client.get(&url),
            "POST" => client.post(&url),
            "DELETE" => client.delete(&url),
            _ => return Err(anyhow!("Unsupported HTTP method: {method}")),
        };

        req = req
            .header("Authorization", format!("Bearer {token}"))
            .header("X-Restli-Protocol-Version", "2.0.0")
            .header("LinkedIn-Version", "202401");

        if let Some(body) = body {
            req = req.header("Content-Type", "application/json").json(&body);
        }

        let resp = req.send().await?;
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();

        if !status.is_success() {
            return Err(anyhow!(
                "LinkedIn API returned {} for {} {}: {}",
                status,
                method,
                path,
                text
            ));
        }

        if text.is_empty() {
            Ok(json!({"success": true}))
        } else {
            serde_json::from_str(&text)
                .map_err(|e| anyhow!("Failed to parse LinkedIn response: {e}: {text}"))
        }
    }

    async fn health_result(&self, agent_name: Option<&str>) -> anyhow::Result<ToolResult> {
        match self.resolve_credentials(agent_name).await {
            Ok(creds) => {
                match self
                    .api_request("GET", "/me", &creds.access_token, None)
                    .await
                {
                    Ok(profile) => {
                        let name = format!(
                            "{} {}",
                            profile
                                .get("localizedFirstName")
                                .and_then(Value::as_str)
                                .unwrap_or(""),
                            profile
                                .get("localizedLastName")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                        )
                        .trim()
                        .to_string();
                        Ok(ToolResult {
                            success: true,
                            output: serde_json::to_string_pretty(&LinkedInHealthStatus {
                                status: "ready".into(),
                                detail: Some(format!(
                                    "Authenticated as {name} (source: {})",
                                    creds.source
                                )),
                                supported_capabilities: LinkedInCapabilityMatrix {
                                    post: true,
                                    comment: true,
                                },
                            })?,
                            error: None,
                            metadata: None,
                        })
                    }
                    Err(e) => Ok(ToolResult {
                        success: false,
                        output: serde_json::to_string_pretty(&LinkedInHealthStatus {
                            status: "auth_failed".into(),
                            detail: Some(format!("Token validation failed: {e}")),
                            supported_capabilities: LinkedInCapabilityMatrix {
                                post: false,
                                comment: false,
                            },
                        })?,
                        error: None,
                        metadata: None,
                    }),
                }
            }
            Err(e) => Ok(ToolResult {
                success: false,
                output: serde_json::to_string_pretty(&LinkedInHealthStatus {
                    status: "credentials_missing".into(),
                    detail: Some(e.to_string()),
                    supported_capabilities: LinkedInCapabilityMatrix {
                        post: false,
                        comment: false,
                    },
                })?,
                error: None,
                metadata: None,
            }),
        }
    }

    async fn execute_action(
        &self,
        action: &str,
        args: &Value,
        creds: &ResolvedLinkedInCredentials,
    ) -> anyhow::Result<ToolResult> {
        let result = match action {
            "my_profile" => self.api_request("GET", "/me", &creds.access_token, None).await?,

            "get_profile" => {
                let vanity = args
                    .get("vanity_name")
                    .and_then(Value::as_str)
                    .ok_or_else(|| anyhow!("Missing vanity_name"))?;
                self.api_request(
                    "GET",
                    &format!(
                        "/people/(vanityName:{})",
                        urlencoding::encode(vanity)
                    ),
                    &creds.access_token,
                    None,
                )
                .await?
            }

            "publish_post" => {
                let text = args
                    .get("text")
                    .and_then(Value::as_str)
                    .ok_or_else(|| anyhow!("Missing text"))?;

                // Get the author URN from /me
                let me = self
                    .api_request("GET", "/me", &creds.access_token, None)
                    .await?;
                let person_id = me
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| anyhow!("Could not resolve person ID from /me"))?;
                let author = format!("urn:li:person:{person_id}");

                let share_commentary = json!({"text": text});

                let specific_content =
                    if let Some(url) = args.get("link_url").and_then(Value::as_str) {
                        json!({
                            "com.linkedin.ugc.ShareContent": {
                                "shareCommentary": share_commentary,
                                "shareMediaCategory": "ARTICLE",
                                "media": [{
                                    "status": "READY",
                                    "originalUrl": url,
                                    "title": {
                                        "text": args.get("link_title").and_then(Value::as_str).unwrap_or(url)
                                    }
                                }]
                            }
                        })
                    } else {
                        json!({
                            "com.linkedin.ugc.ShareContent": {
                                "shareCommentary": share_commentary,
                                "shareMediaCategory": "NONE"
                            }
                        })
                    };

                let body = json!({
                    "author": author,
                    "lifecycleState": "PUBLISHED",
                    "specificContent": specific_content,
                    "visibility": {
                        "com.linkedin.ugc.MemberNetworkVisibility": args
                            .get("visibility")
                            .and_then(Value::as_str)
                            .unwrap_or("PUBLIC")
                    }
                });

                self.api_request("POST", "/ugcPosts", &creds.access_token, Some(body))
                    .await?
            }

            "comment" => {
                let post_urn = args
                    .get("post_urn")
                    .and_then(Value::as_str)
                    .ok_or_else(|| anyhow!("Missing post_urn"))?;
                let text = args
                    .get("text")
                    .and_then(Value::as_str)
                    .ok_or_else(|| anyhow!("Missing text"))?;

                let me = self
                    .api_request("GET", "/me", &creds.access_token, None)
                    .await?;
                let person_id = me
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| anyhow!("Could not resolve person ID from /me"))?;

                let body = json!({
                    "actor": format!("urn:li:person:{person_id}"),
                    "message": {
                        "text": text
                    }
                });

                let encoded_urn = urlencoding::encode(post_urn);
                self.api_request(
                    "POST",
                    &format!("/socialActions/{encoded_urn}/comments"),
                    &creds.access_token,
                    Some(body),
                )
                .await?
            }

            "get_post" => {
                let post_urn = args
                    .get("post_urn")
                    .and_then(Value::as_str)
                    .ok_or_else(|| anyhow!("Missing post_urn"))?;
                let encoded = urlencoding::encode(post_urn);
                self.api_request(
                    "GET",
                    &format!("/ugcPosts/{encoded}"),
                    &creds.access_token,
                    None,
                )
                .await?
            }

            "get_feed" => {
                let count = args
                    .get("count")
                    .and_then(Value::as_u64)
                    .unwrap_or(10);
                self.api_request(
                    "GET",
                    &format!("/ugcPosts?q=authors&authors=List()&count={count}"),
                    &creds.access_token,
                    None,
                )
                .await?
            }

            other => {
                return Err(anyhow!(
                    "Unknown action '{}'. Use one of: status, my_profile, get_profile, publish_post, comment, get_post, get_feed",
                    other
                ))
            }
        };

        let output = serde_json::to_string_pretty(&result)?;
        Ok(ToolResult {
            success: true,
            output,
            error: None,
            metadata: None,
        })
    }
}

#[async_trait]
impl Tool for LinkedInTool {
    fn name(&self) -> &str {
        "linkedin"
    }

    fn description(&self) -> &str {
        "LinkedIn adapter for posting, commenting, and profile lookup via the LinkedIn API. Use this for LinkedIn social media management. Credentials come from environment variables (LINKEDIN_ACCESS_TOKEN) or from saved social accounts in config with platform = \"linkedin\"."
    }

    fn parameters_schema(&self) -> Value {
        json!({
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "status",
                        "my_profile",
                        "get_profile",
                        "publish_post",
                        "comment",
                        "get_post",
                        "get_feed"
                    ]
                },
                "agent_name": {
                    "type": "string",
                    "description": "Optional agent whose saved LinkedIn credentials should be used. Defaults to 'primary'."
                },
                "text": {
                    "type": "string",
                    "description": "Text content for posts or comments."
                },
                "vanity_name": {
                    "type": "string",
                    "description": "LinkedIn vanity name (URL slug) for profile lookup."
                },
                "post_urn": {
                    "type": "string",
                    "description": "LinkedIn post URN (e.g. urn:li:share:12345) for comments or post lookup."
                },
                "link_url": {
                    "type": "string",
                    "description": "URL to attach to a post as an article link."
                },
                "link_title": {
                    "type": "string",
                    "description": "Title for the attached link."
                },
                "visibility": {
                    "type": "string",
                    "enum": ["PUBLIC", "CONNECTIONS"],
                    "description": "Post visibility. Defaults to PUBLIC."
                },
                "count": {
                    "type": "integer",
                    "description": "Number of items to retrieve for feed queries."
                }
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

        let creds = self.resolve_credentials(agent_name).await?;
        self.execute_action(action, &args, &creds).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_includes_publish_post() {
        let tool = LinkedInTool::new();
        let schema = tool.parameters_schema();
        let actions = schema["properties"]["action"]["enum"]
            .as_array()
            .expect("enum array");
        assert!(actions.iter().any(|v| v == "publish_post"));
        assert!(actions.iter().any(|v| v == "comment"));
        assert!(actions.iter().any(|v| v == "get_feed"));
    }

    #[test]
    fn tool_name_is_linkedin() {
        let tool = LinkedInTool::new();
        assert_eq!(tool.name(), "linkedin");
    }
}
