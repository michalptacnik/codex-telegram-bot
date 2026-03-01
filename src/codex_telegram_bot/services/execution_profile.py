from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

PROFILE_SAFE = "safe"
PROFILE_POWER_USER = "power_user"
PROFILE_UNSAFE = "unsafe"
VALID_EXECUTION_PROFILES = {PROFILE_SAFE, PROFILE_POWER_USER, PROFILE_UNSAFE}

UNSAFE_UNLOCK_PHRASE = "I UNDERSTAND THIS CAN EXECUTE ARBITRARY CODE ON MY MACHINE"
UNSAFE_UNLOCK_COUNTDOWN_SEC = 30
UNSAFE_AUTO_EXPIRE_HOURS = 24


@dataclass(frozen=True)
class ExecutionProfileState:
    profile: str
    unsafe_enabled_at: str = ""
    unsafe_expires_at: str = ""
    enabled_by_user_id: str = ""
    unlock_code_hash: str = ""
    unlock_started_at: str = ""
    last_changed_at: str = ""

    @property
    def unsafe_active(self) -> bool:
        return self.profile == PROFILE_UNSAFE and bool(self.unsafe_expires_at)

    def seconds_until_unsafe_expiry(self) -> int:
        if not self.unsafe_expires_at:
            return 0
        expires = _parse_iso(self.unsafe_expires_at)
        if expires is None:
            return 0
        delta = int((expires - _utc_now_dt()).total_seconds())
        return max(0, delta)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "unsafe_enabled_at": self.unsafe_enabled_at,
            "unsafe_expires_at": self.unsafe_expires_at,
            "enabled_by_user_id": self.enabled_by_user_id,
            "unlock_started_at": self.unlock_started_at,
            "last_changed_at": self.last_changed_at,
            "unsafe_active": self.unsafe_active,
            "seconds_until_unsafe_expiry": self.seconds_until_unsafe_expiry(),
        }


