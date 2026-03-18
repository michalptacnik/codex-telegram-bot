//! Soul/Personality system — ported from codex-telegram-bot's `services/soul.py`.
//!
//! This module provides agent identity/personality persistence on top of
//! ZeroClaw's existing `identity` module.  While ZeroClaw supports AIEOS
//! (AI Entity Object Specification) JSON identities, the Soul system adds a
//! lightweight YAML-like "SOUL v1" format with:
//!
//! - Editable name, voice, principles, boundaries
//! - Style knobs (emoji, emphasis, brevity)
//! - Strict size budgets (configurable via `SOUL_MAX_CHARS`)
//! - Version history with diffs
//! - Integration with the gateway Control Center for live editing

use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

// ── Constants ────────────────────────────────────────────────────

const DEFAULT_SOUL_MAX_CHARS: usize = 2000;
const SOUL_MAX_BULLETS: usize = 5;
const SOUL_MAX_BULLET_CHARS: usize = 90;

// ── Types ────────────────────────────────────────────────────────

/// Style preferences for agent output.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SoulStyle {
    /// Emoji usage: "off", "light", "on"
    #[serde(default = "default_emoji")]
    pub emoji: String,
    /// Text emphasis: "plain", "light", "rich"
    #[serde(default = "default_emphasis")]
    pub emphasis: String,
    /// Response length: "short", "normal"
    #[serde(default = "default_brevity")]
    pub brevity: String,
}

fn default_emoji() -> String {
    "light".into()
}
fn default_emphasis() -> String {
    "light".into()
}
fn default_brevity() -> String {
    "short".into()
}

impl Default for SoulStyle {
    fn default() -> Self {
        Self {
            emoji: default_emoji(),
            emphasis: default_emphasis(),
            brevity: default_brevity(),
        }
    }
}

/// The agent's personality profile.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SoulProfile {
    pub name: String,
    pub voice: String,
    #[serde(default)]
    pub principles: Vec<String>,
    #[serde(default)]
    pub boundaries: Vec<String>,
    #[serde(default)]
    pub style: SoulStyle,
}

impl Default for SoulProfile {
    fn default() -> Self {
        Self {
            name: "Clawlet".into(),
            voice: "calm nerdy direct".into(),
            principles: vec![
                "Be truthful; flag uncertainty.".into(),
                "Optimize for safety + legality.".into(),
                "Prefer small, testable steps.".into(),
                "Keep outputs lean and readable.".into(),
            ],
            boundaries: vec![
                "No scams, evasion, or covert harm.".into(),
                "Don't run risky tools without approval.".into(),
                "Don't expose secrets or private data.".into(),
                "Don't invent facts.".into(),
            ],
            style: SoulStyle::default(),
        }
    }
}

/// Result of validating a `SoulProfile`.
#[derive(Debug, Clone)]
pub struct SoulValidation {
    pub ok: bool,
    pub warnings: Vec<String>,
}

// ── Rendering ────────────────────────────────────────────────────

/// Render a `SoulProfile` to the SOUL v1 text format.
pub fn render_soul(profile: &SoulProfile) -> String {
    let mut lines = vec![
        "# SOUL v1".to_string(),
        format!("name: {}", profile.name),
        format!("voice: {}", profile.voice),
        "principles:".to_string(),
    ];
    for bullet in profile.principles.iter().take(SOUL_MAX_BULLETS) {
        lines.push(format!("  - {}", truncate(bullet, SOUL_MAX_BULLET_CHARS)));
    }
    lines.push("boundaries:".to_string());
    for bullet in profile.boundaries.iter().take(SOUL_MAX_BULLETS) {
        lines.push(format!("  - {}", truncate(bullet, SOUL_MAX_BULLET_CHARS)));
    }
    lines.push("style:".to_string());
    lines.push(format!("  emoji: {}", profile.style.emoji));
    lines.push(format!("  emphasis: {}", profile.style.emphasis));
    lines.push(format!("  brevity: {}", profile.style.brevity));
    lines.join("\n") + "\n"
}

/// Parse SOUL v1 text format back into a `SoulProfile`.
pub fn parse_soul(text: &str) -> Result<SoulProfile> {
    let mut name = String::new();
    let mut voice = String::new();
    let mut principles = Vec::new();
    let mut boundaries = Vec::new();
    let mut style = SoulStyle::default();

    let mut section = "";
    let mut in_style = false;

    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        if trimmed == "principles:" {
            section = "principles";
            in_style = false;
            continue;
        }
        if trimmed == "boundaries:" {
            section = "boundaries";
            in_style = false;
            continue;
        }
        if trimmed == "style:" {
            in_style = true;
            section = "";
            continue;
        }

        if in_style {
            if let Some((key, val)) = trimmed.split_once(':') {
                let key = key.trim();
                let val = val.trim().to_string();
                match key {
                    "emoji" => style.emoji = val,
                    "emphasis" => style.emphasis = val,
                    "brevity" => style.brevity = val,
                    _ => {}
                }
            }
            continue;
        }

        if let Some((key, val)) = trimmed.split_once(':') {
            let key = key.trim();
            let val = val.trim().to_string();
            match (section, key) {
                ("", "name") => name = val,
                ("", "voice") => voice = val,
                _ => {}
            }
            continue;
        }

        // Bullet point
        if let Some(bullet) = trimmed.strip_prefix("- ") {
            match section {
                "principles" => principles.push(bullet.to_string()),
                "boundaries" => boundaries.push(bullet.to_string()),
                _ => {}
            }
        }
    }

    if name.is_empty() {
        bail!("SOUL.md missing 'name' field");
    }

    Ok(SoulProfile {
        name,
        voice,
        principles,
        boundaries,
        style,
    })
}

