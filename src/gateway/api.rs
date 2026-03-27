//! REST API handlers for the web dashboard.
//!
//! All `/api/*` routes require bearer token authentication (PairingGuard).

use super::AppState;
use crate::studio;
use crate::tools::traits::Tool;
use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Json},
};
use futures_util::future::join_all;
use serde::{Deserialize, Serialize};
use std::time::Duration;

const BROWSER_HEADLESS_STATUS_TIMEOUT: Duration = Duration::from_secs(30);

const MASKED_SECRET: &str = "***MASKED***";

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct AgentSocialAccountEntry {
    pub agent_name: String,
    pub twitter: Option<crate::config::schema::SocialTwitterCredentials>,
}

#[derive(Debug, Clone, serde::Deserialize)]
pub struct AgentSocialAccountsPutRequest {
    pub accounts: Vec<AgentSocialAccountEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentProfileUpsertRequest {
    pub profile: studio::AgentProfile,
    #[serde(default)]
    pub activate: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OnboardingCompleteRequest {
    #[serde(default)]
    pub active_agent_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AgentSocialBootstrapRequest {
    pub agent_name: String,
    pub mode: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IntegrationCapabilityStatus {
    pub post: bool,
    pub comment: bool,
    pub article: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserExtensionIntegrationStatus {
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HeadlessIntegrationStatus {
    pub status: String,
    pub authenticated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub required_user_action: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recommended_setup_mode: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentXIntegrationStatus {
    pub agent_name: String,
    pub twitter_x: crate::tools::twitter_mcp::TwitterHealthStatus,
    pub browser_headless: HeadlessIntegrationStatus,
    pub browser_ext: BrowserExtensionIntegrationStatus,
    pub supported_capabilities: IntegrationCapabilityStatus,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OnboardingBootstrapResponse {
    pub onboarding: studio::OnboardingState,
    pub active_profile: studio::ResolvedAgentProfile,
}

// ── Bearer token auth extractor ─────────────────────────────────

/// Extract and validate bearer token from Authorization header.
fn extract_bearer_token(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|auth| auth.strip_prefix("Bearer "))
}

/// Verify bearer token against PairingGuard. Returns error response if unauthorized.
fn require_auth(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<(), (StatusCode, Json<serde_json::Value>)> {
    if !state.pairing.require_pairing() {
        return Ok(());
    }

    let token = extract_bearer_token(headers).unwrap_or("");
    if state.pairing.is_authenticated(token) {
        Ok(())
    } else {
        Err((
            StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({
                "error": "Unauthorized — pair first via POST /pair, then send Authorization: Bearer <token>"
            })),
        ))
    }
}

fn require_browser_extension_auth(
    headers: &HeaderMap,
) -> Result<(), (StatusCode, Json<serde_json::Value>)> {
    let expected = std::env::var("BROWSER_EXTENSION_TOKEN")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());

    let Some(expected) = expected else {
        return Ok(());
    };

    let provided = headers
        .get("x-browser-extension-token")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .trim();

    if provided == expected {
        Ok(())
    } else {
        Err((
            StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({
                "detail": "Unauthorized browser extension request"
            })),
        ))
    }
}

fn browser_status_payload(
    bridge: Option<&crate::browser_bridge::BrowserBridge>,
) -> serde_json::Value {
    let Some(bridge) = bridge else {
        return serde_json::json!({
            "connected": false,
            "clients": [],
            "supported_commands": [],
            "pending_commands": 0,
            "completed_commands": 0,
        });
    };

    let status = bridge.status();
    let supported_commands = status
        .clients
        .first()
        .map(|client| bridge.supported_commands_for(&client.instance_id))
        .unwrap_or_default();

    serde_json::json!({
        "connected": status.active_clients > 0,
        "clients": status.clients,
        "supported_commands": supported_commands,
        "pending_commands": status.pending_commands,
        "completed_commands": status.completed_commands,
    })
}

fn browser_bridge_status_payload(
    bridge: Option<&crate::browser_bridge::BrowserBridge>,
) -> serde_json::Value {
    let Some(bridge) = bridge else {
        return serde_json::json!({
            "browser_bridge": {
                "active_clients": 0,
                "clients": [],
                "pending_commands": 0,
                "completed_commands": 0,
            }
        });
    };

    serde_json::json!({
        "browser_bridge": bridge.status()
    })
}

// ── Query parameters ─────────────────────────────────────────────

#[derive(Deserialize)]
pub struct ExtensionPollQuery {
    pub instance_id: String,
    pub limit: Option<usize>,
}

#[derive(Deserialize)]
pub struct MemoryQuery {
    pub query: Option<String>,
    pub category: Option<String>,
}

#[derive(Deserialize)]
pub struct MemoryStoreBody {
    pub key: String,
    pub content: String,
    pub category: Option<String>,
}

#[derive(Deserialize)]
pub struct CronAddBody {
    pub name: Option<String>,
    pub schedule: String,
    pub command: String,
}

#[derive(Deserialize)]
pub struct BrowserCommandBody {
    pub command_type: String,
    #[serde(default)]
    pub payload: serde_json::Value,
    #[serde(default)]
    pub client_id: String,
    #[serde(default)]
    pub wait: bool,
    #[serde(default)]
    pub timeout_sec: Option<u64>,
}

#[derive(Deserialize)]
pub struct BrowserExtensionCommandsQuery {
    pub instance_id: String,
    #[serde(default)]
    pub limit: Option<usize>,
}

#[derive(Deserialize)]
pub struct BrowserExtensionCommandResultBody {
    pub instance_id: String,
    pub ok: bool,
    #[serde(default)]
    pub output: String,
    #[serde(default)]
    pub data: serde_json::Value,
}

// ── Handlers ────────────────────────────────────────────────────

/// GET /api/status — system status overview
pub async fn handle_api_status(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let health = crate::health::snapshot();

    let mut channels = serde_json::Map::new();

    for (channel, present) in config.channels_config.channels() {
        channels.insert(channel.name().to_string(), serde_json::Value::Bool(present));
    }

    let body = serde_json::json!({
        "provider": config.default_provider,
        "model": state.model,
        "temperature": state.temperature,
        "uptime_seconds": health.uptime_seconds,
        "gateway_port": config.gateway.port,
        "locale": "en",
        "memory_backend": state.mem.name(),
        "paired": state.pairing.is_paired(),
        "channels": channels,
        "health": health,
    });

    Json(body).into_response()
}

/// GET /api/config — current config (api_key masked)
pub async fn handle_api_config_get(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();

    // Serialize to TOML after masking sensitive fields.
    let masked_config = mask_sensitive_fields(&config);
    let toml_str = match toml::to_string_pretty(&masked_config) {
        Ok(s) => s,
        Err(e) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": format!("Failed to serialize config: {e}")})),
            )
                .into_response();
        }
    };

    Json(serde_json::json!({
        "format": "toml",
        "content": toml_str,
    }))
    .into_response()
}

/// PUT /api/config — update config from TOML body
pub async fn handle_api_config_put(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: String,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    // Parse the incoming TOML
    let incoming: crate::config::Config = match toml::from_str(&body) {
        Ok(c) => c,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({"error": format!("Invalid TOML: {e}")})),
            )
                .into_response();
        }
    };

    let current_config = state.config.lock().clone();
    let new_config = hydrate_config_for_save(incoming, &current_config);

    if let Err(e) = new_config.validate() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": format!("Invalid config: {e}")})),
        )
            .into_response();
    }

    // Save to disk
    if let Err(e) = new_config.save().await {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Failed to save config: {e}")})),
        )
            .into_response();
    }

    // Update in-memory config
    *state.config.lock() = new_config;

    Json(serde_json::json!({"status": "ok"})).into_response()
}

async fn load_studio_state(
    state: &AppState,
) -> Result<studio::AgentStudioState, (StatusCode, Json<serde_json::Value>)> {
    let config = state.config.lock().clone();
    studio::load_or_bootstrap(&config).await.map_err(|error| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({
                "error": format!("Failed to load agent studio state: {error}")
            })),
        )
    })
}

/// GET /api/classes — built-in class registry
pub async fn handle_api_classes(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    Json(serde_json::json!({
        "classes": studio::built_in_classes(),
    }))
    .into_response()
}

/// GET /api/classes/:id — class detail
pub async fn handle_api_class_get(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(class_id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    match studio::class_by_id(&class_id) {
        Some(class_) => Json(class_).into_response(),
        None => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({"error": format!("Unknown class '{class_id}'")})),
        )
            .into_response(),
    }
}

/// GET /api/agents — list agent profiles
pub async fn handle_api_agents_list(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let studio_state = match load_studio_state(&state).await {
        Ok(value) => value,
        Err(error) => return error.into_response(),
    };

    let mut resolved = Vec::new();
    for profile in &studio_state.profiles {
        match studio::resolve_profile(&studio_state, &profile.id) {
            Ok(item) => resolved.push(item),
            Err(error) => {
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(serde_json::json!({"error": error.to_string()})),
                )
                    .into_response()
            }
        }
    }

    Json(serde_json::json!({
        "active_agent_id": studio_state.active_agent_id,
        "profiles": resolved,
    }))
    .into_response()
}