class ExecutionProfileManager:
    """Global execution profile state with deliberate UNSAFE unlock flow."""

    def __init__(
        self,
        *,
        store: Any = None,
        default_profile: str = PROFILE_SAFE,
    ) -> None:
        normalized = _normalize_profile(default_profile)
        if normalized == PROFILE_UNSAFE:
            normalized = PROFILE_POWER_USER
        self._default_profile = normalized
        self._store = store
        self._memory_state = ExecutionProfileState(profile=self._default_profile)
        self._memory_audit: list[dict] = []
        self._ensure_state_row()

    def get_state(self) -> ExecutionProfileState:
        state = self._load_state()
        if state.profile == PROFILE_UNSAFE:
            expires = _parse_iso(state.unsafe_expires_at)
            if expires is None or expires <= _utc_now_dt():
                previous = state
                state = self._save_state(
                    profile=PROFILE_POWER_USER,
                    enabled_by_user_id=state.enabled_by_user_id,
                    reason="unsafe_auto_expired",
                    origin="system",
                    from_profile=previous.profile,
                    unsafe_enabled_at="",
                    unsafe_expires_at="",
                    unlock_code_hash="",
                    unlock_started_at="",
                )
        return state

    def set_profile(self, *, profile: str, user_id: str, origin: str, reason: str = "manual") -> ExecutionProfileState:
        normalized = _normalize_profile(profile)
        if normalized == PROFILE_UNSAFE:
            raise ValueError("UNSAFE profile requires unlock flow.")
        current = self.get_state()
        return self._save_state(
            profile=normalized,
            enabled_by_user_id=str(user_id or ""),
            reason=reason,
            origin=origin,
            from_profile=current.profile,
            unsafe_enabled_at="",
            unsafe_expires_at="",
            unlock_code_hash="",
            unlock_started_at="",
        )

    def start_unsafe_unlock(self, *, user_id: str, origin: str) -> Dict[str, Any]:
        current = self.get_state()
        now = _utc_now_dt()
        code = f"{secrets.randbelow(900000) + 100000}"
        code_hash = _hash_code(code)
        ready_at = (now + timedelta(seconds=UNSAFE_UNLOCK_COUNTDOWN_SEC)).isoformat()
        state = self._save_state(
            profile=current.profile,
            enabled_by_user_id=current.enabled_by_user_id or str(user_id or ""),
            reason="unsafe_unlock_started",
            origin=origin,
            from_profile=current.profile,
            unsafe_enabled_at=current.unsafe_enabled_at,
            unsafe_expires_at=current.unsafe_expires_at,
            unlock_code_hash=code_hash,
            unlock_started_at=now.isoformat(),
        )
        return {
            "code": code,
            "ready_at": ready_at,
            "countdown_sec": UNSAFE_UNLOCK_COUNTDOWN_SEC,
            "profile": state.profile,
        }

    def confirm_unsafe_unlock(
        self,
        *,
        user_id: str,
        origin: str,
        code: str,
        phrase: str,
    ) -> ExecutionProfileState:
        if (phrase or "").strip() != UNSAFE_UNLOCK_PHRASE:
            raise ValueError("Unlock phrase mismatch.")
        current = self.get_state()
        if not current.unlock_code_hash or not current.unlock_started_at:
            raise ValueError("UNSAFE unlock has not been initiated.")
        if _hash_code(str(code or "").strip()) != current.unlock_code_hash:
            raise ValueError("Invalid unlock code.")
        started = _parse_iso(current.unlock_started_at)
        if started is None:
            raise ValueError("Invalid unlock timestamp.")
        ready_at = started + timedelta(seconds=UNSAFE_UNLOCK_COUNTDOWN_SEC)
        if _utc_now_dt() < ready_at:
            remaining = int((ready_at - _utc_now_dt()).total_seconds())
            raise ValueError(f"Unlock countdown active. Wait {max(1, remaining)} more seconds.")
        now = _utc_now_dt()
        expires = now + timedelta(hours=UNSAFE_AUTO_EXPIRE_HOURS)
        return self._save_state(
            profile=PROFILE_UNSAFE,
            enabled_by_user_id=str(user_id or ""),
            reason="unsafe_unlock_confirmed",
            origin=origin,
            from_profile=current.profile,
            unsafe_enabled_at=now.isoformat(),
            unsafe_expires_at=expires.isoformat(),
            unlock_code_hash="",
            unlock_started_at="",
        )

    def list_audit(self, limit: int = 100) -> list[dict]:
        if self._store is not None and hasattr(self._store, "list_execution_profile_audit"):
            return list(self._store.list_execution_profile_audit(limit=max(1, int(limit))))
        return list(self._memory_audit[-max(1, int(limit)) :])

    def unsafe_warning_text(self) -> str:
        state = self.get_state()
        if state.profile != PROFILE_UNSAFE:
            return ""
        seconds = state.seconds_until_unsafe_expiry()
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return (
            "UNSAFE MODE ENABLED — arbitrary code execution allowed "
            f"(expires in {hours}h {minutes}m)."
        )

    def _ensure_state_row(self) -> None:
        state = self._load_state()
        if state.profile not in VALID_EXECUTION_PROFILES:
            self._save_state(
                profile=self._default_profile,
                enabled_by_user_id="",
                reason="init_invalid_profile",
                origin="system",
                from_profile=state.profile,
                unsafe_enabled_at="",
                unsafe_expires_at="",
                unlock_code_hash="",
                unlock_started_at="",
            )

    def _load_state(self) -> ExecutionProfileState:
        if self._store is not None and hasattr(self._store, "get_execution_profile_state"):
            raw = self._store.get_execution_profile_state()
            return ExecutionProfileState(
                profile=_normalize_profile(str(raw.get("profile") or self._default_profile)),
                unsafe_enabled_at=str(raw.get("unsafe_enabled_at") or ""),
                unsafe_expires_at=str(raw.get("unsafe_expires_at") or ""),
                enabled_by_user_id=str(raw.get("enabled_by_user_id") or ""),
                unlock_code_hash=str(raw.get("unlock_code_hash") or ""),
                unlock_started_at=str(raw.get("unlock_started_at") or ""),
                last_changed_at=str(raw.get("last_changed_at") or ""),
            )
        return self._memory_state

    def _save_state(
        self,
        *,
        profile: str,
        enabled_by_user_id: str,
        reason: str,
        origin: str,
        from_profile: str,
        unsafe_enabled_at: str,
        unsafe_expires_at: str,
        unlock_code_hash: str,
        unlock_started_at: str,
    ) -> ExecutionProfileState:
        normalized = _normalize_profile(profile)
        if self._store is not None and hasattr(self._store, "set_execution_profile_state"):
            self._store.set_execution_profile_state(
                profile=normalized,
                unsafe_enabled_at=unsafe_enabled_at,
                unsafe_expires_at=unsafe_expires_at,
                enabled_by_user_id=str(enabled_by_user_id or ""),
                unlock_code_hash=str(unlock_code_hash or ""),
                unlock_started_at=unlock_started_at,
            )
            if hasattr(self._store, "record_execution_profile_audit"):
                self._store.record_execution_profile_audit(
                    origin=str(origin or "unknown"),
                    changed_by_user_id=str(enabled_by_user_id or ""),
                    from_profile=str(from_profile or ""),
                    to_profile=normalized,
                    reason=str(reason or ""),
                    details={
                        "unsafe_enabled_at": unsafe_enabled_at,
                        "unsafe_expires_at": unsafe_expires_at,
                        "unlock_started_at": unlock_started_at,
                    },
                )
            return self._load_state()

        now = _utc_now_dt().isoformat()
        self._memory_state = ExecutionProfileState(
            profile=normalized,
            unsafe_enabled_at=unsafe_enabled_at,
            unsafe_expires_at=unsafe_expires_at,
            enabled_by_user_id=str(enabled_by_user_id or ""),
            unlock_code_hash=str(unlock_code_hash or ""),
            unlock_started_at=unlock_started_at,
            last_changed_at=now,
        )
        self._memory_audit.append(
            {
                "origin": str(origin or "unknown"),
                "changed_by_user_id": str(enabled_by_user_id or ""),
                "from_profile": str(from_profile or ""),
                "to_profile": normalized,
                "reason": str(reason or ""),
                "created_at": now,
            }
        )
        return self._memory_state


def _normalize_profile(value: str) -> str:
    raw = (value or "").strip().lower()
    aliases = {
        "power": PROFILE_POWER_USER,
        "poweruser": PROFILE_POWER_USER,
        "power-user": PROFILE_POWER_USER,
    }
    normalized = aliases.get(raw, raw)
    if normalized not in VALID_EXECUTION_PROFILES:
        return PROFILE_SAFE
    return normalized


def _parse_iso(raw: str) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _hash_code(code: str) -> str:
    return hashlib.sha256(str(code or "").encode("utf-8")).hexdigest()
