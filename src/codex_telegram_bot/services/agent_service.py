from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
import asyncio
import logging
import re
import shlex
import uuid
import json
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from codex_telegram_bot.agent_core.capabilities import MarkdownCapabilityRegistry
from codex_telegram_bot.domain.contracts import ProviderAdapter, ExecutionRunner
from codex_telegram_bot.domain.agents import AgentRecord
from codex_telegram_bot.domain.runs import RunRecord
from codex_telegram_bot.domain.sessions import TelegramSessionRecord, TelegramSessionMessageRecord
from codex_telegram_bot.events.event_bus import EventBus, RunEvent
from codex_telegram_bot.execution.local_shell import LocalShellRunner
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.observability.alerts import AlertDispatcher
from codex_telegram_bot.observability.structured_log import log_json
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.repo_context import RepositoryContextRetriever
from codex_telegram_bot.services.agent_scheduler import AgentScheduler
from codex_telegram_bot.services.access_control import AccessController
from codex_telegram_bot.services.capability_router import CapabilityRouter
from codex_telegram_bot.services.session_retention import SessionRetentionPolicy
from codex_telegram_bot.services.workspace_manager import WorkspaceManager
from codex_telegram_bot.tools import ToolContext, ToolRegistry, ToolRequest, ToolResult, build_default_tool_registry
from codex_telegram_bot.tools.email import email_tool_enabled

logger = logging.getLogger(__name__)
AGENT_ID_RE = re.compile(r"^[a-z0-9_-]{2,40}$")
PROVIDER_NAME_RE = re.compile(r"^[a-z0-9_-]{2,40}$")
ALLOWED_POLICY_PROFILES = {"strict", "balanced", "trusted"}
CONTEXT_BUDGET_TOTAL_CHARS = 12000
CONTEXT_HISTORY_BUDGET_CHARS = 6500
CONTEXT_RETRIEVAL_BUDGET_CHARS = 4000
CONTEXT_SUMMARY_BUDGET_CHARS = 1200
MODEL_JOB_HEARTBEAT_SEC = 15
AUTONOMOUS_TOOL_LOOP_ENV = "AUTONOMOUS_TOOL_LOOP"
AUTONOMOUS_PROTOCOL_MAX_DEPTH_ENV = "AUTONOMOUS_PROTOCOL_MAX_DEPTH"
TOOL_APPROVAL_SENTINEL = "__tool__"
PROBE_NO_TOOLS = "NO_TOOLS"
PROBE_NEED_TOOLS = "NEED_TOOLS"
MICRO_STYLE_GUIDE = (
    "Be concise, warm, and teammate-like. Prefer plain language over formal report tone. "
    "If user wants action, do it via tools; don't narrate internals. "
    "After actions: 1-3 sentences on what changed, result, and one useful next option. "
    "Ask only when blocked. Never say 'as an AI'."
)
TOOL_SCHEMA_MAP: Dict[str, Dict[str, Any]] = {
    "exec": {
        "name": "exec",
        "protocol": "!exec <command>",
        "args": {"command": "string (required)"},
    },
    "read_file": {
        "name": "read_file",
        "protocol": "!tool",
        "args": {"path": "string (required)", "max_bytes": "int (optional, default=50000)"},
    },
    "write_file": {
        "name": "write_file",
        "protocol": "!tool",
        "args": {"path": "string (required)", "content": "string (required)"},
    },
    "shell_exec": {
        "name": "shell_exec",
        "protocol": "!tool",
        "args": {"cmd": "string (required)", "timeout_sec": "int (optional)"},
    },
    "git_status": {
        "name": "git_status",
        "protocol": "!tool",
        "args": {"short": "bool (optional, default=true)"},
    },
    "git_diff": {
        "name": "git_diff",
        "protocol": "!tool",
        "args": {"staged": "bool (optional, default=false)"},
    },
    "git_log": {
        "name": "git_log",
        "protocol": "!tool",
        "args": {"n": "int (optional, default=10, max=50)"},
    },
    "git_add": {
        "name": "git_add",
        "protocol": "!tool",
        "args": {"paths": "list[string] or string (required)"},
    },
    "git_commit": {
        "name": "git_commit",
        "protocol": "!tool",
        "args": {"message": "string (required)"},
    },
    "ssh_detect": {
        "name": "ssh_detect",
        "protocol": "!tool",
        "args": {},
    },
    "send_email_smtp": {
        "name": "send_email_smtp",
        "protocol": "!tool",
        "args": {
            "to": "string (required)",
            "subject": "string (required)",
            "body": "string (required)",
            "dry_run": "bool (optional)",
        },
    },
    "provider_status": {
        "name": "provider_status",
        "protocol": "!tool",
        "args": {},
    },
    "provider_switch": {
        "name": "provider_switch",
        "protocol": "!tool",
        "args": {"name": "string (required)"},
    },
}
APPROVAL_REQUIRED_TOOLS = {"send_email_smtp"}


@dataclass(frozen=True)
class LoopAction:
    kind: str
    argv: List[str]
    tool_name: str
    tool_args: Dict[str, Any]

    def checkpoint_command(self) -> str:
        if self.kind == "tool":
            return f"tool:{self.tool_name}:{json.dumps(self.tool_args, sort_keys=True)}"
        return " ".join(self.argv)


@dataclass(frozen=True)
class ProbeDecision:
    mode: str
    reply: str
    tools: List[str]
    goal: str
    max_steps: int