/// GET /api/agents/:id — agent profile detail
pub async fn handle_api_agent_get(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(agent_id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let studio_state = match load_studio_state(&state).await {
        Ok(value) => value,
        Err(error) => return error.into_response(),
    };

    match studio::resolve_profile(&studio_state, &agent_id) {
        Ok(profile) => Json(profile).into_response(),
        Err(error) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

/// POST /api/agents — create agent profile
pub async fn handle_api_agents_create(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<AgentProfileUpsertRequest>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let mut current_config = state.config.lock().clone();
    let mut studio_state = match studio::load_or_bootstrap(&current_config).await {
        Ok(value) => value,
        Err(error) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response()
        }
    };

    if studio_state
        .profiles
        .iter()
        .any(|profile| profile.id == body.profile.id)
    {
        return (
            StatusCode::CONFLICT,
            Json(
                serde_json::json!({"error": format!("Agent '{}' already exists", body.profile.id)}),
            ),
        )
            .into_response();
    }

    match studio::upsert_profile(
        &mut current_config,
        &mut studio_state,
        body.profile,
        body.activate,
    )
    .await
    {
        Ok(profile) => {
            *state.config.lock() = current_config;
            Json(profile).into_response()
        }
        Err(error) => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

/// PUT /api/agents/:id — update agent profile
pub async fn handle_api_agent_put(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(agent_id): Path<String>,
    Json(body): Json<AgentProfileUpsertRequest>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if agent_id != body.profile.id {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "Path agent id must match profile.id"})),
        )
            .into_response();
    }

    let mut current_config = state.config.lock().clone();
    let mut studio_state = match studio::load_or_bootstrap(&current_config).await {
        Ok(value) => value,
        Err(error) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response()
        }
    };

    match studio::upsert_profile(
        &mut current_config,
        &mut studio_state,
        body.profile,
        body.activate,
    )
    .await
    {
        Ok(profile) => {
            *state.config.lock() = current_config;
            Json(profile).into_response()
        }
        Err(error) => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

/// POST /api/agents/:id/activate — activate selected agent
pub async fn handle_api_agent_activate(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(agent_id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let mut current_config = state.config.lock().clone();
    let mut studio_state = match studio::load_or_bootstrap(&current_config).await {
        Ok(value) => value,
        Err(error) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response()
        }
    };

    match studio::set_active_agent(&mut current_config, &mut studio_state, &agent_id).await {
        Ok(profile) => {
            *state.config.lock() = current_config;
            Json(serde_json::json!({
                "status": "ok",
                "active_agent_id": agent_id,
                "profile": profile,
            }))
            .into_response()
        }
        Err(error) => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

/// GET /api/onboarding/state — onboarding status and active profile
pub async fn handle_api_onboarding_state(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let studio_state = match studio::load_or_bootstrap(&config).await {
        Ok(value) => value,
        Err(error) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response()
        }
    };
    let active_profile = match studio::resolve_active_profile(&studio_state) {
        Ok(value) => value,
        Err(error) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response()
        }
    };

    Json(OnboardingBootstrapResponse {
        onboarding: studio::onboarding_state(&config, &studio_state),
        active_profile,
    })
    .into_response()
}

/// POST /api/onboarding/bootstrap — ensure seeded state exists
pub async fn handle_api_onboarding_bootstrap(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let studio_state = match studio::load_or_bootstrap(&config).await {
        Ok(value) => value,
        Err(error) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response()
        }
    };
    let active_profile = match studio::resolve_active_profile(&studio_state) {
        Ok(value) => value,
        Err(error) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response()
        }
    };

    Json(OnboardingBootstrapResponse {
        onboarding: studio::onboarding_state(&config, &studio_state),
        active_profile,
    })
    .into_response()
}

/// POST /api/onboarding/complete — mark onboarding complete and activate chosen agent
pub async fn handle_api_onboarding_complete(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<OnboardingCompleteRequest>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let mut current_config = state.config.lock().clone();
    let mut studio_state = match studio::load_or_bootstrap(&current_config).await {
        Ok(value) => value,
        Err(error) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response()
        }
    };

    match studio::complete_onboarding(
        &mut current_config,
        &mut studio_state,
        body.active_agent_id.as_deref(),
    )
    .await
    {
        Ok(active_profile) => {
            *state.config.lock() = current_config.clone();
            Json(OnboardingBootstrapResponse {
                onboarding: studio::onboarding_state(&current_config, &studio_state),
                active_profile,
            })
            .into_response()
        }
        Err(error) => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

/// GET /api/tools — list registered tool specs
pub async fn handle_api_tools(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let tools: Vec<serde_json::Value> = state
        .tools_registry
        .iter()
        .map(|spec| {
            serde_json::json!({
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            })
        })
        .collect();

    Json(serde_json::json!({"tools": tools})).into_response()
}

/// GET /api/cron — list cron jobs
pub async fn handle_api_cron_list(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    match crate::cron::list_jobs(&config) {
        Ok(jobs) => {
            let jobs_json: Vec<serde_json::Value> = jobs
                .iter()
                .map(|job| {
                    serde_json::json!({
                        "id": job.id,
                        "name": job.name,
                        "command": job.command,
                        "next_run": job.next_run.to_rfc3339(),
                        "last_run": job.last_run.map(|t| t.to_rfc3339()),
                        "last_status": job.last_status,
                        "enabled": job.enabled,
                    })
                })
                .collect();
            Json(serde_json::json!({"jobs": jobs_json})).into_response()
        }
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Failed to list cron jobs: {e}")})),
        )
            .into_response(),
    }
}

/// POST /api/cron — add a new cron job
pub async fn handle_api_cron_add(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<CronAddBody>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let schedule = crate::cron::Schedule::Cron {
        expr: body.schedule,
        tz: None,
    };

    match crate::cron::add_shell_job_with_approval(
        &config,
        body.name,
        schedule,
        &body.command,
        false,
    ) {
        Ok(job) => Json(serde_json::json!({
            "status": "ok",
            "job": {
                "id": job.id,
                "name": job.name,
                "command": job.command,
                "enabled": job.enabled,
            }
        }))
        .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Failed to add cron job: {e}")})),
        )
            .into_response(),
    }
}

/// DELETE /api/cron/:id — remove a cron job
pub async fn handle_api_cron_delete(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    match crate::cron::remove_job(&config, &id) {
        Ok(()) => Json(serde_json::json!({"status": "ok"})).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Failed to remove cron job: {e}")})),
        )
            .into_response(),
    }
}

/// GET /api/integrations — list all integrations with status
pub async fn handle_api_integrations(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let entries = crate::integrations::registry::all_integrations();

    let integrations: Vec<serde_json::Value> = entries
        .iter()
        .map(|entry| {
            let status = (entry.status_fn)(&config);
            serde_json::json!({
                "name": entry.name,
                "description": entry.description,
                "category": entry.category,
                "status": status,
            })
        })
        .collect();

    Json(serde_json::json!({"integrations": integrations})).into_response()
}

/// GET /api/integrations/settings — return per-integration settings (enabled + category)
pub async fn handle_api_integrations_settings(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let entries = crate::integrations::registry::all_integrations();

    let mut settings = serde_json::Map::new();
    for entry in &entries {
        let status = (entry.status_fn)(&config);
        let enabled = matches!(status, crate::integrations::IntegrationStatus::Active);
        settings.insert(
            entry.name.to_string(),
            serde_json::json!({
                "enabled": enabled,
                "category": entry.category,
                "status": status,
            }),
        );
    }

    Json(serde_json::json!({"settings": settings})).into_response()
}

/// POST /api/doctor — run diagnostics
pub async fn handle_api_doctor(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let results = crate::doctor::diagnose(&config);

    let ok_count = results
        .iter()
        .filter(|r| r.severity == crate::doctor::Severity::Ok)
        .count();
    let warn_count = results
        .iter()
        .filter(|r| r.severity == crate::doctor::Severity::Warn)
        .count();
    let error_count = results
        .iter()
        .filter(|r| r.severity == crate::doctor::Severity::Error)
        .count();

    Json(serde_json::json!({
        "results": results,
        "summary": {
            "ok": ok_count,
            "warnings": warn_count,
            "errors": error_count,
        }
    }))
    .into_response()
}

/// GET /api/memory — list or search memory entries
pub async fn handle_api_memory_list(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(params): Query<MemoryQuery>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref query) = params.query {
        // Search mode
        match state.mem.recall(query, 50, None).await {
            Ok(entries) => Json(serde_json::json!({"entries": entries})).into_response(),
            Err(e) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": format!("Memory recall failed: {e}")})),
            )
                .into_response(),
        }
    } else {
        // List mode
        let category = params.category.as_deref().map(|cat| match cat {
            "core" => crate::memory::MemoryCategory::Core,
            "daily" => crate::memory::MemoryCategory::Daily,
            "conversation" => crate::memory::MemoryCategory::Conversation,
            other => crate::memory::MemoryCategory::Custom(other.to_string()),
        });

        match state.mem.list(category.as_ref(), None).await {
            Ok(entries) => Json(serde_json::json!({"entries": entries})).into_response(),
            Err(e) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": format!("Memory list failed: {e}")})),
            )
                .into_response(),
        }
    }
}

/// POST /api/memory — store a memory entry
pub async fn handle_api_memory_store(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<MemoryStoreBody>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let category = body
        .category
        .as_deref()
        .map(|cat| match cat {
            "core" => crate::memory::MemoryCategory::Core,
            "daily" => crate::memory::MemoryCategory::Daily,
            "conversation" => crate::memory::MemoryCategory::Conversation,
            other => crate::memory::MemoryCategory::Custom(other.to_string()),
        })
        .unwrap_or(crate::memory::MemoryCategory::Core);

    match state
        .mem
        .store(&body.key, &body.content, category, None)
        .await
    {
        Ok(()) => Json(serde_json::json!({"status": "ok"})).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Memory store failed: {e}")})),
        )
            .into_response(),
    }
}

