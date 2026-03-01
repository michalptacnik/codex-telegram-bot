import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.soul import (
    SOUL_MAX_CHARS,
    SoulStore,
    apply_patch,
    default_soul_profile,
    parse_soul,
    render_soul,
)


class TestSoulFormat(unittest.TestCase):
    def test_default_render_is_small_and_deterministic(self):
        profile = default_soul_profile()
        text = render_soul(profile)
        self.assertIn("# SOUL v1", text)
        self.assertLessEqual(len(text), SOUL_MAX_CHARS)
        reparsed = parse_soul(text)
        self.assertEqual(text, render_soul(reparsed))

    def test_invalid_soul_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            path = ws / "memory" / "SOUL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# bad\nname: x\n", encoding="utf-8")
            store = SoulStore(ws)
            profile, report = store.load_profile_with_report()
            self.assertFalse(report.ok)
            self.assertTrue(report.warnings)
            self.assertEqual(profile.name, default_soul_profile().name)

    def test_patch_limits_enforced(self):
        profile = default_soul_profile()
        with self.assertRaises(ValueError):
            apply_patch(
                profile,
                {
                    "principles": {
                        "set_all": ["a"] * 9,
                    }
                },
            )


class TestSoulStore(unittest.TestCase):
    def test_store_auto_creates_soul_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            store = SoulStore(ws)
            self.assertTrue(store.soul_path.exists())
            text = store.read_text()
            self.assertIn("# SOUL v1", text)

    def test_propose_and_apply_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            store = SoulStore(ws)
            patch = {
                "voice": "calm nerdy concise",
                "style": {"emoji": "off"},
            }
            preview = store.propose_patch(patch)
            self.assertTrue(preview["ok"])
            self.assertIn("memory/SOUL.md", preview["diff"])
            applied = store.apply_patch(
                patch,
                reason="test",
                changed_by="u1",
                session_id="s1",
                run_store=None,
            )
            self.assertTrue(applied["ok"])
            profile = store.load_profile()
            self.assertEqual(profile.style.emoji, "off")


if __name__ == "__main__":
    unittest.main()