class AgentService:
    """Thin application service to isolate handlers from provider details."""

    def __init__(
        self,
        provider: ProviderAdapter,
        run_store: Optional[SqliteRunStore] = None,
        event_bus: Optional[EventBus] = None,
        execution_runner: Optional[ExecutionRunner] = None,
        repo_retriever: Optional[RepositoryContextRetriever] = None,
        session_max_messages: int = 60,
        session_compact_keep: int = 20,
        tool_loop_max_steps: int = 3,
        approval_ttl_sec: int = 900,
        max_pending_approvals_per_user: int = 3,
        session_workspaces_root: Optional[Path] = None,
        alert_dispatcher: Optional[AlertDispatcher] = None,
        tool_registry: Optional[ToolRegistry] = None,
        capability_registry: Optional[MarkdownCapabilityRegistry] = None,
        # Parity services
        workspace_manager: Optional[WorkspaceManager] = None,
        access_controller: Optional[AccessController] = None,
        retention_policy: Optional[SessionRetentionPolicy] = None,
        capability_router: Optional[CapabilityRouter] = None,
        provider_registry: Optional[Any] = None,
        skill_manager: Optional[Any] = None,
    ):
        self._provider = provider
        self._provider_registry = provider_registry
        self._run_store = run_store
        self._event_bus = event_bus
        self._execution_runner = execution_runner or LocalShellRunner()
        self._repo_retriever = repo_retriever
        self._policy_engine = ExecutionPolicyEngine()
        self._session_max_messages = max(10, int(session_max_messages))
        self._session_compact_keep = max(5, min(int(session_compact_keep), self._session_max_messages))
        self._tool_loop_max_steps = max(1, int(tool_loop_max_steps))
        self._approval_ttl_sec = max(60, int(approval_ttl_sec))
        self._max_pending_approvals_per_user = max(1, int(max_pending_approvals_per_user))
        root = session_workspaces_root or (Path.cwd() / ".session_workspaces")
        self._session_workspaces_root = root.expanduser().resolve()
        self._session_workspaces_root.mkdir(parents=True, exist_ok=True)
        self._session_context_diagnostics: Dict[str, Dict[str, Any]] = {}
        self._alert_dispatcher = alert_dispatcher or AlertDispatcher()
        self._tool_registry = tool_registry or build_default_tool_registry(provider_registry=provider_registry)
        self._capability_registry = capability_registry
        # Parity services (optional — degrade gracefully when not provided)
        self._workspace_manager = workspace_manager
        self._access_controller = access_controller
        self._retention_policy = retention_policy
        self._capability_router = capability_router
        self._skill_manager = skill_manager

        if self._run_store and self._event_bus:
            self._event_bus.subscribe(self._run_store.append_event)
        self._scheduler = AgentScheduler(
            executor=self._execute_prompt,
            get_agent_concurrency=self._agent_max_concurrency,
        )

    async def run_prompt(self, prompt: str, agent_id: str = "default") -> str:
        job_id = await self.queue_prompt(prompt=prompt, agent_id=agent_id)
        return await self._scheduler.wait_result(job_id)

    async def queue_prompt(self, prompt: str, agent_id: str = "default") -> str:
        return await self._scheduler.enqueue(agent_id=agent_id, prompt=prompt)

    async def wait_job(self, job_id: str) -> str:
        return await self._scheduler.wait_result(job_id)

    def cancel_job(self, job_id: str) -> bool:
        return self._scheduler.cancel(job_id)

    def job_status(self, job_id: str) -> str:
        return self._scheduler.job_status(job_id)

    async def shutdown(self) -> None:
        await self._scheduler.shutdown()

    async def handoff_prompt(
        self,
        from_agent_id: str,
        to_agent_id: str,
        prompt: str,
        parent_run_id: str = "",
    ) -> Dict[str, Any]:
        envelope = self._build_handoff_envelope(
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            prompt=prompt,
            parent_run_id=parent_run_id,
        )
        self._emit_handoff_event(parent_run_id, "handoff.requested", envelope)

        target = self.get_agent(to_agent_id)
        if not target or not target.enabled:
            recovery_target = self.get_agent("default")
            if recovery_target and recovery_target.enabled:
                envelope["recovered_to"] = "default"
                self._emit_handoff_event(parent_run_id, "handoff.recovered", envelope)
                job_id = await self.queue_prompt(prompt=prompt, agent_id="default")
                output = await self.wait_job(job_id)
                status = "completed" if not output.startswith("Error:") else "failed"
                self._emit_handoff_event(parent_run_id, f"handoff.{status}", envelope)
                return {
                    "status": status,
                    "job_id": job_id,
                    "target_agent_id": "default",
                    "envelope": envelope,
                    "output": output,
                }

            envelope["failure_reason"] = "target_agent_unavailable"
            self._emit_handoff_event(parent_run_id, "handoff.failed", envelope)
            return {
                "status": "failed",
                "job_id": "",
                "target_agent_id": to_agent_id,
                "envelope": envelope,
                "output": "Error: handoff target agent unavailable.",
            }

        self._emit_handoff_event(parent_run_id, "handoff.accepted", envelope)
        job_id = await self.queue_prompt(prompt=prompt, agent_id=to_agent_id)
        output = await self.wait_job(job_id)
        status = "completed" if not output.startswith("Error:") else "failed"
        self._emit_handoff_event(parent_run_id, f"handoff.{status}", envelope)
        return {
            "status": status,
            "job_id": job_id,
            "target_agent_id": to_agent_id,
            "envelope": envelope,
            "output": output,
        }

    async def _execute_prompt(self, agent_id: str, prompt: str, correlation_id: str) -> str:
        run_id = None
        policy_profile = self._agent_policy_profile(agent_id=agent_id)
        requested_provider = self._agent_provider(agent_id=agent_id)
        provider_for_call = self._provider_for_agent(agent_id=agent_id)
        if self._run_store and self._event_bus:
            run_id = self._run_store.create_run(prompt)
            self._run_store.mark_running(run_id)
            provider_label_before = self._provider_label(provider_for_call)
            self._event_bus.publish(
                run_id=run_id,
                event_type="run.started",
                payload=f"Prompt accepted (agent={agent_id}, policy={policy_profile}, job={correlation_id})",
            )
            self._event_bus.publish(
                run_id=run_id,
                event_type="run.provider.selected",
                payload=f"provider={provider_label_before}, requested={requested_provider}",
            )
            self._event_bus.publish(
                run_id=run_id,
                event_type="run.policy.applied",
                payload=f"agent={agent_id}, profile={policy_profile}",
            )
            log_json(
                logger,
                "run.started",
                run_id=run_id,
                agent_id=agent_id,
                policy_profile=policy_profile,
                job_id=correlation_id,
            )

        output = await provider_for_call.generate(
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            correlation_id=run_id or correlation_id,
            policy_profile=policy_profile,
        )

        if self._run_store and self._event_bus and run_id:
            provider_label_after = self._provider_label(provider_for_call)
            self._event_bus.publish(
                run_id=run_id,
                event_type="run.provider.used",
                payload=f"provider={provider_label_after}, requested={requested_provider}",
            )
            if output.startswith("Error:"):
                self._run_store.mark_failed(run_id, output)
                self._event_bus.publish(run_id=run_id, event_type="run.failed", payload=output)
                log_json(logger, "run.failed", run_id=run_id, agent_id=agent_id, job_id=correlation_id)
                self._send_alert(
                    category="run.failed",
                    severity="high",
                    message="Agent run failed",
                    run_id=run_id,
                    agent_id=agent_id,
                    job_id=correlation_id,
                )
            else:
                self._run_store.mark_completed(run_id, output)
                self._event_bus.publish(run_id=run_id, event_type="run.completed", payload=output[:500])
                log_json(
                    logger,
                    "run.completed",
                    run_id=run_id,
                    agent_id=agent_id,
                    policy_profile=policy_profile,
                    job_id=correlation_id,
                )

        return output

    async def provider_version(self) -> str:
        return await self._provider.version()

    async def provider_health(self) -> Dict[str, Any]:
        return await self._provider.health()

    def list_recent_runs(self, limit: int = 20) -> List[RunRecord]:
        if not self._run_store:
            return []
        return self._run_store.list_recent_runs(limit=limit)

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        if not self._run_store:
            return None
        return self._run_store.get_run(run_id)

    def list_run_events(self, run_id: str, limit: int = 200) -> List[RunEvent]:
        if not self._run_store:
            return []
        return self._run_store.list_run_events(run_id=run_id, limit=limit)

    def get_or_create_session(self, chat_id: int, user_id: int) -> TelegramSessionRecord:
        if not self._run_store:
            raise ValueError("Session registry unavailable without persistent store")
        return self._run_store.get_or_create_active_session(chat_id=chat_id, user_id=user_id)

    def reset_session(self, chat_id: int, user_id: int) -> TelegramSessionRecord:
        if not self._run_store:
            raise ValueError("Session registry unavailable without persistent store")
        self._run_store.archive_active_sessions(chat_id=chat_id, user_id=user_id)
        return self._run_store.create_session(chat_id=chat_id, user_id=user_id)

    def get_active_session(self, chat_id: int, user_id: int) -> Optional[TelegramSessionRecord]:
        if not self._run_store:
            return None
        return self._run_store.get_active_session(chat_id=chat_id, user_id=user_id)

    def get_session(self, session_id: str) -> Optional[TelegramSessionRecord]:
        if not self._run_store:
            return None
        return self._run_store.get_session(session_id=session_id)

    def list_recent_sessions(self, limit: int = 50) -> List[TelegramSessionRecord]:
        if not self._run_store:
            return []
        return self._run_store.list_recent_sessions(limit=limit)

    def list_sessions_for_chat_user(self, chat_id: int, user_id: int, limit: int = 50) -> List[TelegramSessionRecord]:
        if not self._run_store:
            return []
        return self._run_store.list_sessions_for_chat_user(chat_id=chat_id, user_id=user_id, limit=limit)

    def list_session_messages(self, session_id: str, limit: int = 20) -> List[TelegramSessionMessageRecord]:
        if not self._run_store:
            return []
        return self._run_store.list_session_messages(session_id=session_id, limit=limit)

    def get_last_user_prompt(self, session_id: str) -> str:
        history = self.list_session_messages(session_id=session_id, limit=40)
        for msg in reversed(history):
            if msg.role == "user" and (msg.content or "").strip():
                return msg.content
        return ""

    def session_workspace(self, session_id: str) -> Path:
        if self._workspace_manager is not None:
            return self._workspace_manager.provision(session_id)
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", (session_id or "").strip())[:64] or "default"
        root = self._session_workspaces_root / safe
        root.mkdir(parents=True, exist_ok=True)
        return root

    def run_retention_sweep(self) -> Dict[str, Any]:
        """Run session retention policy sweep. Returns a summary dict."""
        if self._retention_policy is None:
            return {"skipped": True, "reason": "no_retention_policy"}
        result = self._retention_policy.apply()
        return {
            "archived_idle": result.archived_idle,
            "pruned_old": result.pruned_old,
            "elapsed_ms": result.elapsed_ms,
        }

    def scan_for_secrets(self, text: str) -> List[str]:
        """Return list of secret pattern names found in text (empty when no controller)."""
        if self._access_controller is None:
            return []
        return self._access_controller.scan_for_secrets(text)

    @property
    def capability_router(self) -> Optional[CapabilityRouter]:
        return self._capability_router

    @property
    def access_controller(self) -> Optional[AccessController]:
        return self._access_controller

    def build_session_prompt(self, session_id: str, user_prompt: str, max_turns: int = 8) -> str:
        retrieval_lines, retrieval_meta = self._build_retrieval_context_with_meta(user_prompt=user_prompt, limit=4)
        planning_lines = _planning_guidance_lines(user_prompt=user_prompt)
        capability_lines = self._build_capability_context(user_prompt=user_prompt)
        style_lines = [f"Style guide: {MICRO_STYLE_GUIDE}"]
        if not self._run_store:
            prefix = style_lines + capability_lines + planning_lines + retrieval_lines
            if prefix:
                return "\n".join(prefix + [user_prompt])
            return user_prompt
        session = self._run_store.get_session(session_id=session_id)
        summary = (session.summary or "").strip() if session else ""
        history = self._run_store.list_session_messages(session_id=session_id, limit=max_turns * 2)
        if not history:
            prefix = style_lines + capability_lines + planning_lines + retrieval_lines
            if summary:
                prefix = [f"Session memory summary:\n{summary[:CONTEXT_SUMMARY_BUDGET_CHARS]}"] + prefix
            if prefix:
                self._session_context_diagnostics[session_id] = {
                    "summary_chars": min(len(summary), CONTEXT_SUMMARY_BUDGET_CHARS) if summary else 0,
                    "history_chars": 0,
                    "retrieval_chars": sum(len(x) for x in retrieval_lines),
                    "retrieval_confidence": retrieval_meta.get("confidence", "none"),
                    "retrieval_top_score": retrieval_meta.get("top_score", 0),
                    "budget_total_chars": CONTEXT_BUDGET_TOTAL_CHARS,
                }
                return "\n".join(prefix + [user_prompt])
            return user_prompt
        lines = [
            "Conversation context (most recent first-order preserved):",
        ]
        lines.extend(style_lines)
        used_summary = ""
        if summary:
            used_summary = summary[:CONTEXT_SUMMARY_BUDGET_CHARS]
            lines.append("Session memory summary:")
            lines.append(used_summary)
        if capability_lines:
            lines.extend(capability_lines)
        if planning_lines:
            lines.extend(planning_lines)
        if retrieval_lines:
            lines.extend(_trim_lines_to_budget(retrieval_lines, CONTEXT_RETRIEVAL_BUDGET_CHARS))
        history_lines: List[str] = []
        for msg in history:
            if msg.role not in {"user", "assistant"}:
                continue
            history_lines.append(f"{msg.role}: {msg.content}")
        history_lines = _trim_lines_from_end(history_lines, CONTEXT_HISTORY_BUDGET_CHARS)
        lines.extend(history_lines)
        lines.append("user: " + user_prompt)
        prompt = "\n".join(lines)
        if len(prompt) > CONTEXT_BUDGET_TOTAL_CHARS:
            lines = _trim_lines_from_end(lines, CONTEXT_BUDGET_TOTAL_CHARS)
            prompt = "\n".join(lines)
        self._session_context_diagnostics[session_id] = {
            "summary_chars": len(used_summary),
            "history_chars": sum(len(x) for x in history_lines),
            "retrieval_chars": sum(len(x) for x in retrieval_lines),
            "retrieval_confidence": retrieval_meta.get("confidence", "none"),
            "retrieval_top_score": retrieval_meta.get("top_score", 0),
            "budget_total_chars": CONTEXT_BUDGET_TOTAL_CHARS,
            "prompt_chars": len(prompt),
        }
        return prompt

    def build_retrieval_context(self, user_prompt: str, limit: int = 4) -> List[str]:
        lines, _ = self._build_retrieval_context_with_meta(user_prompt=user_prompt, limit=limit)
        return lines

    def _build_capability_context(self, user_prompt: str) -> List[str]:
        if not self._capability_registry:
            return []
        summaries = self._capability_registry.summarize_for_prompt(user_prompt, max_capabilities=2)
        if not summaries:
            return []
        lines = ["Capability hints (selective summaries):"]
        for item in summaries:
            lines.append(f"- {item.summary[:260]}")
        return lines

    def _build_capability_context_for_tools(self, tool_names: Sequence[str], max_capabilities: int = 4) -> List[str]:
        if not self._capability_registry:
            return []
        summaries = self._capability_registry.summarize_for_tools(tool_names=tool_names, max_capabilities=max_capabilities)
        if not summaries:
            return []
        lines = ["Capability hints (tool-selected):"]
        for item in summaries:
            summary_lines = [ln.rstrip() for ln in str(item.summary or "").splitlines() if ln.strip()]
            if not summary_lines:
                continue
            lines.append(f"- {summary_lines[0][:200]}")
            for bullet in summary_lines[1:3]:
                if bullet.startswith("- "):
                    lines.append(f"  {bullet}")
                else:
                    lines.append(f"  - {bullet}")
        return lines

    def _build_retrieval_context_with_meta(self, user_prompt: str, limit: int = 4) -> tuple[List[str], Dict[str, Any]]:
        if not self._repo_retriever:
            return [], {"confidence": "none", "top_score": 0}
        snippets = self._repo_retriever.retrieve(query=user_prompt, limit=limit)
        if not snippets:
            return ["Retrieval confidence: low (no direct repository matches)."], {"confidence": "low", "top_score": 0}
        top_score = int(snippets[0].score)
        confidence = "high" if top_score >= 35 else ("medium" if top_score >= 18 else "low")
        lines = [f"Retrieval confidence: {confidence} (top_score={top_score})", "Relevant repository snippets:"]
        for s in snippets:
            lines.append(f"[{s.path} score={s.score}]")
            lines.append(s.snippet)
        return lines, {"confidence": confidence, "top_score": top_score}

    def session_context_diagnostics(self, session_id: str) -> Dict[str, Any]:
        return dict(self._session_context_diagnostics.get(session_id, {}))

    def retrieval_stats(self) -> Dict[str, Any]:
        if not self._repo_retriever:
            return {}
        return self._repo_retriever.stats()

    def refresh_retrieval_index(self, force: bool = True) -> Dict[str, int]:
        if not self._repo_retriever:
            return {"indexed_files": 0, "changed_files": 0, "removed_files": 0}
        return self._repo_retriever.refresh_index(force=force)

    def append_session_user_message(self, session_id: str, content: str) -> None:
        if not self._run_store:
            return
        self._run_store.append_session_message(session_id=session_id, role="user", content=content, run_id="")
        self._run_store.compact_session_messages(
            session_id=session_id,
            max_messages=self._session_max_messages,
            keep_recent=self._session_compact_keep,
        )

    def append_session_assistant_message(self, session_id: str, content: str, run_id: str = "") -> None:
        if not self._run_store:
            return
        self._run_store.append_session_message(
            session_id=session_id,
            role="assistant",
            content=content,
            run_id=run_id,
        )
        self._run_store.compact_session_messages(
            session_id=session_id,
            max_messages=self._session_max_messages,
            keep_recent=self._session_compact_keep,
        )
        if run_id:
            self._run_store.set_session_last_run(session_id=session_id, run_id=run_id)

    def activate_session(self, chat_id: int, user_id: int, session_id: str) -> Optional[TelegramSessionRecord]:
        if not self._run_store:
            return None
        return self._run_store.activate_session(chat_id=chat_id, user_id=user_id, session_id=session_id)

    def create_branch_session(
        self,
        chat_id: int,
        user_id: int,
        from_session_id: str,
        copy_messages: int = 12,
    ) -> Optional[TelegramSessionRecord]:
        if not self._run_store:
            return None
        return self._run_store.create_branch_session(
            chat_id=chat_id,
            user_id=user_id,
            from_session_id=from_session_id,
            copy_messages=copy_messages,
        )

    def list_pending_tool_approvals(self, chat_id: int, user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        if not self._run_store:
            return []
        self._expire_old_approvals()
        return self._run_store.list_pending_tool_approvals(chat_id=chat_id, user_id=user_id, limit=limit)

    def list_all_pending_tool_approvals(self, limit: int = 200) -> List[Dict[str, Any]]:
        if not self._run_store:
            return []
        self._expire_old_approvals()
        return self._run_store.list_all_pending_tool_approvals(limit=limit)

    def deny_tool_action(self, approval_id: str, chat_id: int, user_id: int) -> str:
        if self._access_controller is not None:
            try:
                self._access_controller.check_action(user_id, "deny_tool", chat_id)
            except Exception as exc:
                return f"Error: {exc}"
        if not self._run_store:
            return "Error: approval registry unavailable."
        approval = self._run_store.get_tool_approval(approval_id)
        if not approval:
            return "Error: approval id not found."
        if approval["chat_id"] != chat_id or approval["user_id"] != user_id:
            return "Error: approval does not belong to this chat/user."
        if approval["status"] != "pending":
            return f"Error: approval status is {approval['status']}."
        self._run_store.set_tool_approval_status(approval_id, "denied")
        self._emit_tool_event(
            run_id=approval.get("run_id", ""),
            event_type="tool.approval.denied",
            payload=f"approval_id={approval_id}, session_id={approval['session_id']}",
        )
        return "Denied."

    async def approve_tool_action(self, approval_id: str, chat_id: int, user_id: int) -> str:
        if self._access_controller is not None:
            try:
                self._access_controller.check_action(user_id, "approve_tool", chat_id)
            except Exception as exc:
                return f"Error: {exc}"
        if not self._run_store:
            return "Error: approval registry unavailable."
        approval = self._run_store.get_tool_approval(approval_id)
        if not approval:
            return "Error: approval id not found."
        if approval["chat_id"] != chat_id or approval["user_id"] != user_id:
            return "Error: approval does not belong to this chat/user."
        if approval["status"] != "pending":
            return f"Error: approval status is {approval['status']}."

        self._run_store.set_tool_approval_status(approval_id, "approved")
        self._emit_tool_event(
            run_id=approval.get("run_id", ""),
            event_type="tool.approval.approved",
            payload=f"approval_id={approval_id}, session_id={approval['session_id']}",
        )
        action_id = "tool-" + uuid.uuid4().hex[:8]
        session_id = approval["session_id"]
        agent_id = approval["agent_id"]
        argv = approval["argv"]
        timeout_sec = int(approval["timeout_sec"])
        policy_profile = self._agent_policy_profile(agent_id=agent_id)
        self.append_session_assistant_message(
            session_id=session_id,
            content=f"tool.action.approved action_id={action_id} argv={' '.join(argv)}",
        )
        tool_run_id = ""
        if argv and argv[0] == TOOL_APPROVAL_SENTINEL:
            tool_name = str(argv[1] if len(argv) > 1 else "").strip().lower()
            raw_args = str(argv[2] if len(argv) > 2 else "{}")
            try:
                tool_args = json.loads(raw_args)
            except Exception:
                tool_args = {}
            if not isinstance(tool_args, dict):
                tool_args = {}
            result_obj = await self._execute_registered_tool_action(
                action_id=action_id,
                tool_name=tool_name,
                tool_args=tool_args,
                workspace_root=self.session_workspace(session_id=session_id),
                policy_profile=policy_profile,
                extra_tools={},
            )
            text = (
                f"[tool:{action_id}] rc={0 if result_obj.ok else 1}\n"
                f"output:\n{(result_obj.output or '').strip()[:1800]}"
            ).strip()
            self._run_store.set_tool_approval_status(approval_id, "executed")
            self.append_session_assistant_message(session_id=session_id, content=text, run_id=tool_run_id)
            return text
        result, tool_run_id = await self._execute_tool_action_with_telemetry(
            action_id=action_id,
            session_id=session_id,
            agent_id=agent_id,
            argv=argv,
            stdin_text=approval.get("stdin_text", ""),
            timeout_sec=timeout_sec,
            policy_profile=policy_profile,
            workspace_root=str(self.session_workspace(session_id=session_id)),
        )
        self._run_store.set_tool_approval_status(approval_id, "executed")
        text = (
            f"[tool:{action_id}] rc={result.returncode}\n"
            f"stdout:\n{(result.stdout or '').strip()[:1200]}\n"
            f"stderr:\n{(result.stderr or '').strip()[:600]}"
        ).strip()
        self.append_session_assistant_message(session_id=session_id, content=text, run_id=tool_run_id)
        return text

    async def run_prompt_with_tool_loop(
        self,
        prompt: str,
        chat_id: int,
        user_id: int,
        session_id: str,
        agent_id: str = "default",
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        autonomy_depth: int = 0,
    ) -> str:
        autonomy_depth = max(0, int(autonomy_depth or 0))
        if self._access_controller is not None:
            try:
                self._access_controller.check_action(user_id, "send_prompt", chat_id)
            except Exception as exc:
                return f"Error: {exc}"
        actions, cleaned_prompt, final_prompt = _extract_loop_actions(prompt)
        active_skills: List[Any] = []
        extra_tools: Dict[str, Any] = {}
        if self._skill_manager is not None:
            try:
                active_skills = self._skill_manager.auto_activate(cleaned_prompt or prompt)
                extra_tools = self._skill_manager.tools_for_skills(active_skills)
            except Exception:
                active_skills = []
                extra_tools = {}
        if active_skills:
            await self._notify_progress(
                progress_callback,
                {
                    "event": "skills.activated",
                    "skills": [s.skill_id for s in active_skills],
                },
            )
        available_tool_names = sorted(set(self._tool_registry.names()) | set(extra_tools.keys()))
        if len(actions) > self._tool_loop_max_steps:
            msg = (
                f"Error: tool step budget exceeded ({len(actions)} > {self._tool_loop_max_steps}). "
                "Split into smaller requests."
            )
            self.append_session_assistant_message(session_id=session_id, content=msg)
            await self._notify_progress(
                progress_callback,
                {"event": "loop.failed", "reason": "budget_exceeded", "steps_total": len(actions)},
            )
            if active_skills:
                await self._notify_progress(
                    progress_callback,
                    {"event": "skills.deactivated", "skills": [s.skill_id for s in active_skills]},
                )
            return msg
        if not actions:
            if self._autonomous_tool_loop_enabled():
                await self._notify_progress(progress_callback, {"event": "loop.autoplan.started"})
                planned_actions, planned_final_prompt = await self._plan_tool_loop_actions(
                    prompt=(cleaned_prompt or prompt),
                    agent_id=agent_id,
                    tool_names=available_tool_names,
                )
                if planned_actions:
                    actions = planned_actions
                    if planned_final_prompt:
                        final_prompt = planned_final_prompt
                    await self._notify_progress(
                        progress_callback,
                        {"event": "loop.autoplan.ready", "steps_total": len(actions)},
                    )
                else:
                    await self._notify_progress(progress_callback, {"event": "loop.autoplan.none"})

        if not actions:
            await self._notify_progress(progress_callback, {"event": "loop.probe.started"})
            probe = await self._run_probe_decision(
                prompt=(cleaned_prompt or prompt),
                agent_id=agent_id,
                available_tool_names=available_tool_names,
            )
            if probe.mode != PROBE_NEED_TOOLS and _prompt_expects_action(cleaned_prompt or prompt):
                fallback_tools = _default_probe_tools_for_prompt(cleaned_prompt or prompt, available_tool_names)
                if fallback_tools:
                    probe = ProbeDecision(
                        mode=PROBE_NEED_TOOLS,
                        reply="",
                        tools=fallback_tools,
                        goal=(cleaned_prompt or prompt).strip(),
                        max_steps=self._tool_loop_max_steps,
                    )
            if probe.mode == PROBE_NO_TOOLS and probe.reply:
                await self._notify_progress(progress_callback, {"event": "loop.probe.no_tools"})
                if active_skills:
                    await self._notify_progress(
                        progress_callback,
                        {"event": "skills.deactivated", "skills": [s.skill_id for s in active_skills]},
                    )
                return probe.reply
            if probe.mode == PROBE_NEED_TOOLS and probe.tools:
                await self._notify_progress(progress_callback, {"event": "loop.probe.need_tools", "tools": probe.tools})
                need_tools_output = await self._run_need_tools_inference(
                    prompt=(cleaned_prompt or prompt),
                    goal=probe.goal,
                    selected_tools=probe.tools,
                    max_steps=probe.max_steps,
                    agent_id=agent_id,
                )
                generated_actions, _, generated_final_prompt = _extract_loop_actions(need_tools_output)
                if generated_actions:
                    max_probe_steps = max(1, min(int(probe.max_steps or 1), self._tool_loop_max_steps))
                    if len(generated_actions) > max_probe_steps:
                        generated_actions = generated_actions[:max_probe_steps]
                    loop_payload = {
                        "steps": [_loop_action_to_step(item) for item in generated_actions],
                        "final_prompt": generated_final_prompt
                        or _need_tools_summary_prompt(goal=(probe.goal or (cleaned_prompt or prompt))),
                    }
                    routed = "!loop " + json.dumps(loop_payload, ensure_ascii=True)
                    output = await self.run_prompt_with_tool_loop(
                        prompt=routed,
                        chat_id=chat_id,
                        user_id=user_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        progress_callback=progress_callback,
                        autonomy_depth=autonomy_depth + 1,
                    )
                    if active_skills:
                        await self._notify_progress(
                            progress_callback,
                            {"event": "skills.deactivated", "skills": [s.skill_id for s in active_skills]},
                        )
                    return output
                if need_tools_output.strip():
                    output = need_tools_output.strip()
                    session_workspace_now = self.session_workspace(session_id=session_id)
                    policy_profile_now = self._agent_policy_profile(agent_id=agent_id)
                    protocol_output = await self._attempt_autonomous_protocol_output(
                        output=output,
                        prompt=(cleaned_prompt or prompt),
                        chat_id=chat_id,
                        user_id=user_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        progress_callback=progress_callback,
                        autonomy_depth=autonomy_depth,
                    )
                    if protocol_output is not None:
                        output = protocol_output
                    executed_tool = await self._attempt_autonomous_tool_invocation(
                        output=output,
                        session_id=session_id,
                        workspace_root=session_workspace_now,
                        policy_profile=policy_profile_now,
                        extra_tools=extra_tools,
                    )
                    has_executed_tool = executed_tool is not None
                    if has_executed_tool:
                        output = executed_tool
                    executed_slash = await self._attempt_autonomous_email_slash_command(
                        output=output,
                        session_id=session_id,
                        workspace_root=session_workspace_now,
                        policy_profile=policy_profile_now,
                        extra_tools=extra_tools,
                    )
                    if executed_slash is not None:
                        output = executed_slash
                    elif (not has_executed_tool) and _output_claims_email_sent(output):
                        recovered = await self._attempt_autonomous_email_send_recovery(
                            output=output,
                            prompt=(cleaned_prompt or prompt),
                            session_id=session_id,
                            workspace_root=session_workspace_now,
                            policy_profile=policy_profile_now,
                            extra_tools=extra_tools,
                        )
                        if recovered is None:
                            output = (
                                "Error: email send was claimed, but no SMTP tool action was executed.\n"
                                "Please provide explicit recipient email, subject, and body so I can execute the send."
                            )
                        else:
                            output = recovered
                    if active_skills:
                        await self._notify_progress(
                            progress_callback,
                            {"event": "skills.deactivated", "skills": [s.skill_id for s in active_skills]},
                        )
                    return output
        if not actions:
            contextual = self.build_session_prompt(session_id=session_id, user_prompt=cleaned_prompt or prompt)
            job_id = await self.queue_prompt(prompt=contextual, agent_id=agent_id)
            await self._notify_progress(progress_callback, {"event": "model.job.queued", "job_id": job_id})
            output = await self._wait_job_with_progress(job_id=job_id, progress_callback=progress_callback)
            await self._notify_progress(progress_callback, {"event": "model.job.finished", "job_id": job_id})
            protocol_output = await self._attempt_autonomous_protocol_output(
                output=output,
                prompt=(cleaned_prompt or prompt),
                chat_id=chat_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                progress_callback=progress_callback,
                autonomy_depth=autonomy_depth,
            )
            if protocol_output is not None:
                output = protocol_output
            executed_tool = await self._attempt_autonomous_tool_invocation(
                output=output,
                session_id=session_id,
                workspace_root=self.session_workspace(session_id=session_id),
                policy_profile=self._agent_policy_profile(agent_id=agent_id),
                extra_tools=extra_tools,
            )
            has_executed_tool = executed_tool is not None
            if has_executed_tool:
                output = executed_tool
            executed_slash = await self._attempt_autonomous_email_slash_command(
                output=output,
                session_id=session_id,
                workspace_root=self.session_workspace(session_id=session_id),
                policy_profile=self._agent_policy_profile(agent_id=agent_id),
                extra_tools=extra_tools,
            )
            if executed_slash is not None:
                output = executed_slash
            elif (not has_executed_tool) and _output_claims_email_sent(output):
                recovered = await self._attempt_autonomous_email_send_recovery(
                    output=output,
                    prompt=(cleaned_prompt or prompt),
                    session_id=session_id,
                    workspace_root=self.session_workspace(session_id=session_id),
                    policy_profile=self._agent_policy_profile(agent_id=agent_id),
                    extra_tools=extra_tools,
                )
                if recovered is None:
                    output = (
                        "Error: email send was claimed, but no SMTP tool action was executed.\n"
                        "Please provide explicit recipient email, subject, and body so I can execute the send."
                    )
                else:
                    output = recovered
            if active_skills:
                await self._notify_progress(
                    progress_callback,
                    {"event": "skills.deactivated", "skills": [s.skill_id for s in active_skills]},
                )
            return output

        policy_profile = self._agent_policy_profile(agent_id=agent_id)
        prompt_fingerprint = _tool_loop_fingerprint(actions=actions, cleaned_prompt=cleaned_prompt, final_prompt=final_prompt)
        checkpoints: Dict[int, Dict[str, Any]] = {}
        if self._run_store:
            for cp in self._run_store.list_tool_loop_checkpoints(session_id=session_id, prompt_fingerprint=prompt_fingerprint):
                checkpoints[int(cp["step_index"])] = cp
        await self._notify_progress(
            progress_callback,
            {"event": "loop.started", "steps_total": len(actions), "agent_id": agent_id},
        )
        observations: List[str] = []
        session_workspace = self.session_workspace(session_id=session_id)
        for index, action in enumerate(actions, start=1):
            action_id = "tool-" + uuid.uuid4().hex[:8]
            command = action.checkpoint_command()
            checkpoint = checkpoints.get(index)
            if checkpoint and checkpoint.get("command") == command and checkpoint.get("status") == "completed":
                observations.append(f"{action_id} skipped via checkpoint: step {index} already completed")
                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "loop.step.skipped_checkpoint",
                        "step": index,
                        "action_id": action_id,
                        "command": command,
                    },
                )
                continue
            await self._notify_progress(
                progress_callback,
                {
                    "event": "loop.step.started",
                    "step": index,
                    "steps_total": len(actions),
                    "action_id": action_id,
                    "command": command,
                },
            )
            if action.kind == "tool":
                if self._tool_action_requires_approval(action.tool_name):
                    if not self._run_store:
                        return "Error: approval registry unavailable."
                    if self._run_store.count_pending_tool_approvals(chat_id=chat_id, user_id=user_id) >= self._max_pending_approvals_per_user:
                        msg = (
                            "Error: too many pending approvals for this user. "
                            "Resolve existing approvals with /pending, /approve, or /deny."
                        )
                        self.append_session_assistant_message(session_id=session_id, content=msg)
                        return msg
                    tool_argv = [
                        TOOL_APPROVAL_SENTINEL,
                        action.tool_name,
                        json.dumps(action.tool_args or {}, sort_keys=True, ensure_ascii=True),
                    ]
                    existing = self._run_store.find_pending_tool_approval(
                        chat_id=chat_id,
                        user_id=user_id,
                        session_id=session_id,
                        argv=tool_argv,
                    )
                    if existing:
                        approval_id = existing["approval_id"]
                    else:
                        run_id = self._run_store.create_run(f"Approval requested for tool: {action.tool_name}")
                        self._run_store.mark_running(run_id)
                        approval_id = self._run_store.create_tool_approval(
                            chat_id=chat_id,
                            user_id=user_id,
                            session_id=session_id,
                            agent_id=agent_id,
                            run_id=run_id,
                            argv=tool_argv,
                            stdin_text="",
                            timeout_sec=60,
                            risk_tier="high",
                        )
                        self._emit_tool_event(
                            run_id=run_id,
                            event_type="tool.approval.requested",
                            payload=f"approval_id={approval_id}, action_id={action_id}, risk=high",
                        )
                        self._run_store.mark_completed(
                            run_id,
                            f"Approval requested approval_id={approval_id} action_id={action_id}",
                        )
                    if self._run_store:
                        self._run_store.upsert_tool_loop_checkpoint(
                            session_id=session_id,
                            prompt_fingerprint=prompt_fingerprint,
                            step_index=index,
                            command=command,
                            status="pending_approval",
                        )
                    await self._notify_progress(
                        progress_callback,
                        {
                            "event": "loop.step.awaiting_approval",
                            "step": index,
                            "action_id": action_id,
                            "approval_id": approval_id,
                        },
                    )
                    msg = (
                        f"Approval required for high-risk tool action ({action_id}).\n"
                        f"Run: /approve {approval_id[:8]}\n"
                        f"Tool: {action.tool_name}"
                    )
                    self.append_session_assistant_message(session_id=session_id, content=msg)
                    return msg
                result = await self._execute_registered_tool_action(
                    action_id=action_id,
                    tool_name=action.tool_name,
                    tool_args=action.tool_args,
                    workspace_root=session_workspace,
                    policy_profile=policy_profile,
                    extra_tools=extra_tools,
                )
                observations.append(result.output)
                rc = 0 if result.ok else 1
                if self._run_store:
                    self._run_store.upsert_tool_loop_checkpoint(
                        session_id=session_id,
                        prompt_fingerprint=prompt_fingerprint,
                        step_index=index,
                        command=command,
                        status="completed" if result.ok else "failed",
                    )
                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "loop.step.completed",
                        "step": index,
                        "action_id": action_id,
                        "returncode": rc,
                    },
                )
                self.append_session_assistant_message(
                    session_id=session_id,
                    content=f"tool.action.completed action_id={action_id} rc={rc}",
                )
                if not result.ok:
                    break
                continue

            argv = action.argv
            decision = self._policy_engine.evaluate(argv=argv, policy_profile=policy_profile)
            override_requires_approval = _requires_manual_approval_override(argv=argv)
            if override_requires_approval and decision.risk_tier == "low":
                decision = decision.__class__(allowed=True, risk_tier="high", reason="Manual approval required for mutating command.")
            if not decision.allowed:
                msg = (
                    f"Error: tool action blocked ({action_id}) risk={decision.risk_tier}. "
                    f"{decision.reason}"
                )
                self.append_session_assistant_message(session_id=session_id, content=msg)
                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "loop.step.blocked",
                        "step": index,
                        "action_id": action_id,
                        "risk_tier": decision.risk_tier,
                    },
                )
                if self._run_store:
                    self._run_store.upsert_tool_loop_checkpoint(
                        session_id=session_id,
                        prompt_fingerprint=prompt_fingerprint,
                        step_index=index,
                        command=command,
                        status="blocked",
                    )
                return msg

            if decision.risk_tier == "high":
                if not self._run_store:
                    return "Error: approval registry unavailable."
                if self._run_store.count_pending_tool_approvals(chat_id=chat_id, user_id=user_id) >= self._max_pending_approvals_per_user:
                    msg = (
                        "Error: too many pending approvals for this user. "
                        "Resolve existing approvals with /pending, /approve, or /deny."
                    )
                    self.append_session_assistant_message(session_id=session_id, content=msg)
                    return msg
                existing = self._run_store.find_pending_tool_approval(
                    chat_id=chat_id,
                    user_id=user_id,
                    session_id=session_id,
                    argv=argv,
                )
                if existing:
                    approval_id = existing["approval_id"]
                    self._run_store.upsert_tool_loop_checkpoint(
                        session_id=session_id,
                        prompt_fingerprint=prompt_fingerprint,
                        step_index=index,
                        command=command,
                        status="pending_approval",
                        run_id=existing.get("run_id", ""),
                    )
                    await self._notify_progress(
                        progress_callback,
                        {
                            "event": "loop.step.awaiting_approval",
                            "step": index,
                            "action_id": action_id,
                            "approval_id": approval_id,
                        },
                    )
                    msg = (
                        f"Approval required for high-risk action ({action_id}).\n"
                        f"Run: /approve {approval_id[:8]}\n"
                        f"Command: {' '.join(argv)}"
                    )
                    self.append_session_assistant_message(session_id=session_id, content=msg)
                    return msg
                run_id = self._run_store.create_run(f"Approval requested for: {' '.join(argv)}")
                self._run_store.mark_running(run_id)
                approval_id = self._run_store.create_tool_approval(
                    chat_id=chat_id,
                    user_id=user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    argv=argv,
                    stdin_text="",
                    timeout_sec=60,
                    risk_tier=decision.risk_tier,
                )
                self._emit_tool_event(
                    run_id=run_id,
                    event_type="tool.approval.requested",
                    payload=f"approval_id={approval_id}, action_id={action_id}, risk={decision.risk_tier}",
                )
                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "loop.step.awaiting_approval",
                        "step": index,
                        "action_id": action_id,
                        "approval_id": approval_id,
                    },
                )
                self._run_store.mark_completed(
                    run_id,
                    f"Approval requested approval_id={approval_id} action_id={action_id}",
                )
                self._run_store.upsert_tool_loop_checkpoint(
                    session_id=session_id,
                    prompt_fingerprint=prompt_fingerprint,
                    step_index=index,
                    command=command,
                    status="pending_approval",
                    run_id=run_id,
                )
                msg = (
                    f"Approval required for high-risk action ({action_id}).\n"
                    f"Run: /approve {approval_id[:8]}\n"
                    f"Command: {' '.join(argv)}"
                )
                self.append_session_assistant_message(session_id=session_id, content=msg)
                return msg

            result, tool_run_id = await self._execute_tool_action_with_telemetry(
                action_id=action_id,
                session_id=session_id,
                agent_id=agent_id,
                argv=argv,
                stdin_text="",
                timeout_sec=60,
                policy_profile=policy_profile,
                workspace_root=str(session_workspace),
            )
            observations.append(
                f"{action_id} rc={result.returncode}\n"
                f"stdout:\n{(result.stdout or '').strip()[:1000]}\n"
                f"stderr:\n{(result.stderr or '').strip()[:400]}"
            )
            if result.returncode != 0 and _looks_like_patch_command(argv):
                observations.append(
                    "patch.safeguard: patch command failed. Provide a deterministic fallback with: "
                    "1) failed hunk summary, 2) exact file references, 3) smallest recoverable next patch."
                )
            await self._notify_progress(
                progress_callback,
                {
                    "event": "loop.step.completed",
                    "step": index,
                    "action_id": action_id,
                    "returncode": result.returncode,
                },
            )
            self.append_session_assistant_message(
                session_id=session_id,
                content=f"tool.action.completed action_id={action_id} rc={result.returncode}",
                run_id=tool_run_id,
            )
            if self._run_store:
                self._run_store.upsert_tool_loop_checkpoint(
                    session_id=session_id,
                    prompt_fingerprint=prompt_fingerprint,
                    step_index=index,
                    command=command,
                    status="completed" if result.returncode == 0 else "failed",
                    run_id=tool_run_id,
                )

        enriched = (final_prompt or cleaned_prompt).strip()
        if observations:
            enriched = (
                enriched
                + "\n\nTool observations (deterministic):\n"
                + "\n\n".join(observations)
            ).strip()
        contextual = self.build_session_prompt(session_id=session_id, user_prompt=enriched)
        job_id = await self.queue_prompt(prompt=contextual, agent_id=agent_id)
        await self._notify_progress(progress_callback, {"event": "model.job.queued", "job_id": job_id})
        output = await self._wait_job_with_progress(job_id=job_id, progress_callback=progress_callback)
        await self._notify_progress(progress_callback, {"event": "model.job.finished", "job_id": job_id})
        protocol_output = await self._attempt_autonomous_protocol_output(
            output=output,
            prompt=(final_prompt or cleaned_prompt or prompt),
            chat_id=chat_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            progress_callback=progress_callback,
            autonomy_depth=autonomy_depth,
        )
        if protocol_output is not None:
            output = protocol_output
        executed_tool = await self._attempt_autonomous_tool_invocation(
            output=output,
            session_id=session_id,
            workspace_root=session_workspace,
            policy_profile=policy_profile,
            extra_tools=extra_tools,
        )
        has_executed_tool = executed_tool is not None
        if has_executed_tool:
            output = executed_tool
        executed_slash = await self._attempt_autonomous_email_slash_command(
            output=output,
            session_id=session_id,
            workspace_root=session_workspace,
            policy_profile=policy_profile,
            extra_tools=extra_tools,
        )
        if executed_slash is not None:
            output = executed_slash
        elif (not has_executed_tool) and _output_claims_email_sent(output):
            recovered = await self._attempt_autonomous_email_send_recovery(
                output=output,
                prompt=(cleaned_prompt or prompt),
                session_id=session_id,
                workspace_root=session_workspace,
                policy_profile=policy_profile,
                extra_tools=extra_tools,
            )
            if recovered is None:
                output = (
                    "Error: email send was claimed, but no SMTP tool action was executed.\n"
                    "Please provide explicit recipient email, subject, and body so I can execute the send."
                )
            else:
                output = recovered
        await self._notify_progress(
            progress_callback,
            {"event": "loop.finished", "steps_total": len(actions)},
        )
        if active_skills:
            await self._notify_progress(
                progress_callback,
                {
                    "event": "skills.deactivated",
                    "skills": [s.skill_id for s in active_skills],
                },
            )
        return output

    def append_run_event(self, run_id: str, event_type: str, payload: str) -> None:
        if not self._event_bus:
            return
        self._event_bus.publish(run_id=run_id, event_type=event_type, payload=payload)

    def metrics(self) -> dict:
        runs = self.list_recent_runs(limit=500)
        return {
            "total_runs": len(runs),
            "running_runs": len([r for r in runs if r.status == "running"]),
            "completed_runs": len([r for r in runs if r.status == "completed"]),
            "failed_runs": len([r for r in runs if r.status == "failed"]),
            "pending_runs": len([r for r in runs if r.status == "pending"]),
        }

    def reliability_snapshot(self, limit: int = 500) -> Dict[str, Any]:
        runs = self.list_recent_runs(limit=max(10, min(limit, 5000)))
        total = len(runs)
        completed = [r for r in runs if r.status == "completed"]
        failed = [r for r in runs if r.status == "failed"]
        durations = []
        for r in completed:
            if r.started_at and r.completed_at:
                durations.append(max(0.0, (r.completed_at - r.started_at).total_seconds()))
        p95 = _p95(durations)
        failure_rate = (len(failed) / total) if total else 0.0
        recovery_events = 0
        for r in runs:
            events = self.list_run_events(run_id=r.run_id, limit=30)
            recovery_events += len([e for e in events if e.event_type.startswith("recovery.")])
        status = "ok"
        if failure_rate > 0.2:
            status = "degraded"
        if failure_rate > 0.4:
            status = "critical"
        return {
            "status": status,
            "window_runs": total,
            "completed_runs": len(completed),
            "failed_runs": len(failed),
            "failure_rate": round(failure_rate, 4),
            "latency_p95_sec": round(p95, 4),
            "recovery_events": recovery_events,
            "alerts_enabled": self._alert_dispatcher.enabled,
            "alerts": self._alert_dispatcher.state(),
        }

    def list_agents(self) -> List[AgentRecord]:
        if not self._run_store:
            return []
        return self._run_store.list_agents()

    def get_agent(self, agent_id: str) -> Optional[AgentRecord]:
        if not self._run_store:
            return None
        return self._run_store.get_agent(agent_id=agent_id)

    def upsert_agent(
        self,
        agent_id: str,
        name: str,
        provider: str,
        policy_profile: str,
        max_concurrency: int,
        enabled: bool,
    ) -> AgentRecord:
        if not self._run_store:
            raise ValueError("Agent registry unavailable without persistent store")
        agent_id = (agent_id or "").strip().lower()
        name = (name or "").strip()
        provider = (provider or "").strip().lower()
        policy_profile = (policy_profile or "").strip().lower()
        if not AGENT_ID_RE.match(agent_id):
            raise ValueError("Invalid agent_id. Use 2-40 chars: lowercase letters, numbers, '_' or '-'.")
        if not name:
            raise ValueError("Agent name is required.")
        provider = self._normalize_provider_name(provider)
        if not provider or not PROVIDER_NAME_RE.match(provider):
            raise ValueError("Invalid provider name.")
        allowed = self.available_provider_names()
        if allowed and provider not in allowed:
            known = ", ".join(sorted(allowed))
            raise ValueError(f"Unsupported provider. Available: {known}")
        if policy_profile not in ALLOWED_POLICY_PROFILES:
            raise ValueError("Invalid policy profile.")
        if max_concurrency < 1 or max_concurrency > 10:
            raise ValueError("max_concurrency must be between 1 and 10.")
        return self._run_store.upsert_agent(
            agent_id=agent_id,
            name=name,
            provider=provider,
            policy_profile=policy_profile,
            max_concurrency=max_concurrency,
            enabled=enabled,
        )

    def delete_agent(self, agent_id: str) -> bool:
        if not self._run_store:
            return False
        return self._run_store.delete_agent(agent_id)

    def _agent_max_concurrency(self, agent_id: str) -> int:
        if not self._run_store:
            return 1
        agent = self._run_store.get_agent(agent_id)
        if not agent or not agent.enabled:
            return 1
        return max(1, int(agent.max_concurrency))

    def _agent_policy_profile(self, agent_id: str) -> str:
        if not self._run_store:
            return "trusted"
        agent = self._run_store.get_agent(agent_id)
        if not agent or not agent.enabled:
            return "trusted"
        profile = (agent.policy_profile or "").strip().lower()
        if profile not in ALLOWED_POLICY_PROFILES:
            return "trusted"
        return profile

    def _agent_provider(self, agent_id: str) -> str:
        default_provider = self.default_provider_name()
        if not self._run_store:
            return default_provider
        agent = self._run_store.get_agent(agent_id)
        if not agent or not agent.enabled:
            return default_provider
        value = self._normalize_provider_name(agent.provider or "")
        allowed = self.available_provider_names()
        if allowed and value not in allowed:
            return default_provider
        return value

    def _provider_for_agent(self, agent_id: str):
        selected = self._agent_provider(agent_id=agent_id)
        if self._provider_registry is not None and hasattr(self._provider_registry, "get_provider"):
            try:
                return self._provider_registry.get_provider(selected)
            except Exception:
                pass
        return self._provider

    def _build_handoff_envelope(
        self,
        from_agent_id: str,
        to_agent_id: str,
        prompt: str,
        parent_run_id: str,
    ) -> Dict[str, Any]:
        return {
            "version": 1,
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "parent_run_id": parent_run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "prompt_preview": (prompt or "")[:120],
        }

    def infer_run_agent_id(self, run_id: str) -> str:
        events = self.list_run_events(run_id=run_id, limit=50)
        for event in events:
            if event.event_type != "run.started":
                continue
            payload = event.payload or ""
            marker = "agent="
            idx = payload.find(marker)
            if idx < 0:
                continue
            tail = payload[idx + len(marker):]
            return tail.split(",", 1)[0].strip() or "default"
        return "default"

    def _emit_handoff_event(self, parent_run_id: str, event_type: str, envelope: Dict[str, Any]) -> None:
        if not self._event_bus or not parent_run_id:
            return
        self._event_bus.publish(
            run_id=parent_run_id,
            event_type=event_type,
            payload=str(envelope),
        )

    async def _execute_registered_tool_action(
        self,
        action_id: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        workspace_root: Path,
        policy_profile: str,
        extra_tools: Optional[Dict[str, Any]] = None,
    ):
        tool = (extra_tools or {}).get(tool_name) or self._tool_registry.get(tool_name)
        if not tool:
            if (tool_name or "").strip().lower() == "send_email_smtp":
                return ToolResult(
                    ok=False,
                    output=(
                        f"{action_id} tool={tool_name} error=tool_unavailable "
                        "Email tool is not available in this runtime. "
                        "Set SMTP_HOST, SMTP_USER, SMTP_APP_PASSWORD (and optionally ENABLE_EMAIL_TOOL=1)."
                    ),
                )
            return ToolResult(
                ok=False,
                output=f"{action_id} tool={tool_name} error=tool_unavailable",
            )
        context = ToolContext(workspace_root=workspace_root, policy_profile=policy_profile)
        req = ToolRequest(name=tool_name, args=dict(tool_args or {}))
        try:
            arun = getattr(tool, "arun", None)
            if callable(arun):
                result = await arun(req, context)
            else:
                result = tool.run(req, context)
        except Exception as exc:
            return ToolResult(ok=False, output=f"{action_id} tool={tool_name} error=tool_exception {exc}")
        status = "ok" if result.ok else "error"
        return ToolResult(
            ok=result.ok,
            output=f"{action_id} tool={tool_name} status={status}\n{result.output}",
        )

    def _tool_action_requires_approval(self, tool_name: str) -> bool:
        name = (tool_name or "").strip().lower()
        if name not in APPROVAL_REQUIRED_TOOLS:
            return False
        if name == "send_email_smtp":
            return email_tool_enabled(os.environ)
        return True

    async def _attempt_autonomous_email_send_recovery(
        self,
        output: str,
        prompt: str,
        session_id: str,
        workspace_root: Path,
        policy_profile: str,
        extra_tools: Dict[str, Any],
    ) -> Optional[str]:
        tool_name = "send_email_smtp"
        if tool_name not in (extra_tools or {}) and self._tool_registry.get(tool_name) is None:
            return None
        to_addr, subject, body = _extract_email_triplet_from_slash_command(output)
        if not to_addr:
            to_addr = _extract_email_address(prompt)
            if not to_addr and self._run_store:
                history = self._run_store.list_session_messages(session_id=session_id, limit=40)
                to_addr = _extract_email_address_from_messages(history)
        if not subject or not body:
            subject, body = _extract_subject_and_body_from_email_text(output)
        if not to_addr:
            to_addr = _extract_email_address(output)
        if not to_addr or not subject or not body:
            return None
        action_id = "tool-" + uuid.uuid4().hex[:8]
        result = await self._execute_registered_tool_action(
            action_id=action_id,
            tool_name=tool_name,
            tool_args={"to": to_addr, "subject": subject, "body": body},
            workspace_root=workspace_root,
            policy_profile=policy_profile,
            extra_tools=extra_tools,
        )
        self.append_session_assistant_message(
            session_id=session_id,
            content=f"tool.action.completed action_id={action_id} rc={0 if result.ok else 1}",
        )
        if not result.ok:
            return f"Error: attempted autonomous email send but failed.\n{result.output}"
        return _compact_email_tool_output(result.output)

    async def _attempt_autonomous_email_slash_command(
        self,
        output: str,
        session_id: str,
        workspace_root: Path,
        policy_profile: str,
        extra_tools: Dict[str, Any],
    ) -> Optional[str]:
        tool_name = "send_email_smtp"
        if tool_name not in (extra_tools or {}) and self._tool_registry.get(tool_name) is None:
            return None
        to_addr, subject, body = _extract_email_triplet_from_slash_command(output)
        if not to_addr or not subject or not body:
            return None
        action_id = "tool-" + uuid.uuid4().hex[:8]
        result = await self._execute_registered_tool_action(
            action_id=action_id,
            tool_name=tool_name,
            tool_args={"to": to_addr, "subject": subject, "body": body},
            workspace_root=workspace_root,
            policy_profile=policy_profile,
            extra_tools=extra_tools,
        )
        self.append_session_assistant_message(
            session_id=session_id,
            content=f"tool.action.completed action_id={action_id} rc={0 if result.ok else 1}",
        )
        if not result.ok:
            return f"Error: attempted /email execution but failed.\n{result.output}"
        return _compact_email_tool_output(result.output)

    async def _attempt_autonomous_tool_invocation(
        self,
        output: str,
        session_id: str,
        workspace_root: Path,
        policy_profile: str,
        extra_tools: Dict[str, Any],
    ) -> Optional[str]:
        parsed = _extract_tool_invocation_from_output(output)
        if not parsed:
            return None
        tool_name, tool_args = parsed
        action_id = "tool-" + uuid.uuid4().hex[:8]
        result = await self._execute_registered_tool_action(
            action_id=action_id,
            tool_name=tool_name,
            tool_args=tool_args,
            workspace_root=workspace_root,
            policy_profile=policy_profile,
            extra_tools=extra_tools,
        )
        self.append_session_assistant_message(
            session_id=session_id,
            content=f"tool.action.completed action_id={action_id} rc={0 if result.ok else 1}",
        )
        if not result.ok:
            return f"Error: attempted autonomous tool execution but failed.\n{result.output}"
        return _compact_tool_output(result.output)

    async def _attempt_autonomous_protocol_output(
        self,
        output: str,
        prompt: str,
        chat_id: int,
        user_id: int,
        session_id: str,
        agent_id: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
        autonomy_depth: int,
    ) -> Optional[str]:
        if autonomy_depth >= self._autonomous_protocol_max_depth():
            return None
        actions, cleaned_output, final_prompt = _extract_loop_actions(output)
        if not actions:
            return None
        if len(actions) > self._tool_loop_max_steps:
            actions = actions[: self._tool_loop_max_steps]
        loop_payload = {
            "steps": [_loop_action_to_step(item) for item in actions],
            "final_prompt": final_prompt or _need_tools_summary_prompt(goal=(cleaned_output or prompt)),
        }
        routed = "!loop " + json.dumps(loop_payload, ensure_ascii=True)
        return await self.run_prompt_with_tool_loop(
            prompt=routed,
            chat_id=chat_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            progress_callback=progress_callback,
            autonomy_depth=autonomy_depth + 1,
        )

    async def _execute_tool_action_with_telemetry(
        self,
        action_id: str,
        session_id: str,
        agent_id: str,
        argv: List[str],
        stdin_text: str,
        timeout_sec: int,
        policy_profile: str,
        workspace_root: str = "",
    ):
        run_id = ""
        if self._run_store:
            run_id = self._run_store.create_run(f"Tool action: {' '.join(argv)}")
            self._run_store.mark_running(run_id)
            self._emit_tool_event(
                run_id=run_id,
                event_type="tool.step.started",
                payload=(
                    f"action_id={action_id}, session_id={session_id}, "
                    f"agent_id={agent_id}, argv={' '.join(argv)}"
                ),
            )
        result = await self._execution_runner.run(
            argv=argv,
            stdin_text=stdin_text,
            timeout_sec=timeout_sec,
            policy_profile=policy_profile,
            workspace_root=workspace_root,
        )
        if self._run_store and run_id:
            if result.returncode == 0:
                self._run_store.mark_completed(
                    run_id,
                    f"stdout={(result.stdout or '')[:1200]}\nstderr={(result.stderr or '')[:600]}",
                )
                self._emit_tool_event(
                    run_id=run_id,
                    event_type="tool.step.completed",
                    payload=f"action_id={action_id}, rc={result.returncode}",
                )
            else:
                self._run_store.mark_failed(
                    run_id,
                    f"Tool step failed rc={result.returncode}. stderr={(result.stderr or '')[:600]}",
                )
                self._emit_tool_event(
                    run_id=run_id,
                    event_type="tool.step.failed",
                    payload=f"action_id={action_id}, rc={result.returncode}",
                )
                self._send_alert(
                    category="tool.step.failed",
                    severity="high",
                    message="Tool action failed",
                    run_id=run_id,
                    action_id=action_id,
                    rc=result.returncode,
                )
        return result, run_id

    def _emit_tool_event(self, run_id: str, event_type: str, payload: str) -> None:
        if not self._event_bus or not run_id:
            return
        self._event_bus.publish(run_id=run_id, event_type=event_type, payload=payload)
        if event_type in {"tool.approval.requested", "tool.step.failed"}:
            self._send_alert(
                category=event_type,
                severity="medium" if event_type == "tool.approval.requested" else "high",
                message=payload[:200],
                run_id=run_id,
            )

    def _expire_old_approvals(self) -> None:
        if not self._run_store:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._approval_ttl_sec)
        self._run_store.expire_tool_approvals_before(cutoff.isoformat())

    async def _notify_progress(
        self,
        callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
        payload: Dict[str, Any],
    ) -> None:
        if not callback:
            return
        try:
            await callback(payload)
        except Exception:
            logger.exception("tool loop progress callback failed")

    async def _wait_job_with_progress(
        self,
        job_id: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
    ) -> str:
        wait_task = asyncio.create_task(self.wait_job(job_id))
        started = asyncio.get_running_loop().time()
        tick = 0
        try:
            while not wait_task.done():
                await asyncio.sleep(MODEL_JOB_HEARTBEAT_SEC)
                if wait_task.done():
                    break
                tick += 1
                elapsed_sec = int(max(0, asyncio.get_running_loop().time() - started))
                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "model.job.heartbeat",
                        "job_id": job_id,
                        "elapsed_sec": elapsed_sec,
                        "phase": _model_job_phase_hint(elapsed_sec=elapsed_sec, tick=tick),
                    },
                )
            return await wait_task
        finally:
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

    def _send_alert(self, category: str, severity: str, message: str, **fields: Any) -> None:
        if not self._alert_dispatcher.enabled:
            return
        ok = self._alert_dispatcher.send(category=category, severity=severity, message=message, **fields)
        if not ok:
            logger.warning("alert dispatch failed: category=%s", category)

    def _provider_label(self, provider: Optional[Any] = None) -> str:
        selected = provider if provider is not None else self._provider
        active = getattr(selected, "_active_provider", "")
        if isinstance(active, str) and active.strip():
            return active.strip()
        getter = getattr(selected, "capabilities", None)
        if callable(getter):
            try:
                caps = getter()
            except Exception:
                caps = {}
            if isinstance(caps, dict):
                value = str(caps.get("provider") or "").strip()
                if value:
                    return value
        return selected.__class__.__name__.lower()

    def _autonomous_tool_loop_enabled(self) -> bool:
        raw = (os.environ.get(AUTONOMOUS_TOOL_LOOP_ENV) or "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _autonomous_protocol_max_depth(self) -> int:
        raw = (os.environ.get(AUTONOMOUS_PROTOCOL_MAX_DEPTH_ENV) or "").strip()
        if not raw:
            return 6
        try:
            value = int(raw)
        except Exception:
            return 6
        return max(1, min(value, 12))

    async def _run_probe_decision(
        self,
        prompt: str,
        agent_id: str,
        available_tool_names: Sequence[str],
    ) -> ProbeDecision:
        provider_for_probe = self._provider_for_agent(agent_id=agent_id)
        selectors = sorted(set(_normalize_tool_names(list(available_tool_names)) + ["exec"]))
        max_steps = max(1, self._tool_loop_max_steps)
        probe_prompt = (
            "Classify whether the request needs tool execution before final answer.\n"
            "Output strictly in one format only:\n"
            f"- {PROBE_NO_TOOLS}\\n<final assistant reply>\n"
            f"- {PROBE_NEED_TOOLS} {{\"tools\":[\"name\"],\"goal\":\"...\",\"max_steps\":{max_steps}}}\n"
            "Rules:\n"
            "- Use tool names from: " + ", ".join(selectors) + "\n"
            "- If unsure, prefer NEED_TOOLS.\n"
            "- No markdown fences.\n"
            "User request:\n"
            + (prompt or "").strip()
        )
        try:
            raw = await provider_for_probe.generate(
                [{"role": "user", "content": probe_prompt}],
                stream=False,
                policy_profile=self._agent_policy_profile(agent_id=agent_id),
            )
        except Exception:
            return ProbeDecision(mode="", reply="", tools=[], goal="", max_steps=max_steps)
        return _parse_probe_output(
            raw=raw,
            available_tool_names=selectors,
            default_max_steps=max_steps,
        )

    async def _run_need_tools_inference(
        self,
        prompt: str,
        goal: str,
        selected_tools: Sequence[str],
        max_steps: int,
        agent_id: str,
    ) -> str:
        normalized_tools = _normalize_tool_names(list(selected_tools))
        if not normalized_tools:
            return ""
        provider_for_call = self._provider_for_agent(agent_id=agent_id)
        call_prompt = self._build_need_tools_prompt(
            prompt=prompt,
            goal=goal,
            selected_tools=normalized_tools,
            max_steps=max_steps,
        )
        try:
            output = await provider_for_call.generate(
                [{"role": "user", "content": call_prompt}],
                stream=False,
                policy_profile=self._agent_policy_profile(agent_id=agent_id),
            )
        except Exception as exc:
            return f"Error: NEED_TOOLS generation failed: {exc}"
        return (output or "").strip()

    def _build_need_tools_prompt(
        self,
        prompt: str,
        goal: str,
        selected_tools: Sequence[str],
        max_steps: int,
    ) -> str:
        tool_lines = _render_tool_schema_lines(selected_tools)
        capability_lines = self._build_capability_context_for_tools(selected_tools, max_capabilities=4)
        lines = [
            f"Style guide: {MICRO_STYLE_GUIDE}",
            "Behavior rules (strict):",
            "1) Tools are for the assistant to call, never for the user to type.",
            "2) If you can proceed, proceed. Ask only when blocked or approval is required.",
            "3) If tools are required now, output only one block: !exec ... OR !tool {...} OR !loop {...}.",
            "4) Do not output explanations before a tool call.",
            "5) Do not stop at diagnosis; execute the next concrete step immediately when safe.",
            "6) After tool results: continue with next tool call until goal is done or blocked.",
            f"Step budget: {max(1, min(max_steps, self._tool_loop_max_steps))}",
            "Selected tool APIs:",
        ]
        if tool_lines:
            lines.extend(tool_lines)
        if capability_lines:
            lines.extend(capability_lines)
        lines.extend(
            [
                "Example (format only):",
                'User: "Create a file hello.txt with hi and commit it."',
                'Assistant: !exec printf "hi\\n" > hello.txt',
                "(tool result)",
                'Assistant: !exec git add hello.txt && git commit -m "Add hello.txt"',
                "(tool result)",
                "Assistant: Done. Created hello.txt and committed. Next: push to origin?",
                f"Goal: {(goal or prompt or '').strip()}",
                "User request:",
                (prompt or "").strip(),
            ]
        )
        return "\n".join([ln for ln in lines if ln is not None and str(ln).strip() != ""])

    async def _plan_tool_loop_actions(
        self,
        prompt: str,
        agent_id: str,
        tool_names: Optional[List[str]] = None,
    ) -> tuple[List[LoopAction], str]:
        provider_for_plan = self._provider_for_agent(agent_id=agent_id)
        available_tools = tool_names if tool_names is not None else self._tool_registry.names()
        tools_line = ", ".join(sorted({str(x).strip() for x in available_tools if str(x).strip()}))
        planner_prompt = (
            "You are an execution planner.\n"
            "Decide whether local tool/shell actions are required before answering the user.\n"
            "Return STRICT JSON only, no markdown, no prose:\n"
            "{\"steps\":[...],\"final_prompt\":\"...\"}\n"
            "Rules:\n"
            f"- max steps: {self._tool_loop_max_steps}\n"
            "- if no actions are needed, return {\"steps\":[],\"final_prompt\":\"\"}\n"
            "- step kinds:\n"
            "  - {\"kind\":\"exec\",\"command\":\"...\"}\n"
            "  - {\"kind\":\"tool\",\"tool\":\"<name>\",\"args\":{...}}\n"
            f"- available tool names: {tools_line}\n"
            "- prefer read-only checks first; keep steps minimal and deterministic.\n"
            "- do not include dangerous/destructive cleanup commands unless explicitly requested.\n"
            f"User request:\n{(prompt or '').strip()}"
        )
        try:
            raw = await provider_for_plan.generate(
                [{"role": "user", "content": planner_prompt}],
                stream=False,
                policy_profile="balanced",
            )
        except Exception:
            return [], ""
        if isinstance(raw, str) and raw.strip().lower().startswith("error:"):
            return [], ""
        parsed = _parse_planner_output(raw or "")
        if not parsed:
            return [], ""
        steps = parsed.get("steps")
        if not isinstance(steps, list):
            return [], ""
        planned_actions: List[LoopAction] = []
        for item in steps:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("type") or "").strip().lower()
            if kind == "exec":
                command = str(item.get("command") or "").strip()
                if not command:
                    continue
                try:
                    argv = shlex.split(command)
                except ValueError:
                    argv = []
                if argv:
                    planned_actions.append(LoopAction(kind="exec", argv=argv, tool_name="", tool_args={}))
            elif kind == "tool":
                tool_name = str(item.get("tool") or item.get("name") or "").strip().lower()
                tool_args = item.get("args")
                if tool_name and isinstance(tool_args, dict):
                    planned_actions.append(
                        LoopAction(kind="tool", argv=[], tool_name=tool_name, tool_args=dict(tool_args))
                    )
            if len(planned_actions) >= self._tool_loop_max_steps:
                break
        final_prompt = str(parsed.get("final_prompt") or "").strip()
        return planned_actions, final_prompt

    def provider_registry(self):
        return self._provider_registry

    def list_skills(self) -> List[Dict[str, Any]]:
        if self._skill_manager is None:
            return []
        rows = self._skill_manager.list_skills()
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "skill_id": row.skill_id,
                    "name": row.name,
                    "description": row.description,
                    "keywords": list(row.keywords),
                    "tools": list(row.tools),
                    "requires_env": list(row.requires_env),
                    "enabled": bool(row.enabled),
                    "source": row.source,
                    "trusted": bool(row.trusted),
                }
            )
        return out

    def install_skill_from_url(self, source_url: str) -> Dict[str, Any]:
        if self._skill_manager is None:
            raise ValueError("Skill manager is not configured.")
        row = self._skill_manager.install_from_url(source_url)
        return {
            "skill_id": row.skill_id,
            "name": row.name,
            "description": row.description,
            "keywords": list(row.keywords),
            "tools": list(row.tools),
            "requires_env": list(row.requires_env),
            "enabled": bool(row.enabled),
            "source": row.source,
            "trusted": bool(row.trusted),
        }

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> Dict[str, Any]:
        if self._skill_manager is None:
            raise ValueError("Skill manager is not configured.")
        row = self._skill_manager.enable(skill_id=skill_id, enabled=enabled)
        if row is None:
            raise ValueError("Skill not found.")
        return {
            "skill_id": row.skill_id,
            "name": row.name,
            "description": row.description,
            "keywords": list(row.keywords),
            "tools": list(row.tools),
            "requires_env": list(row.requires_env),
            "enabled": bool(row.enabled),
            "source": row.source,
            "trusted": bool(row.trusted),
        }

    def available_provider_names(self) -> List[str]:
        registry = self._provider_registry
        if registry is None:
            return []
        lister = getattr(registry, "list_providers", None)
        if not callable(lister):
            return []
        try:
            providers = lister()
        except Exception:
            return []
        names: List[str] = []
        for item in providers or []:
            if not isinstance(item, dict):
                continue
            value = self._normalize_provider_name(str(item.get("name") or ""))
            if value and value not in names:
                names.append(value)
        return names

    def default_provider_name(self) -> str:
        registry = self._provider_registry
        if registry is not None:
            getter = getattr(registry, "get_active_name", None)
            if callable(getter):
                try:
                    value = self._normalize_provider_name(str(getter() or ""))
                except Exception:
                    value = ""
                if value:
                    return value
        return "codex_cli"

    @staticmethod
    def _normalize_provider_name(raw: str) -> str:
        value = (raw or "").strip().lower()
        aliases = {
            "codex-cli": "codex_cli",
            "codex": "codex_cli",
            "quen": "qwen",
            "qwen-openai": "qwen",
            "deepseek-openai": "deepseek",
        }
        value = aliases.get(value, value)
        return value.replace("-", "_")