/// DELETE /api/memory/:key — delete a memory entry
pub async fn handle_api_memory_delete(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(key): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    match state.mem.forget(&key).await {
        Ok(deleted) => {
            Json(serde_json::json!({"status": "ok", "deleted": deleted})).into_response()
        }
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Memory forget failed: {e}")})),
        )
            .into_response(),
    }
}

/// GET /api/cost — cost summary
pub async fn handle_api_cost(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref tracker) = state.cost_tracker {
        match tracker.get_summary() {
            Ok(summary) => Json(serde_json::json!({"cost": summary})).into_response(),
            Err(e) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": format!("Cost summary failed: {e}")})),
            )
                .into_response(),
        }
    } else {
        // Cost tracking is disabled or failed to initialize — return explicit zeros
        // with tracking_enabled=false so the UI can show a "tracking disabled" notice
        // rather than silently showing $0.0000 for everything.
        Json(serde_json::json!({
            "cost": {
                "session_cost_usd": 0.0,
                "daily_cost_usd": 0.0,
                "monthly_cost_usd": 0.0,
                "total_tokens": 0,
                "monthly_tokens": 0,
                "request_count": 0,
                "by_model": {},
                "tracking_enabled": false,
            }
        }))
        .into_response()
    }
}

/// GET /api/cli-tools — discovered CLI tools
pub async fn handle_api_cli_tools(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let tools = crate::tools::cli_discovery::discover_cli_tools(&[], &[]);

    Json(serde_json::json!({"cli_tools": tools})).into_response()
}

/// GET /api/health — component health snapshot
pub async fn handle_api_health(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let snapshot = crate::health::snapshot();
    Json(serde_json::json!({"health": snapshot})).into_response()
}

// ── Helpers ─────────────────────────────────────────────────────

fn is_masked_secret(value: &str) -> bool {
    value == MASKED_SECRET
}

fn mask_optional_secret(value: &mut Option<String>) {
    if value.is_some() {
        *value = Some(MASKED_SECRET.to_string());
    }
}

fn mask_required_secret(value: &mut String) {
    if !value.is_empty() {
        *value = MASKED_SECRET.to_string();
    }
}

fn mask_vec_secrets(values: &mut [String]) {
    for value in values.iter_mut() {
        if !value.is_empty() {
            *value = MASKED_SECRET.to_string();
        }
    }
}

#[allow(clippy::ref_option)]
fn restore_optional_secret(value: &mut Option<String>, current: &Option<String>) {
    if value.as_deref().is_some_and(is_masked_secret) {
        *value = current.clone();
    }
}

fn restore_required_secret(value: &mut String, current: &str) {
    if is_masked_secret(value) {
        *value = current.to_string();
    }
}

fn restore_vec_secrets(values: &mut [String], current: &[String]) {
    for (idx, value) in values.iter_mut().enumerate() {
        if is_masked_secret(value) {
            if let Some(existing) = current.get(idx) {
                *value = existing.clone();
            }
        }
    }
}

fn normalize_route_field(value: &str) -> String {
    value.trim().to_ascii_lowercase()
}

fn model_route_identity_matches(
    incoming: &crate::config::schema::ModelRouteConfig,
    current: &crate::config::schema::ModelRouteConfig,
) -> bool {
    normalize_route_field(&incoming.hint) == normalize_route_field(&current.hint)
        && normalize_route_field(&incoming.provider) == normalize_route_field(&current.provider)
        && normalize_route_field(&incoming.model) == normalize_route_field(&current.model)
}

fn model_route_provider_model_matches(
    incoming: &crate::config::schema::ModelRouteConfig,
    current: &crate::config::schema::ModelRouteConfig,
) -> bool {
    normalize_route_field(&incoming.provider) == normalize_route_field(&current.provider)
        && normalize_route_field(&incoming.model) == normalize_route_field(&current.model)
}

fn embedding_route_identity_matches(
    incoming: &crate::config::schema::EmbeddingRouteConfig,
    current: &crate::config::schema::EmbeddingRouteConfig,
) -> bool {
    normalize_route_field(&incoming.hint) == normalize_route_field(&current.hint)
        && normalize_route_field(&incoming.provider) == normalize_route_field(&current.provider)
        && normalize_route_field(&incoming.model) == normalize_route_field(&current.model)
}

fn embedding_route_provider_model_matches(
    incoming: &crate::config::schema::EmbeddingRouteConfig,
    current: &crate::config::schema::EmbeddingRouteConfig,
) -> bool {
    normalize_route_field(&incoming.provider) == normalize_route_field(&current.provider)
        && normalize_route_field(&incoming.model) == normalize_route_field(&current.model)
}

fn restore_model_route_api_keys(
    incoming: &mut [crate::config::schema::ModelRouteConfig],
    current: &[crate::config::schema::ModelRouteConfig],
) {
    let mut used_current = vec![false; current.len()];
    for incoming_route in incoming {
        if !incoming_route
            .api_key
            .as_deref()
            .is_some_and(is_masked_secret)
        {
            continue;
        }

        let exact_match_idx = current
            .iter()
            .enumerate()
            .find(|(idx, current_route)| {
                !used_current[*idx] && model_route_identity_matches(incoming_route, current_route)
            })
            .map(|(idx, _)| idx);

        let match_idx = exact_match_idx.or_else(|| {
            current
                .iter()
                .enumerate()
                .find(|(idx, current_route)| {
                    !used_current[*idx]
                        && model_route_provider_model_matches(incoming_route, current_route)
                })
                .map(|(idx, _)| idx)
        });

        if let Some(idx) = match_idx {
            used_current[idx] = true;
            incoming_route.api_key = current[idx].api_key.clone();
        } else {
            // Never persist UI placeholders to disk when no safe restore target exists.
            incoming_route.api_key = None;
        }
    }
}

fn restore_embedding_route_api_keys(
    incoming: &mut [crate::config::schema::EmbeddingRouteConfig],
    current: &[crate::config::schema::EmbeddingRouteConfig],
) {
    let mut used_current = vec![false; current.len()];
    for incoming_route in incoming {
        if !incoming_route
            .api_key
            .as_deref()
            .is_some_and(is_masked_secret)
        {
            continue;
        }

        let exact_match_idx = current
            .iter()
            .enumerate()
            .find(|(idx, current_route)| {
                !used_current[*idx]
                    && embedding_route_identity_matches(incoming_route, current_route)
            })
            .map(|(idx, _)| idx);

        let match_idx = exact_match_idx.or_else(|| {
            current
                .iter()
                .enumerate()
                .find(|(idx, current_route)| {
                    !used_current[*idx]
                        && embedding_route_provider_model_matches(incoming_route, current_route)
                })
                .map(|(idx, _)| idx)
        });

        if let Some(idx) = match_idx {
            used_current[idx] = true;
            incoming_route.api_key = current[idx].api_key.clone();
        } else {
            // Never persist UI placeholders to disk when no safe restore target exists.
            incoming_route.api_key = None;
        }
    }
}

