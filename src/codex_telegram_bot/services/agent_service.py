from typing import Any, Dict, List, Optional
import logging
import re
from datetime import datetime, timezone

from codex_telegram_bot.domain.contracts import ProviderAdapter
from codex_telegram_bot.domain.agents import AgentRecord
from codex_telegram_bot.domain.runs import RunRecord
from codex_telegram_bot.events.event_bus import EventBus, RunEvent
from codex_telegram_bot.observability.structured_log import log_json
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
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
    ):
        self._provider = provider
        self._run_store = run_store
        self._event_bus = event_bus

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
        if self._run_store and self._event_bus:
            run_id = self._run_store.create_run(prompt)
            self._run_store.mark_running(run_id)
            self._event_bus.publish(
                run_id=run_id,
                event_type="run.started",
                payload=f"Prompt accepted (agent={agent_id}, job={correlation_id})",
            )
            log_json(logger, "run.started", run_id=run_id, agent_id=agent_id, job_id=correlation_id)

        output = await self._provider.execute(prompt, correlation_id=run_id or correlation_id)

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

    def _emit_handoff_event(self, parent_run_id: str, event_type: str, envelope: Dict[str, Any]) -> None:
        if not self._event_bus or not parent_run_id:
            return
        self._event_bus.publish(
            run_id=parent_run_id,
            event_type=event_type,
            payload=str(envelope),
        )