def _normalize_tool_names(names: Sequence[str]) -> List[str]:
    out: List[str] = []
    for raw in names or []:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        if name not in out:
            out.append(name)
    return out


def _prompt_expects_action(prompt: str) -> bool:
    low = (prompt or "").strip().lower()
    if not low:
        return False
    action_markers = [
        "install",
        "download",
        "deploy",
        "set up",
        "setup",
        "configure",
        "create ",
        "fix ",
        "update ",
        "write ",
        "edit ",
        "send ",
        "run ",
        "execute ",
        "find ",
        "implement",
        "do ",
    ]
    info_markers = [
        "what is",
        "explain",
        "why ",
        "how does",
        "summarize",
        "definition",
    ]
    if any(m in low for m in info_markers) and not any(m in low for m in action_markers):
        return False
    return any(m in low for m in action_markers)


def _default_probe_tools_for_prompt(prompt: str, available_tool_names: Sequence[str]) -> List[str]:
    low = (prompt or "").lower()
    available = set(_normalize_tool_names(list(available_tool_names)))
    picks: List[str] = ["exec"]
    for name in ["shell_exec", "read_file", "write_file", "ssh_detect", "git_status", "git_diff"]:
        if name in available and name not in picks:
            picks.append(name)
    if ("email" in low or "mail" in low) and "send_email_smtp" in available and "send_email_smtp" not in picks:
        picks.append("send_email_smtp")
    if "provider" in low and "provider_status" in available and "provider_status" not in picks:
        picks.append("provider_status")
    return picks[:6]