fn mask_sensitive_fields(config: &crate::config::Config) -> crate::config::Config {
    let mut masked = config.clone();

    mask_optional_secret(&mut masked.api_key);
    mask_vec_secrets(&mut masked.reliability.api_keys);
    mask_vec_secrets(&mut masked.gateway.paired_tokens);
    mask_optional_secret(&mut masked.composio.api_key);
    mask_optional_secret(&mut masked.browser.computer_use.api_key);
    mask_optional_secret(&mut masked.web_search.brave_api_key);
    mask_optional_secret(&mut masked.storage.provider.config.db_url);
    mask_optional_secret(&mut masked.memory.qdrant.api_key);
    if let Some(cloudflare) = masked.tunnel.cloudflare.as_mut() {
        mask_required_secret(&mut cloudflare.token);
    }
    if let Some(ngrok) = masked.tunnel.ngrok.as_mut() {
        mask_required_secret(&mut ngrok.auth_token);
    }

    for agent in masked.agents.values_mut() {
        mask_optional_secret(&mut agent.api_key);
        if let Some(twitter) = agent.social_accounts.twitter.as_mut() {
            mask_optional_secret(&mut twitter.password);
            mask_optional_secret(&mut twitter.email);
        }
    }
    if let Some(twitter) = masked.agent.social_accounts.twitter.as_mut() {
        mask_optional_secret(&mut twitter.password);
        mask_optional_secret(&mut twitter.email);
    }
    for route in &mut masked.model_routes {
        mask_optional_secret(&mut route.api_key);
    }
    for route in &mut masked.embedding_routes {
        mask_optional_secret(&mut route.api_key);
    }

    if let Some(telegram) = masked.channels_config.telegram.as_mut() {
        mask_required_secret(&mut telegram.bot_token);
    }
    if let Some(discord) = masked.channels_config.discord.as_mut() {
        mask_required_secret(&mut discord.bot_token);
    }
    if let Some(slack) = masked.channels_config.slack.as_mut() {
        mask_required_secret(&mut slack.bot_token);
        mask_optional_secret(&mut slack.app_token);
    }
    if let Some(mattermost) = masked.channels_config.mattermost.as_mut() {
        mask_required_secret(&mut mattermost.bot_token);
    }
    if let Some(webhook) = masked.channels_config.webhook.as_mut() {
        mask_optional_secret(&mut webhook.secret);
    }
    if let Some(matrix) = masked.channels_config.matrix.as_mut() {
        mask_required_secret(&mut matrix.access_token);
    }
    if let Some(whatsapp) = masked.channels_config.whatsapp.as_mut() {
        mask_optional_secret(&mut whatsapp.access_token);
        mask_optional_secret(&mut whatsapp.app_secret);
        mask_optional_secret(&mut whatsapp.verify_token);
    }
    if let Some(linq) = masked.channels_config.linq.as_mut() {
        mask_required_secret(&mut linq.api_token);
        mask_optional_secret(&mut linq.signing_secret);
    }
    if let Some(nextcloud) = masked.channels_config.nextcloud_talk.as_mut() {
        mask_required_secret(&mut nextcloud.app_token);
        mask_optional_secret(&mut nextcloud.webhook_secret);
    }
    if let Some(wati) = masked.channels_config.wati.as_mut() {
        mask_required_secret(&mut wati.api_token);
    }
    if let Some(irc) = masked.channels_config.irc.as_mut() {
        mask_optional_secret(&mut irc.server_password);
        mask_optional_secret(&mut irc.nickserv_password);
        mask_optional_secret(&mut irc.sasl_password);
    }
    if let Some(lark) = masked.channels_config.lark.as_mut() {
        mask_required_secret(&mut lark.app_secret);
        mask_optional_secret(&mut lark.encrypt_key);
        mask_optional_secret(&mut lark.verification_token);
    }
    if let Some(feishu) = masked.channels_config.feishu.as_mut() {
        mask_required_secret(&mut feishu.app_secret);
        mask_optional_secret(&mut feishu.encrypt_key);
        mask_optional_secret(&mut feishu.verification_token);
    }
    if let Some(dingtalk) = masked.channels_config.dingtalk.as_mut() {
        mask_required_secret(&mut dingtalk.client_secret);
    }
    if let Some(qq) = masked.channels_config.qq.as_mut() {
        mask_required_secret(&mut qq.app_secret);
    }
    #[cfg(feature = "channel-nostr")]
    if let Some(nostr) = masked.channels_config.nostr.as_mut() {
        mask_required_secret(&mut nostr.private_key);
    }
    if let Some(clawdtalk) = masked.channels_config.clawdtalk.as_mut() {
        mask_required_secret(&mut clawdtalk.api_key);
        mask_optional_secret(&mut clawdtalk.webhook_secret);
    }
    if let Some(email) = masked.channels_config.email.as_mut() {
        mask_required_secret(&mut email.password);
    }
    masked
}

fn restore_masked_sensitive_fields(
    incoming: &mut crate::config::Config,
    current: &crate::config::Config,
) {
    restore_optional_secret(&mut incoming.api_key, &current.api_key);
    restore_vec_secrets(
        &mut incoming.gateway.paired_tokens,
        &current.gateway.paired_tokens,
    );
    restore_vec_secrets(
        &mut incoming.reliability.api_keys,
        &current.reliability.api_keys,
    );
    restore_optional_secret(&mut incoming.composio.api_key, &current.composio.api_key);
    restore_optional_secret(
        &mut incoming.browser.computer_use.api_key,
        &current.browser.computer_use.api_key,
    );
    restore_optional_secret(
        &mut incoming.web_search.brave_api_key,
        &current.web_search.brave_api_key,
    );
    restore_optional_secret(
        &mut incoming.storage.provider.config.db_url,
        &current.storage.provider.config.db_url,
    );
    restore_optional_secret(
        &mut incoming.memory.qdrant.api_key,
        &current.memory.qdrant.api_key,
    );
    if let (Some(incoming_tunnel), Some(current_tunnel)) = (
        incoming.tunnel.cloudflare.as_mut(),
        current.tunnel.cloudflare.as_ref(),
    ) {
        restore_required_secret(&mut incoming_tunnel.token, &current_tunnel.token);
    }
    if let (Some(incoming_tunnel), Some(current_tunnel)) = (
        incoming.tunnel.ngrok.as_mut(),
        current.tunnel.ngrok.as_ref(),
    ) {
        restore_required_secret(&mut incoming_tunnel.auth_token, &current_tunnel.auth_token);
    }

    for (name, agent) in &mut incoming.agents {
        if let Some(current_agent) = current.agents.get(name) {
            restore_optional_secret(&mut agent.api_key, &current_agent.api_key);
            if let (Some(incoming_twitter), Some(current_twitter)) = (
                agent.social_accounts.twitter.as_mut(),
                current_agent.social_accounts.twitter.as_ref(),
            ) {
                restore_optional_secret(&mut incoming_twitter.password, &current_twitter.password);
                restore_optional_secret(&mut incoming_twitter.email, &current_twitter.email);
            }
        }
    }
    if let (Some(incoming_twitter), Some(current_twitter)) = (
        incoming.agent.social_accounts.twitter.as_mut(),
        current.agent.social_accounts.twitter.as_ref(),
    ) {
        restore_optional_secret(&mut incoming_twitter.password, &current_twitter.password);
        restore_optional_secret(&mut incoming_twitter.email, &current_twitter.email);
    }
    restore_model_route_api_keys(&mut incoming.model_routes, &current.model_routes);
    restore_embedding_route_api_keys(&mut incoming.embedding_routes, &current.embedding_routes);

    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.telegram.as_mut(),
        current.channels_config.telegram.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.bot_token, &current_ch.bot_token);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.discord.as_mut(),
        current.channels_config.discord.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.bot_token, &current_ch.bot_token);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.slack.as_mut(),
        current.channels_config.slack.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.bot_token, &current_ch.bot_token);
        restore_optional_secret(&mut incoming_ch.app_token, &current_ch.app_token);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.mattermost.as_mut(),
        current.channels_config.mattermost.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.bot_token, &current_ch.bot_token);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.webhook.as_mut(),
        current.channels_config.webhook.as_ref(),
    ) {
        restore_optional_secret(&mut incoming_ch.secret, &current_ch.secret);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.matrix.as_mut(),
        current.channels_config.matrix.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.access_token, &current_ch.access_token);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.whatsapp.as_mut(),
        current.channels_config.whatsapp.as_ref(),
    ) {
        restore_optional_secret(&mut incoming_ch.access_token, &current_ch.access_token);
        restore_optional_secret(&mut incoming_ch.app_secret, &current_ch.app_secret);
        restore_optional_secret(&mut incoming_ch.verify_token, &current_ch.verify_token);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.linq.as_mut(),
        current.channels_config.linq.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.api_token, &current_ch.api_token);
        restore_optional_secret(&mut incoming_ch.signing_secret, &current_ch.signing_secret);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.nextcloud_talk.as_mut(),
        current.channels_config.nextcloud_talk.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.app_token, &current_ch.app_token);
        restore_optional_secret(&mut incoming_ch.webhook_secret, &current_ch.webhook_secret);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.wati.as_mut(),
        current.channels_config.wati.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.api_token, &current_ch.api_token);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.irc.as_mut(),
        current.channels_config.irc.as_ref(),
    ) {
        restore_optional_secret(
            &mut incoming_ch.server_password,
            &current_ch.server_password,
        );
        restore_optional_secret(
            &mut incoming_ch.nickserv_password,
            &current_ch.nickserv_password,
        );
        restore_optional_secret(&mut incoming_ch.sasl_password, &current_ch.sasl_password);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.lark.as_mut(),
        current.channels_config.lark.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.app_secret, &current_ch.app_secret);
        restore_optional_secret(&mut incoming_ch.encrypt_key, &current_ch.encrypt_key);
        restore_optional_secret(
            &mut incoming_ch.verification_token,
            &current_ch.verification_token,
        );
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.feishu.as_mut(),
        current.channels_config.feishu.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.app_secret, &current_ch.app_secret);
        restore_optional_secret(&mut incoming_ch.encrypt_key, &current_ch.encrypt_key);
        restore_optional_secret(
            &mut incoming_ch.verification_token,
            &current_ch.verification_token,
        );
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.dingtalk.as_mut(),
        current.channels_config.dingtalk.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.client_secret, &current_ch.client_secret);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.qq.as_mut(),
        current.channels_config.qq.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.app_secret, &current_ch.app_secret);
    }
    #[cfg(feature = "channel-nostr")]
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.nostr.as_mut(),
        current.channels_config.nostr.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.private_key, &current_ch.private_key);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.clawdtalk.as_mut(),
        current.channels_config.clawdtalk.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.api_key, &current_ch.api_key);
        restore_optional_secret(&mut incoming_ch.webhook_secret, &current_ch.webhook_secret);
    }
    if let (Some(incoming_ch), Some(current_ch)) = (
        incoming.channels_config.email.as_mut(),
        current.channels_config.email.as_ref(),
    ) {
        restore_required_secret(&mut incoming_ch.password, &current_ch.password);
    }
}

fn agent_social_accounts_payload(config: &crate::config::Config) -> Vec<AgentSocialAccountEntry> {
    let mut accounts = Vec::new();
    accounts.push(AgentSocialAccountEntry {
        agent_name: "primary".into(),
        twitter: config.agent.social_accounts.twitter.clone(),
    });
    let mut delegate_names: Vec<_> = config.agents.keys().cloned().collect();
    delegate_names.sort();
    for name in delegate_names {
        if let Some(agent) = config.agents.get(&name) {
            accounts.push(AgentSocialAccountEntry {
                agent_name: name,
                twitter: agent.social_accounts.twitter.clone(),
            });
        }
    }
    accounts
}

