"""Escalation and notification service (EPIC 9, issue #92).

Routes mission events (blocked, high-risk, failure, budget breach) to
configured notification channels (Telegram and webhook).  Supports
configurable thresholds so minor events don't generate noise.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event severity
# ---------------------------------------------------------------------------

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

# Which event types map to which severity
_EVENT_SEVERITY: Dict[str, str] = {
    "mission.started": SEVERITY_INFO,
    "mission.completed": SEVERITY_INFO,
    "mission.paused": SEVERITY_INFO,
    "mission.resumed": SEVERITY_INFO,
    "step.completed": SEVERITY_INFO,
    "step.skipped": SEVERITY_INFO,
    "mission.stopped": SEVERITY_WARN,
    "step.failed": SEVERITY_WARN,
    "watchdog.stalled": SEVERITY_WARN,
    "watchdog.recovered": SEVERITY_WARN,
    "mission.failed": SEVERITY_CRITICAL,
    "budget.breach": SEVERITY_CRITICAL,
    "kill_switch.triggered": SEVERITY_CRITICAL,
    "mission.planned": SEVERITY_INFO,
}


def event_severity(event_type: str) -> str:
    return _EVENT_SEVERITY.get(event_type, SEVERITY_INFO)


# ---------------------------------------------------------------------------
# Escalation channel protocols
# ---------------------------------------------------------------------------


NotifyFn = Callable[[str, str, str], Awaitable[None]]
# (channel_id, subject, body) → None


@dataclass
class EscalationRule:
    """Route events matching ``min_severity`` to ``channel_ids``."""
    min_severity: str                       # "info", "warn", or "critical"
    channel_ids: List[str]
    event_filter: Optional[Set[str]] = None  # None = all events; set = only these
    cooldown_sec: float = 60.0               # suppress duplicate alerts within window

    def matches(self, event_type: str, severity: str) -> bool:
        sev_rank = {SEVERITY_INFO: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}
        if sev_rank.get(severity, 0) < sev_rank.get(self.min_severity, 0):
            return False
        if self.event_filter is not None and event_type not in self.event_filter:
            return False
        return True


# ---------------------------------------------------------------------------
# Escalation dispatcher
# ---------------------------------------------------------------------------


class EscalationDispatcher:
    """Dispatch mission events to notification channels based on rules.

    Usage::

        async def telegram_notify(channel_id, subject, body):
            await bot.send_message(chat_id=int(channel_id), text=f"{subject}\\n{body}")

        dispatcher = EscalationDispatcher()
        dispatcher.add_channel("tg:12345", telegram_notify)
        dispatcher.add_rule(EscalationRule(
            min_severity=SEVERITY_CRITICAL,
            channel_ids=["tg:12345"],
        ))

        # Then use as the progress_callback in AutonomousMissionRunner:
        runner = AutonomousMissionRunner(
            ...,
            progress_callback=dispatcher.dispatch,
        )
    """

    def __init__(self) -> None:
        self._channels: Dict[str, NotifyFn] = {}
        self._rules: List[EscalationRule] = []
        # cooldown tracking: (channel_id, event_type, mission_id) → last_sent
        self._last_sent: Dict[tuple, datetime] = {}

    def add_channel(self, channel_id: str, notify_fn: NotifyFn) -> None:
        self._channels[channel_id] = notify_fn

    def remove_channel(self, channel_id: str) -> None:
        self._channels.pop(channel_id, None)

    def add_rule(self, rule: EscalationRule) -> None:
        self._rules.append(rule)

    def clear_rules(self) -> None:
        self._rules.clear()

    async def dispatch(
        self, mission_id: str, event_type: str, detail: str
    ) -> None:
        """Primary callback — wire this to AutonomousMissionRunner.progress_callback."""
        severity = event_severity(event_type)
        now = datetime.now(timezone.utc)

        for rule in self._rules:
            if not rule.matches(event_type, severity):
                continue
            for channel_id in rule.channel_ids:
                notify_fn = self._channels.get(channel_id)
                if notify_fn is None:
                    continue
                # Cooldown check
                key = (channel_id, event_type, mission_id)
                last = self._last_sent.get(key)
                if last and (now - last).total_seconds() < rule.cooldown_sec:
                    continue
                self._last_sent[key] = now
                subject = f"[{severity.upper()}] {event_type}"
                body = self._format_body(mission_id, event_type, detail, severity)
                try:
                    await notify_fn(channel_id, subject, body)
                except Exception:
                    logger.exception(
                        "escalation: notify failed channel=%s event=%s mission=%s",
                        channel_id, event_type, mission_id,
                    )

    @staticmethod
    def _format_body(
        mission_id: str, event_type: str, detail: str, severity: str
    ) -> str:
        lines = [
            f"Mission: {mission_id}",
            f"Event:   {event_type}",
        ]
        if detail:
            lines.append(f"Detail:  {detail}")
        lines.append(f"Time:    {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
        return "\n".join(lines)

    def channel_ids(self) -> List[str]:
        return list(self._channels.keys())


# ---------------------------------------------------------------------------
# Webhook channel (simple HTTP POST)
# ---------------------------------------------------------------------------


class WebhookChannel:
    """Sends escalation events to an HTTP endpoint as JSON POST."""

    def __init__(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        http_post_fn: Optional[Callable] = None,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._http_post = http_post_fn

    async def notify(self, channel_id: str, subject: str, body: str) -> None:
        payload = {
            "channel_id": channel_id,
            "subject": subject,
            "body": body,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if self._http_post:
            await self._http_post(self._url, self._headers, payload)
            return
        try:
            import aiohttp  # type: ignore[import]
            async with aiohttp.ClientSession() as session:
                await session.post(
                    self._url,
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=payload,
                )
        except Exception as exc:
            logger.warning("webhook: POST to %s failed: %s", self._url, exc)