def _render_tool_schema_lines(selected_tools: Sequence[str]) -> List[str]:
    lines: List[str] = []
    for name in _normalize_tool_names(selected_tools):
        schema = TOOL_SCHEMA_MAP.get(name)
        if not schema:
            continue
        lines.append("- " + json.dumps(schema, ensure_ascii=True, sort_keys=True))
    if lines:
        return lines
    return ["- " + json.dumps(TOOL_SCHEMA_MAP["exec"], ensure_ascii=True, sort_keys=True)]


def _parse_probe_output(raw: str, available_tool_names: Sequence[str], default_max_steps: int) -> ProbeDecision:
    text = (raw or "").strip()
    if not text:
        return ProbeDecision(mode="", reply="", tools=[], goal="", max_steps=default_max_steps)
    if text.startswith(PROBE_NO_TOOLS):
        reply = text[len(PROBE_NO_TOOLS) :].strip()
        return ProbeDecision(mode=PROBE_NO_TOOLS, reply=reply, tools=[], goal="", max_steps=default_max_steps)
    if not text.startswith(PROBE_NEED_TOOLS):
        return ProbeDecision(mode="", reply="", tools=[], goal="", max_steps=default_max_steps)
    payload_raw = text[len(PROBE_NEED_TOOLS) :].strip()
    if not payload_raw:
        return ProbeDecision(mode="", reply="", tools=[], goal="", max_steps=default_max_steps)
    parsed = _parse_planner_output(payload_raw)
    if not parsed:
        return ProbeDecision(mode="", reply="", tools=[], goal="", max_steps=default_max_steps)
    allowed = set(_normalize_tool_names(list(available_tool_names)))
    raw_tools = parsed.get("tools") if isinstance(parsed.get("tools"), list) else []
    selected: List[str] = []
    for item in raw_tools:
        name = str(item or "").strip().lower()
        if name in allowed and name not in selected:
            selected.append(name)
    max_steps = default_max_steps
    try:
        max_steps = max(1, min(int(parsed.get("max_steps") or default_max_steps), default_max_steps))
    except Exception:
        max_steps = default_max_steps
    goal = str(parsed.get("goal") or "").strip()
    if not selected:
        return ProbeDecision(mode="", reply="", tools=[], goal="", max_steps=max_steps)
    return ProbeDecision(mode=PROBE_NEED_TOOLS, reply="", tools=selected, goal=goal, max_steps=max_steps)