fn build_browser_headless_tool(
    config: &crate::config::Config,
) -> crate::tools::BrowserHeadlessTool {
    crate::tools::BrowserHeadlessTool::new(
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
    )
}

async fn twitter_x_status_for_agent(
    _agent_name: &str,
) -> crate::tools::twitter_mcp::TwitterHealthStatus {
    crate::tools::twitter_mcp::TwitterHealthStatus {
        status: "disabled".into(),
        detail: Some(
            "The direct X/Twitter adapter is disabled because it is not reliable enough yet. Use browser_headless first, then browser_ext.".into(),
        ),
        backend: None,
        supported_capabilities: crate::tools::twitter_mcp::TwitterCapabilityMatrix {
            post: false,
            comment: false,
            article: false,
        },
    }
}

async fn browser_headless_status_for_agent(
    config: &crate::config::Config,
    agent_name: &str,
) -> HeadlessIntegrationStatus {
    let tool = build_browser_headless_tool(config);
    let args = serde_json::json!({
        "action": "status",
        "platform": "x",
        "agent_name": agent_name,
    });
    match tokio::time::timeout(BROWSER_HEADLESS_STATUS_TIMEOUT, tool.execute(args)).await {
        Err(_) => HeadlessIntegrationStatus {
            status: "status_timeout".into(),
            authenticated: false,
            detail: Some("browser_headless status check timed out.".into()),
            session: None,
            url: None,
            required_user_action: None,
            recommended_setup_mode: None,
        },
        Ok(Ok(result)) => {
            let success = result.success;
            let output = result.output.clone();
            let error = result.error.clone();
            let extra = result
                .metadata
                .and_then(|meta| meta.extra)
                .unwrap_or_else(|| serde_json::json!({}));
            let runtime_status = extra
                .get("runtime")
                .and_then(|runtime: &serde_json::Value| runtime.get("status"))
                .and_then(serde_json::Value::as_str)
                .unwrap_or(if success { "ready" } else { "failed" })
                .to_string();
            let x = extra
                .get("x")
                .cloned()
                .unwrap_or_else(|| serde_json::json!({}));
            HeadlessIntegrationStatus {
                status: runtime_status,
                authenticated: x
                    .get("authenticated")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false),
                detail: x
                    .get("detail")
                    .and_then(serde_json::Value::as_str)
                    .map(ToString::to_string)
                    .or_else(|| extra.get("runtime").and_then(|runtime: &serde_json::Value| runtime.get("detail")).and_then(serde_json::Value::as_str).map(ToString::to_string))
                    .or(error)
                    .or(Some(output)),
                session: extra.get("session").and_then(serde_json::Value::as_str).map(ToString::to_string),
                url: x.get("url").and_then(serde_json::Value::as_str).map(ToString::to_string),
                required_user_action: match x
                    .get("status")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("")
                {
                    "login_required" => Some(
                        "One-time setup required: stay signed into X in Google Chrome, then run 'Import Chrome X Session'. If that fails, run 'Interactive X Bootstrap' and complete any challenge in the opened browser window.".into(),
                    ),
                    "suspicious_login_prevented" => Some(
                        "X blocked fresh automated login. Stay signed into X in Google Chrome, then run 'Import Chrome X Session'.".into(),
                    ),
                    _ => None,
                },
                recommended_setup_mode: match x
                    .get("status")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("")
                {
                    "login_required" | "suspicious_login_prevented" => Some("import_chrome".into()),
                    _ => None,
                },
            }
        }
        Ok(Err(error)) => HeadlessIntegrationStatus {
            status: "failed".into(),
            authenticated: false,
            detail: Some(error.to_string()),
            session: None,
            url: None,
            required_user_action: None,
            recommended_setup_mode: None,
        },
    }
}

fn browser_ext_x_status(
    bridge: Option<&crate::browser_bridge::BrowserBridge>,
) -> BrowserExtensionIntegrationStatus {
    let Some(bridge) = bridge else {
        return BrowserExtensionIntegrationStatus {
            status: "not_connected".into(),
            detail: Some("No browser extension bridge is available.".into()),
        };
    };
    let status = bridge.status();
    if status.active_clients > 0 {
        BrowserExtensionIntegrationStatus {
            status: "ready".into(),
            detail: Some(format!(
                "{} live browser extension client(s) connected.",
                status.active_clients
            )),
        }
    } else {
        BrowserExtensionIntegrationStatus {
            status: "not_connected".into(),
            detail: Some("No live browser extension client is connected.".into()),
        }
    }
}

async fn x_integration_status_payload(
    config: &crate::config::Config,
    bridge: Option<&crate::browser_bridge::BrowserBridge>,
) -> Vec<AgentXIntegrationStatus> {
    join_all(
        agent_social_accounts_payload(config)
            .into_iter()
            .map(|account| {
                let agent_name = account.agent_name.clone();
                let browser_ext = browser_ext_x_status(bridge);
                async move {
                    let (twitter_x, browser_headless) = tokio::join!(
                        twitter_x_status_for_agent(&agent_name),
                        browser_headless_status_for_agent(config, &agent_name)
                    );
                    AgentXIntegrationStatus {
                        agent_name,
                        twitter_x,
                        browser_headless,
                        browser_ext,
                        supported_capabilities: IntegrationCapabilityStatus {
                            post: true,
                            comment: true,
                            article: true,
                        },
                    }
                }
            }),
    )
    .await
}

/// GET /api/agents/social-accounts — masked social credentials by agent
pub async fn handle_api_agent_social_accounts_get(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let masked = mask_sensitive_fields(&config);
    let x_status = x_integration_status_payload(&config, state.browser_bridge.as_deref()).await;
    Json(serde_json::json!({
        "accounts": agent_social_accounts_payload(&masked),
        "x_status": x_status,
    }))
    .into_response()
}

/// PUT /api/agents/social-accounts — update per-agent social credentials
pub async fn handle_api_agent_social_accounts_put(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<AgentSocialAccountsPutRequest>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let current = state.config.lock().clone();
    let mut new_config = current.clone();

    for entry in body.accounts {
        if entry.agent_name == "primary" {
            new_config.agent.social_accounts.twitter = entry.twitter;
            continue;
        }
        if let Some(agent) = new_config.agents.get_mut(&entry.agent_name) {
            agent.social_accounts.twitter = entry.twitter;
        }
    }

    restore_masked_sensitive_fields(&mut new_config, &current);

    if let Err(e) = new_config.validate() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": format!("Invalid config: {e}")})),
        )
            .into_response();
    }

    if let Err(e) = new_config.save().await {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Failed to save config: {e}")})),
        )
            .into_response();
    }

    *state.config.lock() = new_config.clone();
    let masked = mask_sensitive_fields(&new_config);
    let x_status = x_integration_status_payload(&new_config, state.browser_bridge.as_deref()).await;

    Json(serde_json::json!({
        "status": "ok",
        "accounts": agent_social_accounts_payload(&masked),
        "x_status": x_status,
    }))
    .into_response()
}

/// POST /api/agents/social-accounts/bootstrap/x — bootstrap persistent X headless session
pub async fn handle_api_agent_social_accounts_bootstrap_x(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<AgentSocialBootstrapRequest>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let tool = build_browser_headless_tool(&config);
    let result = match crate::tools::Tool::execute(
        &tool,
        serde_json::json!({
            "action": match body.mode.as_deref() {
                Some("interactive") => "bootstrap_x_session_interactive",
                Some("import_chrome") => "import_x_session_from_chrome",
                _ => "bootstrap_x_session",
            },
            "agent_name": body.agent_name,
            "mode": body.mode,
        }),
    )
    .await
    {
        Ok(result) => result,
        Err(error) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response();
        }
    };

    Json(serde_json::json!({
        "status": if result.success { "ok" } else { "failed" },
        "message": result.output,
        "error": result.error,
        "metadata": result.metadata.as_ref().and_then(|meta| meta.extra.clone()),
    }))
    .into_response()
}

fn hydrate_config_for_save(
    mut incoming: crate::config::Config,
    current: &crate::config::Config,
) -> crate::config::Config {
    restore_masked_sensitive_fields(&mut incoming, current);
    // These are runtime-computed fields skipped from TOML serialization.
    incoming.config_path = current.config_path.clone();
    incoming.workspace_dir = current.workspace_dir.clone();
    incoming
}

// ══════════════════════════════════════════════════════════════════
// Agent HQ Control Center API extensions
// ══════════════════════════════════════════════════════════════════

/// GET /api/soul — get current soul profile
pub async fn handle_api_soul_get(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let soul_store = crate::soul::SoulStore::new(&config.workspace_dir);
    match soul_store.load() {
        Ok(profile) => Json(serde_json::json!({
            "profile": profile,
            "rendered": crate::soul::render_soul(&profile),
        }))
        .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Failed to load soul: {e}")})),
        )
            .into_response(),
    }
}

/// PUT /api/soul — update soul profile
pub async fn handle_api_soul_put(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(profile): Json<crate::soul::SoulProfile>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let config = state.config.lock().clone();
    let soul_store = crate::soul::SoulStore::new(&config.workspace_dir);

    let validation = crate::soul::validate_soul(&profile, None);
    if !validation.ok {
        return (
            StatusCode::BAD_REQUEST,
            Json(
                serde_json::json!({"error": "Validation failed", "warnings": validation.warnings}),
            ),
        )
            .into_response();
    }

    match soul_store.save(&profile) {
        Ok(()) => Json(serde_json::json!({"ok": true, "profile": profile})).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Failed to save soul: {e}")})),
        )
            .into_response(),
    }
}

