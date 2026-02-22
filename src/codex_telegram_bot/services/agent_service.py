from typing import Any, Awaitable, Callable, Dict, List, Optional
import logging
import re
import shlex
import uuid
import json
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

from codex_telegram_bot.domain.contracts import ProviderAdapter, ExecutionRunner
from codex_telegram_bot.domain.agents import AgentRecord
from codex_telegram_bot.domain.runs import RunRecord
from codex_telegram_bot.domain.sessions import TelegramSessionRecord, TelegramSessionMessageRecord
from codex_telegram_bot.events.event_bus import EventBus, RunEvent
from codex_telegram_bot.execution.local_shell import LocalShellRunner
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.observability.structured_log import log_json
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.repo_context import RepositoryContextRetriever
from codex_telegram_bot.services.agent_scheduler import AgentScheduler

logger = logging.getLogger(__name__)
AGENT_ID_RE = re.compile(r"^[a-z0-9_-]{2,40}$")
ALLOWED_POLICY_PROFILES = {"strict", "balanced", "trusted"}


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
    ):
        self._provider = provider
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
        if self._run_store and self._event_bus:
            run_id = self._run_store.create_run(prompt)
            self._run_store.mark_running(run_id)
            self._event_bus.publish(
                run_id=run_id,
                event_type="run.started",
                payload=f"Prompt accepted (agent={agent_id}, policy={policy_profile}, job={correlation_id})",
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

        output = await self._provider.execute(
            prompt,
            correlation_id=run_id or correlation_id,
            policy_profile=policy_profile,
        )

        if self._run_store and self._event_bus and run_id:
            if output.startswith("Error:"):
                self._run_store.mark_failed(run_id, output)
                self._event_bus.publish(run_id=run_id, event_type="run.failed", payload=output)
                log_json(logger, "run.failed", run_id=run_id, agent_id=agent_id, job_id=correlation_id)
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
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", (session_id or "").strip())[:64] or "default"
        root = self._session_workspaces_root / safe
        root.mkdir(parents=True, exist_ok=True)
        return root

    def build_session_prompt(self, session_id: str, user_prompt: str, max_turns: int = 8) -> str:
        context_lines = self.build_retrieval_context(user_prompt=user_prompt, limit=4)
        if not self._run_store:
            if context_lines:
                return "\n".join(context_lines + [user_prompt])
            return user_prompt
        history = self._run_store.list_session_messages(session_id=session_id, limit=max_turns * 2)
        if not history:
            if context_lines:
                return "\n".join(context_lines + [user_prompt])
            return user_prompt
        lines = [
            "Conversation context (most recent first-order preserved):",
        ]
        if context_lines:
            lines.extend(context_lines)
        for msg in history:
            if msg.role not in {"user", "assistant"}:
                continue
            lines.append(f"{msg.role}: {msg.content}")
        lines.append("user: " + user_prompt)
        return "\n".join(lines)

    def build_retrieval_context(self, user_prompt: str, limit: int = 4) -> List[str]:
        if not self._repo_retriever:
            return []
        snippets = self._repo_retriever.retrieve(query=user_prompt, limit=limit)
        if not snippets:
            return []
        lines = ["Relevant repository snippets:"]
        for s in snippets:
            lines.append(f"[{s.path} score={s.score}]")
            lines.append(s.snippet)
        return lines

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
    ) -> str:
        actions, cleaned_prompt, final_prompt = _extract_loop_actions(prompt)
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
            return msg
        if not actions:
            contextual = self.build_session_prompt(session_id=session_id, user_prompt=cleaned_prompt or prompt)
            job_id = await self.queue_prompt(prompt=contextual, agent_id=agent_id)
            await self._notify_progress(progress_callback, {"event": "model.job.queued", "job_id": job_id})
            output = await self.wait_job(job_id)
            await self._notify_progress(progress_callback, {"event": "model.job.finished", "job_id": job_id})
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
        for index, argv in enumerate(actions, start=1):
            action_id = "tool-" + uuid.uuid4().hex[:8]
            command = " ".join(argv)
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
            decision = self._policy_engine.evaluate(argv=argv, policy_profile=policy_profile)
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
        output = await self.wait_job(job_id)
        await self._notify_progress(progress_callback, {"event": "model.job.finished", "job_id": job_id})
        await self._notify_progress(
            progress_callback,
            {"event": "loop.finished", "steps_total": len(actions)},
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
        if provider not in {"codex_cli"}:
            raise ValueError("Unsupported provider.")
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
            return "balanced"
        agent = self._run_store.get_agent(agent_id)
        if not agent or not agent.enabled:
            return "balanced"
        profile = (agent.policy_profile or "").strip().lower()
        if profile not in ALLOWED_POLICY_PROFILES:
            return "balanced"
        return profile

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
        return result, run_id

    def _emit_tool_event(self, run_id: str, event_type: str, payload: str) -> None:
        if not self._event_bus or not run_id:
            return
        self._event_bus.publish(run_id=run_id, event_type=event_type, payload=payload)

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


def _extract_loop_actions(prompt: str) -> tuple[List[List[str]], str, str]:
    actions: List[List[str]] = []
    keep_lines: List[str] = []
    final_prompt = ""
    for raw in (prompt or "").splitlines():
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
                        if kind != "exec":
                            continue
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
                            actions.append(argv)
                fp = str(obj.get("final_prompt") or "").strip()
                if fp:
                    final_prompt = fp
            continue
        if line.startswith("!exec "):
            cmd = line[len("!exec "):].strip()
            if cmd:
                try:
                    argv = shlex.split(cmd)
                except ValueError:
                    argv = []
                if argv:
                    actions.append(argv)
            continue
        keep_lines.append(raw)
    return actions, "\n".join(keep_lines).strip(), final_prompt


def _tool_loop_fingerprint(actions: List[List[str]], cleaned_prompt: str, final_prompt: str) -> str:
    payload = {
        "actions": actions,
        "cleaned_prompt": (cleaned_prompt or "").strip(),
        "final_prompt": (final_prompt or "").strip(),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