def _loop_action_to_step(action: LoopAction) -> Dict[str, Any]:
    if action.kind == "tool":
        return {"kind": "tool", "tool": action.tool_name, "args": dict(action.tool_args or {})}
    return {"kind": "exec", "command": shlex.join(action.argv)}


def _need_tools_summary_prompt(goal: str) -> str:
    g = (goal or "").strip()
    if not g:
        return (
            "Check progress after tool results.\n"
            "- If more work is needed and you can continue: output exactly one tool call block (!exec/!tool/!loop), no prose.\n"
            "- If blocked by missing info or approval: ask one short blocking question.\n"
            "- If done: short natural summary (1-3 sentences) of what you did and outcome."
        )
    return (
        f"Goal: {g}\n"
        "Check whether the goal is complete.\n"
        "- If not complete and you can continue: output exactly one next tool call block (!exec/!tool/!loop), no prose.\n"
        "- If blocked by missing info or approval: ask one short blocking question.\n"
        "- If complete: short natural summary (1-3 sentences) with concrete outcome and useful next options only when they help."
    )


def _coerce_tool_value(raw: str) -> Any:
    value = str(raw or "").strip()
    if not value:
        return ""
    low = value.lower()
    if low in {"true", "false"}:
        return low == "true"
    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except Exception:
            return value
    return value