/// GET /api/missions — list all missions
pub async fn handle_api_missions_list(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref runner) = state.mission_runner {
        let missions = runner.list_missions().await;
        Json(serde_json::json!({"missions": missions})).into_response()
    } else {
        Json(serde_json::json!({"missions": []})).into_response()
    }
}

/// POST /api/missions — create a new mission
pub async fn handle_api_mission_create(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let goal = body
        .get("goal")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if goal.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "goal is required"})),
        )
            .into_response();
    }

    if let Some(ref runner) = state.mission_runner {
        let mission = runner.create_mission(goal).await;
        Json(serde_json::json!({"mission": mission})).into_response()
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "Mission runner not available"})),
        )
            .into_response()
    }
}

/// POST /api/missions/:id/pause — pause a mission
pub async fn handle_api_mission_pause(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref runner) = state.mission_runner {
        let ok = runner.pause(&id).await;
        Json(serde_json::json!({"ok": ok})).into_response()
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "not available"})),
        )
            .into_response()
    }
}

/// POST /api/missions/:id/resume — resume a mission
pub async fn handle_api_mission_resume(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref runner) = state.mission_runner {
        let ok = runner.resume(&id).await;
        Json(serde_json::json!({"ok": ok})).into_response()
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "not available"})),
        )
            .into_response()
    }
}

/// POST /api/missions/:id/stop — stop a mission
pub async fn handle_api_mission_stop(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref runner) = state.mission_runner {
        let ok = runner.stop(&id).await;
        Json(serde_json::json!({"ok": ok})).into_response()
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "not available"})),
        )
            .into_response()
    }
}

/// GET /api/plugins — list installed plugins
pub async fn handle_api_plugins_list(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref mgr) = state.plugin_manager {
        match mgr.list_plugins() {
            Ok(plugins) => Json(serde_json::json!({"plugins": plugins})).into_response(),
            Err(e) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": format!("{e}")})),
            )
                .into_response(),
        }
    } else {
        Json(serde_json::json!({"plugins": []})).into_response()
    }
}

/// POST /api/plugins/:id/enable — enable a plugin
pub async fn handle_api_plugin_enable(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref mgr) = state.plugin_manager {
        match mgr.enable_plugin(&id) {
            Ok(record) => Json(serde_json::json!({"plugin": record})).into_response(),
            Err(e) => (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({"error": format!("{e}")})),
            )
                .into_response(),
        }
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "not available"})),
        )
            .into_response()
    }
}

/// POST /api/plugins/:id/disable — disable a plugin
pub async fn handle_api_plugin_disable(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref mgr) = state.plugin_manager {
        match mgr.disable_plugin(&id) {
            Ok(record) => Json(serde_json::json!({"plugin": record})).into_response(),
            Err(e) => (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({"error": format!("{e}")})),
            )
                .into_response(),
        }
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "not available"})),
        )
            .into_response()
    }
}

/// DELETE /api/plugins/:id — uninstall a plugin
pub async fn handle_api_plugin_uninstall(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref mgr) = state.plugin_manager {
        match mgr.uninstall_plugin(&id) {
            Ok(()) => Json(serde_json::json!({"ok": true})).into_response(),
            Err(e) => (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({"error": format!("{e}")})),
            )
                .into_response(),
        }
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "not available"})),
        )
            .into_response()
    }
}

/// GET /api/sessions — list sessions
pub async fn handle_api_sessions_list(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    if let Some(ref store) = state.session_store {
        let sessions = store.lock().list_sessions();
        Json(serde_json::json!({"sessions": sessions})).into_response()
    } else {
        Json(serde_json::json!({"sessions": []})).into_response()
    }
}

/// GET /api/browser/status — browser bridge status for local diagnostics.
pub async fn handle_api_browser_status(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    Json(browser_status_payload(state.browser_bridge.as_deref())).into_response()
}

/// POST /api/browser/command — enqueue a browser extension command.
pub async fn handle_api_browser_command(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<BrowserCommandBody>,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    let Some(ref bridge) = state.browser_bridge else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"ok": false, "error": "Browser bridge unavailable"})),
        )
            .into_response();
    };

    let command_type = body.command_type.trim();
    if command_type.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"ok": false, "error": "command_type is required"})),
        )
            .into_response();
    }

    let client = if body.client_id.trim().is_empty() {
        bridge.pick_client(Some(command_type))
    } else {
        bridge
            .active_clients()
            .into_iter()
            .find(|item| item.instance_id == body.client_id)
    };

    let Some(client) = client else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"ok": false, "error": "No active browser extension client available"})),
        )
            .into_response();
    };

    let command_id = bridge.enqueue_command(&client.instance_id, command_type, body.payload);
    if body.wait {
        let timeout_ms = body.timeout_sec.unwrap_or(20).saturating_mul(1000);
        if let Some(command) = bridge.wait_for_command(&command_id, timeout_ms).await {
            return Json(serde_json::json!({
                "ok": command.ok.unwrap_or(false),
                "command_id": command.command_id,
                "client_id": command.client_id,
                "command": command,
            }))
            .into_response();
        }
    }

    Json(serde_json::json!({
        "ok": true,
        "command_id": command_id,
        "client_id": client.instance_id,
        "queued": true,
    }))
    .into_response()
}

/// POST /api/browser/extension/register — register extension client metadata.
pub async fn handle_api_browser_extension_register(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<crate::browser_bridge::HeartbeatMessage>,
) -> impl IntoResponse {
    if let Err(e) = require_browser_extension_auth(&headers) {
        return e.into_response();
    }

    let Some(ref bridge) = state.browser_bridge else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"detail": "Browser bridge unavailable"})),
        )
            .into_response();
    };

    let instance_id = bridge.heartbeat(body);
    Json(serde_json::json!({"ok": true, "instance_id": instance_id})).into_response()
}

/// POST /api/browser/extension/heartbeat — refresh extension presence and tab state.
pub async fn handle_api_browser_extension_heartbeat(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<crate::browser_bridge::HeartbeatMessage>,
) -> impl IntoResponse {
    if let Err(e) = require_browser_extension_auth(&headers) {
        return e.into_response();
    }

    let Some(ref bridge) = state.browser_bridge else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"detail": "Browser bridge unavailable"})),
        )
            .into_response();
    };

    let instance_id = bridge.heartbeat(body);
    Json(serde_json::json!({"ok": true, "instance_id": instance_id})).into_response()
}

/// GET /api/browser/extension/commands — poll pending commands for a client.
pub async fn handle_api_browser_extension_commands(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<BrowserExtensionCommandsQuery>,
) -> impl IntoResponse {
    if let Err(e) = require_browser_extension_auth(&headers) {
        return e.into_response();
    }

    let Some(ref bridge) = state.browser_bridge else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"detail": "Browser bridge unavailable"})),
        )
            .into_response();
    };

    if query.instance_id.trim().is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"detail": "instance_id is required"})),
        )
            .into_response();
    }

    let _ = query.limit;
    let items = bridge.poll_commands(&query.instance_id);

    Json(serde_json::json!({"items": items})).into_response()
}

/// POST /api/browser/extension/commands/{command_id}/result — report command completion.
pub async fn handle_api_browser_extension_command_result(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(command_id): Path<String>,
    Json(body): Json<BrowserExtensionCommandResultBody>,
) -> impl IntoResponse {
    if let Err(e) = require_browser_extension_auth(&headers) {
        return e.into_response();
    }

    let Some(ref bridge) = state.browser_bridge else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"detail": "Browser bridge unavailable"})),
        )
            .into_response();
    };

    if command_id.trim().is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"detail": "command_id is required"})),
        )
            .into_response();
    }

    let _ = &body.instance_id;
    bridge.complete_command(crate::browser_bridge::CommandResult {
        command_id,
        ok: body.ok,
        output: Some(body.output),
        data: Some(body.data),
    });

    Json(serde_json::json!({"ok": true})).into_response()
}

