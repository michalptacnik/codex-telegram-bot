from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import re

try:
    import dateparser  # type: ignore[import]
except Exception:  # pragma: no cover - optional dependency fallback
    dateparser = None  # type: ignore[assignment]


_WEEKDAY = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def parse_natural_when(when: str, tz_name: str, now: Optional[datetime] = None) -> Optional[datetime]:
    text = str(when or "").strip()
    if not text:
        return None
    tz = ZoneInfo(tz_name or "Europe/Amsterdam")
    base = (now or datetime.now(tz)).astimezone(tz)
    if dateparser is not None:
        parsed = dateparser.parse(
            text,
            settings={
                "TIMEZONE": tz.key,
                "TO_TIMEZONE": tz.key,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "RELATIVE_BASE": base,
                "PREFER_DATES_FROM": "future",
            },
        )
        if parsed is not None:
            return parsed.astimezone(tz)
    iso_try = _parse_iso(text, tz)
    if iso_try is not None:
        return iso_try
    return _parse_next_weekday_at_time(text, base)


def validate_reasonable_datetime(dt: datetime) -> bool:
    return 2000 <= dt.year <= 2100


def repeat_to_cron(repeat: str, anchor: datetime) -> tuple[bool, str]:
    value = str(repeat or "none").strip().lower()
    if value in {"", "none", "once", "one-shot", "oneshot"}:
        return True, ""
    if value.startswith("cron:"):
        return False, value[len("cron:") :].strip()
    minute = int(anchor.minute)
    hour = int(anchor.hour)
    weekday = int(anchor.weekday())
    if value == "hourly":
        return False, f"{minute} * * * *"
    if value == "daily":
        return False, f"{minute} {hour} * * *"
    if value == "weekly":
        return False, f"{minute} {hour} * * {weekday}"
    raise ValueError("Unsupported repeat value. Use none|hourly|daily|weekly|cron:<expr>.")


def cron_next_run(cron_expr: str, after: datetime) -> datetime:
    fields = str(cron_expr or "").strip().split()
    if len(fields) != 5:
        raise ValueError("cron expression must contain 5 fields")
    m_field, h_field, dom_field, mon_field, dow_field = fields
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(0, 366 * 24 * 60):
        if (
            _cron_match(m_field, cursor.minute, 0, 59)
            and _cron_match(h_field, cursor.hour, 0, 23)
            and _cron_match(dom_field, cursor.day, 1, 31)
            and _cron_match(mon_field, cursor.month, 1, 12)
            and _cron_match(dow_field, cursor.weekday(), 0, 6)
        ):
            return cursor
        cursor += timedelta(minutes=1)
    raise ValueError("Could not compute next cron run within one year")


def summarize_repeat(one_shot: bool, cron_expr: str) -> str:
    if one_shot:
        return "once"
    expr = str(cron_expr or "").strip()
    if not expr:
        return "recurring"
    return f"cron:{expr}"


def _cron_match(field: str, value: int, min_value: int, max_value: int) -> bool:
    token = str(field or "*").strip()
    if token == "*":
        return True
    if token.startswith("*/"):
        try:
            step = int(token[2:])
        except ValueError:
            return False
        return step > 0 and (value - min_value) % step == 0
    if "," in token:
        return any(_cron_match(part, value, min_value, max_value) for part in token.split(","))
    if "-" in token:
        start_s, end_s = token.split("-", 1)
        try:
            start, end = int(start_s), int(end_s)
        except ValueError:
            return False
        return start <= value <= end
    try:
        exact = int(token)
    except ValueError:
        return False
    return min_value <= exact <= max_value and exact == value


def _parse_iso(text: str, tz: ZoneInfo) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _parse_next_weekday_at_time(text: str, now: datetime) -> Optional[datetime]:
    match = re.match(r"(?is)^next\s+([a-z]+)\s+at\s+(\d{1,2}):(\d{2})$", text.strip())
    if not match:
        return None
    day_name = match.group(1).strip().lower()
    weekday = _WEEKDAY.get(day_name)
    if weekday is None:
        return None
    hour = int(match.group(2))
    minute = int(match.group(3))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    days_ahead = (weekday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    target = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target