def _parse_tool_directive(body: str) -> Optional[LoopAction]:
    payload = (body or "").strip()
    if not payload:
        return None
    try:
        obj = json.loads(payload)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        tool_name = str(obj.get("name") or obj.get("tool") or "").strip().lower()
        args = obj.get("args")
        if tool_name == "exec":
            cmd = str((args or {}).get("cmd") or (args or {}).get("command") or "").strip() if isinstance(args, dict) else ""
            if not cmd:
                return None
            try:
                argv = shlex.split(cmd)
            except ValueError:
                argv = []
            if argv:
                return LoopAction(kind="exec", argv=argv, tool_name="", tool_args={})
            return None
        if tool_name and isinstance(args, dict):
            return LoopAction(kind="tool", argv=[], tool_name=tool_name, tool_args=dict(args))
        return None

    try:
        tokens = shlex.split(payload)
    except ValueError:
        return None
    if not tokens:
        return None
    tool_name = str(tokens[0] or "").strip().lower()
    if not tool_name:
        return None

    args: Dict[str, Any] = {}
    positional: List[str] = []
    for token in tokens[1:]:
        if "=" in token:
            k, v = token.split("=", 1)
            key = k.strip()
            if key:
                args[key] = _coerce_tool_value(v)
            continue
        positional.append(token)

    if tool_name == "exec":
        command = str(args.get("cmd") or args.get("command") or "").strip()
        if command:
            try:
                argv = shlex.split(command)
            except ValueError:
                argv = []
            if argv:
                return LoopAction(kind="exec", argv=argv, tool_name="", tool_args={})
            return None
        if positional:
            return LoopAction(kind="exec", argv=positional, tool_name="", tool_args={})
        return None

    if positional:
        if tool_name in {"read_file", "write_file"} and "path" not in args:
            args["path"] = positional[0]
        elif tool_name == "shell_exec" and "cmd" not in args:
            args["cmd"] = " ".join(positional)
        elif tool_name == "git_add" and "paths" not in args:
            args["paths"] = positional

    return LoopAction(kind="tool", argv=[], tool_name=tool_name, tool_args=args)