/// GET /api/browser-bridge/status — browser bridge status
pub async fn handle_api_browser_bridge_status(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = require_auth(&state, &headers) {
        return e.into_response();
    }

    Json(browser_bridge_status_payload(
        state.browser_bridge.as_deref(),
    ))
    .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gateway::AppState;
    use crate::memory::traits::Memory;
    use crate::providers::traits::Provider;
    use crate::security::PairingGuard;
    use async_trait::async_trait;
    use axum::body::to_bytes;
    use parking_lot::Mutex;
    use serde_json::Value;
    use std::sync::Arc;
    use std::time::Duration;

    #[derive(Default)]
    struct TestProvider;

    #[async_trait]
    impl Provider for TestProvider {
        async fn chat_with_system(
            &self,
            _system_prompt: Option<&str>,
            _message: &str,
            _model: &str,
            _temperature: f64,
        ) -> anyhow::Result<String> {
            Ok(String::new())
        }
    }

    #[derive(Default)]
    struct TestMemory;

    #[async_trait]
    impl Memory for TestMemory {
        fn name(&self) -> &str {
            "test"
        }

        async fn store(
            &self,
            _key: &str,
            _content: &str,
            _category: crate::memory::traits::MemoryCategory,
            _session_id: Option<&str>,
        ) -> anyhow::Result<()> {
            Ok(())
        }

        async fn recall(
            &self,
            _query: &str,
            _limit: usize,
            _session_id: Option<&str>,
        ) -> anyhow::Result<Vec<crate::memory::traits::MemoryEntry>> {
            Ok(Vec::new())
        }

        async fn get(
            &self,
            _key: &str,
        ) -> anyhow::Result<Option<crate::memory::traits::MemoryEntry>> {
            Ok(None)
        }

        async fn list(
            &self,
            _category: Option<&crate::memory::traits::MemoryCategory>,
            _session_id: Option<&str>,
        ) -> anyhow::Result<Vec<crate::memory::traits::MemoryEntry>> {
            Ok(Vec::new())
        }

        async fn forget(&self, _key: &str) -> anyhow::Result<bool> {
            Ok(false)
        }

        async fn count(&self) -> anyhow::Result<usize> {
            Ok(0)
        }

        async fn health_check(&self) -> bool {
            true
        }
    }

    fn test_state_with_browser_bridge() -> AppState {
        AppState {
            config: Arc::new(Mutex::new(crate::config::Config::default())),
            provider: Arc::new(TestProvider),
            model: "test-model".into(),
            temperature: 0.0,
            mem: Arc::new(TestMemory),
            auto_save: false,
            webhook_secret_hash: None,
            pairing: Arc::new(PairingGuard::new(false, &[])),
            trust_forwarded_headers: false,
            rate_limiter: Arc::new(crate::gateway::GatewayRateLimiter::new(100, 100, 100)),
            idempotency_store: Arc::new(crate::gateway::IdempotencyStore::new(
                Duration::from_secs(300),
                1000,
            )),
            whatsapp: None,
            whatsapp_app_secret: None,
            linq: None,
            linq_signing_secret: None,
            nextcloud_talk: None,
            nextcloud_talk_webhook_secret: None,
            wati: None,
            observer: Arc::new(crate::observability::NoopObserver),
            tools_registry: Arc::new(Vec::new()),
            cost_tracker: None,
            event_tx: tokio::sync::broadcast::channel(16).0,
            shutdown_tx: tokio::sync::watch::channel(false).0,
            mission_runner: None,
            plugin_manager: None,
            session_store: None,
            browser_bridge: Some(Arc::new(crate::browser_bridge::BrowserBridge::new())),
        }
    }

    async fn json_body(response: axum::response::Response) -> Value {
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&body).unwrap()
    }

    #[tokio::test]
    async fn browser_status_endpoints_agree_on_connected_client() {
        let state = test_state_with_browser_bridge();
        let bridge = state.browser_bridge.as_ref().unwrap();
        bridge.heartbeat(crate::browser_bridge::HeartbeatMessage {
            instance_id: "ext-1".into(),
            label: Some("Test Chrome".into()),
            version: Some("1.0".into()),
            platform: Some("macOS".into()),
            user_agent: Some("Chrome".into()),
            active_tab_url: Some("https://x.com".into()),
            active_tab_title: Some("X".into()),
            supported_commands: Some(vec!["open_url".into(), "snapshot".into()]),
            extension_version: Some("2.0.0".into()),
        });

        let browser = json_body(
            handle_api_browser_status(State(state.clone()), HeaderMap::new())
                .await
                .into_response(),
        )
        .await;
        let bridge_status = json_body(
            handle_api_browser_bridge_status(State(state), HeaderMap::new())
                .await
                .into_response(),
        )
        .await;

        assert_eq!(browser["connected"], Value::Bool(true));
        assert_eq!(browser["clients"].as_array().map(Vec::len), Some(1));
        assert_eq!(
            browser["supported_commands"],
            serde_json::json!(["open_url", "snapshot"])
        );
        assert_eq!(
            bridge_status["browser_bridge"]["active_clients"],
            Value::from(1)
        );
        assert_eq!(
            bridge_status["browser_bridge"]["clients"],
            browser["clients"]
        );
    }

    #[test]
    fn masking_keeps_toml_valid_and_preserves_api_keys_type() {
        let mut cfg = crate::config::Config::default();
        cfg.api_key = Some("sk-live-123".to_string());
        cfg.reliability.api_keys = vec!["rk-1".to_string(), "rk-2".to_string()];
        cfg.gateway.paired_tokens = vec!["pair-token-1".to_string()];
        cfg.tunnel.cloudflare = Some(crate::config::schema::CloudflareTunnelConfig {
            token: "cf-token".to_string(),
        });
        cfg.memory.qdrant.api_key = Some("qdrant-key".to_string());
        cfg.channels_config.wati = Some(crate::config::schema::WatiConfig {
            api_token: "wati-token".to_string(),
            api_url: "https://live-mt-server.wati.io".to_string(),
            tenant_id: None,
            allowed_numbers: vec![],
        });
        cfg.channels_config.feishu = Some(crate::config::schema::FeishuConfig {
            app_id: "cli_aabbcc".to_string(),
            app_secret: "feishu-secret".to_string(),
            encrypt_key: Some("feishu-encrypt".to_string()),
            verification_token: Some("feishu-verify".to_string()),
            allowed_users: vec!["*".to_string()],
            receive_mode: crate::config::schema::LarkReceiveMode::Websocket,
            port: None,
        });
        cfg.channels_config.email = Some(crate::channels::email_channel::EmailConfig {
            imap_host: "imap.example.com".to_string(),
            imap_port: 993,
            imap_folder: "INBOX".to_string(),
            smtp_host: "smtp.example.com".to_string(),
            smtp_port: 465,
            smtp_tls: true,
            username: "agent@example.com".to_string(),
            password: "email-password-secret".to_string(),
            from_address: "agent@example.com".to_string(),
            idle_timeout_secs: 1740,
            allowed_senders: vec!["*".to_string()],
            default_subject: "ZeroClaw Message".to_string(),
        });
        cfg.model_routes = vec![crate::config::schema::ModelRouteConfig {
            hint: "reasoning".to_string(),
            provider: "openrouter".to_string(),
            model: "anthropic/claude-sonnet-4.6".to_string(),
            api_key: Some("route-model-key".to_string()),
        }];
        cfg.embedding_routes = vec![crate::config::schema::EmbeddingRouteConfig {
            hint: "semantic".to_string(),
            provider: "openai".to_string(),
            model: "text-embedding-3-small".to_string(),
            dimensions: Some(1536),
            api_key: Some("route-embed-key".to_string()),
        }];

        let masked = mask_sensitive_fields(&cfg);
        let toml = toml::to_string_pretty(&masked).expect("masked config should serialize");
        let parsed: crate::config::Config =
            toml::from_str(&toml).expect("masked config should remain valid TOML for Config");

        assert_eq!(parsed.api_key.as_deref(), Some(MASKED_SECRET));
        assert_eq!(
            parsed.reliability.api_keys,
            vec![MASKED_SECRET.to_string(), MASKED_SECRET.to_string()]
        );
        assert_eq!(
            parsed.gateway.paired_tokens,
            vec![MASKED_SECRET.to_string()]
        );
        assert_eq!(
            parsed.tunnel.cloudflare.as_ref().map(|v| v.token.as_str()),
            Some(MASKED_SECRET)
        );
        assert_eq!(
            parsed
                .channels_config
                .wati
                .as_ref()
                .map(|v| v.api_token.as_str()),
            Some(MASKED_SECRET)
        );
        assert_eq!(parsed.memory.qdrant.api_key.as_deref(), Some(MASKED_SECRET));
        assert_eq!(
            parsed
                .channels_config
                .feishu
                .as_ref()
                .map(|v| v.app_secret.as_str()),
            Some(MASKED_SECRET)
        );
        assert_eq!(
            parsed
                .channels_config
                .feishu
                .as_ref()
                .and_then(|v| v.encrypt_key.as_deref()),
            Some(MASKED_SECRET)
        );
        assert_eq!(
            parsed
                .channels_config
                .feishu
                .as_ref()
                .and_then(|v| v.verification_token.as_deref()),
            Some(MASKED_SECRET)
        );
        assert_eq!(
            parsed
                .model_routes
                .first()
                .and_then(|v| v.api_key.as_deref()),
            Some(MASKED_SECRET)
        );
        assert_eq!(
            parsed
                .embedding_routes
                .first()
                .and_then(|v| v.api_key.as_deref()),
            Some(MASKED_SECRET)
        );
        assert_eq!(
            parsed
                .channels_config
                .email
                .as_ref()
                .map(|v| v.password.as_str()),
            Some(MASKED_SECRET)
        );
    }

    #[test]
    fn hydrate_config_for_save_restores_masked_secrets_and_paths() {
        let mut current = crate::config::Config::default();
        current.config_path = std::path::PathBuf::from("/tmp/current/config.toml");
        current.workspace_dir = std::path::PathBuf::from("/tmp/current/workspace");
        current.api_key = Some("real-key".to_string());
        current.reliability.api_keys = vec!["r1".to_string(), "r2".to_string()];
        current.gateway.paired_tokens = vec!["pair-1".to_string(), "pair-2".to_string()];
        current.tunnel.cloudflare = Some(crate::config::schema::CloudflareTunnelConfig {
            token: "cf-token-real".to_string(),
        });
        current.tunnel.ngrok = Some(crate::config::schema::NgrokTunnelConfig {
            auth_token: "ngrok-token-real".to_string(),
            domain: None,
        });
        current.memory.qdrant.api_key = Some("qdrant-real".to_string());
        current.channels_config.wati = Some(crate::config::schema::WatiConfig {
            api_token: "wati-real".to_string(),
            api_url: "https://live-mt-server.wati.io".to_string(),
            tenant_id: None,
            allowed_numbers: vec![],
        });
        current.channels_config.feishu = Some(crate::config::schema::FeishuConfig {
            app_id: "cli_current".to_string(),
            app_secret: "feishu-secret-real".to_string(),
            encrypt_key: Some("feishu-encrypt-real".to_string()),
            verification_token: Some("feishu-verify-real".to_string()),
            allowed_users: vec!["*".to_string()],
            receive_mode: crate::config::schema::LarkReceiveMode::Websocket,
            port: None,
        });
        current.channels_config.email = Some(crate::channels::email_channel::EmailConfig {
            imap_host: "imap.example.com".to_string(),
            imap_port: 993,
            imap_folder: "INBOX".to_string(),
            smtp_host: "smtp.example.com".to_string(),
            smtp_port: 465,
            smtp_tls: true,
            username: "agent@example.com".to_string(),
            password: "email-password-real".to_string(),
            from_address: "agent@example.com".to_string(),
            idle_timeout_secs: 1740,
            allowed_senders: vec!["*".to_string()],
            default_subject: "ZeroClaw Message".to_string(),
        });
        current.model_routes = vec![
            crate::config::schema::ModelRouteConfig {
                hint: "reasoning".to_string(),
                provider: "openrouter".to_string(),
                model: "anthropic/claude-sonnet-4.6".to_string(),
                api_key: Some("route-model-key-1".to_string()),
            },
            crate::config::schema::ModelRouteConfig {
                hint: "fast".to_string(),
                provider: "openrouter".to_string(),
                model: "openai/gpt-4.1-mini".to_string(),
                api_key: Some("route-model-key-2".to_string()),
            },
        ];
        current.embedding_routes = vec![
            crate::config::schema::EmbeddingRouteConfig {
                hint: "semantic".to_string(),
                provider: "openai".to_string(),
                model: "text-embedding-3-small".to_string(),
                dimensions: Some(1536),
                api_key: Some("route-embed-key-1".to_string()),
            },
            crate::config::schema::EmbeddingRouteConfig {
                hint: "archive".to_string(),
                provider: "custom:https://emb.example.com/v1".to_string(),
                model: "bge-m3".to_string(),
                dimensions: Some(1024),
                api_key: Some("route-embed-key-2".to_string()),
            },
        ];

        let mut incoming = mask_sensitive_fields(&current);
        incoming.default_model = Some("gpt-4.1-mini".to_string());
        // Simulate UI changing only one key and keeping the first masked.
        incoming.reliability.api_keys = vec![MASKED_SECRET.to_string(), "r2-new".to_string()];
        incoming.gateway.paired_tokens = vec![MASKED_SECRET.to_string(), "pair-2-new".to_string()];
        if let Some(cloudflare) = incoming.tunnel.cloudflare.as_mut() {
            cloudflare.token = MASKED_SECRET.to_string();
        }
        if let Some(ngrok) = incoming.tunnel.ngrok.as_mut() {
            ngrok.auth_token = MASKED_SECRET.to_string();
        }
        incoming.memory.qdrant.api_key = Some(MASKED_SECRET.to_string());
        if let Some(wati) = incoming.channels_config.wati.as_mut() {
            wati.api_token = MASKED_SECRET.to_string();
        }
        if let Some(feishu) = incoming.channels_config.feishu.as_mut() {
            feishu.app_secret = MASKED_SECRET.to_string();
            feishu.encrypt_key = Some(MASKED_SECRET.to_string());
            feishu.verification_token = Some("feishu-verify-new".to_string());
        }
        if let Some(email) = incoming.channels_config.email.as_mut() {
            email.password = MASKED_SECRET.to_string();
        }
        incoming.model_routes[1].api_key = Some("route-model-key-2-new".to_string());
        incoming.embedding_routes[1].api_key = Some("route-embed-key-2-new".to_string());

        let hydrated = hydrate_config_for_save(incoming, &current);

        assert_eq!(hydrated.config_path, current.config_path);
        assert_eq!(hydrated.workspace_dir, current.workspace_dir);
        assert_eq!(hydrated.api_key, current.api_key);
        assert_eq!(hydrated.default_model.as_deref(), Some("gpt-4.1-mini"));
        assert_eq!(
            hydrated.reliability.api_keys,
            vec!["r1".to_string(), "r2-new".to_string()]
        );
        assert_eq!(
            hydrated.gateway.paired_tokens,
            vec!["pair-1".to_string(), "pair-2-new".to_string()]
        );
        assert_eq!(
            hydrated
                .tunnel
                .cloudflare
                .as_ref()
                .map(|v| v.token.as_str()),
            Some("cf-token-real")
        );
        assert_eq!(
            hydrated
                .tunnel
                .ngrok
                .as_ref()
                .map(|v| v.auth_token.as_str()),
            Some("ngrok-token-real")
        );
        assert_eq!(
            hydrated.memory.qdrant.api_key.as_deref(),
            Some("qdrant-real")
        );
        assert_eq!(
            hydrated
                .channels_config
                .wati
                .as_ref()
                .map(|v| v.api_token.as_str()),
            Some("wati-real")
        );
        assert_eq!(
            hydrated
                .channels_config
                .feishu
                .as_ref()
                .map(|v| v.app_secret.as_str()),
            Some("feishu-secret-real")
        );
        assert_eq!(
            hydrated
                .channels_config
                .feishu
                .as_ref()
                .and_then(|v| v.encrypt_key.as_deref()),
            Some("feishu-encrypt-real")
        );
        assert_eq!(
            hydrated
                .channels_config
                .feishu
                .as_ref()
                .and_then(|v| v.verification_token.as_deref()),
            Some("feishu-verify-new")
        );
        assert_eq!(
            hydrated.model_routes[0].api_key.as_deref(),
            Some("route-model-key-1")
        );
        assert_eq!(
            hydrated.model_routes[1].api_key.as_deref(),
            Some("route-model-key-2-new")
        );
        assert_eq!(
            hydrated.embedding_routes[0].api_key.as_deref(),
            Some("route-embed-key-1")
        );
        assert_eq!(
            hydrated.embedding_routes[1].api_key.as_deref(),
            Some("route-embed-key-2-new")
        );
        assert_eq!(
            hydrated
                .channels_config
                .email
                .as_ref()
                .map(|v| v.password.as_str()),
            Some("email-password-real")
        );
    }

    #[test]
    fn hydrate_config_for_save_restores_route_keys_by_identity_and_clears_unmatched_masks() {
        let mut current = crate::config::Config::default();
        current.model_routes = vec![
            crate::config::schema::ModelRouteConfig {
                hint: "reasoning".to_string(),
                provider: "openrouter".to_string(),
                model: "anthropic/claude-sonnet-4.6".to_string(),
                api_key: Some("route-model-key-1".to_string()),
            },
            crate::config::schema::ModelRouteConfig {
                hint: "fast".to_string(),
                provider: "openrouter".to_string(),
                model: "openai/gpt-4.1-mini".to_string(),
                api_key: Some("route-model-key-2".to_string()),
            },
        ];
        current.embedding_routes = vec![
            crate::config::schema::EmbeddingRouteConfig {
                hint: "semantic".to_string(),
                provider: "openai".to_string(),
                model: "text-embedding-3-small".to_string(),
                dimensions: Some(1536),
                api_key: Some("route-embed-key-1".to_string()),
            },
            crate::config::schema::EmbeddingRouteConfig {
                hint: "archive".to_string(),
                provider: "custom:https://emb.example.com/v1".to_string(),
                model: "bge-m3".to_string(),
                dimensions: Some(1024),
                api_key: Some("route-embed-key-2".to_string()),
            },
        ];

        let mut incoming = mask_sensitive_fields(&current);
        incoming.model_routes.swap(0, 1);
        incoming.embedding_routes.swap(0, 1);
        incoming
            .model_routes
            .push(crate::config::schema::ModelRouteConfig {
                hint: "new".to_string(),
                provider: "openai".to_string(),
                model: "gpt-4.1".to_string(),
                api_key: Some(MASKED_SECRET.to_string()),
            });
        incoming
            .embedding_routes
            .push(crate::config::schema::EmbeddingRouteConfig {
                hint: "new-embed".to_string(),
                provider: "custom:https://emb2.example.com/v1".to_string(),
                model: "bge-small".to_string(),
                dimensions: Some(768),
                api_key: Some(MASKED_SECRET.to_string()),
            });

        let hydrated = hydrate_config_for_save(incoming, &current);

        assert_eq!(
            hydrated.model_routes[0].api_key.as_deref(),
            Some("route-model-key-2")
        );
        assert_eq!(
            hydrated.model_routes[1].api_key.as_deref(),
            Some("route-model-key-1")
        );
        assert_eq!(hydrated.model_routes[2].api_key, None);
        assert_eq!(
            hydrated.embedding_routes[0].api_key.as_deref(),
            Some("route-embed-key-2")
        );
        assert_eq!(
            hydrated.embedding_routes[1].api_key.as_deref(),
            Some("route-embed-key-1")
        );
        assert_eq!(hydrated.embedding_routes[2].api_key, None);
        assert!(hydrated
            .model_routes
            .iter()
            .all(|route| route.api_key.as_deref() != Some(MASKED_SECRET)));
        assert!(hydrated
            .embedding_routes
            .iter()
            .all(|route| route.api_key.as_deref() != Some(MASKED_SECRET)));
    }
}
