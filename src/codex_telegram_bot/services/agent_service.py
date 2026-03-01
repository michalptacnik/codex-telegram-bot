from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
import asyncio
import logging
import re
import shlex
import uuid
import json
import hashlib
import os
import time
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
from codex_telegram_bot.execution.process_manager import ProcessManager
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.observability.alerts import AlertDispatcher
from codex_telegram_bot.observability.structured_log import log_json
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.runtime_contract import (
    AssistantText as RuntimeAssistantText,
    RuntimeError as RuntimeContractError,
    ToolCall as RuntimeToolCall,
    decode_provider_response,
    decode_text_response,
    to_telegram_text,
)
from codex_telegram_bot.services.capabilities_manifest import (
    build_system_capabilities_chunk,
    write_capabilities_manifest,
)
from codex_telegram_bot.services.probe_loop import ProbeLoop
from codex_telegram_bot.services.repo_context import RepositoryContextRetriever
from codex_telegram_bot.services.agent_scheduler import AgentScheduler
from codex_telegram_bot.services.access_control import AccessController
from codex_telegram_bot.services.capability_router import CapabilityRouter
from codex_telegram_bot.services.session_retention import SessionRetentionPolicy
from codex_telegram_bot.services.workspace_manager import WorkspaceManager
from codex_telegram_bot.tools import ToolContext, ToolRegistry, ToolRequest, ToolResult, build_default_tool_registry
from codex_telegram_bot.tools.runtime_registry import ToolRegistrySnapshot, build_runtime_tool_registry
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
SAFE_RUNTIME_ERROR_TEXT = (
    "I could not safely decode the model output for this turn. "
    "Please retry and I will continue."
)
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
        "args": {
            "command": "string (required)",
            "workdir": "string (optional)",
            "env": "object (optional)",
            "background": "bool (optional, default=false)",
            "timeoutSec": "int (optional, default=60)",
            "timeoutMs": "int (optional)",
        },
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
        "args": {
            "cmd": "string (required for short/start)",
            "action": "string (optional: start|poll|write|terminate|status|list|search)",
            "mode": "string (optional: short|session)",
            "session_id": "string (required for non-start session actions)",
            "stdin": "string (optional for write)",
            "pty": "bool (optional; default true for session)",
            "timeout_sec": "int (optional, short mode only)",
            "cursor": "int (optional for poll/search)",
            "query": "string (required for search)",
            "max_results": "int (optional for search)",
            "context_lines": "int (optional for search)",
        },
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
    "send_email": {
        "name": "send_email",
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
    # Session tools (Issue #105)
    "sessions_list": {
        "name": "sessions_list",
        "protocol": "!tool",
        "args": {"chat_id": "int (required)", "user_id": "int (required)"},
    },
    "sessions_history": {
        "name": "sessions_history",
        "protocol": "!tool",
        "args": {"session_id": "string (required)", "chat_id": "int", "user_id": "int", "limit": "int (optional)"},
    },
    "sessions_send": {
        "name": "sessions_send",
        "protocol": "!tool",
        "args": {"session_id": "string (required)", "content": "string (required)", "chat_id": "int", "user_id": "int"},
    },
    "sessions_spawn": {
        "name": "sessions_spawn",
        "protocol": "!tool",
        "args": {"chat_id": "int (required)", "user_id": "int (required)", "summary": "string (optional)"},
    },
    "session_status": {
        "name": "session_status",
        "protocol": "!tool",
        "args": {"session_id": "string (required)", "chat_id": "int", "user_id": "int"},
    },
    # Memory tools (Issue #106)
    "memory_get": {
        "name": "memory_get",
        "protocol": "!tool",
        "args": {"path": "string (required)", "startLine": "int (optional)", "endLine": "int (optional)"},
    },
    "memory_search": {
        "name": "memory_search",
        "protocol": "!tool",
        "args": {"query": "string (required)", "k": "int (optional)"},
    },
    # MCP tools (Issue #103)
    "mcp_search": {
        "name": "mcp_search",
        "protocol": "!tool",
        "args": {"query": "string (required)", "k": "int (optional)"},
    },
    "mcp_call": {
        "name": "mcp_call",
        "protocol": "!tool",
        "args": {"tool_id": "string (required)", "args": "object (optional)"},
    },
}
APPROVAL_REQUIRED_TOOLS = {"send_email_smtp", "send_email"}


@dataclass(frozen=True)
class LoopAction:
    kind: str
    argv: List[str]
    tool_name: str
    tool_args: Dict[str, Any]
    timeout_sec: int = 60

    def checkpoint_command(self) -> str:
        if self.kind == "tool":
            return f"tool:{self.tool_name}:{json.dumps(self.tool_args, sort_keys=True)}"
        return f"{' '.join(self.argv)}|timeout={int(self.timeout_sec)}"


@dataclass(frozen=True)
class ProbeDecision:
    mode: str
    reply: str
    tools: List[str]
    goal: str
    max_steps: int


@dataclass(frozen=True)
class ToolCall:
    kind: str
    name: str
    args: Dict[str, Any]
    raw: str
    confidence: float = 1.0
    timeout_s: int = 60


