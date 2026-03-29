use crate::config::schema::AgentSocialAccountsConfig;
use crate::config::{Config, DelegateAgentConfig};
use crate::soul::{SoulProfile, SoulStyle};
use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use tokio::fs;

const STUDIO_STATE_VERSION: u32 = 1;
const STUDIO_STATE_FILE: &str = "state/agent_studio.json";
const DEFAULT_AGENT_ID: &str = "agent";
const DEFAULT_AGENT_NAME: &str = "Agent";
const UNIVERSAL_BROWSER_TOOL_GRANTS: &[&str] = &["browser_headless"];
const UNIVERSAL_SKILL_GRANTS: &[&str] = &["browser-operator"];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AgentClassStatus {
    Active,
    ComingSoon,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct SoulProfileOverlay {
    #[serde(default)]
    pub voice: Option<String>,
    #[serde(default)]
    pub principles: Vec<String>,
    #[serde(default)]
    pub boundaries: Vec<String>,
    #[serde(default)]
    pub style: Option<SoulStyle>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct IdentityOverlay {
    #[serde(default)]
    pub creature: Option<String>,
    #[serde(default)]
    pub vibe: Option<String>,
    #[serde(default)]
    pub emoji: Option<String>,
    #[serde(default)]
    pub role_title: Option<String>,
    #[serde(default)]
    pub tagline: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AgentClassManifest {
    pub version: u32,
    pub id: String,
    pub name: String,
    pub status: AgentClassStatus,
    pub description: String,
    pub fantasy_theme: String,
    pub default_role_summary: String,
    #[serde(default)]
    pub default_soul_overlay: SoulProfileOverlay,
    #[serde(default)]
    pub default_identity_overlay: IdentityOverlay,
    #[serde(default)]
    pub tool_grants: Vec<String>,
    #[serde(default)]
    pub skill_grants: Vec<String>,
    #[serde(default)]
    pub channel_affinities: Vec<String>,
    #[serde(default)]
    pub integration_affinities: Vec<String>,
    #[serde(default)]
    pub guardrails: Vec<String>,
    #[serde(default)]
    pub evaluation_scenarios: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentProfileOverrides {
    #[serde(default)]
    pub summary: Option<String>,
    #[serde(default)]
    pub system_prompt_appendix: Option<String>,
    #[serde(default)]
    pub provider: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub temperature: Option<f64>,
    #[serde(default)]
    pub max_depth: Option<u32>,
    #[serde(default)]
    pub agentic: Option<bool>,
    #[serde(default)]
    pub max_iterations: Option<usize>,
    #[serde(default)]
    pub tool_grants: Vec<String>,
    #[serde(default)]
    pub skill_grants: Vec<String>,
    #[serde(default)]
    pub soul: SoulProfileOverlay,
    #[serde(default)]
    pub identity: IdentityOverlay,
}

impl Default for AgentProfileOverrides {
    fn default() -> Self {
        Self {
            summary: None,
            system_prompt_appendix: None,
            provider: None,
            model: None,
            temperature: None,
            max_depth: None,
            agentic: None,
            max_iterations: None,
            tool_grants: Vec::new(),
            skill_grants: Vec::new(),
            soul: SoulProfileOverlay::default(),
            identity: IdentityOverlay::default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentProfile {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub avatar: Option<String>,
    #[serde(default)]
    pub launch_on_startup: bool,
    pub primary_class: String,
    #[serde(default)]
    pub secondary_classes: Vec<String>,
    #[serde(default)]
    pub social_accounts: AgentSocialAccountsConfig,
    #[serde(default)]
    pub overrides: AgentProfileOverrides,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentStudioState {
    pub version: u32,
    pub onboarding_completed: bool,
    pub active_agent_id: String,
    pub profiles: Vec<AgentProfile>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResolvedIdentity {
    pub name: String,
    pub creature: String,
    pub vibe: String,
    pub emoji: String,
    pub role_title: String,
    pub tagline: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResolvedAgentProfile {
    pub profile: AgentProfile,
    pub classes: Vec<AgentClassManifest>,
    pub summary: String,
    pub soul: SoulProfile,
    pub identity: ResolvedIdentity,
    pub tool_grants: Vec<String>,
    pub skill_grants: Vec<String>,
    pub guardrails: Vec<String>,
    pub evaluation_scenarios: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OnboardingState {
    pub version: u32,
    pub completed: bool,
    pub active_agent_id: String,
    pub startup_agent_id: Option<String>,
    pub has_provider_config: bool,
    pub runtime_ready: bool,
    pub profile_count: usize,
}

pub fn built_in_classes() -> Vec<AgentClassManifest> {
    vec![
        AgentClassManifest {
            version: 1,
            id: "social_media_manager".into(),
            name: "Social Media Manager".into(),
            status: AgentClassStatus::Active,
            description: "Plans, drafts, publishes, and coordinates social presence across channels with a campaign mindset.".into(),
            fantasy_theme: "Campaign tactician".into(),
            default_role_summary: "Lead social strategy, content planning, account coordination, and publishing with sharp brand judgment.".into(),
            default_soul_overlay: SoulProfileOverlay {
                voice: Some("strategic energetic polished".into()),
                principles: vec![
                    "Protect brand trust while staying human.".into(),
                    "Prefer audience-aware messaging over generic hype.".into(),
                    "Turn research into concrete posting plans.".into(),
                ],
                boundaries: vec![
                    "Never publish externally without explicit user intent or approved workflow.".into(),
                    "Do not fake metrics, testimonials, or engagement.".into(),
                ],
                style: Some(SoulStyle {
                    emoji: "light".into(),
                    emphasis: "light".into(),
                    brevity: "short".into(),
                }),
            },
            default_identity_overlay: IdentityOverlay {
                creature: Some("a field commander for online attention".into()),
                vibe: Some("fast, sharp, trend-aware, and surprisingly tasteful".into()),
                emoji: Some("📡".into()),
                role_title: Some("Social Media Manager".into()),
                tagline: Some("Campaign-minded operator for content, channels, and momentum.".into()),
            },
            tool_grants: vec![
                "web_search".into(),
                "web_fetch".into(),
                "browser_open".into(),
                "browser_headless".into(),
                "twitter_mcp".into(),
                "schedule".into(),
                "memory_recall".into(),
                "memory_store".into(),
                "content_search".into(),
                "file_read".into(),
            ],
            skill_grants: vec!["social-media-manager".into()],
            channel_affinities: vec!["twitter_x".into(), "discord".into(), "telegram".into()],
            integration_affinities: vec!["browser_headless".into(), "browser_bridge".into()],
            guardrails: vec![
                "Route external posts through explicit publish/approval moments.".into(),
                "Surface missing brand context instead of inventing it.".into(),
            ],
            evaluation_scenarios: vec![
                "Draft a one-week content calendar from a product launch brief.".into(),
                "Turn a rough founder note into three channel-specific post variants.".into(),
            ],
        },
        AgentClassManifest {
            version: 1,
            id: "va".into(),
            name: "VA".into(),
            status: AgentClassStatus::Active,
            description: "Administrative operator for coordination, reminders, inbox triage, and practical follow-through.".into(),
            fantasy_theme: "Operations steward".into(),
            default_role_summary: "Handle practical coordination, scheduling, reminders, inbox support, and lightweight research with calm reliability.".into(),
            default_soul_overlay: SoulProfileOverlay {
                voice: Some("calm organized dependable".into()),
                principles: vec![
                    "Reduce cognitive load with clear next steps.".into(),
                    "Keep records current and action-oriented.".into(),
                    "Favor reliable follow-through over flourish.".into(),
                ],
                boundaries: vec![
                    "Do not send messages or emails as the user without approval.".into(),
                    "Escalate when instructions are ambiguous and externally visible.".into(),
                ],
                style: Some(SoulStyle {
                    emoji: "off".into(),
                    emphasis: "plain".into(),
                    brevity: "short".into(),
                }),
            },
            default_identity_overlay: IdentityOverlay {
                creature: Some("a composed logistics companion".into()),
                vibe: Some("steady, clear, and low-drama".into()),
                emoji: Some("🗂️".into()),
                role_title: Some("Virtual Assistant".into()),
                tagline: Some("Turns loose tasks into orderly action.".into()),
            },
            tool_grants: vec![
                "schedule".into(),
                "cron_add".into(),
                "cron_list".into(),
                "cron_update".into(),
                "memory_recall".into(),
                "memory_store".into(),
                "file_read".into(),
                "file_write".into(),
                "glob_search".into(),
                "web_fetch".into(),
            ],
            skill_grants: Vec::new(),
            channel_affinities: vec!["email".into(), "telegram".into()],
            integration_affinities: vec!["cron".into()],
            guardrails: vec![
                "Keep externally visible communication supervised.".into(),
                "Preserve user context accurately when scheduling or tracking work.".into(),
            ],
            evaluation_scenarios: vec![
                "Convert a messy task dump into a prioritized action plan.".into(),
                "Set recurring reminders and summarize upcoming obligations.".into(),
            ],
        },
        AgentClassManifest {
            version: 1,
            id: "sales".into(),
            name: "Sales".into(),
            status: AgentClassStatus::Active,
            description: "Runs AI SDR workflows: finds prospects, qualifies accounts, researches targets, drafts outreach, triages replies, and prepares meeting handoffs.".into(),
            fantasy_theme: "Pipeline closer".into(),
            default_role_summary: "Generate pipeline through disciplined prospecting, account qualification, personalized outreach prep, reply triage, and clean human handoffs.".into(),
            default_soul_overlay: SoulProfileOverlay {
                voice: Some("sharp commercial evidence-driven".into()),
                principles: vec![
                    "Qualify before outreach.".into(),
                    "Personalize from evidence, not filler.".into(),
                    "Optimize for booked conversations and healthy pipeline, not noisy volume.".into(),
                ],
                boundaries: vec![
                    "Do not send first-touch outbound messages without explicit approval or an approved automation policy.".into(),
                    "Never fabricate prospect facts, company pain points, or personalization hooks.".into(),
                    "Respect stop signals, opt-outs, and reputational risk.".into(),
                ],
                style: Some(SoulStyle {
                    emoji: "off".into(),
                    emphasis: "plain".into(),
                    brevity: "short".into(),
                }),
            },
            default_identity_overlay: IdentityOverlay {
                creature: Some("a relentless but disciplined pipeline operator".into()),
                vibe: Some("commercial, methodical, and useful under pressure".into()),
                emoji: Some("💼".into()),
                role_title: Some("Sales Agent".into()),
                tagline: Some("Turns research into qualified pipeline and ready-to-send outreach.".into()),
            },
            tool_grants: vec![
                "web_search".into(),
                "web_fetch".into(),
                "browser_open".into(),
                "browser_headless".into(),
                "schedule".into(),
                "mail".into(),
                "memory_recall".into(),
                "memory_store".into(),
                "content_search".into(),
                "file_read".into(),
                "file_write".into(),
                "glob_search".into(),
            ],
            skill_grants: vec![
                "sales-prospector".into(),
                "sales-icp-qualifier".into(),
                "sales-account-researcher".into(),
                "sales-personalization-writer".into(),
                "sales-followup-planner".into(),
                "sales-reply-triage".into(),
                "sales-meeting-handoff".into(),
                "sales-pipeline-reporter".into(),
            ],
            channel_affinities: vec!["email".into(), "slack".into(), "telegram".into()],
            integration_affinities: vec!["browser_headless".into(), "schedule".into(), "mail".into()],
            guardrails: vec![
                "Treat first-touch outbound as approval-gated unless the user explicitly authorizes autonomous sending.".into(),
                "Do qualification and account research before drafting outreach.".into(),
                "When evidence is weak, say so instead of pretending personalization exists.".into(),
            ],
            evaluation_scenarios: vec![
                "Build a ranked prospect list from a narrow ICP with evidence for fit.".into(),
                "Draft a personalized first-touch email and two follow-ups from real account research.".into(),
                "Classify inbound replies and prepare a clean meeting handoff summary.".into(),
            ],
        },
        placeholder_manifest(
            "tester",
            "Tester",
            "Dungeon diagnostician",
            "Breaks flows, probes regressions, and documents defects once the class pack is ready.",
        ),
        placeholder_manifest(
            "bizdev",
            "BizDev",
            "Alliance broker",
            "Builds partnerships, lead maps, and opportunity pipelines once the class pack is ready.",
        ),
    ]
}

fn placeholder_manifest(
    id: &str,
    name: &str,
    fantasy_theme: &str,
    description: &str,
) -> AgentClassManifest {
    AgentClassManifest {
        version: 1,
        id: id.into(),
        name: name.into(),
        status: AgentClassStatus::ComingSoon,
        description: description.into(),
        fantasy_theme: fantasy_theme.into(),
        default_role_summary: "Coming soon.".into(),
        default_soul_overlay: SoulProfileOverlay::default(),
        default_identity_overlay: IdentityOverlay::default(),
        tool_grants: Vec::new(),
        skill_grants: Vec::new(),
        channel_affinities: Vec::new(),
        integration_affinities: Vec::new(),
        guardrails: vec!["Class pack not yet selectable.".into()],
        evaluation_scenarios: vec!["Coming soon.".into()],
    }
}

pub fn class_by_id(id: &str) -> Option<AgentClassManifest> {
    built_in_classes()
        .into_iter()
        .find(|class_| class_.id == id)
}

pub async fn load_or_bootstrap(config: &Config) -> Result<AgentStudioState> {
    let path = state_path(&config.workspace_dir);
    if path.exists() {
        let raw = fs::read_to_string(&path)
            .await
            .with_context(|| format!("Failed to read {}", path.display()))?;
        let mut state: AgentStudioState =
            serde_json::from_str(&raw).context("Failed to parse agent studio state")?;
        normalize_state(&mut state, config)?;
        return Ok(state);
    }

    let mut state = migrate_legacy_workspace(config).await?;
    normalize_state(&mut state, config)?;
    save_state(&config.workspace_dir, &state).await?;
    Ok(state)
}

pub async fn save_state(workspace_dir: &Path, state: &AgentStudioState) -> Result<()> {
    let path = state_path(workspace_dir);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).await?;
    }
    let payload = serde_json::to_string_pretty(state)?;
    fs::write(&path, payload)
        .await
        .with_context(|| format!("Failed to write {}", path.display()))?;
    Ok(())
}

pub fn onboarding_state(config: &Config, state: &AgentStudioState) -> OnboardingState {
    OnboardingState {
        version: state.version,
        completed: state.onboarding_completed,
        active_agent_id: state.active_agent_id.clone(),
        startup_agent_id: startup_agent_id(state),
        has_provider_config: config.default_provider.is_some() && config.default_model.is_some(),
        runtime_ready: config.gateway.port > 0,
        profile_count: state.profiles.len(),
    }
}

pub fn resolve_profile(state: &AgentStudioState, agent_id: &str) -> Result<ResolvedAgentProfile> {
    let profile = state
        .profiles
        .iter()
        .find(|profile| profile.id == agent_id)
        .cloned()
        .ok_or_else(|| anyhow!("Unknown agent '{agent_id}'"))?;
    resolve_profile_record(&profile)
}

pub fn resolve_active_profile(state: &AgentStudioState) -> Result<ResolvedAgentProfile> {
    resolve_profile(state, &state.active_agent_id)
}

pub async fn set_active_agent(
    config: &mut Config,
    state: &mut AgentStudioState,
    agent_id: &str,
) -> Result<ResolvedAgentProfile> {
    let resolved = resolve_profile(state, agent_id)?;
    state.active_agent_id = agent_id.to_string();
    save_state(&config.workspace_dir, state).await?;
    apply_state_to_runtime(config, state).await?;
    Ok(resolved)
}

pub async fn upsert_profile(
    config: &mut Config,
    state: &mut AgentStudioState,
    profile: AgentProfile,
    activate: bool,
) -> Result<ResolvedAgentProfile> {
    validate_profile(&profile)?;
    if let Some(existing) = state.profiles.iter_mut().find(|item| item.id == profile.id) {
        *existing = profile.clone();
    } else {
        state.profiles.push(profile.clone());
    }
    if activate {
        state.active_agent_id = profile.id.clone();
    }
    normalize_state(state, config)?;
    save_state(&config.workspace_dir, state).await?;
    apply_state_to_runtime(config, state).await?;
    resolve_profile(state, &profile.id)
}

pub async fn complete_onboarding(
    config: &mut Config,
    state: &mut AgentStudioState,
    active_agent_id: Option<&str>,
) -> Result<ResolvedAgentProfile> {
    if let Some(agent_id) = active_agent_id {
        state.active_agent_id = agent_id.to_string();
    }
    state.onboarding_completed = true;
    save_state(&config.workspace_dir, state).await?;
    apply_state_to_runtime(config, state).await?;
    resolve_active_profile(state)
}

pub async fn apply_state_to_runtime(config: &mut Config, state: &AgentStudioState) -> Result<()> {
    let mut normalized = state.clone();
    normalize_state(&mut normalized, config)?;
    let state = &normalized;
    let active = resolve_active_profile(state)?;
    let identity_path = config.workspace_dir.join("IDENTITY.md");
    let soul_path = config.workspace_dir.join("SOUL.md");
    let identity = render_identity_markdown(&active.identity, &active.classes, &active.summary);
    let soul = crate::soul::render_soul(&active.soul);
    fs::write(&identity_path, identity).await?;
    fs::write(&soul_path, soul).await?;

    config.agent.social_accounts = active.profile.social_accounts.clone();

    for profile in &state.profiles {
        let resolved = resolve_profile_record(profile)?;
        config.agents.insert(
            profile.id.clone(),
            DelegateAgentConfig {
                provider: resolved
                    .profile
                    .overrides
                    .provider
                    .clone()
                    .or_else(|| config.default_provider.clone())
                    .unwrap_or_else(|| "openrouter".into()),
                model: resolved
                    .profile
                    .overrides
                    .model
                    .clone()
                    .or_else(|| config.default_model.clone())
                    .unwrap_or_else(|| "gpt-4.1-mini".into()),
                system_prompt: Some(render_system_prompt(
                    &config.workspace_dir,
                    config.skills.prompt_injection_mode,
                    &resolved,
                )),
                api_key: None,
                temperature: resolved
                    .profile
                    .overrides
                    .temperature
                    .or(Some(config.default_temperature)),
                max_depth: resolved.profile.overrides.max_depth.unwrap_or(3),
                agentic: resolved.profile.overrides.agentic.unwrap_or(true),
                allowed_tools: resolved.tool_grants.clone(),
                max_iterations: resolved.profile.overrides.max_iterations.unwrap_or(8),
                social_accounts: resolved.profile.social_accounts.clone(),
            },
        );
    }

    config.save().await?;
    Ok(())
}

fn resolve_profile_record(profile: &AgentProfile) -> Result<ResolvedAgentProfile> {
    validate_profile(profile)?;

    let mut class_manifests = Vec::new();
    class_manifests.push(
        class_by_id(&profile.primary_class)
            .ok_or_else(|| anyhow!("Unknown class '{}'", profile.primary_class))?,
    );
    for class_id in &profile.secondary_classes {
        class_manifests
            .push(class_by_id(class_id).ok_or_else(|| anyhow!("Unknown class '{class_id}'"))?);
    }

    let mut soul = SoulProfile {
        name: profile.name.clone(),
        voice: "capable warm direct".into(),
        principles: vec![
            "Be truthful and operationally useful.".into(),
            "Prefer concrete next steps over vague encouragement.".into(),
        ],
        boundaries: vec![
            "Do not act externally without clear user intent.".into(),
            "Protect private information and secrets.".into(),
        ],
        style: SoulStyle {
            emoji: "light".into(),
            emphasis: "light".into(),
            brevity: "short".into(),
        },
    };
    let mut identity = ResolvedIdentity {
        name: profile.name.clone(),
        creature: "a local-first AI operative".into(),
        vibe: "intuitive, decisive, and human-friendly".into(),
        emoji: "⚔️".into(),
        role_title: "Agent".into(),
        tagline: "A configurable digital operative for practical work.".into(),
    };
    let mut tool_grants = BTreeSet::new();
    let mut skill_grants = BTreeSet::new();
    let mut guardrails = BTreeSet::new();
    let mut evaluation_scenarios = BTreeSet::new();
    let mut summary_parts = Vec::new();

    for class_ in &class_manifests {
        apply_soul_overlay(&mut soul, &class_.default_soul_overlay);
        apply_identity_overlay(&mut identity, &class_.default_identity_overlay);
        tool_grants.extend(class_.tool_grants.iter().cloned());
        skill_grants.extend(class_.skill_grants.iter().cloned());
        guardrails.extend(class_.guardrails.iter().cloned());
        evaluation_scenarios.extend(class_.evaluation_scenarios.iter().cloned());
        if !class_.default_role_summary.trim().is_empty() {
            summary_parts.push(class_.default_role_summary.clone());
        }
    }

    apply_soul_overlay(&mut soul, &profile.overrides.soul);
    apply_identity_overlay(&mut identity, &profile.overrides.identity);
    tool_grants.extend(
        UNIVERSAL_BROWSER_TOOL_GRANTS
            .iter()
            .map(|grant| (*grant).to_string()),
    );
    skill_grants.extend(
        UNIVERSAL_SKILL_GRANTS
            .iter()
            .map(|grant| (*grant).to_string()),
    );
    tool_grants.extend(profile.overrides.tool_grants.iter().cloned());
    skill_grants.extend(profile.overrides.skill_grants.iter().cloned());

    let summary = profile
        .overrides
        .summary
        .clone()
        .unwrap_or_else(|| summary_parts.join(" "));

    Ok(ResolvedAgentProfile {
        profile: profile.clone(),
        classes: class_manifests,
        summary,
        soul,
        identity,
        tool_grants: tool_grants.into_iter().collect(),
        skill_grants: skill_grants.into_iter().collect(),
        guardrails: guardrails.into_iter().collect(),
        evaluation_scenarios: evaluation_scenarios.into_iter().collect(),
    })
}

fn apply_soul_overlay(target: &mut SoulProfile, overlay: &SoulProfileOverlay) {
    if let Some(voice) = &overlay.voice {
        target.voice = voice.clone();
    }
    extend_unique(&mut target.principles, &overlay.principles);
    extend_unique(&mut target.boundaries, &overlay.boundaries);
    if let Some(style) = &overlay.style {
        target.style = style.clone();
    }
}

fn apply_identity_overlay(target: &mut ResolvedIdentity, overlay: &IdentityOverlay) {
    if let Some(creature) = &overlay.creature {
        target.creature = creature.clone();
    }
    if let Some(vibe) = &overlay.vibe {
        target.vibe = vibe.clone();
    }
    if let Some(emoji) = &overlay.emoji {
        target.emoji = emoji.clone();
    }
    if let Some(role_title) = &overlay.role_title {
        target.role_title = role_title.clone();
    }
    if let Some(tagline) = &overlay.tagline {
        target.tagline = tagline.clone();
    }
}

fn extend_unique(target: &mut Vec<String>, incoming: &[String]) {
    for item in incoming {
        if !target.contains(item) {
            target.push(item.clone());
        }
    }
}

fn render_identity_markdown(
    identity: &ResolvedIdentity,
    classes: &[AgentClassManifest],
    summary: &str,
) -> String {
    let class_label = classes
        .iter()
        .map(|class_| class_.name.as_str())
        .collect::<Vec<_>>()
        .join(" / ");
    format!(
        "# IDENTITY.md — Who Am I?\n\n\
         - **Name:** {name}\n\
         - **Creature:** {creature}\n\
         - **Vibe:** {vibe}\n\
         - **Emoji:** {emoji}\n\
         - **Role:** {role}\n\
         - **Classes:** {class_label}\n\n\
         ---\n\n\
         {summary}\n\n\
         {tagline}\n",
        name = identity.name,
        creature = identity.creature,
        vibe = identity.vibe,
        emoji = identity.emoji,
        role = identity.role_title,
        tagline = identity.tagline,
    )
}

fn render_system_prompt(
    workspace_dir: &Path,
    skills_prompt_mode: crate::config::SkillsPromptInjectionMode,
    resolved: &ResolvedAgentProfile,
) -> String {
    let classes = resolved
        .classes
        .iter()
        .map(|class_| class_.name.as_str())
        .collect::<Vec<_>>()
        .join(" / ");
    let guardrails = resolved
        .guardrails
        .iter()
        .map(|item| format!("- {item}"))
        .collect::<Vec<_>>()
        .join("\n");
    let appendix = resolved
        .profile
        .overrides
        .system_prompt_appendix
        .clone()
        .unwrap_or_default();
    let skills = crate::skills::filter_skills_by_name(
        &crate::skills::load_skills(workspace_dir),
        &resolved.skill_grants,
    );
    let skills_block = if skills.is_empty() {
        String::new()
    } else {
        format!(
            "\n\nSkills:\n{}",
            crate::skills::skills_to_prompt_with_mode(&skills, workspace_dir, skills_prompt_mode,)
        )
    };
    format!(
        "You are {name}, a {classes} specialist.\n\n\
         Mission:\n{summary}\n\n\
         Guardrails:\n{guardrails}\n\n\
         Use only the tools granted to this class build. Treat externally visible actions as supervised unless the user made the intent explicit.{skills_block}\n\n\
         {appendix}",
        name = resolved.profile.name,
        summary = resolved.summary,
    )
}

fn validate_profile(profile: &AgentProfile) -> Result<()> {
    if profile.id.trim().is_empty() {
        bail!("Agent id must not be empty");
    }
    if profile.name.trim().is_empty() {
        bail!("Agent name must not be empty");
    }

    let primary = class_by_id(&profile.primary_class)
        .ok_or_else(|| anyhow!("Unknown class '{}'", profile.primary_class))?;
    if primary.status == AgentClassStatus::ComingSoon {
        bail!(
            "Class '{}' is visible but not selectable yet",
            profile.primary_class
        );
    }

    let mut seen = BTreeSet::new();
    for class_id in &profile.secondary_classes {
        let class_ = class_by_id(class_id).ok_or_else(|| anyhow!("Unknown class '{class_id}'"))?;
        if class_.status == AgentClassStatus::ComingSoon {
            bail!("Class '{class_id}' is visible but not selectable yet");
        }
        if !seen.insert(class_id.clone()) {
            bail!("Duplicate secondary class '{class_id}'");
        }
    }
    Ok(())
}

fn normalize_state(state: &mut AgentStudioState, config: &Config) -> Result<()> {
    if state.version == 0 {
        state.version = STUDIO_STATE_VERSION;
    }

    let mut deduped_profiles = Vec::new();
    let mut seen_ids = BTreeSet::new();
    for profile in state.profiles.drain(..) {
        let normalized_id = profile.id.trim().to_ascii_lowercase();
        if seen_ids.insert(normalized_id) {
            deduped_profiles.push(profile);
        }
    }
    state.profiles = deduped_profiles;

    if state.profiles.is_empty() {
        state
            .profiles
            .push(default_profile_from_workspace(&config.workspace_dir));
    }
    if state.active_agent_id.trim().is_empty()
        || !state
            .profiles
            .iter()
            .any(|profile| profile.id == state.active_agent_id)
    {
        state.active_agent_id = state.profiles[0].id.clone();
    }

    let startup_ids = state
        .profiles
        .iter()
        .filter(|profile| profile.launch_on_startup)
        .map(|profile| profile.id.clone())
        .collect::<Vec<_>>();
    if startup_ids.len() > 1 {
        let keep = startup_ids[0].clone();
        for profile in &mut state.profiles {
            profile.launch_on_startup = profile.id == keep;
        }
    }
    if let Some(startup_agent_id) = startup_agent_id(state) {
        state.active_agent_id = startup_agent_id;
    }

    for profile in &state.profiles {
        validate_profile(profile)?;
    }

    if !config.workspace_dir.exists() {
        bail!("Workspace directory does not exist");
    }
    Ok(())
}

fn startup_agent_id(state: &AgentStudioState) -> Option<String> {
    state
        .profiles
        .iter()
        .find(|profile| profile.launch_on_startup)
        .map(|profile| profile.id.clone())
}

async fn migrate_legacy_workspace(config: &Config) -> Result<AgentStudioState> {
    let mut profiles = vec![default_profile_from_workspace(&config.workspace_dir)];
    for (id, agent) in &config.agents {
        profiles.push(AgentProfile {
            id: id.clone(),
            name: id.replace('_', " "),
            avatar: None,
            launch_on_startup: false,
            primary_class: "va".into(),
            secondary_classes: Vec::new(),
            social_accounts: agent.social_accounts.clone(),
            overrides: AgentProfileOverrides {
                provider: Some(agent.provider.clone()),
                model: Some(agent.model.clone()),
                temperature: agent.temperature,
                max_depth: Some(agent.max_depth),
                agentic: Some(agent.agentic),
                max_iterations: Some(agent.max_iterations),
                system_prompt_appendix: agent.system_prompt.clone(),
                ..AgentProfileOverrides::default()
            },
        });
    }

    if let Some(name) = detect_identity_name(&config.workspace_dir).await? {
        profiles[0].name = name;
    }

    Ok(AgentStudioState {
        version: STUDIO_STATE_VERSION,
        onboarding_completed: config.default_provider.is_some() && config.default_model.is_some(),
        active_agent_id: profiles[0].id.clone(),
        profiles,
    })
}

fn detect_identity_name_from_content(content: &str) -> Option<String> {
    for line in content.lines() {
        let trimmed = line.trim();
        if let Some(rest) = trimmed.strip_prefix("- **Name:**") {
            let value = rest.trim();
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

async fn detect_identity_name(workspace_dir: &Path) -> Result<Option<String>> {
    let path = workspace_dir.join("IDENTITY.md");
    if !path.exists() {
        return Ok(None);
    }
    let content = fs::read_to_string(path).await?;
    Ok(detect_identity_name_from_content(&content))
}

fn detect_identity_name_sync(workspace_dir: &Path) -> Option<String> {
    let path = workspace_dir.join("IDENTITY.md");
    let content = std::fs::read_to_string(path).ok()?;
    detect_identity_name_from_content(&content)
}

fn default_profile_from_workspace(workspace_dir: &Path) -> AgentProfile {
    let name =
        detect_identity_name_sync(workspace_dir).unwrap_or_else(|| DEFAULT_AGENT_NAME.into());
    AgentProfile {
        id: DEFAULT_AGENT_ID.into(),
        name,
        avatar: Some("local operator".into()),
        launch_on_startup: false,
        primary_class: "va".into(),
        secondary_classes: Vec::new(),
        social_accounts: AgentSocialAccountsConfig::default(),
        overrides: AgentProfileOverrides {
            summary: Some(
                "Handle practical work across the workspace with reliable follow-through and shared browser capability."
                    .into(),
            ),
            ..AgentProfileOverrides::default()
        },
    }
}

fn load_state_for_runtime(config: &Config) -> Result<AgentStudioState> {
    let path = state_path(&config.workspace_dir);
    if !path.exists() {
        return Ok(AgentStudioState {
            version: STUDIO_STATE_VERSION,
            onboarding_completed: false,
            active_agent_id: DEFAULT_AGENT_ID.into(),
            profiles: vec![default_profile_from_workspace(&config.workspace_dir)],
        });
    }

    let raw = std::fs::read_to_string(path)?;
    let mut state: AgentStudioState = serde_json::from_str(&raw)?;
    normalize_state(&mut state, config)?;
    Ok(state)
}

pub fn skill_grants_for_profile_or_active(
    config: &Config,
    profile_id: Option<&str>,
) -> Vec<String> {
    let state = match load_state_for_runtime(config) {
        Ok(state) => state,
        Err(error) => {
            tracing::warn!("failed to load studio state for skill grants: {error}");
            return Vec::new();
        }
    };
    let resolved = match profile_id {
        Some(profile_id) => resolve_profile(&state, profile_id),
        None => resolve_active_profile(&state),
    };
    resolved
        .map(|profile| profile.skill_grants)
        .unwrap_or_else(|error| {
            tracing::warn!("failed to resolve profile skill grants: {error}");
            Vec::new()
        })
}

pub fn active_skill_grants_for_config(config: &Config) -> Vec<String> {
    skill_grants_for_profile_or_active(config, None)
}

pub fn profile_for_runtime(config: &Config, profile_id: &str) -> Result<ResolvedAgentProfile> {
    let state = load_state_for_runtime(config)?;
    resolve_profile(&state, profile_id)
}

fn state_path(workspace_dir: &Path) -> PathBuf {
    workspace_dir.join(STUDIO_STATE_FILE)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use tempfile::TempDir;

    fn test_config(tmp: &TempDir) -> Config {
        Config {
            workspace_dir: tmp.path().join("workspace"),
            config_path: tmp.path().join("config.toml"),
            ..Config::default()
        }
    }

    #[test]
    fn built_in_classes_include_placeholders() {
        let classes = built_in_classes();
        assert!(classes
            .iter()
            .any(|class_| class_.id == "social_media_manager"));
        assert!(classes.iter().any(|class_| class_.id == "va"));
        assert!(classes.iter().any(|class_| class_.id == "sales"));
        assert_eq!(
            classes
                .iter()
                .find(|class_| class_.id == "tester")
                .unwrap()
                .status,
            AgentClassStatus::ComingSoon
        );
    }

    #[test]
    fn resolve_profile_merges_tools_skills_and_identity() {
        let profile = AgentProfile {
            id: "tanith".into(),
            name: "Tanith".into(),
            avatar: None,
            launch_on_startup: false,
            primary_class: "social_media_manager".into(),
            secondary_classes: vec!["va".into()],
            social_accounts: AgentSocialAccountsConfig::default(),
            overrides: AgentProfileOverrides {
                tool_grants: vec!["file_write".into()],
                identity: IdentityOverlay {
                    emoji: Some("🧭".into()),
                    ..IdentityOverlay::default()
                },
                ..AgentProfileOverrides::default()
            },
        };

        let resolved = resolve_profile_record(&profile).unwrap();
        assert!(resolved.tool_grants.contains(&"twitter_mcp".into()));
        assert!(resolved.tool_grants.contains(&"schedule".into()));
        assert!(resolved.tool_grants.contains(&"browser_headless".into()));
        assert!(resolved.tool_grants.contains(&"file_write".into()));
        assert!(resolved
            .skill_grants
            .contains(&"social-media-manager".into()));
        assert!(resolved.skill_grants.contains(&"browser-operator".into()));
        assert_eq!(resolved.identity.emoji, "🧭");
        assert!(resolved.summary.contains("Lead social strategy"));
    }

    #[test]
    fn va_profile_gets_universal_browser_skill_only() {
        let profile = AgentProfile {
            id: "assistant".into(),
            name: "Assistant".into(),
            avatar: None,
            launch_on_startup: false,
            primary_class: "va".into(),
            secondary_classes: Vec::new(),
            social_accounts: AgentSocialAccountsConfig::default(),
            overrides: AgentProfileOverrides::default(),
        };

        let resolved = resolve_profile_record(&profile).unwrap();
        assert!(resolved.tool_grants.contains(&"browser_headless".into()));
        assert!(resolved.skill_grants.contains(&"browser-operator".into()));
        assert!(!resolved.skill_grants.contains(&"ops_coordination".into()));
        assert!(!resolved.skill_grants.contains(&"task_triage".into()));
    }

    #[test]
    fn sales_profile_gets_sales_skills_and_mail_tool() {
        let profile = AgentProfile {
            id: "closer".into(),
            name: "Closer".into(),
            avatar: None,
            launch_on_startup: false,
            primary_class: "sales".into(),
            secondary_classes: Vec::new(),
            social_accounts: AgentSocialAccountsConfig::default(),
            overrides: AgentProfileOverrides::default(),
        };

        let resolved = resolve_profile_record(&profile).unwrap();
        assert!(resolved.tool_grants.contains(&"browser_headless".into()));
        assert!(resolved.tool_grants.contains(&"mail".into()));
        assert!(resolved.skill_grants.contains(&"browser-operator".into()));
        assert!(resolved.skill_grants.contains(&"sales-prospector".into()));
        assert!(resolved
            .skill_grants
            .contains(&"sales-pipeline-reporter".into()));
        assert!(resolved.summary.contains("Generate pipeline"));
    }

    #[test]
    fn normalize_state_does_not_inject_tanith() {
        let tmp = TempDir::new().unwrap();
        let config = test_config(&tmp);
        std::fs::create_dir_all(&config.workspace_dir).unwrap();
        let mut state = AgentStudioState {
            version: STUDIO_STATE_VERSION,
            onboarding_completed: false,
            active_agent_id: "assistant".into(),
            profiles: vec![AgentProfile {
                id: "assistant".into(),
                name: "Assistant".into(),
                avatar: None,
                launch_on_startup: false,
                primary_class: "va".into(),
                secondary_classes: Vec::new(),
                social_accounts: AgentSocialAccountsConfig::default(),
                overrides: AgentProfileOverrides::default(),
            }],
        };

        normalize_state(&mut state, &config).unwrap();
        assert!(state.profiles.iter().all(|profile| profile.id != "tanith"));
        assert_eq!(state.profiles.len(), 1);
        assert_eq!(state.active_agent_id, "assistant");
    }

    #[test]
    fn normalize_state_bootstraps_neutral_default_profile() {
        let tmp = TempDir::new().unwrap();
        let config = test_config(&tmp);
        std::fs::create_dir_all(&config.workspace_dir).unwrap();
        let mut state = AgentStudioState {
            version: STUDIO_STATE_VERSION,
            onboarding_completed: false,
            active_agent_id: String::new(),
            profiles: Vec::new(),
        };

        normalize_state(&mut state, &config).unwrap();
        assert_eq!(state.profiles.len(), 1);
        assert_eq!(state.profiles[0].id, "agent");
        assert_eq!(state.profiles[0].name, "Agent");
        assert!(!state.profiles[0].launch_on_startup);
        assert_eq!(state.profiles[0].primary_class, "va");
        assert_eq!(state.active_agent_id, "agent");
    }

    #[test]
    fn validate_profile_rejects_coming_soon_classes() {
        let profile = AgentProfile {
            id: "qa".into(),
            name: "QA".into(),
            avatar: None,
            launch_on_startup: false,
            primary_class: "tester".into(),
            secondary_classes: Vec::new(),
            social_accounts: AgentSocialAccountsConfig::default(),
            overrides: AgentProfileOverrides::default(),
        };

        let error = validate_profile(&profile).unwrap_err().to_string();
        assert!(error.contains("visible but not selectable"));
    }
}