// ── Validation ───────────────────────────────────────────────────

/// Validate a `SoulProfile` against size constraints.
pub fn validate_soul(profile: &SoulProfile, max_chars: Option<usize>) -> SoulValidation {
    let max = max_chars.unwrap_or(DEFAULT_SOUL_MAX_CHARS);
    let mut warnings = Vec::new();

    if profile.name.is_empty() || profile.name.len() > 40 {
        warnings.push("name must be 1-40 chars".into());
    }

    let word_count = profile.voice.split_whitespace().count();
    if word_count > 6 {
        warnings.push("voice should be ≤6 words".into());
    }

    if profile.principles.len() > SOUL_MAX_BULLETS {
        warnings.push(format!("max {} principles", SOUL_MAX_BULLETS));
    }
    if profile.boundaries.len() > SOUL_MAX_BULLETS {
        warnings.push(format!("max {} boundaries", SOUL_MAX_BULLETS));
    }

    for (i, p) in profile.principles.iter().enumerate() {
        if p.len() > SOUL_MAX_BULLET_CHARS {
            warnings.push(format!(
                "principle[{}] exceeds {} chars",
                i, SOUL_MAX_BULLET_CHARS
            ));
        }
    }
    for (i, b) in profile.boundaries.iter().enumerate() {
        if b.len() > SOUL_MAX_BULLET_CHARS {
            warnings.push(format!(
                "boundary[{}] exceeds {} chars",
                i, SOUL_MAX_BULLET_CHARS
            ));
        }
    }

    let rendered = render_soul(profile);
    if rendered.len() > max {
        warnings.push(format!(
            "rendered soul ({} chars) exceeds max ({})",
            rendered.len(),
            max
        ));
    }

    if !["off", "light", "on"].contains(&profile.style.emoji.as_str()) {
        warnings.push(format!("invalid emoji value: {}", profile.style.emoji));
    }
    if !["plain", "light", "rich"].contains(&profile.style.emphasis.as_str()) {
        warnings.push(format!(
            "invalid emphasis value: {}",
            profile.style.emphasis
        ));
    }
    if !["short", "normal"].contains(&profile.style.brevity.as_str()) {
        warnings.push(format!("invalid brevity value: {}", profile.style.brevity));
    }

    SoulValidation {
        ok: warnings.is_empty(),
        warnings,
    }
}

// ── Persistence ──────────────────────────────────────────────────

/// The soul store manages loading/saving SOUL.md from the workspace.
pub struct SoulStore {
    path: PathBuf,
}

impl SoulStore {
    /// Create a new soul store rooted at the given workspace directory.
    pub fn new(workspace_dir: &Path) -> Self {
        Self {
            path: workspace_dir.join("memory").join("SOUL.md"),
        }
    }

    /// Load the soul profile, or return the default if the file doesn't exist.
    pub fn load(&self) -> Result<SoulProfile> {
        if !self.path.exists() {
            return Ok(SoulProfile::default());
        }
        let text = fs::read_to_string(&self.path)?;
        parse_soul(&text)
    }

    /// Save a soul profile after validation.
    pub fn save(&self, profile: &SoulProfile) -> Result<()> {
        let validation = validate_soul(profile, None);
        if !validation.ok {
            bail!("Soul validation failed: {}", validation.warnings.join("; "));
        }

        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let rendered = render_soul(profile);
        fs::write(&self.path, &rendered)?;
        Ok(())
    }

    /// Get the system prompt fragment for the current soul.
    pub fn system_prompt_fragment(&self) -> String {
        match self.load() {
            Ok(profile) => {
                format!(
                    "Your name is {}. Your voice: {}.\n\nPrinciples:\n{}\n\nBoundaries:\n{}\n",
                    profile.name,
                    profile.voice,
                    profile
                        .principles
                        .iter()
                        .map(|p| format!("- {p}"))
                        .collect::<Vec<_>>()
                        .join("\n"),
                    profile
                        .boundaries
                        .iter()
                        .map(|b| format!("- {b}"))
                        .collect::<Vec<_>>()
                        .join("\n"),
                )
            }
            Err(_) => String::new(),
        }
    }
}

impl fmt::Display for SoulProfile {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", render_soul(self))
    }
}

// ── Helpers ──────────────────────────────────────────────────────

fn truncate(s: &str, max: usize) -> &str {
    if s.len() <= max {
        s
    } else {
        &s[..max]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_roundtrip() {
        let profile = SoulProfile::default();
        let rendered = render_soul(&profile);
        let parsed = parse_soul(&rendered).unwrap();
        assert_eq!(profile.name, parsed.name);
        assert_eq!(profile.voice, parsed.voice);
        assert_eq!(profile.principles.len(), parsed.principles.len());
        assert_eq!(profile.boundaries.len(), parsed.boundaries.len());
    }

    #[test]
    fn test_validate_default() {
        let profile = SoulProfile::default();
        let v = validate_soul(&profile, None);
        assert!(v.ok);
    }

    #[test]
    fn test_validate_bad_name() {
        let mut profile = SoulProfile::default();
        profile.name = String::new();
        let v = validate_soul(&profile, None);
        assert!(!v.ok);
    }
}
