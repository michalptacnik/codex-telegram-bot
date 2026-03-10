"""CodexCLIBackend – wraps the existing Codex CLI provider behind the Backend protocol.

Preserves the existing approvals UX (Accept once / Accept similar / Reject)
and sandbox-vs-approval mode separation from ``codex_telegram_bot.providers.codex_cli``
and ``codex_telegram_bot.execution.policy``.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, List, Optional

from codex_telegram_bot.core.backend import (
    ApprovalAction,
    ApprovalRequested,
    Backend,
    BackendEvent,
    Error,
    RunFinished,
    RunStarted,
    TextDelta,
)
from codex_telegram_bot.domain.contracts import CommandResult, ExecutionRunner
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.observability.structured_log import log_json
from codex_telegram_bot.util import redact

logger = logging.getLogger(__name__)

_SANDBOX_MAP: Dict[str, str] = {
    "strict": "",
    "balanced": "--sandbox=workspace-write",
    "trusted": "--sandbox=danger-full-access",
}


def _build_exec_argv(policy_profile: str) -> list[str]:
    profile = _normalize_policy(policy_profile)
    argv = ["codex", "exec", "-", "--color", "never", "--skip-git-repo-check"]
    sandbox_flag = _SANDBOX_MAP.get(profile)
    if sandbox_flag:
        argv.append(sandbox_flag)
    return argv


def _normalize_policy(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in {"strict", "balanced", "trusted"} else "balanced"


class _RunState:
    """Internal bookkeeping for a single run."""

    def __init__(self, run_id: str, policy_profile: str) -> None:
        self.run_id = run_id
        self.policy_profile = policy_profile
        self.events: asyncio.Queue[BackendEvent | None] = asyncio.Queue()
        self.cancelled = False
        self.finished = False
        self.pending_approvals: Dict[str, ApprovalRequested] = {}
        self.approval_responses: Dict[str, ApprovalAction] = {}


class CodexCLIBackend:
    """Backend implementation that delegates to the Codex CLI."""

    def __init__(
        self,
        runner: ExecutionRunner,
        exec_timeout_sec: int = 900,
        policy_engine: Optional[ExecutionPolicyEngine] = None,
    ) -> None:
        self._runner = runner
        self._exec_timeout_sec = exec_timeout_sec
        self._policy_engine = policy_engine or ExecutionPolicyEngine()
        self._runs: Dict[str, _RunState] = {}

    # -- Backend protocol --------------------------------------------------

    @property
    def name(self) -> str:
        return "codex"

    async def start_run(
        self,
        prompt: str,
        *,
        correlation_id: str = "",
        policy_profile: str = "balanced",
        workspace_root: str = "",
        instruction_paths: Optional[List[str]] = None,
    ) -> str:
        run_id = correlation_id or str(uuid.uuid4())
        state = _RunState(run_id=run_id, policy_profile=policy_profile)
        self._runs[run_id] = state

        log_json(
            logger,
            "codex_backend.start_run",
            run_id=run_id,
            policy_profile=policy_profile,
        )

        # Launch execution in background so stream_events can yield immediately.
        asyncio.create_task(self._execute(state, prompt, workspace_root))
        return run_id

    async def stream_events(self, run_id: str) -> AsyncIterator[BackendEvent]:
        state = self._runs.get(run_id)
        if state is None:
            yield Error(run_id=run_id, message="Unknown run_id", recoverable=False)
            return

        while True:
            event = await state.events.get()
            if event is None:
                break
            yield event

    async def send_approval(
        self,
        run_id: str,
        approval_id: str,
        action: ApprovalAction,
    ) -> None:
        state = self._runs.get(run_id)
        if state is None:
            return
        state.approval_responses[approval_id] = action
        log_json(
            logger,
            "codex_backend.approval",
            run_id=run_id,
            approval_id=approval_id,
            action=action.value,
        )

    async def cancel_run(self, run_id: str) -> None:
        state = self._runs.get(run_id)
        if state is None:
            return
        state.cancelled = True
        if not state.finished:
            await state.events.put(
                RunFinished(
                    run_id=run_id,
                    output="",
                    exit_code=-1,
                    finished_at=datetime.now(timezone.utc),
                )
            )
            await state.events.put(None)
            state.finished = True

    async def close(self) -> None:
        for run_id in list(self._runs):
            await self.cancel_run(run_id)
        self._runs.clear()

    # -- Internal ----------------------------------------------------------

    async def _execute(self, state: _RunState, prompt: str, workspace_root: str) -> None:
        run_id = state.run_id
        try:
            await state.events.put(
                RunStarted(
                    run_id=run_id,
                    backend_name=self.name,
                    started_at=datetime.now(timezone.utc),
                )
            )

            if state.cancelled:
                return

            safe_prompt = redact(prompt)
            argv = _build_exec_argv(state.policy_profile)

            # Policy check
            decision = self._policy_engine.evaluate(argv, state.policy_profile)
            if not decision.allowed:
                approval_id = str(uuid.uuid4())
                approval = ApprovalRequested(
                    run_id=run_id,
                    approval_id=approval_id,
                    command=" ".join(argv),
                    risk_tier=decision.risk_tier,
                    explanation=decision.reason,
                )
                state.pending_approvals[approval_id] = approval
                await state.events.put(approval)

                # Wait for approval response (poll with timeout).
                approved = await self._wait_for_approval(state, approval_id)
                if not approved:
                    await state.events.put(
                        RunFinished(
                            run_id=run_id,
                            output="Run rejected by user.",
                            exit_code=1,
                            finished_at=datetime.now(timezone.utc),
                        )
                    )
                    await state.events.put(None)
                    state.finished = True
                    return

            result: CommandResult = await self._runner.run(
                argv,
                stdin_text=safe_prompt,
                timeout_sec=self._exec_timeout_sec,
                policy_profile=state.policy_profile,
                workspace_root=workspace_root,
            )

            if state.cancelled:
                return

            # Emit output as a single text delta (Codex CLI is non-streaming).
            output_text = redact(result.stdout) if result.stdout.strip() else "(no output)"
            if result.returncode != 0:
                tail = (result.stderr.strip() or result.stdout.strip())[:300]
                output_text = f"Error: codex exited with code {result.returncode}."
                if tail:
                    output_text += f" {redact(tail)}"

            await state.events.put(TextDelta(run_id=run_id, delta=output_text))
            await state.events.put(
                RunFinished(
                    run_id=run_id,
                    output=output_text,
                    exit_code=result.returncode,
                    finished_at=datetime.now(timezone.utc),
                )
            )
        except FileNotFoundError:
            await state.events.put(
                Error(run_id=run_id, message="codex CLI not found", recoverable=False)
            )
        except Exception as exc:
            logger.exception("codex_backend execution error: %s", exc)
            await state.events.put(
                Error(run_id=run_id, message=str(exc), recoverable=False)
            )
        finally:
            if not state.finished:
                await state.events.put(None)
                state.finished = True

    async def _wait_for_approval(
        self,
        state: _RunState,
        approval_id: str,
        timeout_sec: int = 300,
    ) -> bool:
        """Poll for an approval response, returning True if accepted."""
        elapsed = 0
        poll_interval = 0.5
        while elapsed < timeout_sec:
            if state.cancelled:
                return False
            action = state.approval_responses.get(approval_id)
            if action is not None:
                return action in (ApprovalAction.ACCEPT_ONCE, ApprovalAction.ACCEPT_SIMILAR)
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        return False
