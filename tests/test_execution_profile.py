import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.execution_profile import (
    ExecutionProfileManager,
    PROFILE_POWER_USER,
    PROFILE_SAFE,
    PROFILE_UNSAFE,
    UNSAFE_UNLOCK_PHRASE,
)


class TestExecutionProfileManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteRunStore(db_path=Path(self.tmp.name) / "state.db")
        self.manager = ExecutionProfileManager(store=self.store, default_profile=PROFILE_SAFE)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_state_is_safe(self):
        state = self.manager.get_state()
        self.assertEqual(state.profile, PROFILE_SAFE)
        self.assertFalse(state.unsafe_active)

    def test_set_power_user_profile(self):
        state = self.manager.set_profile(profile=PROFILE_POWER_USER, user_id="42", origin="test")
        self.assertEqual(state.profile, PROFILE_POWER_USER)
        self.assertGreaterEqual(len(self.manager.list_audit(limit=10)), 1)

    def test_set_unsafe_directly_is_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.set_profile(profile=PROFILE_UNSAFE, user_id="42", origin="test")

    def test_unlock_flow_requires_countdown(self):
        unlock = self.manager.start_unsafe_unlock(user_id="7", origin="test")
        with self.assertRaises(ValueError):
            self.manager.confirm_unsafe_unlock(
                user_id="7",
                origin="test",
                code=str(unlock.get("code") or ""),
                phrase=UNSAFE_UNLOCK_PHRASE,
            )

    def test_unlock_flow_allows_after_countdown(self):
        unlock = self.manager.start_unsafe_unlock(user_id="7", origin="test")
        raw = self.store.get_execution_profile_state()
        past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        self.store.set_execution_profile_state(
            profile=str(raw.get("profile") or PROFILE_SAFE),
            unsafe_enabled_at=str(raw.get("unsafe_enabled_at") or ""),
            unsafe_expires_at=str(raw.get("unsafe_expires_at") or ""),
            enabled_by_user_id=str(raw.get("enabled_by_user_id") or ""),
            unlock_code_hash=str(raw.get("unlock_code_hash") or ""),
            unlock_started_at=past,
        )
        state = self.manager.confirm_unsafe_unlock(
            user_id="7",
            origin="test",
            code=str(unlock.get("code") or ""),
            phrase=UNSAFE_UNLOCK_PHRASE,
        )
        self.assertEqual(state.profile, PROFILE_UNSAFE)
        self.assertTrue(state.unsafe_active)

    def test_unsafe_auto_reverts_to_power_user(self):
        self.store.set_execution_profile_state(
            profile=PROFILE_UNSAFE,
            unsafe_enabled_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            unsafe_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            enabled_by_user_id="9",
            unlock_code_hash="",
            unlock_started_at="",
        )
        state = self.manager.get_state()
        self.assertEqual(state.profile, PROFILE_POWER_USER)
        self.assertFalse(state.unsafe_active)


if __name__ == "__main__":
    unittest.main()