@dataclass(frozen=True)
class TurnResult:
    kind: str
    text: str
    session_id: str


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
        probe_loop: Optional[ProbeLoop] = None,
        # Parity services
        workspace_manager: Optional[WorkspaceManager] = None,
        access_controller: Optional[AccessController] = None,
        retention_policy: Optional[SessionRetentionPolicy] = None,
        capability_router: Optional[CapabilityRouter] = None,
        provider_registry: Optional[Any] = None,
        skill_manager: Optional[Any] = None,
        # OpenClaw parity services (Issue #100 epic)
        mcp_bridge: Optional[Any] = None,
        skill_pack_loader: Optional[Any] = None,
        tool_policy_engine: Optional[Any] = None,
        process_manager: Optional[ProcessManager] = None,
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
        self._session_workspace_roots: Dict[str, str] = {}
        self._runtime_registry_snapshots: Dict[str, ToolRegistrySnapshot] = {}
        self._alert_dispatcher = alert_dispatcher or AlertDispatcher()
        self._tool_registry = tool_registry or build_default_tool_registry(provider_registry=provider_registry)
        self._capability_registry = capability_registry
        self._probe_loop = probe_loop
        # Parity services (optional — degrade gracefully when not provided)
        self._workspace_manager = workspace_manager
        self._access_controller = access_controller
        self._retention_policy = retention_policy
        self._capability_router = capability_router
        self._skill_manager = skill_manager
        # OpenClaw parity services (Issue #100 epic)
        self._mcp_bridge = mcp_bridge
        self._skill_pack_loader = skill_pack_loader
        self._tool_policy_engine = tool_policy_engine
        self._process_manager = process_manager or ProcessManager(run_store=run_store)
        self._last_process_cleanup_ts = 0.0
        self._process_cleanup_interval_sec = max(5, int(os.environ.get("PROCESS_CLEANUP_TICK_SEC", "15") or 15))

        if self._run_store and self._event_bus:
            self._event_bus.subscribe(self._run_store.append_event)
        self._scheduler = AgentScheduler(
            executor=self._execute_prompt,
            get_agent_concurrency=self._agent_max_concurrency,
        )

    # ------------------------------------------------------------------
    # Native function-calling agentic loop
    # ------------------------------------------------------------------
    _NATIVE_LOOP_MAX_TURNS = 15
    _TOOL_RESULT_MAX_CHARS = 4000

    def _supports_native_tool_loop(self, provider: Any) -> bool:
        """Check if the given provider supports native function calling."""
        return callable(getattr(provider, "generate_with_tools", None))

    async def run_native_tool_loop(
        self,
        user_message: str,
        chat_id: int,
        user_id: int,
        session_id: str,
        agent_id: str = "default",
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> str:
        """Run the typed runtime loop with a strict contract boundary."""
        provider = self._provider_for_agent(agent_id=agent_id)
        if not self._supports_native_tool_loop(provider):
            return await self.run_prompt_with_tool_loop(
                prompt=user_message,
                chat_id=chat_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                progress_callback=progress_callback,
            )

        self.initialize_session_workspace(session_id=session_id)
        workspace_root = self.session_workspace(session_id=session_id)
        policy_profile = self._agent_policy_profile(agent_id=agent_id)
        snapshot = self.runtime_tool_snapshot(session_id=session_id, refresh=True)
        manifest_paths = write_capabilities_manifest(workspace_root=workspace_root, snapshot=snapshot)
        tool_schemas = list(snapshot.schemas)
        if not tool_schemas:
            return await self.run_prompt_with_tool_loop(
                prompt=user_message,
                chat_id=chat_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                progress_callback=progress_callback,
            )

        system_lines = [
            MICRO_STYLE_GUIDE,
            build_system_capabilities_chunk(snapshot),
            (
                "Structured runtime contract:\n"
                "- Emit assistant text as normal text blocks.\n"
                "- Emit tool calls only via native tool_use blocks.\n"
                "- Do not emit raw !tool/!exec protocol text."
            ),
            (
                f"Capabilities files are available at: {manifest_paths['markdown_path']} "
                f"and {manifest_paths['json_path']}"
            ),
        ]
        system_prompt = "\n".join(system_lines)

        messages: List[Dict[str, Any]] = []
        if self._run_store:
            history = self.list_session_messages(session_id=session_id, limit=20)
            for msg in history:
                if msg.role == "user":
                    messages.append({"role": "user", "content": msg.content})
                elif msg.role == "assistant":
                    # Skip internal traces
                    if not _is_internal_assistant_trace(msg.content):
                        messages.append({"role": "assistant", "content": msg.content})
        messages.append({"role": "user", "content": user_message})

        await self._notify_progress(
            progress_callback,
            {
                "event": "native_loop.started",
                "agent_id": agent_id,
                "tool_count": len(tool_schemas),
                "repo_root": str(snapshot.invariants.repo_root),
                "cwd": str(snapshot.invariants.cwd),
                "is_git_repo": bool(snapshot.invariants.is_git_repo),
            },
        )

        max_turns = min(
            self._NATIVE_LOOP_MAX_TURNS,
            max(3, self._tool_loop_max_steps * 3),
        )
        decode_retry_budget = 1
        text_accumulator: List[str] = []

        for turn in range(max_turns):
            await self._notify_progress(
                progress_callback,
                {"event": "native_loop.turn", "turn": turn + 1, "max_turns": max_turns},
            )

            try:
                response = await provider.generate_with_tools(
                    messages=messages,
                    tools=tool_schemas,
                    system=system_prompt,
                )
            except TypeError:
                # Compatibility path for providers still using ``tool_schemas`` arg.
                response = await provider.generate_with_tools(
                    messages=messages,
                    tool_schemas=tool_schemas,
                    correlation_id="",
                )

            events = decode_provider_response(response, allowed_tools=snapshot.names())
            decode_error = next((ev for ev in events if isinstance(ev, RuntimeContractError)), None)
            if decode_error is not None:
                log_json(
                    logger,
                    "runtime.decode.failed",
                    session_id=session_id,
                    turn=turn + 1,
                    kind=decode_error.kind,
                    detail=decode_error.detail,
                )
                if decode_retry_budget > 0:
                    decode_retry_budget -= 1
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous output violated the structured runtime contract. "
                                "Retry and emit only valid native content blocks."
                            ),
                        }
                    )
                    continue
                return SAFE_RUNTIME_ERROR_TEXT

            assistant_chunks = [e.content for e in events if isinstance(e, RuntimeAssistantText) and e.content.strip()]
            text_contract_violation = False
            for chunk in assistant_chunks:
                parsed_text_events = decode_text_response(chunk, allowed_tools=snapshot.names())
                if any(isinstance(item, (RuntimeToolCall, RuntimeContractError)) for item in parsed_text_events):
                    text_contract_violation = True
                    break
            if text_contract_violation:
                log_json(
                    logger,
                    "runtime.decode.failed",
                    session_id=session_id,
                    turn=turn + 1,
                    kind="text_protocol_violation",
                    detail="assistant text contained protocol bytes",
                )
                if decode_retry_budget > 0:
                    decode_retry_budget -= 1
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous text included raw tool protocol bytes. "
                                "Retry with valid native content blocks only."
                            ),
                        }
                    )
                    continue
                return SAFE_RUNTIME_ERROR_TEXT
            if assistant_chunks:
                text_accumulator.extend(assistant_chunks)
            tool_calls = [e for e in events if isinstance(e, RuntimeToolCall)]

            if not tool_calls and str(response.get("stop_reason") or "end_turn") != "tool_use":
                final_reply = ("\n".join(assistant_chunks) or "\n".join(text_accumulator)).strip()
                if not final_reply:
                    final_reply = "(No response from model.)"
                await self._notify_progress(progress_callback, {"event": "native_loop.finished", "turns": turn + 1})
                return self.enforce_transport_text_contract(session_id=session_id, raw_output=final_reply)

            assistant_payload_blocks: List[Dict[str, Any]] = []
            for chunk in assistant_chunks:
                assistant_payload_blocks.append({"type": "text", "text": chunk})
            for tc in tool_calls:
                assistant_payload_blocks.append(
                    {"type": "tool_use", "id": tc.call_id, "name": tc.name, "input": dict(tc.args or {})}
                )
            messages.append({"role": "assistant", "content": assistant_payload_blocks})

            tool_results: List[Dict[str, Any]] = []
            for call in tool_calls:
                tool_name = call.name
                tool_input = dict(call.args or {})
                tool_use_id = call.call_id

                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "native_loop.tool_call",
                        "turn": turn + 1,
                        "tool_name": tool_name,
                        "tool_use_id": tool_use_id,
                    },
                )

                # Check if approval is required
                if self._tool_action_requires_approval(tool_name):
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": f"Error: tool '{tool_name}' requires approval. Use /approve to grant permission.",
                        "is_error": True,
                    })
                    continue

                action_id = "tool-" + uuid.uuid4().hex[:8]
                result = await self._execute_registered_tool_action(
                    action_id=action_id,
                    tool_name=tool_name,
                    tool_args=dict(tool_input or {}),
                    workspace_root=workspace_root,
                    policy_profile=policy_profile,
                    extra_tools=dict(snapshot.tools),
                    allowed_tool_names=snapshot.names(),
                    chat_id=chat_id,
                    user_id=user_id,
                    session_id=session_id,
                )

                # Truncate large results
                result_text = (result.output or "")
                if len(result_text) > self._TOOL_RESULT_MAX_CHARS:
                    result_text = result_text[:self._TOOL_RESULT_MAX_CHARS] + "\n(output truncated)"

                log_json(
                    logger,
                    "native_loop.tool.executed",
                    session_id=session_id,
                    action_id=action_id,
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    ok=result.ok,
                    output_len=len(result_text),
                )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_text,
                    "is_error": not result.ok,
                })

                self.append_session_assistant_message(
                    session_id=session_id,
                    content=f"tool.action.completed action_id={action_id} rc={0 if result.ok else 1}",
                )

            messages.append({"role": "user", "content": tool_results})

        final_text = "\n".join(text_accumulator).strip() if text_accumulator else ""
        if not final_text:
            final_text = (
                "I've reached the maximum number of tool execution steps. "
                "Please tell me whether to continue."
            )
        await self._notify_progress(
            progress_callback,
            {"event": "native_loop.max_turns_reached", "turns": max_turns},
        )
        return self.enforce_transport_text_contract(session_id=session_id, raw_output=final_text)

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
        if self._process_manager is not None:
            try:
                self._process_manager.cleanup_sessions()
            except Exception:
                logger.exception("process manager shutdown cleanup failed")

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
        session = self._run_store.get_or_create_active_session(chat_id=chat_id, user_id=user_id)
        self.initialize_session_workspace(session_id=session.session_id)
        return session

    def reset_session(self, chat_id: int, user_id: int) -> TelegramSessionRecord:
        if not self._run_store:
            raise ValueError("Session registry unavailable without persistent store")
        previous = self._run_store.get_active_session(chat_id=chat_id, user_id=user_id)
        self._run_store.archive_active_sessions(chat_id=chat_id, user_id=user_id)
        session = self._run_store.create_session(chat_id=chat_id, user_id=user_id)
        self.initialize_session_workspace(
            session_id=session.session_id,
            previous_session_id=(previous.session_id if previous else ""),
        )
        return session

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
            ws = self._workspace_manager.provision(session_id)
            self._session_workspace_roots[session_id] = str(ws.resolve())
            return ws
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", (session_id or "").strip())[:64] or "default"
        root = self._session_workspaces_root / safe
        root.mkdir(parents=True, exist_ok=True)
        self._session_workspace_roots[session_id] = str(root.resolve())
        return root

    def initialize_session_workspace(self, session_id: str, previous_session_id: str = "") -> Dict[str, Any]:
        ws = self.session_workspace(session_id=session_id).resolve()
        previous_root = ""
        if previous_session_id:
            previous_root = self._session_workspace_roots.get(previous_session_id, "")
            if not previous_root:
                try:
                    previous_root = str(self.session_workspace(session_id=previous_session_id).resolve())
                except Exception:
                    previous_root = ""
        entries: List[str] = []
        try:
            entries = sorted([p.name for p in ws.iterdir()])[:40]
        except Exception:
            entries = []
        info = {
            "session_id": session_id,
            "workspace_root": str(ws),
            "previous_session_id": previous_session_id,
            "previous_workspace_root": previous_root,
            "pwd": str(ws),
            "ls": entries,
        }
        log_json(
            logger,
            "workspace.reinit",
            session_id=session_id,
            previous_session_id=previous_session_id or "",
            workspace_root=str(ws),
            previous_workspace_root=previous_root,
            ls=entries[:20],
        )
        self._runtime_registry_snapshots.pop(session_id, None)
        return info

    def runtime_tool_snapshot(
        self,
        session_id: str,
        *,
        extra_tools: Optional[Dict[str, Any]] = None,
        refresh: bool = False,
    ) -> ToolRegistrySnapshot:
        if (not refresh) and session_id in self._runtime_registry_snapshots and not extra_tools:
            return self._runtime_registry_snapshots[session_id]
        workspace_root = self.session_workspace(session_id=session_id)
        snapshot = build_runtime_tool_registry(
            self._tool_registry,
            workspace_root=workspace_root,
            extra_tools=extra_tools,
        )
        if not extra_tools:
            self._runtime_registry_snapshots[session_id] = snapshot
            try:
                write_capabilities_manifest(workspace_root=workspace_root, snapshot=snapshot)
            except Exception:
                logger.exception("failed to write capabilities manifest")
        cache_key = f"{session_id}:workspace.invariants.logged"
        if cache_key not in self._session_context_diagnostics:
            self._session_context_diagnostics[cache_key] = {"logged": True}
            log_json(
                logger,
                "workspace.invariants",
                session_id=session_id,
                repo_root=str(snapshot.invariants.repo_root),
                cwd=str(snapshot.invariants.cwd),
                is_git_repo=bool(snapshot.invariants.is_git_repo),
                disabled_tools=dict(snapshot.disabled),
            )
        return snapshot

    def enforce_transport_text_contract(
        self,
        *,
        session_id: str,
        raw_output: str,
    ) -> str:
        snapshot = self.runtime_tool_snapshot(session_id=session_id)
        events = decode_text_response(raw_output, allowed_tools=snapshot.names())
        if any(isinstance(event, RuntimeToolCall) for event in events):
            log_json(
                logger,
                "runtime_contract.drop",
                session_id=session_id,
                reason="decoded_toolcall_at_transport_boundary",
                preview=(raw_output or "")[:200],
            )
            return SAFE_RUNTIME_ERROR_TEXT
        if any(isinstance(event, RuntimeContractError) for event in events):
            log_json(
                logger,
                "runtime_contract.drop",
                session_id=session_id,
                reason="decode_error",
                preview=(raw_output or "")[:200],
            )
            return SAFE_RUNTIME_ERROR_TEXT
        return to_telegram_text(events, safe_fallback=SAFE_RUNTIME_ERROR_TEXT)

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

    def tick_process_sessions_cleanup(self, force: bool = False) -> int:
        if self._process_manager is None:
            return 0
        now = time.monotonic()
        if not force and (now - self._last_process_cleanup_ts) < self._process_cleanup_interval_sec:
            return 0
        self._last_process_cleanup_ts = now
        try:
            return int(self._process_manager.cleanup_sessions() or 0)
        except Exception:
            logger.exception("process session cleanup tick failed")
            return 0

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

    @property
    def process_manager(self) -> Optional[ProcessManager]:
        return self._process_manager

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
            if msg.role == "assistant" and _is_internal_assistant_trace(msg.content):
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
            log_json(
                logger,
                "tool.exec.pre",
                session_id=session_id,
                action_id=action_id,
                kind="tool",
                tool_name=tool_name,
                command=f"tool:{tool_name}",
                allowed=True,
                approval_required=True,
                approval_granted=True,
                exit_code=None,
            )
            snapshot = self.runtime_tool_snapshot(session_id=session_id, refresh=True)
            result_obj = await self._execute_registered_tool_action(
                action_id=action_id,
                tool_name=tool_name,
                tool_args=tool_args,
                workspace_root=self.session_workspace(session_id=session_id),
                policy_profile=policy_profile,
                extra_tools=dict(snapshot.tools),
                allowed_tool_names=snapshot.names(),
                chat_id=chat_id,
                user_id=user_id,
                session_id=session_id,
            )
            text = (
                f"[tool:{action_id}] rc={0 if result_obj.ok else 1}\n"
                f"output:\n{(result_obj.output or '').strip()[:1800]}"
            ).strip()
            log_json(
                logger,
                "tool.exec.post",
                session_id=session_id,
                action_id=action_id,
                kind="tool",
                tool_name=tool_name,
                command=f"tool:{tool_name}",
                allowed=True,
                approval_required=True,
                approval_granted=True,
                exit_code=(0 if result_obj.ok else 1),
            )
            self._run_store.set_tool_approval_status(approval_id, "executed")
            self.append_session_assistant_message(session_id=session_id, content=text, run_id=tool_run_id)
            return text
        log_json(
            logger,
            "tool.exec.pre",
            session_id=session_id,
            action_id=action_id,
            kind="exec",
            command=" ".join(argv),
            allowed=True,
            approval_required=True,
            approval_granted=True,
            exit_code=None,
        )
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
        log_json(
            logger,
            "tool.exec.post",
            session_id=session_id,
            action_id=action_id,
            kind="exec",
            command=" ".join(argv),
            allowed=True,
            approval_required=True,
            approval_granted=True,
            exit_code=result.returncode,
        )
        self._run_store.set_tool_approval_status(approval_id, "executed")
        text = (
            f"[tool:{action_id}] rc={result.returncode}\n"
            f"stdout:\n{(result.stdout or '').strip()[:1200]}\n"
            f"stderr:\n{(result.stderr or '').strip()[:600]}"
        ).strip()
        self.append_session_assistant_message(session_id=session_id, content=text, run_id=tool_run_id)
        return text

    async def run_turn(
        self,
        prompt: str,
        chat_id: int,
        user_id: int,
        session_id: str,
        agent_id: str = "default",
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> TurnResult:
        text_prompt = str(prompt or "").strip()
        if not text_prompt:
            text_prompt = "(empty)"
        self.tick_process_sessions_cleanup()
        self.append_session_user_message(session_id=session_id, content=text_prompt)
        output = await self.run_prompt_with_tool_loop(
            prompt=text_prompt,
            chat_id=chat_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            progress_callback=progress_callback,
        )
        hops = 0
        while _contains_tool_call_signatures(output) and hops < 3:
            hops += 1
            log_json(
                logger,
                "output.firewall.reroute",
                chat_id=chat_id,
                user_id=user_id,
                session_id=session_id,
                hop=hops,
                preview=(output or "")[:200],
            )
            forced_actions, _, forced_final_prompt = _extract_loop_actions(
                output,
                preferred_tools=self._tool_registry.names(),
            )
            if forced_actions:
                loop_payload = {
                    "steps": [_loop_action_to_step(item) for item in forced_actions],
                    "final_prompt": forced_final_prompt or _need_tools_summary_prompt(goal=text_prompt),
                }
                routed = "!loop " + json.dumps(loop_payload, ensure_ascii=True)
                output = await self.run_prompt_with_tool_loop(
                    prompt=routed,
                    chat_id=chat_id,
                    user_id=user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    progress_callback=progress_callback,
                    autonomy_depth=1,
                )
                continue
            output = await self.run_prompt_with_tool_loop(
                prompt=output,
                chat_id=chat_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                progress_callback=progress_callback,
                autonomy_depth=1,
            )
        if _contains_tool_call_signatures(output):
            output = (
                "I hit a tool-call formatting loop while executing your request. "
                "Reply with 'retry' and I will continue automatically."
            )
        output = self.enforce_transport_text_contract(
            session_id=session_id,
            raw_output=(output or "").strip() or "(no output)",
        )
        lowered = output.lower()
        if "approve once: /approve" in lowered or lowered.startswith("approval required"):
            kind = "approval_request"
        elif _is_blocking_question(output):
            kind = "clarifying_question"
        else:
            kind = "final_text"
        self.append_session_assistant_message(session_id=session_id, content=output)
        return TurnResult(kind=kind, text=output, session_id=session_id)

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
        self.initialize_session_workspace(session_id=session_id)
        workspace_root = self.session_workspace(session_id=session_id)
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
        snapshot = self.runtime_tool_snapshot(
            session_id=session_id,
            extra_tools=extra_tools if extra_tools else None,
            refresh=True,
        )
        available_tool_names = snapshot.names()
        if not actions:
            reparsed_actions, reparsed_cleaned_prompt, reparsed_final_prompt = _extract_loop_actions(
                prompt,
                preferred_tools=available_tool_names,
            )
            if reparsed_actions:
                actions = reparsed_actions
                cleaned_prompt = reparsed_cleaned_prompt or cleaned_prompt
                final_prompt = reparsed_final_prompt or final_prompt
        if actions:
            validated_actions, validation_error = self._validate_actions(
                actions=actions,
                workspace_root=workspace_root,
                available_tool_names=available_tool_names,
            )
            if validation_error:
                repaired = await self._request_tool_call_correction(
                    prompt=(cleaned_prompt or prompt),
                    model_output=(prompt or ""),
                    selected_tools=available_tool_names,
                    goal=(cleaned_prompt or prompt),
                    agent_id=agent_id,
                )
                if repaired:
                    repaired_actions, repaired_cleaned_prompt, repaired_final_prompt = _extract_loop_actions(
                        repaired,
                        preferred_tools=available_tool_names,
                    )
                    repaired_validated, repaired_error = self._validate_actions(
                        actions=repaired_actions,
                        workspace_root=workspace_root,
                        available_tool_names=available_tool_names,
                    )
                    log_json(
                        logger,
                        "toolcall.repair",
                        chat_id=chat_id,
                        user_id=user_id,
                        session_id=session_id,
                        repaired=bool(repaired_actions),
                        error=(repaired_error or ""),
                        preview=(repaired or "")[:200],
                    )
                    if repaired_validated and not repaired_error:
                        actions = repaired_validated
                        if repaired_cleaned_prompt:
                            cleaned_prompt = repaired_cleaned_prompt
                        if repaired_final_prompt:
                            final_prompt = repaired_final_prompt
                    else:
                        msg = repaired_error or validation_error
                        self.append_session_assistant_message(session_id=session_id, content=msg)
                        return msg
                else:
                    self.append_session_assistant_message(session_id=session_id, content=validation_error)
                    return validation_error
            else:
                actions = validated_actions
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
            await self._notify_progress(progress_callback, {"event": "loop.probe.started"})
            probe = await self._run_probe_decision(
                prompt=(cleaned_prompt or prompt),
                agent_id=agent_id,
                available_tool_names=available_tool_names,
            )
            if (
                probe.mode != PROBE_NEED_TOOLS
                and _prompt_expects_action(cleaned_prompt or prompt)
                and (not _reply_contains_executable_action(probe.reply))
            ):
                fallback_tools = _default_probe_tools_for_prompt(cleaned_prompt or prompt, available_tool_names)
                if fallback_tools:
                    probe = ProbeDecision(
                        mode=PROBE_NEED_TOOLS,
                        reply="",
                        tools=fallback_tools,
                        goal=(cleaned_prompt or prompt).strip(),
                        max_steps=self._tool_loop_max_steps,
                    )
            log_json(
                logger,
                "probe.decision",
                chat_id=chat_id,
                user_id=user_id,
                session_id=session_id,
                mode=probe.mode or "",
                tools=list(probe.tools or []),
            )
            if probe.mode == PROBE_NO_TOOLS and probe.reply:
                await self._notify_progress(progress_callback, {"event": "loop.probe.no_tools"})
                resolved_reply = probe.reply
                must_resolve_reply = (
                    _prompt_expects_action(cleaned_prompt or prompt)
                    or bool(_extract_exec_candidate_from_output(probe.reply))
                    or any(marker in probe.reply for marker in ("!exec", "!tool", "!loop"))
                )
                if must_resolve_reply:
                    resolved_reply = await self._resolve_autonomous_output(
                        output=probe.reply,
                        base_prompt=(cleaned_prompt or prompt),
                        chat_id=chat_id,
                        user_id=user_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        progress_callback=progress_callback,
                        autonomy_depth=autonomy_depth,
                        workspace_root=workspace_root,
                        policy_profile=self._agent_policy_profile(agent_id=agent_id),
                        extra_tools=extra_tools,
                        goal=(cleaned_prompt or prompt),
                    )
                if active_skills:
                    await self._notify_progress(
                        progress_callback,
                        {"event": "skills.deactivated", "skills": [s.skill_id for s in active_skills]},
                    )
                return resolved_reply
            if probe.mode == PROBE_NEED_TOOLS and probe.tools:
                await self._notify_progress(progress_callback, {"event": "loop.probe.need_tools", "tools": probe.tools})
                need_tools_output = await self._run_need_tools_inference(
                    prompt=(cleaned_prompt or prompt),
                    goal=probe.goal,
                    selected_tools=probe.tools,
                    max_steps=probe.max_steps,
                    agent_id=agent_id,
                    workspace_root=workspace_root,
                )
                (
                    need_tools_output,
                    need_tools_protocol,
                    need_tools_transpiled,
                ) = await self._enforce_need_tools_protocol_output(
                    output=need_tools_output,
                    prompt=(cleaned_prompt or prompt),
                    goal=probe.goal,
                    selected_tools=probe.tools,
                    agent_id=agent_id,
                )
                log_json(
                    logger,
                    "need_tools.model.output",
                    chat_id=chat_id,
                    user_id=user_id,
                    session_id=session_id,
                    preview=(need_tools_output or "")[:200],
                    protocol=bool(need_tools_protocol),
                    transpiled=bool(need_tools_transpiled),
                    decoded=bool(need_tools_protocol),
                    dialect=_detect_toolcall_dialect(need_tools_output),
                    repaired=bool(need_tools_transpiled),
                )
                generated_actions, _, generated_final_prompt = _extract_loop_actions(
                    need_tools_output,
                    preferred_tools=probe.tools,
                )
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
                    output = await self._resolve_autonomous_output(
                        output=need_tools_output.strip(),
                        base_prompt=(cleaned_prompt or prompt),
                        chat_id=chat_id,
                        user_id=user_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        progress_callback=progress_callback,
                        autonomy_depth=autonomy_depth,
                        workspace_root=workspace_root,
                        policy_profile=self._agent_policy_profile(agent_id=agent_id),
                        extra_tools=extra_tools,
                        selected_tools=probe.tools,
                        goal=probe.goal,
                    )
                    if active_skills:
                        await self._notify_progress(
                            progress_callback,
                            {"event": "skills.deactivated", "skills": [s.skill_id for s in active_skills]},
                        )
                    return output
        if not actions and self._autonomous_tool_loop_enabled():
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
            contextual = self.build_session_prompt(session_id=session_id, user_prompt=cleaned_prompt or prompt)
            job_id = await self.queue_prompt(prompt=contextual, agent_id=agent_id)
            await self._notify_progress(progress_callback, {"event": "model.job.queued", "job_id": job_id})
            output = await self._wait_job_with_progress(job_id=job_id, progress_callback=progress_callback)
            await self._notify_progress(progress_callback, {"event": "model.job.finished", "job_id": job_id})
            output = await self._resolve_autonomous_output(
                output=output,
                base_prompt=(cleaned_prompt or prompt),
                chat_id=chat_id,
                user_id=user_id,
                session_id=session_id,
                workspace_root=workspace_root,
                agent_id=agent_id,
                progress_callback=progress_callback,
                autonomy_depth=autonomy_depth,
                policy_profile=self._agent_policy_profile(agent_id=agent_id),
                extra_tools=extra_tools,
                goal=(cleaned_prompt or prompt),
            )
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
        session_workspace = workspace_root
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
                approval_required = self._tool_action_requires_approval(action.tool_name)
                log_json(
                    logger,
                    "tool.exec.pre",
                    session_id=session_id,
                    action_id=action_id,
                    step=index,
                    kind="tool",
                    tool_name=action.tool_name,
                    command=command,
                    toolcall_ir=_loop_action_to_toolcall_ir(action),
                    allowed=True,
                    approval_required=approval_required,
                    approval_granted=not approval_required,
                    exit_code=None,
                )
                if approval_required:
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
                            timeout_sec=max(1, int(action.timeout_sec or 60)),
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
                    log_json(
                        logger,
                        "tool.exec.post",
                        session_id=session_id,
                        action_id=action_id,
                        step=index,
                        kind="tool",
                        tool_name=action.tool_name,
                        command=command,
                        toolcall_ir=_loop_action_to_toolcall_ir(action),
                        allowed=True,
                        approval_required=True,
                        approval_granted=False,
                        exit_code=None,
                    )
                    action_preview = _format_tool_action_preview(action.tool_name, action.tool_args)
                    msg = (
                        "Approval required for high-risk tool action before I can continue.\n"
                        f"Action: {action_preview}\n"
                        f"Approve once: /approve {approval_id[:8]}\n"
                        f"Deny: /deny {approval_id[:8]}"
                    )
                    self.append_session_assistant_message(session_id=session_id, content=msg)
                    return msg
                result = await self._execute_registered_tool_action(
                    action_id=action_id,
                    tool_name=action.tool_name,
                    tool_args=action.tool_args,
                    workspace_root=session_workspace,
                    policy_profile=policy_profile,
                    extra_tools=dict(snapshot.tools),
                    allowed_tool_names=available_tool_names,
                    chat_id=chat_id,
                    user_id=user_id,
                    session_id=session_id,
                )
                observations.append(result.output)
                rc = 0 if result.ok else 1
                log_json(
                    logger,
                    "tool.exec.post",
                    session_id=session_id,
                    action_id=action_id,
                    step=index,
                    kind="tool",
                    tool_name=action.tool_name,
                    command=command,
                    toolcall_ir=_loop_action_to_toolcall_ir(action),
                    allowed=True,
                    approval_required=False,
                    approval_granted=True,
                    exit_code=rc,
                )
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
            approval_required = decision.risk_tier == "high"
            log_json(
                logger,
                "tool.exec.pre",
                session_id=session_id,
                action_id=action_id,
                step=index,
                kind="exec",
                command=" ".join(argv),
                toolcall_ir=_loop_action_to_toolcall_ir(action),
                allowed=decision.allowed,
                approval_required=approval_required,
                approval_granted=(not approval_required),
                exit_code=None,
            )
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
                log_json(
                    logger,
                    "tool.exec.post",
                    session_id=session_id,
                    action_id=action_id,
                    step=index,
                    kind="exec",
                    command=" ".join(argv),
                    toolcall_ir=_loop_action_to_toolcall_ir(action),
                    allowed=False,
                    approval_required=approval_required,
                    approval_granted=False,
                    exit_code=126,
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
                    log_json(
                        logger,
                        "tool.exec.post",
                        session_id=session_id,
                        action_id=action_id,
                        step=index,
                        kind="exec",
                        command=" ".join(argv),
                        toolcall_ir=_loop_action_to_toolcall_ir(action),
                        allowed=True,
                        approval_required=True,
                        approval_granted=False,
                        exit_code=None,
                    )
                    command_preview = " ".join(argv)
                    msg = (
                        "Approval required before I can continue with this high-risk action.\n"
                        f"Action: {command_preview}\n"
                        f"Approve once: /approve {approval_id[:8]}\n"
                        f"Deny: /deny {approval_id[:8]}"
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
                    timeout_sec=max(1, int(action.timeout_sec or 60)),
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
                log_json(
                    logger,
                    "tool.exec.post",
                    session_id=session_id,
                    action_id=action_id,
                    step=index,
                    kind="exec",
                    command=" ".join(argv),
                    toolcall_ir=_loop_action_to_toolcall_ir(action),
                    allowed=True,
                    approval_required=True,
                    approval_granted=False,
                    exit_code=None,
                )
                command_preview = " ".join(argv)
                msg = (
                    "Approval required before I can continue with this high-risk action.\n"
                    f"Action: {command_preview}\n"
                    f"Approve once: /approve {approval_id[:8]}\n"
                    f"Deny: /deny {approval_id[:8]}"
                )
                self.append_session_assistant_message(session_id=session_id, content=msg)
                return msg

            result, tool_run_id = await self._execute_tool_action_with_telemetry(
                action_id=action_id,
                session_id=session_id,
                agent_id=agent_id,
                argv=argv,
                stdin_text="",
                timeout_sec=max(1, int(action.timeout_sec or 60)),
                policy_profile=policy_profile,
                workspace_root=str(session_workspace),
            )
            log_json(
                logger,
                "tool.exec.post",
                session_id=session_id,
                action_id=action_id,
                step=index,
                kind="exec",
                command=" ".join(argv),
                toolcall_ir=_loop_action_to_toolcall_ir(action),
                allowed=True,
                approval_required=False,
                approval_granted=True,
                exit_code=result.returncode,
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
        selected_tools = [a.tool_name for a in actions if a.kind == "tool" and a.tool_name]
        if any(a.kind == "exec" for a in actions):
            selected_tools.append("exec")
        output = await self._resolve_autonomous_output(
            output=output,
            base_prompt=(final_prompt or cleaned_prompt or prompt),
            chat_id=chat_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            progress_callback=progress_callback,
            autonomy_depth=autonomy_depth,
            workspace_root=session_workspace,
            policy_profile=policy_profile,
            extra_tools=extra_tools,
            selected_tools=selected_tools,
            goal=(cleaned_prompt or prompt),
        )
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

    def runtime_capabilities(self) -> Dict[str, Any]:
        runner_name = self._execution_runner.__class__.__name__
        backend = "docker" if "docker" in runner_name.lower() else "local"
        return {
            "execution_backend": backend,
            "execution_runner": runner_name,
            "probe_loop_enabled": bool(self._probe_loop is not None),
            "mcp_bridge_enabled": bool(self._mcp_bridge is not None),
            "skill_packs_enabled": bool(self._skill_pack_loader is not None),
            "tool_policy_enabled": bool(self._tool_policy_engine is not None),
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

    def _workspace_is_git_repo(self, workspace_root: Path) -> bool:
        try:
            snapshot = build_runtime_tool_registry(self._tool_registry, workspace_root=workspace_root)
            return bool(snapshot.invariants.is_git_repo)
        except Exception:
            return False

    def _validate_actions(
        self,
        actions: Sequence[LoopAction],
        workspace_root: Path,
        available_tool_names: Sequence[str],
    ) -> tuple[List[LoopAction], str]:
        normalized: List[LoopAction] = []
        allowed_tools = set(_normalize_tool_names(list(available_tool_names)))
        for action in actions:
            if action.kind == "tool":
                name = str(action.tool_name or "").strip().lower()
                if not name:
                    return [], "Error: invalid tool call (missing tool name)."
                if name not in allowed_tools:
                    return [], f"Error: tool '{name}' is not available in this turn."
                args = dict(action.tool_args or {})
                if name in {"read_file", "write_file"}:
                    path = _resolve_workspace_bound_path(args.get("path"), workspace_root)
                    if path is None:
                        return [], "Error: file operation path must be inside WORKSPACE_ROOT."
                    if path:
                        args["path"] = path
                if name.startswith("git_") and not self._workspace_is_git_repo(workspace_root):
                    return [], "Error: git tools are disabled because WORKSPACE_ROOT is not a git repository."
                normalized.append(
                    LoopAction(
                        kind="tool",
                        argv=[],
                        tool_name=name,
                        tool_args=args,
                        timeout_sec=max(1, int(action.timeout_sec or 60)),
                    )
                )
                continue
            if action.kind != "exec":
                return [], f"Error: unsupported tool action kind '{action.kind}'."
            argv = [str(x or "").strip() for x in list(action.argv or []) if str(x or "").strip()]
            if not argv:
                return [], "Error: invalid exec action (empty command)."
            first = argv[0].lower()
            rendered = " ".join(argv)
            rendered_low = rendered.lower()
            if first.startswith("{") or "{cmd:" in rendered_low:
                return [], "Error: invalid exec action format. Expected canonical command syntax."
            if re.search(r"(^|[^A-Za-z0-9_])find\s+/(?:\s|$)", rendered_low):
                return [], "Error: unsafe search scope. Use absolute paths under WORKSPACE_ROOT only."
            invokes_git = first == "git"
            if (not invokes_git) and first in {"bash", "sh", "zsh"} and len(argv) >= 3:
                inner = str(argv[2] or "").strip().lower()
                invokes_git = bool(re.search(r"(^|\s)git(\s|$)", inner))
            if invokes_git and not self._workspace_is_git_repo(workspace_root):
                return [], "Error: git commands are disabled because WORKSPACE_ROOT is not a git repository."
            normalized.append(
                LoopAction(
                    kind="exec",
                    argv=argv,
                    tool_name="",
                    tool_args={},
                    timeout_sec=max(1, int(action.timeout_sec or 60)),
                )
            )
        return normalized, ""

    async def _execute_registered_tool_action(
        self,
        action_id: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        workspace_root: Path,
        policy_profile: str,
        extra_tools: Optional[Dict[str, Any]] = None,
        allowed_tool_names: Optional[Sequence[str]] = None,
        chat_id: int = 0,
        user_id: int = 0,
        session_id: str = "",
    ):
        normalized_tool_name = (tool_name or "").strip().lower()
        allowed = set(_normalize_tool_names(list(allowed_tool_names or [])))
        if allowed and normalized_tool_name not in allowed:
            return ToolResult(
                ok=False,
                output=f"{action_id} tool={tool_name} error=tool_unavailable_in_this_turn",
            )
        normalized_args = _normalize_tool_args_for_workspace(
            tool_name=normalized_tool_name,
            tool_args=dict(tool_args or {}),
            workspace_root=workspace_root,
            policy_profile=policy_profile,
        )
        if normalized_args is None:
            return ToolResult(
                ok=False,
                output=f"{action_id} tool={tool_name} error=invalid_path outside_workspace",
            )
        if normalized_tool_name == "shell_exec":
            normalized_args.setdefault("_chat_id", int(chat_id or 0))
            normalized_args.setdefault("_user_id", int(user_id or 0))
            normalized_args.setdefault("_session_id", session_id or "")
        if allowed:
            tool = (extra_tools or {}).get(normalized_tool_name)
        else:
            tool = (extra_tools or {}).get(normalized_tool_name) or self._tool_registry.get(normalized_tool_name)
        if not tool:
            if normalized_tool_name in {"send_email_smtp", "send_email"}:
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
        context = ToolContext(
            workspace_root=workspace_root,
            policy_profile=policy_profile,
            chat_id=int(chat_id or 0),
            user_id=int(user_id or 0),
            session_id=session_id or "",
        )
        req = ToolRequest(name=tool_name, args=normalized_args)
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
        if name in {"send_email_smtp", "send_email"}:
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
        snapshot = self.runtime_tool_snapshot(
            session_id=session_id,
            extra_tools=extra_tools if extra_tools else None,
            refresh=True,
        )
        tool_name = "send_email_smtp"
        if tool_name not in snapshot.names():
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
            extra_tools=dict(snapshot.tools),
            allowed_tool_names=snapshot.names(),
            session_id=session_id,
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
        snapshot = self.runtime_tool_snapshot(
            session_id=session_id,
            extra_tools=extra_tools if extra_tools else None,
            refresh=True,
        )
        tool_name = "send_email_smtp"
        if tool_name not in snapshot.names():
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
            extra_tools=dict(snapshot.tools),
            allowed_tool_names=snapshot.names(),
            session_id=session_id,
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
        snapshot = self.runtime_tool_snapshot(
            session_id=session_id,
            extra_tools=extra_tools if extra_tools else None,
            refresh=True,
        )
        parsed = _extract_tool_invocation_from_output(output)
        if not parsed:
            return None
        tool_name, tool_args = parsed
        if tool_name not in snapshot.names():
            return None
        action_id = "tool-" + uuid.uuid4().hex[:8]
        result = await self._execute_registered_tool_action(
            action_id=action_id,
            tool_name=tool_name,
            tool_args=tool_args,
            workspace_root=workspace_root,
            policy_profile=policy_profile,
            extra_tools=dict(snapshot.tools),
            allowed_tool_names=snapshot.names(),
            session_id=session_id,
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

    async def _request_tool_call_correction(
        self,
        prompt: str,
        model_output: str,
        selected_tools: Sequence[str],
        goal: str,
        agent_id: str,
    ) -> str:
        tool_names = _normalize_tool_names(list(selected_tools or []))
        if not tool_names:
            tool_names = _default_probe_tools_for_prompt(prompt, self._tool_registry.names())
        if not tool_names:
            tool_names = ["exec"]
        tool_lines = _render_tool_schema_lines(tool_names[:6])
        capability_lines = self._build_capability_context_for_tools(tool_names[:6], max_capabilities=4)
        guidance = [
            "You are in autonomous execution mode.",
            "The previous response described intent but did not execute.",
            "Output exactly one next action now.",
            "Allowed outputs:",
            "- !exec ...",
            "- !tool {...}",
            "- !loop {...}",
            "If blocked by missing required input/approval, ask one short question only.",
            "Do not include explanation before tool call.",
            "Do not use markdown code fences.",
            "Use absolute paths for file operations and never run `find /`.",
            f"Goal: {(goal or prompt or '').strip()}",
            "User request:",
            (prompt or "").strip(),
            "Previous response:",
            (model_output or "").strip(),
            "Selected tool APIs:",
        ]
        guidance.extend(tool_lines)
        if capability_lines:
            guidance.extend(capability_lines)
        text = "\n".join([ln for ln in guidance if str(ln).strip()])
        provider_for_call = self._provider_for_agent(agent_id=agent_id)
        try:
            raw = await provider_for_call.generate(
                [{"role": "user", "content": text}],
                stream=False,
                policy_profile=self._agent_policy_profile(agent_id=agent_id),
            )
        except Exception:
            return ""
        return (raw or "").strip()

    async def _enforce_need_tools_protocol_output(
        self,
        output: str,
        prompt: str,
        goal: str,
        selected_tools: Sequence[str],
        agent_id: str,
    ) -> tuple[str, bool, bool]:
        text = (output or "").strip()
        if not text:
            return "", False, False
        text, auto_transpiled = _transpile_need_tools_output(text, selected_tools=selected_tools)
        actions, _, _ = _extract_loop_actions(text, preferred_tools=selected_tools)
        if actions:
            return text, True, auto_transpiled
        if _is_blocking_question(text):
            return text, False, auto_transpiled
        original_command = _extract_exec_candidate_from_output(text)

        corrected = await self._request_tool_call_correction(
            prompt=prompt,
            model_output=text,
            selected_tools=selected_tools,
            goal=goal,
            agent_id=agent_id,
        )
        corrected = (corrected or "").strip()
        if corrected:
            corrected, corrected_transpiled = _transpile_need_tools_output(corrected, selected_tools=selected_tools)
            actions, _, _ = _extract_loop_actions(corrected, preferred_tools=selected_tools)
            if actions:
                return corrected, True, (auto_transpiled or corrected_transpiled)
            if _is_blocking_question(corrected):
                return corrected, False, (auto_transpiled or corrected_transpiled)
            text = corrected

        command = _extract_exec_candidate_from_output(text)
        if not command:
            command = original_command
        argv = _parse_exec_command_argv(command) if command else []
        if argv:
            return f"!exec {str(command).strip()}", True, True
        return text, False, auto_transpiled

    async def _resolve_autonomous_output(
        self,
        output: str,
        base_prompt: str,
        chat_id: int,
        user_id: int,
        session_id: str,
        agent_id: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
        autonomy_depth: int,
        workspace_root: Path,
        policy_profile: str,
        extra_tools: Dict[str, Any],
        selected_tools: Optional[Sequence[str]] = None,
        goal: str = "",
        allow_correction: bool = True,
    ) -> str:
        text = (output or "").strip()
        protocol_output = await self._attempt_autonomous_protocol_output(
            output=text,
            prompt=base_prompt,
            chat_id=chat_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            progress_callback=progress_callback,
            autonomy_depth=autonomy_depth,
        )
        if protocol_output is not None:
            text = protocol_output

        executed_tool = await self._attempt_autonomous_tool_invocation(
            output=text,
            session_id=session_id,
            workspace_root=workspace_root,
            policy_profile=policy_profile,
            extra_tools=extra_tools,
        )
        has_executed_tool = executed_tool is not None
        if has_executed_tool:
            text = executed_tool

        executed_slash = await self._attempt_autonomous_email_slash_command(
            output=text,
            session_id=session_id,
            workspace_root=workspace_root,
            policy_profile=policy_profile,
            extra_tools=extra_tools,
        )
        if executed_slash is not None:
            text = executed_slash
        elif (not has_executed_tool) and _output_claims_email_sent(text):
            recovered = await self._attempt_autonomous_email_send_recovery(
                output=text,
                prompt=base_prompt,
                session_id=session_id,
                workspace_root=workspace_root,
                policy_profile=policy_profile,
                extra_tools=extra_tools,
            )
            if recovered is None:
                text = (
                    "Error: email send was claimed, but no SMTP tool action was executed.\n"
                    "Please provide explicit recipient email, subject, and body so I can execute the send."
                )
            else:
                text = recovered

        if (
            allow_correction
            and (not has_executed_tool)
            and executed_slash is None
            and _prompt_expects_action(base_prompt)
            and autonomy_depth < self._autonomous_protocol_max_depth()
        ):
            extracted_command = _extract_exec_candidate_from_output(text)
            extracted_argv = _parse_exec_command_argv(extracted_command) if extracted_command else []
            if extracted_argv:
                loop_payload = {
                    "steps": [{"kind": "exec", "command": shlex.join(extracted_argv)}],
                    "final_prompt": _need_tools_summary_prompt(goal=(goal or base_prompt)),
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

        if (
            allow_correction
            and (not has_executed_tool)
            and executed_slash is None
            and _prompt_expects_action(base_prompt)
            and (_output_sounds_like_action_promise(text) or _contains_tool_call_signatures(text))
            and autonomy_depth < self._autonomous_protocol_max_depth()
        ):
            corrected = await self._request_tool_call_correction(
                prompt=base_prompt,
                model_output=text,
                selected_tools=selected_tools or [],
                goal=goal,
                agent_id=agent_id,
            )
            corrected = (corrected or "").strip()
            if corrected and corrected != text:
                return await self._resolve_autonomous_output(
                    output=corrected,
                    base_prompt=base_prompt,
                    chat_id=chat_id,
                    user_id=user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    progress_callback=progress_callback,
                    autonomy_depth=autonomy_depth + 1,
                    workspace_root=workspace_root,
                    policy_profile=policy_profile,
                    extra_tools=extra_tools,
                    selected_tools=selected_tools,
                    goal=goal,
                    allow_correction=False,
                )
        return text

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
        workspace_root: Path,
    ) -> str:
        normalized_tools = _normalize_tool_names(list(selected_tools))
        if not normalized_tools:
            return ""
        provider_for_call = self._provider_for_agent(agent_id=agent_id)
        tool_lines = _render_tool_schema_lines(normalized_tools)
        capability_lines = self._build_capability_context_for_tools(normalized_tools, max_capabilities=4)
        call_prompt = self._build_need_tools_prompt(
            prompt=prompt,
            goal=goal,
            selected_tools=normalized_tools,
            max_steps=max_steps,
            workspace_root=workspace_root,
            tool_lines=tool_lines,
            capability_lines=capability_lines,
        )
        log_json(
            logger,
            "need_tools.model.request",
            agent_id=agent_id,
            allowed_tools=list(normalized_tools),
            injected_schemas=list(tool_lines),
            workspace_root=str(workspace_root),
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
        workspace_root: Path,
        tool_lines: Optional[List[str]] = None,
        capability_lines: Optional[List[str]] = None,
    ) -> str:
        tool_lines = tool_lines or _render_tool_schema_lines(selected_tools)
        capability_lines = capability_lines or self._build_capability_context_for_tools(
            selected_tools,
            max_capabilities=4,
        )
        lines = [
            f"Style guide: {MICRO_STYLE_GUIDE}",
            "Behavior rules (strict):",
            "1) Tools are for the assistant to call, never for the user to type.",
            "2) If you can proceed, proceed. Ask only when blocked or approval is required.",
            "3) If tools are required now, output only one block: !exec ... OR !tool {...} OR !loop {...}.",
            "4) Do not output explanations before a tool call.",
            "5) Do not stop at diagnosis; execute the next concrete step immediately when safe.",
            "6) After tool results: continue with next tool call until goal is done or blocked.",
            "7) Never output shell snippets or markdown fences. Emit protocol blocks only.",
            "8) Use absolute paths under WORKSPACE_ROOT for file operations.",
            "9) Never run `find /`; search only inside WORKSPACE_ROOT.",
            f"WORKSPACE_ROOT: {str(workspace_root)}",
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
                    planned_actions.append(
                        LoopAction(kind="exec", argv=argv, tool_name="", tool_args={}, timeout_sec=60)
                    )
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
    if "email" in low or "mail" in low:
        if "send_email_smtp" in available and "send_email_smtp" not in picks:
            picks.append("send_email_smtp")
        elif "send_email" in available and "send_email" not in picks:
            picks.append("send_email")
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
    return {
        "kind": "exec",
        "command": shlex.join(action.argv),
        "timeout_sec": max(1, int(action.timeout_sec or 60)),
    }


def _loop_action_to_toolcall_ir(action: LoopAction) -> Dict[str, Any]:
    if action.kind == "tool":
        return {
            "kind": "tool",
            "name": action.tool_name,
            "args": dict(action.tool_args or {}),
            "raw": action.checkpoint_command(),
            "confidence": 1.0,
            "timeout_s": max(1, int(action.timeout_sec or 60)),
        }
    command = shlex.join(action.argv) if action.argv else ""
    return {
        "kind": "exec",
        "name": "exec",
        "args": {"command": command},
        "raw": command,
        "confidence": 1.0,
        "timeout_s": max(1, int(action.timeout_sec or 60)),
    }


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


def _format_tool_action_preview(tool_name: str, tool_args: Dict[str, Any]) -> str:
    name = (tool_name or "").strip() or "tool"
    args = dict(tool_args or {})
    if name in {"send_email_smtp", "send_email"}:
        to_addr = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        parts = [name]
        if to_addr:
            parts.append(f"to={to_addr}")
        if subject:
            parts.append(f"subject={subject}")
        return " | ".join(parts)
    if not args:
        return name
    compact = ", ".join([f"{k}={v}" for k, v in list(args.items())[:3]])
    return f"{name}({compact})"


def _resolve_workspace_bound_path(raw_path: Any, workspace_root: Path) -> Optional[str]:
    value = str(raw_path or "").strip()
    if not value:
        return ""
    candidate = Path(value).expanduser()
    base = workspace_root.resolve()
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    if resolved == base or resolved.is_relative_to(base):
        return str(resolved)
    return None


def _normalize_tool_args_for_workspace(
    tool_name: str,
    tool_args: Dict[str, Any],
    workspace_root: Path,
    policy_profile: str = "balanced",
) -> Optional[Dict[str, Any]]:
    normalized = dict(tool_args or {})
    name = (tool_name or "").strip().lower()
    if name in {"read_file", "write_file"}:
        if "path" not in normalized:
            return normalized
        path = _resolve_workspace_bound_path(normalized.get("path"), workspace_root)
        if path is None:
            return None
        normalized["path"] = path
    return normalized


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


def _command_needs_shell_wrapper(command: str) -> bool:
    text = str(command or "")
    if not text.strip():
        return False
    shell_markers = ("|", "&&", "||", ";", ">", "<", "$(", "`", "&")
    return any(marker in text for marker in shell_markers)


def _extract_timeout_suffix(text: str) -> int:
    raw = str(text or "").strip()
    match = re.search(r"(?i)\|\s*timeout\s*=\s*(\d+)\s*$", raw)
    if not match:
        return 0
    try:
        return max(1, min(int(match.group(1)), 1800))
    except Exception:
        return 0


def _strip_timeout_suffix(text: str) -> str:
    raw = str(text or "").strip()
    return re.sub(r"(?is)\|\s*timeout\s*=\s*\d+\s*$", "", raw).strip()


def _unwrap_step_cmd_syntax(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    saw_step = False
    step = re.match(r"(?is)^step\s*\d+\s*:\s*(.+)$", raw)
    if step:
        saw_step = True
        raw = step.group(1).strip()
    raw = _strip_timeout_suffix(raw)
    if saw_step:
        cmd_match = re.match(r"(?is)^\{cmd\s*:\s*(.+)\}$", raw)
        if cmd_match:
            return cmd_match.group(1).strip()
    return raw


def _parse_exec_command_argv(command: str) -> List[str]:
    cmd = str(command or "").strip()
    if re.match(r"(?is)^step\s*\d+\s*:", cmd):
        cmd = _unwrap_step_cmd_syntax(cmd)
    if not cmd:
        return []
    if re.match(r"(?is)^\{cmd\s*:", cmd):
        return []
    if re.search(r"(^|[^A-Za-z0-9_])find\s+/(?:\s|$)", cmd):
        return []
    try:
        parsed = shlex.split(cmd)
    except ValueError:
        parsed = []
    if parsed and (parsed[0].startswith("{") or "{cmd:" in " ".join(parsed).lower()):
        return []
    if len(parsed) >= 3 and parsed[0] in {"bash", "sh", "zsh"} and parsed[1] == "-lc":
        if "{cmd:" in str(parsed[2]).lower():
            return []
        if re.search(r"(^|[^A-Za-z0-9_])find\s+/(?:\s|$)", str(parsed[2]).lower()):
            return []
        return parsed
    if _command_needs_shell_wrapper(cmd):
        return ["bash", "-lc", cmd]
    return parsed


def _coerce_exec_timeout_sec(source: Dict[str, Any], default: int = 60) -> int:
    timeout = default
    raw_sec = source.get("timeout_sec")
    if raw_sec is None:
        raw_sec = source.get("timeoutSec")
    if raw_sec is None:
        raw_sec = source.get("timeout")
    if raw_sec is not None:
        try:
            timeout = int(raw_sec)
        except (TypeError, ValueError):
            timeout = default
    else:
        raw_ms = source.get("timeout_ms")
        if raw_ms is None:
            raw_ms = source.get("timeoutMs")
        if raw_ms is not None:
            try:
                timeout = max(1, int(int(raw_ms) / 1000))
            except (TypeError, ValueError):
                timeout = default
    return max(1, min(timeout, 1800))


def _coerce_exec_background(source: Dict[str, Any]) -> bool:
    raw = source.get("background")
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _prepare_exec_command(command: str, options: Dict[str, Any]) -> str:
    cmd = str(command or "").strip()
    if not cmd:
        return ""
    workdir = str(options.get("workdir") or options.get("cwd") or "").strip()
    env = options.get("env")
    background = _coerce_exec_background(options)
    if isinstance(env, dict) and env:
        pairs: List[str] = []
        for key, value in env.items():
            k = str(key or "").strip()
            if not k or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                continue
            pairs.append(f"{k}={shlex.quote(str(value))}")
        if pairs:
            cmd = f"env {' '.join(pairs)} {cmd}"
    if workdir:
        cmd = f"cd {shlex.quote(workdir)} && {cmd}"
    if background:
        cmd = f"nohup {cmd} >/tmp/codex-exec-bg.log 2>&1 & echo background_started"
    return cmd


def _infer_tool_name_from_args(args: Dict[str, Any], preferred_tools: Sequence[str]) -> str:
    allowed = set(_normalize_tool_names(list(preferred_tools or [])))
    if not allowed:
        return ""
    keys = {str(k or "").strip().lower() for k in dict(args or {}).keys()}
    if "cmd" in keys and "shell_exec" in allowed:
        return "shell_exec"
    if "query" in keys:
        for name in ("mcp_search", "memory_search"):
            if name in allowed:
                return name
    if "path" in keys:
        if "content" in keys and "write_file" in allowed:
            return "write_file"
        if "read_file" in allowed:
            return "read_file"
    if {"to", "subject", "body"} <= keys:
        for name in ("send_email_smtp", "send_email"):
            if name in allowed:
                return name
    return ""


def _parse_tool_directive(
    body: str,
    preferred_tool_name: str = "",
    preferred_tools: Optional[Sequence[str]] = None,
) -> Optional[LoopAction]:
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
            args_dict = dict(args or {}) if isinstance(args, dict) else {}
            cmd = str(args_dict.get("cmd") or args_dict.get("command") or "").strip()
            if not cmd:
                return None
            timeout_sec = _coerce_exec_timeout_sec(args_dict, default=60)
            cmd = _prepare_exec_command(cmd, args_dict)
            argv = _parse_exec_command_argv(cmd)
            if argv:
                return LoopAction(kind="exec", argv=argv, tool_name="", tool_args={}, timeout_sec=timeout_sec)
            return None
        if (not tool_name) and isinstance(obj, dict):
            direct_args = dict(obj)
            direct_args.pop("name", None)
            direct_args.pop("tool", None)
            direct_args.pop("args", None)
            merged: Dict[str, Any] = {}
            if isinstance(args, dict):
                merged.update(dict(args))
            merged.update(direct_args)
            inferred = preferred_tool_name or _infer_tool_name_from_args(merged, preferred_tools or [])
            if inferred:
                return LoopAction(kind="tool", argv=[], tool_name=inferred, tool_args=merged)
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
        timeout_sec = _coerce_exec_timeout_sec(args, default=60)
        command = str(args.get("cmd") or args.get("command") or "").strip()
        if command:
            command = _prepare_exec_command(command, args)
            argv = _parse_exec_command_argv(command)
            if argv:
                return LoopAction(kind="exec", argv=argv, tool_name="", tool_args={}, timeout_sec=timeout_sec)
            return None
        if positional:
            command = _prepare_exec_command(" ".join(positional), args)
            argv = _parse_exec_command_argv(command)
            if argv:
                return LoopAction(kind="exec", argv=argv, tool_name="", tool_args={}, timeout_sec=timeout_sec)
        return None

    if positional:
        if tool_name in {"read_file", "write_file"} and "path" not in args:
            args["path"] = positional[0]
        elif tool_name == "shell_exec" and "cmd" not in args:
            args["cmd"] = " ".join(positional)
        elif tool_name == "git_add" and "paths" not in args:
            args["paths"] = positional

    return LoopAction(kind="tool", argv=[], tool_name=tool_name, tool_args=args)


def _extract_inline_protocol_suffix(line: str) -> str:
    raw = str(line or "").strip()
    if not raw:
        return ""
    patterns = [
        r"(?is)(?:^|[\s])(![A-Za-z_][A-Za-z0-9_-]*\s+.+)\s*$",
        r"(?is)(?:^|[\s])(!?loop\s+\{.*\})\s*$",
        r"(?is)(?:^|[\s])(!?tool\s+\{.*\})\s*$",
        r"(?is)(?:^|[\s])(!?exec\s+.+)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def _extract_loop_actions(prompt: str, preferred_tools: Optional[Sequence[str]] = None) -> tuple[List[LoopAction], str, str]:
    actions: List[LoopAction] = []
    keep_lines: List[str] = []
    final_prompt = ""
    normalized_preferred = _normalize_tool_names(list(preferred_tools or []))
    preferred_tool_name = normalized_preferred[0] if len(normalized_preferred) == 1 else ""
    raw_lines = (prompt or "").splitlines()
    index = 0
    while index < len(raw_lines):
        raw = raw_lines[index]
        line = raw.strip()
        if line and not re.match(r"(?is)^!?\s*(loop|exec|tool)\b", line):
            inline_suffix = _extract_inline_protocol_suffix(line)
            if inline_suffix:
                line = inline_suffix
        loop_match = re.match(r"(?is)^!?loop\s+(.+)$", line)
        if loop_match:
            body = loop_match.group(1).strip()
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
                                argv = _parse_exec_command_argv(command)
                            if argv:
                                actions.append(
                                    LoopAction(
                                        kind="exec",
                                        argv=argv,
                                        tool_name="",
                                        tool_args={},
                                        timeout_sec=_coerce_exec_timeout_sec(item, default=60),
                                    )
                                )
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
        exec_match = re.match(r"(?is)^!?exec(?:\s+(.*))?$", line)
        if exec_match:
            cmd_seed = str(exec_match.group(1) or "").strip()
            candidate = cmd_seed
            consumed = index + 1
            while (not candidate) and consumed < len(raw_lines):
                next_line = raw_lines[consumed].strip()
                consumed += 1
                if not next_line:
                    continue
                candidate = next_line
                break
            argv: List[str] = []
            if candidate:
                if _command_needs_shell_wrapper(candidate):
                    argv = _parse_exec_command_argv(candidate)
                else:
                    parsed = _parse_exec_command_argv(candidate)
                    if parsed:
                        argv = parsed
                    else:
                        while consumed < len(raw_lines):
                            candidate = candidate + "\n" + raw_lines[consumed]
                            consumed += 1
                            parsed = _parse_exec_command_argv(candidate)
                            if parsed:
                                argv = parsed
                                break
            if argv:
                actions.append(LoopAction(kind="exec", argv=argv, tool_name="", tool_args={}, timeout_sec=60))
            else:
                keep_lines.extend(raw_lines[index:consumed])
            index = consumed
            continue
        tool_match = re.match(r"(?is)^!?tool\s+(.+)$", line)
        if tool_match:
            body = tool_match.group(1).strip()
            parsed = _parse_tool_directive(
                body,
                preferred_tool_name=preferred_tool_name,
                preferred_tools=normalized_preferred,
            )
            if parsed:
                actions.append(parsed)
            else:
                keep_lines.append(raw)
            index += 1
            continue
        unknown_bang = re.match(r"(?is)^!([A-Za-z_][A-Za-z0-9_-]*)\s*(.*)$", line)
        if unknown_bang:
            name = str(unknown_bang.group(1) or "").strip().lower().replace("-", "_")
            if name in {"exec", "tool", "loop"}:
                keep_lines.append(raw)
                index += 1
                continue
            body = str(unknown_bang.group(2) or "").strip()
            args: Dict[str, Any] = {}
            if body:
                parsed_obj: Any = None
                if body.startswith("{") and body.endswith("}"):
                    try:
                        parsed_obj = json.loads(body)
                    except Exception:
                        parsed_obj = None
                if isinstance(parsed_obj, dict):
                    args = dict(parsed_obj)
                else:
                    args = {"query": body}
            actions.append(LoopAction(kind="tool", argv=[], tool_name=name, tool_args=args, timeout_sec=60))
            index += 1
            continue
        step_match = re.match(r"(?is)^step\s*\d+\s*:\s*(.+)$", line)
        if step_match:
            timeout_override = _extract_timeout_suffix(line)
            candidate = _unwrap_step_cmd_syntax(line)
            argv = _parse_exec_command_argv(candidate)
            if argv:
                actions.append(
                    LoopAction(
                        kind="exec",
                        argv=argv,
                        tool_name="",
                        tool_args={},
                        timeout_sec=max(1, int(timeout_override or 60)),
                    )
                )
            else:
                keep_lines.append(raw)
            index += 1
            continue
        keep_lines.append(raw)
        index += 1
    return actions, "\n".join(keep_lines).strip(), final_prompt


def _transpile_need_tools_output(text: str, selected_tools: Sequence[str]) -> tuple[str, bool]:
    raw = (text or "").strip()
    if not raw:
        return raw, False
    changed = False
    out = raw
    normalized_selected = _normalize_tool_names(list(selected_tools or []))
    default_tool = normalized_selected[0] if len(normalized_selected) == 1 else ""

    step_match = re.match(r"(?is)^step\s*\d+\s*:\s*(.+)$", out)
    if step_match:
        command = _unwrap_step_cmd_syntax(out)
        timeout = _extract_timeout_suffix(out)
        if command:
            if timeout > 0:
                out = "!loop " + json.dumps(
                    {"steps": [{"kind": "exec", "command": command, "timeout_sec": timeout}]},
                    ensure_ascii=True,
                )
            else:
                out = f"!exec {command}"
            changed = True

    if out.startswith("!tool ") and default_tool:
        payload_raw = out[len("!tool ") :].strip()
        try:
            payload_obj = json.loads(payload_raw)
        except Exception:
            payload_obj = None
        if isinstance(payload_obj, dict) and not str(payload_obj.get("name") or payload_obj.get("tool") or "").strip():
            out = "!tool " + json.dumps(
                {"name": default_tool, "args": payload_obj},
                ensure_ascii=True,
                sort_keys=True,
            )
            changed = True
    return out, changed


def _contains_tool_call_signatures(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if re.search(r"(?is)![a-z_][a-z0-9_-]*(?:\s|$)", raw):
        return True
    if re.search(r"(?is)!(exec|tool|loop)\s", raw):
        return True
    if re.search(r"(?is)\b(exec|tool|loop)\s*\{", raw):
        return True
    if re.search(r'(?is)\{[^{}]{0,500}"(?:name|tool|args|arguments|input|command)"\s*:', raw):
        return True
    if re.search(r"(?is)step\s*\d+\s*:.*\{cmd\s*:", raw) and re.search(r"(?is)\|\s*timeout\s*=", raw):
        return True
    if re.search(r"(?is)```.*?(?:!exec|!tool|!loop|\{cmd\s*:).*?```", raw):
        return True
    parsed = _parse_tool_invocation_json(raw)
    if parsed is not None:
        return True
    return False


def _detect_toolcall_dialect(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "none"
    if re.search(r"(?mi)^\s*!loop\b", raw):
        return "legacy_loop"
    if re.search(r"(?mi)^\s*!exec\b", raw):
        return "legacy_exec"
    if re.search(r"(?mi)^\s*!tool\b", raw):
        return "legacy_tool"
    if re.search(r"(?is)^step\s*\d+\s*:\s*", raw):
        return "step_cmd"
    if _parse_tool_invocation_json(raw):
        return "json_wrapper"
    return "none"


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


def _output_sounds_like_action_promise(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low.startswith("error:"):
        return False
    if any(line.strip().startswith(("!exec ", "!tool ", "!loop ")) for line in raw.splitlines()):
        return False
    if _extract_tool_invocation_from_output(raw):
        return False

    completion_markers = (
        "done.",
        "completed",
        "executed",
        "created",
        "installed",
        "updated",
        "fixed",
        "sent to",
        "email sent",
        "i did",
        "i've done",
    )
    future_markers = (
        "i will",
        "i'll",
        "let me",
        "i can",
        "i'm going to",
        "about to",
        "next i",
        "trying now",
        "sending response",
    )
    action_verbs = (
        "run",
        "execute",
        "check",
        "list",
        "inspect",
        "create",
        "make",
        "install",
        "download",
        "send",
        "open",
        "write",
    )
    has_future = any(marker in low for marker in future_markers)
    has_action_verb = any(verb in low for verb in action_verbs)
    has_completion = any(marker in low for marker in completion_markers)
    if re.search(r"(?mi)^\s*step\s*\d+\s*:\s*", raw):
        return True
    if has_future and has_action_verb and not has_completion:
        return True
    if "about to do" in low or "going to do" in low:
        return True
    return False


def _is_internal_assistant_trace(content: str) -> bool:
    low = str(content or "").strip().lower()
    if not low:
        return False
    markers = (
        "tool.action.completed action_id=",
        "tool.action.approved action_id=",
        "[tool:",
        "approval required for high-risk tool action before i can continue.",
        "approval required before i can continue with this high-risk action.",
        "approve once: /approve ",
    )
    return any(marker in low for marker in markers)


def _is_blocking_question(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.startswith(("!exec", "!tool", "!loop")):
        return False
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return False
    first = lines[0].lower()
    question_markers = ("?", "can you", "should i", "do you want", "which ", "what ", "where ")
    if any(first.startswith(marker) for marker in question_markers if marker != "?"):
        return True
    if raw.endswith("?"):
        return True
    return False


def _looks_like_shell_command_line(line: str) -> bool:
    raw = str(line or "").strip()
    if not raw:
        return False
    if raw.startswith(("!exec", "!tool", "!loop", "/")):
        return False
    if raw.lower().startswith(("assistant:", "user:", "step result", "done")):
        return False
    cleaned = raw[1:].strip() if raw.startswith("$") else raw
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        tokens = cleaned.split()
    if not tokens:
        return False
    first = tokens[0].lower()
    common = {
        "ls",
        "cat",
        "echo",
        "touch",
        "mkdir",
        "rm",
        "cp",
        "mv",
        "python",
        "python3",
        "pip",
        "pip3",
        "git",
        "npm",
        "node",
        "apt",
        "apt-get",
        "docker",
        "kubectl",
        "systemctl",
        "service",
        "ssh",
        "scp",
        "grep",
        "sed",
        "awk",
        "find",
        "pwd",
        "whoami",
        "history",
        "tail",
        "head",
        "chmod",
        "chown",
        "curl",
        "wget",
        "bash",
        "sh",
        "zsh",
        "printf",
        "tee",
    }
    if first in common:
        return True
    return first.startswith("./") or first.startswith("/") or first.endswith(".sh")


def _extract_exec_candidate_from_output(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""

    for match in re.finditer(r"```(?:bash|sh|shell|zsh)?\s*\n(.*?)```", raw, flags=re.I | re.S):
        block = match.group(1)
        for line in block.splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if candidate.startswith("$"):
                candidate = candidate[1:].strip()
            if _looks_like_shell_command_line(candidate):
                return candidate

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        step = re.match(r"(?i)^step\s*\d+\s*:\s*(.+)$", stripped)
        if step:
            candidate = _unwrap_step_cmd_syntax(stripped)
            if _looks_like_shell_command_line(candidate):
                return candidate
        if stripped.startswith("$"):
            candidate = stripped[1:].strip()
            if _looks_like_shell_command_line(candidate):
                return candidate

    wrapped = _unwrap_step_cmd_syntax(raw)
    if wrapped and _looks_like_shell_command_line(wrapped):
        return wrapped

    single = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(single) == 1 and _looks_like_shell_command_line(single[0]):
        return single[0].lstrip("$").strip()
    return ""


def _reply_contains_executable_action(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    actions, _, _ = _extract_loop_actions(raw)
    if actions:
        return True
    if _extract_exec_candidate_from_output(raw):
        return True
    return _extract_tool_invocation_from_output(raw) is not None


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
        name = str(
            obj.get("name")
            or obj.get("tool")
            or obj.get("tool_name")
            or obj.get("action")
            or ""
        ).strip().lower()
        args = obj.get("args")
        if args is None:
            args = obj.get("arguments")
        if args is None:
            args = obj.get("input")
        if args is None:
            args = obj.get("parameters")
        if isinstance(args, str):
            raw_args = args.strip()
            if raw_args.startswith("{") and raw_args.endswith("}"):
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:
                    parsed_args = None
                if isinstance(parsed_args, dict):
                    args = parsed_args
        if args is None:
            args = {}
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