def _extract_loop_actions(prompt: str) -> tuple[List[LoopAction], str, str]:
    actions: List[LoopAction] = []
    keep_lines: List[str] = []
    final_prompt = ""
    raw_lines = (prompt or "").splitlines()
    index = 0
    while index < len(raw_lines):
        raw = raw_lines[index]
        line = raw.strip()
        if line.startswith("!loop "):
            body = line[len("!loop "):].strip()
            try:
                obj = json.loads(body)
            except Exception:
                obj = {}
            if isinstance(obj, dict):
                steps = obj.get("steps") or []
                if isinstance(steps, list):
                    for item in steps:
                        if not isinstance(item, dict):
                            continue
                        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
                        if kind == "exec":
                            argv_raw = item.get("argv")
                            if isinstance(argv_raw, list):
                                argv = [str(x) for x in argv_raw if str(x).strip()]
                            else:
                                command = str(item.get("command") or "").strip()
                                try:
                                    argv = shlex.split(command) if command else []
                                except ValueError:
                                    argv = []
                            if argv:
                                actions.append(LoopAction(kind="exec", argv=argv, tool_name="", tool_args={}))
                        elif kind == "tool":
                            tool_name = str(item.get("tool") or item.get("name") or "").strip().lower()
                            tool_args = item.get("args")
                            if tool_name and isinstance(tool_args, dict):
                                actions.append(
                                    LoopAction(kind="tool", argv=[], tool_name=tool_name, tool_args=dict(tool_args))
                                )
                fp = str(obj.get("final_prompt") or "").strip()
                if fp:
                    final_prompt = fp
            index += 1
            continue
        if line.startswith("!exec "):
            cmd_seed = line[len("!exec "):].strip()
            candidate = cmd_seed
            consumed = index + 1
            argv: List[str] = []
            if candidate:
                while True:
                    try:
                        argv = shlex.split(candidate)
                        break
                    except ValueError:
                        if consumed >= len(raw_lines):
                            argv = []
                            break
                        candidate = candidate + "\n" + raw_lines[consumed]
                        consumed += 1
            if argv:
                actions.append(LoopAction(kind="exec", argv=argv, tool_name="", tool_args={}))
            else:
                keep_lines.extend(raw_lines[index:consumed])
            index = consumed
            continue
        if line.startswith("!tool "):
            body = line[len("!tool "):].strip()
            parsed = _parse_tool_directive(body)
            if parsed:
                actions.append(parsed)
            else:
                keep_lines.append(raw)
            index += 1
            continue
        keep_lines.append(raw)
        index += 1
    return actions, "\n".join(keep_lines).strip(), final_prompt


