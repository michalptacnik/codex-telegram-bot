from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict

from codex_telegram_bot.services.access_control import UnauthorizedAction
from codex_telegram_bot.services.cron_utils import (
    cron_next_run,
    parse_natural_when,
    repeat_to_cron,
    summarize_repeat,
    validate_reasonable_datetime,
)
from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


def _default_tz() -> str:
    raw = (os.environ.get("CRON_DEFAULT_TZ") or "Europe/Amsterdam").strip()
    return raw or "Europe/Amsterdam"


def _max_active_jobs_per_user() -> int:
    raw = (os.environ.get("CRON_MAX_ACTIVE_PER_USER") or "100").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 100


def _is_admin(access_controller: Any, user_id: int, chat_id: int) -> bool:
    if access_controller is None:
        return False
    profile = access_controller.get_profile(user_id, chat_id)
    return "admin" in {str(x).strip().lower() for x in profile.roles}


class ScheduleTaskTool:
    name = "schedule_task"
    description = "Schedule a one-shot reminder or recurring message."

    def __init__(self, run_store: Any = None, access_controller: Any = None) -> None:
        self._store = run_store
        self._access = access_controller

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._store is None:
            return ToolResult(ok=False, output="No session store configured.")
        when_raw = str(request.args.get("when") or "").strip()
        repeat_raw = str(request.args.get("repeat") or "none").strip()
        message = str(request.args.get("message") or "").strip()
        session_id = str(request.args.get("session_id") or context.session_id or "").strip()
        if not session_id:
            return ToolResult(ok=False, output="session_id is required.")
        if not message:
            return ToolResult(ok=False, output="message is required.")
        session = self._store.get_session(session_id)
        if session is None:
            return ToolResult(ok=False, output="Session not found.")
        requester = int(context.user_id or 0)
        request_chat = int(context.chat_id or 0)
        if self._access is not None and requester:
            try:
                self._access.check_action(requester, "send_prompt", request_chat)
            except UnauthorizedAction as exc:
                return ToolResult(ok=False, output=f"Access denied: {exc}")
        admin = _is_admin(self._access, requester, request_chat)
        if requester and not admin and int(session.user_id) != requester:
            return ToolResult(ok=False, output="Access denied: cannot schedule for another user.")
        owner_user_id = str(session.user_id)
        if self._store.count_active_cron_jobs_for_user(owner_user_id) >= _max_active_jobs_per_user():
            return ToolResult(ok=False, output="Active schedule limit reached for this user.")

        tz_name = _default_tz()
        tz = ZoneInfo(tz_name)
        now_local = datetime.now(tz)
        one_shot = True
        cron_expr = ""
        anchor = parse_natural_when(when_raw, tz_name, now=now_local) if when_raw else None
        if anchor is not None and not validate_reasonable_datetime(anchor):
            return ToolResult(ok=False, output="Parsed date out of allowed range (2000-2100).")
        try:
            one_shot, cron_expr = repeat_to_cron(repeat_raw, anchor or now_local)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))

        if one_shot:
            if anchor is None:
                return ToolResult(ok=False, output="when is required for one-shot reminders.")
            next_run_local = anchor
        else:
            next_run_local = cron_next_run(cron_expr, anchor or now_local)

        next_run_utc = next_run_local.astimezone(timezone.utc).isoformat()
        payload = {"message": message}
        job_id = self._store.create_cron_job(
            owner_user_id=owner_user_id,
            session_id=session_id,
            one_shot=one_shot,
            cron_expr=cron_expr,
            next_run=next_run_utc,
            tz=tz_name,
            payload=payload,
        )
        repeat_summary = summarize_repeat(one_shot=one_shot, cron_expr=cron_expr)
        return ToolResult(
            ok=True,
            output=f"Scheduled job {job_id} next_run={next_run_utc} repeat={repeat_summary}",
        )


class ListSchedulesTool:
    name = "list_schedules"
    description = "List active schedules for this user/session."

    def __init__(self, run_store: Any = None, access_controller: Any = None) -> None:
        self._store = run_store
        self._access = access_controller

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._store is None:
            return ToolResult(ok=False, output="No session store configured.")
        session_id = str(request.args.get("session_id") or "").strip()
        requester = int(context.user_id or 0)
        request_chat = int(context.chat_id or 0)
        admin = _is_admin(self._access, requester, request_chat)
        owner = "" if admin else str(requester or "")
        jobs = self._store.list_cron_jobs(session_id=session_id, owner_user_id=owner, include_non_active=False, limit=200)
        if not jobs:
            return ToolResult(ok=True, output="No active schedules.")
        lines = []
        for job in jobs:
            payload = json.loads(job.get("payload_json") or "{}")
            msg = str(payload.get("message") or "").strip().replace("\n", " ")
            lines.append(
                f"- id={job['id']} next_run={job['next_run']} "
                f"repeat={summarize_repeat(bool(job.get('one_shot')), str(job.get('cron_expr') or ''))} "
                f"message={msg[:80]}"
            )
        return ToolResult(ok=True, output="\n".join(lines))


class CancelScheduleTool:
    name = "cancel_schedule"
    description = "Cancel an active schedule."

    def __init__(self, run_store: Any = None, access_controller: Any = None) -> None:
        self._store = run_store
        self._access = access_controller

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._store is None:
            return ToolResult(ok=False, output="No session store configured.")
        job_id = str(request.args.get("id") or "").strip()
        if not job_id:
            return ToolResult(ok=False, output="id is required.")
        job = self._store.get_cron_job(job_id)
        if not job:
            return ToolResult(ok=False, output="Schedule not found.")
        requester = int(context.user_id or 0)
        request_chat = int(context.chat_id or 0)
        admin = _is_admin(self._access, requester, request_chat)
        if requester and not admin and str(job.get("owner_user_id") or "") != str(requester):
            return ToolResult(ok=False, output="Access denied: cannot cancel another user's schedule.")
        cancelled = self._store.cancel_cron_job(job_id)
        if not cancelled:
            return ToolResult(ok=False, output="Schedule already canceled or inactive.")
        return ToolResult(ok=True, output=f"Canceled schedule {job_id}.")

