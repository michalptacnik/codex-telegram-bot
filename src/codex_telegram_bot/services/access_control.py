"""Role/scope-based access control and per-user spend ceilings (Parity Epic 9).

Provides:
- ``AccessController``: checks action permissions by role, enforces spend
  ceilings per user/day, and scans text for common secret patterns.
- ``UserProfile``: configurable per-user role set and spend limit.
- ``SpendLimitExceeded`` / ``UnauthorizedAction``: raised on policy violations.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Role constants
# ---------------------------------------------------------------------------

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_VIEWER = "viewer"

_ROLE_RANK: Dict[str, int] = {ROLE_VIEWER: 0, ROLE_USER: 1, ROLE_ADMIN: 2}

# Actions → minimum role required
_ACTION_ROLES: Dict[str, str] = {
    "approve_tool": ROLE_USER,
    "deny_tool": ROLE_USER,
    "reset_session": ROLE_USER,
    "branch_session": ROLE_USER,
    "send_prompt": ROLE_USER,
    "interrupt_run": ROLE_USER,
    "continue_run": ROLE_USER,
    "view_status": ROLE_VIEWER,
    "view_help": ROLE_VIEWER,
    "switch_provider": ROLE_ADMIN,
    "manage_agents": ROLE_ADMIN,
    "view_logs": ROLE_ADMIN,
    "prune_sessions": ROLE_ADMIN,
}

# ---------------------------------------------------------------------------
# Secret patterns for scanning
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: Dict[str, str] = {
    "aws_access_key": r"AKIA[0-9A-Z]{16}",
    "github_token": r"gh[ps]_[A-Za-z0-9]{36}",
    "stripe_key": r"sk_(?:live|test)_[A-Za-z0-9]{24,}",
    "generic_api_key": r"(?i)(?:api[_-]?key|apikey)\s*[:=]\s*['\"]?([A-Za-z0-9\-_]{20,})",
    "bearer_token": r"(?i)bearer\s+[A-Za-z0-9\-_\.]{20,}",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UnauthorizedAction(Exception):
    """Raised when a user attempts an action above their role level."""


class SpendLimitExceeded(Exception):
    """Raised when a spend event would push a user over their daily ceiling."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class UserProfile:
    user_id: int
    chat_id: int
    roles: List[str] = field(default_factory=lambda: [ROLE_USER])
    spend_limit_usd: float = 10.0
    spend_window_sec: int = 86_400  # 24 hours

    def highest_role(self) -> str:
        rank = max((_ROLE_RANK.get(r, 0) for r in self.roles), default=0)
        for role, r in _ROLE_RANK.items():
            if r == rank:
                return role
        return ROLE_VIEWER


@dataclass
class SpendRecord:
    user_id: int
    amount_usd: float
    at: datetime


# ---------------------------------------------------------------------------
# AccessController
# ---------------------------------------------------------------------------


class AccessController:
    """Thread-safe access control and spend-ceiling enforcement."""

    def __init__(self) -> None:
        self._profiles: Dict[int, UserProfile] = {}
        self._spend: List[SpendRecord] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def set_profile(self, profile: UserProfile) -> None:
        with self._lock:
            self._profiles[profile.user_id] = profile

    def get_profile(self, user_id: int, chat_id: int = 0) -> UserProfile:
        with self._lock:
            return self._profiles.get(user_id) or UserProfile(
                user_id=user_id, chat_id=chat_id
            )

    # ------------------------------------------------------------------
    # Action authorization
    # ------------------------------------------------------------------

    def check_action(self, user_id: int, action: str, chat_id: int = 0) -> bool:
        """Return True if user is authorized; raise UnauthorizedAction if not."""
        profile = self.get_profile(user_id, chat_id)
        required_role = _ACTION_ROLES.get(action, ROLE_USER)
        required_rank = _ROLE_RANK.get(required_role, 1)
        user_rank = max((_ROLE_RANK.get(r, 0) for r in profile.roles), default=0)
        if user_rank < required_rank:
            raise UnauthorizedAction(
                f"User {user_id} (role={profile.highest_role()!r}) "
                f"is not authorized to perform {action!r} "
                f"(requires {required_role!r})"
            )
        return True

    def is_allowed(self, user_id: int, action: str, chat_id: int = 0) -> bool:
        """Return True/False without raising."""
        try:
            return self.check_action(user_id, action, chat_id)
        except UnauthorizedAction:
            return False

    # ------------------------------------------------------------------
    # Spend tracking
    # ------------------------------------------------------------------

    def record_spend(
        self, user_id: int, amount_usd: float, chat_id: int = 0
    ) -> None:
        """Record a spend event. Raises SpendLimitExceeded if ceiling reached."""
        profile = self.get_profile(user_id, chat_id)
        with self._lock:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(seconds=profile.spend_window_sec)
            window_total = sum(
                r.amount_usd
                for r in self._spend
                if r.user_id == user_id and r.at >= cutoff
            )
            if window_total + amount_usd > profile.spend_limit_usd:
                raise SpendLimitExceeded(
                    f"User {user_id} would exceed spend ceiling "
                    f"${profile.spend_limit_usd:.2f}/day "
                    f"(current=${window_total:.4f}, requested=${amount_usd:.4f})"
                )
            self._spend.append(
                SpendRecord(user_id=user_id, amount_usd=amount_usd, at=now)
            )
            # Trim memory: keep only records within the widest window (1 day)
            if len(self._spend) > 10_000:
                max_cutoff = now - timedelta(seconds=86_400)
                self._spend = [r for r in self._spend if r.at >= max_cutoff]

    def get_spend(self, user_id: int, window_sec: int = 86_400) -> float:
        """Return total spend for a user within the given window."""
        with self._lock:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_sec)
            return sum(
                r.amount_usd
                for r in self._spend
                if r.user_id == user_id and r.at >= cutoff
            )

    # ------------------------------------------------------------------
    # Secret scanning
    # ------------------------------------------------------------------

    def scan_for_secrets(self, text: str) -> List[str]:
        """Return list of secret pattern names found in ``text``."""
        found: List[str] = []
        for name, pattern in _SECRET_PATTERNS.items():
            if re.search(pattern, text or ""):
                found.append(name)
        return found
