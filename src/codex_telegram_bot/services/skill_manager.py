from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import parse, request

from codex_telegram_bot.services.email_assets import EmailAssetsStore
from codex_telegram_bot.tools.email_assets import (
    ContactListTool,
    ContactRemoveTool,
    ContactUpsertTool,
    EmailValidateTool,
    SendEmailTemplateTool,
    TemplateDeleteTool,
    TemplateGetTool,
    TemplateListTool,
    TemplateUpsertTool,
)
from codex_telegram_bot.tools.email import SendEmailSmtpTool
from codex_telegram_bot.tools.outbound import GitHubCloseIssueTool, GitHubCommentTool, GitHubCreateIssueTool


@dataclass(frozen=True)
class SkillSpec:
    skill_id: str
    name: str
    description: str
    keywords: List[str]
    tools: List[str]
    requires_env: List[str]
    enabled: bool
    source: str = "builtin"
    trusted: bool = True


_SUPPORTED_TOOLS = {
    "send_email_smtp",
    "email_validate",
    "contact_upsert",
    "contact_list",
    "contact_remove",
    "template_upsert",
    "template_list",
    "template_get",
    "template_delete",
    "send_email_template",
    "github_comment",
    "github_close_issue",
    "github_create_issue",
}


class SkillManager:
    def __init__(self, config_dir: Path):
        self._config_dir = config_dir.expanduser().resolve()
        self._skills_dir = self._config_dir / "skills"
        self._registry_path = self._skills_dir / "registry.json"
        self._email_store = EmailAssetsStore(config_dir=self._config_dir)
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        self._trusted_hosts = self._read_trusted_hosts()
        self._ensure_registry()

    def list_skills(self) -> List[SkillSpec]:
        rows = self._load_registry().get("skills", {})
        out: List[SkillSpec] = []
        for _, item in sorted(rows.items()):
            out.append(_dict_to_skill(item))
        return out

    def get_skill(self, skill_id: str) -> Optional[SkillSpec]:
        row = self._load_registry().get("skills", {}).get((skill_id or "").strip().lower())
        if not row:
            return None
        return _dict_to_skill(row)

    def enable(self, skill_id: str, enabled: bool = True) -> Optional[SkillSpec]:
        registry = self._load_registry()
        key = (skill_id or "").strip().lower()
        row = registry.get("skills", {}).get(key)
        if not row:
            return None
        row["enabled"] = bool(enabled)
        self._write_registry(registry)
        return _dict_to_skill(row)

    def install_from_url(self, source_url: str) -> SkillSpec:
        source_url = (source_url or "").strip()
        if not source_url:
            raise ValueError("source_url is required.")
        host = (parse.urlparse(source_url).hostname or "").strip().lower()
        if host not in self._trusted_hosts:
            raise ValueError(f"Untrusted skill host: {host or 'unknown'}")

        with request.urlopen(source_url, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Invalid skill manifest.")
        skill = _manifest_to_skill(payload, source=source_url, trusted=True)
        self._upsert(skill)
        return skill

    def auto_activate(self, prompt: str) -> List[SkillSpec]:
        text = (prompt or "").lower()
        active: List[SkillSpec] = []
        for skill in self.list_skills():
            if not skill.enabled:
                continue
            if skill.requires_env and not all((os.environ.get(k) or "").strip() for k in skill.requires_env):
                continue
            if any(kw and kw in text for kw in skill.keywords):
                active.append(skill)
        return active

    def tools_for_skills(self, skills: List[SkillSpec]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for skill in skills:
            for tool_name in skill.tools:
                tool = self._build_tool(tool_name)
                if tool is None:
                    continue
                out[tool_name] = tool
        return out

    def _build_tool(self, tool_name: str) -> Optional[Any]:
        name = (tool_name or "").strip().lower()
        if name == "send_email_smtp":
            return SendEmailSmtpTool()
        if name == "email_validate":
            return EmailValidateTool()
        if name == "contact_upsert":
            return ContactUpsertTool(self._email_store)
        if name == "contact_list":
            return ContactListTool(self._email_store)
        if name == "contact_remove":
            return ContactRemoveTool(self._email_store)
        if name == "template_upsert":
            return TemplateUpsertTool(self._email_store)
        if name == "template_list":
            return TemplateListTool(self._email_store)
        if name == "template_get":
            return TemplateGetTool(self._email_store)
        if name == "template_delete":
            return TemplateDeleteTool(self._email_store)
        if name == "send_email_template":
            return SendEmailTemplateTool(self._email_store)
        if name == "github_comment":
            return GitHubCommentTool(token=os.environ.get("GITHUB_TOKEN", ""))
        if name == "github_close_issue":
            return GitHubCloseIssueTool(token=os.environ.get("GITHUB_TOKEN", ""))
        if name == "github_create_issue":
            return GitHubCreateIssueTool(token=os.environ.get("GITHUB_TOKEN", ""))
        return None

    def _ensure_registry(self) -> None:
        seeds = {
            "skills": {
                "smtp_email": asdict(
                    SkillSpec(
                        skill_id="smtp_email",
                        name="SMTP Email",
                        description="Send outbound email through SMTP app-password credentials.",
                        keywords=["email", "mail", "smtp", "send message", "gmail"],
                        tools=["send_email_smtp"],
                        requires_env=["SMTP_HOST", "SMTP_USER", "SMTP_APP_PASSWORD"],
                        enabled=True,
                        source="builtin",
                        trusted=True,
                    )
                ),
                "email_ops": asdict(
                    SkillSpec(
                        skill_id="email_ops",
                        name="Email Operations",
                        description="Email validation, contact management, and template management.",
                        keywords=[
                            "validate email",
                            "email validation",
                            "contact list",
                            "contact",
                            "template",
                            "email template",
                        ],
                        tools=[
                            "email_validate",
                            "contact_upsert",
                            "contact_list",
                            "contact_remove",
                            "template_upsert",
                            "template_list",
                            "template_get",
                            "template_delete",
                            "send_email_template",
                        ],
                        requires_env=[],
                        enabled=True,
                        source="builtin",
                        trusted=True,
                    )
                ),
                "github_outbound": asdict(
                    SkillSpec(
                        skill_id="github_outbound",
                        name="GitHub Outbound",
                        description="Post comments/create/close issues on GitHub.",
                        keywords=["github issue", "github", "comment on issue", "close issue", "open issue"],
                        tools=["github_comment", "github_close_issue", "github_create_issue"],
                        requires_env=["GITHUB_TOKEN"],
                        enabled=False,
                        source="builtin",
                        trusted=True,
                    )
                ),
            }
        }
        if not self._registry_path.exists():
            self._write_registry(seeds)
            return
        current = self._load_registry()
        skills = current.setdefault("skills", {})
        changed = False
        for sid, payload in seeds.get("skills", {}).items():
            if sid not in skills:
                skills[sid] = payload
                changed = True
        if changed:
            self._write_registry(current)

    def _upsert(self, skill: SkillSpec) -> None:
        registry = self._load_registry()
        registry.setdefault("skills", {})[skill.skill_id] = asdict(skill)
        self._write_registry(registry)

    def _load_registry(self) -> Dict[str, Any]:
        if not self._registry_path.exists():
            return {"skills": {}}
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {"skills": {}}
        if not isinstance(data, dict):
            return {"skills": {}}
        if not isinstance(data.get("skills"), dict):
            data["skills"] = {}
        return data

    def _write_registry(self, payload: Dict[str, Any]) -> None:
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _read_trusted_hosts(self) -> set[str]:
        raw = (os.environ.get("SKILL_TRUSTED_HOSTS") or "").strip()
        if not raw:
            return {"raw.githubusercontent.com", "github.com"}
        out = set()
        for chunk in raw.split(","):
            host = (chunk or "").strip().lower()
            if host:
                out.add(host)
        return out or {"raw.githubusercontent.com", "github.com"}


def _dict_to_skill(row: Dict[str, Any]) -> SkillSpec:
    return SkillSpec(
        skill_id=str(row.get("skill_id") or "").strip().lower(),
        name=str(row.get("name") or "").strip() or "Unnamed Skill",
        description=str(row.get("description") or "").strip(),
        keywords=[str(x).strip().lower() for x in list(row.get("keywords") or []) if str(x).strip()],
        tools=[str(x).strip().lower() for x in list(row.get("tools") or []) if str(x).strip()],
        requires_env=[str(x).strip() for x in list(row.get("requires_env") or []) if str(x).strip()],
        enabled=bool(row.get("enabled", False)),
        source=str(row.get("source") or "custom"),
        trusted=bool(row.get("trusted", False)),
    )


def _manifest_to_skill(payload: Dict[str, Any], source: str, trusted: bool) -> SkillSpec:
    skill_id = str(payload.get("skill_id") or "").strip().lower()
    if not skill_id:
        raise ValueError("skill_id is required.")
    tools = [str(x).strip().lower() for x in list(payload.get("tools") or []) if str(x).strip()]
    if not tools:
        raise ValueError("Skill manifest must declare at least one tool.")
        for tool_name in tools:
            if tool_name not in _SUPPORTED_TOOLS:
                raise ValueError(f"Unsupported tool in manifest: {tool_name}")
    return SkillSpec(
        skill_id=skill_id,
        name=str(payload.get("name") or skill_id).strip(),
        description=str(payload.get("description") or "").strip(),
        keywords=[str(x).strip().lower() for x in list(payload.get("keywords") or []) if str(x).strip()],
        tools=tools,
        requires_env=[str(x).strip() for x in list(payload.get("requires_env") or []) if str(x).strip()],
        enabled=bool(payload.get("enabled", True)),
        source=source,
        trusted=bool(trusted),
    )