def _parse_planner_output(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    candidate = fenced.group(1) if fenced else ""
    if not candidate:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return {}
    try:
        payload = json.loads(candidate)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _tool_loop_fingerprint(actions: List[LoopAction], cleaned_prompt: str, final_prompt: str) -> str:
    payload = {
        "actions": [
            {
                "kind": a.kind,
                "argv": a.argv,
                "tool_name": a.tool_name,
                "tool_args": a.tool_args,
            }
            for a in actions
        ],
        "cleaned_prompt": (cleaned_prompt or "").strip(),
        "final_prompt": (final_prompt or "").strip(),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _trim_lines_to_budget(lines: List[str], budget_chars: int) -> List[str]:
    out: List[str] = []
    used = 0
    for line in lines:
        n = len(line)
        if used + n > budget_chars:
            break
        out.append(line)
        used += n
    return out


def _trim_lines_from_end(lines: List[str], budget_chars: int) -> List[str]:
    out: List[str] = []
    used = 0
    for line in reversed(lines):
        n = len(line)
        if used + n > budget_chars:
            break
        out.append(line)
        used += n
    return list(reversed(out))


def _looks_like_patch_command(argv: List[str]) -> bool:
    if not argv:
        return False
    cmd = argv[0].lower()
    if cmd == "apply_patch":
        return True
    return "patch" in cmd


def _requires_manual_approval_override(argv: List[str]) -> bool:
    if not argv:
        return False
    cmd = (argv[0] or "").strip().lower()
    tail = " ".join(argv[1:]).lower()
    if cmd == "rm" and ("-rf" in tail or "-fr" in tail):
        return True
    if cmd == "git" and (
        "reset --hard" in tail
        or "checkout --" in tail
        or "clean -fd" in tail
        or "clean -xdf" in tail
    ):
        return True
    if _looks_like_patch_command(argv):
        return True
    return False


def _planning_guidance_lines(user_prompt: str) -> List[str]:
    low = (user_prompt or "").lower()
    keywords = ["refactor", "multi-file", "edit", "patch", "implement", "rename", "bugfix", "regression"]
    if not any(k in low for k in keywords):
        return []
    return [
        "Engineering response contract:",
        "1) PLAN: deterministic numbered steps before edits.",
        "2) CHANGES: explicit file references using path:line when possible.",
        "3) RISKS: list behavior/regression risks before claiming done.",
        "4) VERIFY: list concrete tests/commands run or missing.",
    ]


def _is_email_send_intent(text: str) -> bool:
    low = (text or "").lower()
    has_send = any(
        k in low for k in ("send", "sending", "sent", "resend", "deliver")
    )
    if not (("email" in low or "mail" in low) and has_send):
        return False
    negative = ("do not send", "don't send", "dont send", "not send", "draft only")
    return not any(n in low for n in negative)


def _output_claims_email_sent(text: str) -> bool:
    low = (text or "").lower()
    if "email" not in low and "mail" not in low:
        return False
    phrases = (
        "i sent",
        "i've sent",
        "email sent",
        "i'll send",
        "sending now",
        "sent to",
        "delivered",
    )
    return any(p in low for p in phrases)


def _extract_email_address(text: str) -> str:
    hit = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
    return hit.group(0).strip() if hit else ""


def _extract_email_address_from_messages(messages: List[Any]) -> str:
    for msg in reversed(messages or []):
        role = str(getattr(msg, "role", "") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(getattr(msg, "content", "") or "")
        addr = _extract_email_address(content)
        if addr:
            return addr
    return ""


def _extract_subject_and_body_from_email_text(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    subject = ""
    lines = raw.splitlines()
    body_start = 0
    for idx, line in enumerate(lines):
        match = re.match(r"^\s*\*{0,2}\s*subject\s*:\s*\*{0,2}\s*(.+?)\s*$", line, flags=re.I)
        if match:
            subject = match.group(1).strip()
            body_start = idx + 1
            break
    if not subject:
        return "", ""
    body_lines = []
    for line in lines[body_start:]:
        if line.strip().startswith("```"):
            continue
        body_lines.append(line.rstrip())
    body = "\n".join(body_lines).strip()
    body = re.sub(r"^Autonomous recovery:.*$", "", body, flags=re.I | re.M).strip()
    return subject, body


def _extract_email_triplet_from_slash_command(text: str) -> tuple[str, str, str]:
    raw = (text or "").replace("\r\n", "\n")
    match = re.search(r"/email\s+(.+)", raw, flags=re.I | re.S)
    if not match:
        return "", "", ""
    payload = match.group(1).strip()
    parts = [p.strip() for p in payload.split("|", 2)]
    if len(parts) != 3:
        return "", "", ""
    to_addr, subject, body = parts
    to_addr = _extract_email_address(to_addr)
    if not to_addr or not subject or not body:
        return "", "", ""
    return to_addr, subject, body


def _extract_tool_invocation_from_output(text: str) -> Optional[tuple[str, Dict[str, Any]]]:
    raw = (text or "").strip()
    if not raw:
        return None
    parsed_json = _parse_tool_invocation_json(raw)
    if parsed_json:
        return parsed_json
    return _parse_tool_invocation_slash(raw)


def _parse_tool_invocation_json(text: str) -> Optional[tuple[str, Dict[str, Any]]]:
    candidates: List[str] = []
    stripped = (text or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", flags=re.S | re.I):
        candidates.append(match.group(1))
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        name = str(obj.get("name") or obj.get("tool") or "").strip().lower()
        args = obj.get("args")
        if name and isinstance(args, dict):
            return name, dict(args)
    return None


def _parse_tool_invocation_slash(text: str) -> Optional[tuple[str, Dict[str, Any]]]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("/")]
    for line in lines:
        if line.lower().startswith("/email "):
            to_addr, subject, body = _extract_email_triplet_from_slash_command(line)
            if to_addr and subject and body:
                return "send_email_smtp", {"to": to_addr, "subject": subject, "body": body}
        if line.lower().startswith("/email_check "):
            addr = _extract_email_address(line)
            if addr:
                return "email_validate", {"email": addr}
        if line.lower().startswith("/contact "):
            tail = line[len("/contact ") :].strip()
            parts = tail.split()
            if not parts:
                continue
            op = parts[0].lower()
            if op == "list":
                return "contact_list", {}
            if op == "add" and len(parts) >= 2:
                return "contact_upsert", {"email": parts[1], "name": " ".join(parts[2:]).strip()}
            if op == "remove" and len(parts) >= 2:
                return "contact_remove", {"email": parts[1]}
        if line.lower().startswith("/template "):
            tail = line[len("/template ") :].strip()
            if tail.lower() == "list":
                return "template_list", {}
            if tail.lower().startswith("show "):
                template_id = tail.split(maxsplit=1)[1].strip() if " " in tail else ""
                if template_id:
                    return "template_get", {"template_id": template_id}
            if tail.lower().startswith("delete "):
                template_id = tail.split(maxsplit=1)[1].strip() if " " in tail else ""
                if template_id:
                    return "template_delete", {"template_id": template_id}
            if tail.lower().startswith("save "):
                payload = tail[len("save ") :].strip()
                parts = [p.strip() for p in payload.split("|", 2)]
                if len(parts) == 3 and parts[0] and parts[1] and parts[2]:
                    return "template_upsert", {"template_id": parts[0], "subject": parts[1], "body": parts[2]}
        if line.lower().startswith("/email_template "):
            tail = line[len("/email_template ") :].strip()
            dry_run = "--dry-run" in tail
            tokens = [t for t in tail.split() if t != "--dry-run"]
            if len(tokens) >= 2:
                return "send_email_template", {"template_id": tokens[0], "to": tokens[1], "dry_run": dry_run}
    return None


def _compact_email_tool_output(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "Email sent."
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return "Email sent."
    if len(lines) >= 2 and lines[0].startswith("tool-"):
        return lines[-1]
    return raw


def _compact_tool_output(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "Done."
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) >= 2 and lines[0].startswith("tool-"):
        return lines[-1]
    return raw


def _p95(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, int((len(ordered) * 0.95) - 1))
    return ordered[idx]


def _model_job_phase_hint(elapsed_sec: int, tick: int) -> str:
    phases = [
        "analyzing request and workspace context",
        "planning next concrete steps",
        "performing repository edits/checks",
        "verifying output and preparing response",
    ]
    if elapsed_sec < 30:
        return phases[0]
    if elapsed_sec < 90:
        return phases[1]
    if elapsed_sec < 180:
        return phases[2]
    return phases[min(len(phases) - 1, 2 + (tick % 2))]
