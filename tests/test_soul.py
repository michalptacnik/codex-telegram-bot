import json
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.soul import (
    SOUL_MAX_CHARS,
    SoulStore,
    apply_patch,
    default_soul_profile,
    parse_soul,
    render_soul,
)
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.soul import SoulApplyPatchTool, SoulGetTool, SoulProposePatchTool


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


class TestSoulTools(unittest.TestCase):
    def test_soul_get_and_patch_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SqliteRunStore(db_path=root / "state.db")
            get_tool = SoulGetTool()
            propose_tool = SoulProposePatchTool()
            apply_tool = SoulApplyPatchTool(run_store=store)
            ctx = ToolContext(workspace_root=root, user_id=7, session_id="sess-1")

            get_res = get_tool.run(ToolRequest(name="soul_get", args={}), ctx)
            self.assertTrue(get_res.ok)
            self.assertIn("\"text\":", get_res.output)

            patch = {"style": {"emoji": "off"}}
            preview = propose_tool.run(ToolRequest(name="soul_propose_patch", args={"patch": patch}), ctx)
            self.assertTrue(preview.ok)
            self.assertIn("\"changed\": true", preview.output)

            applied = apply_tool.run(
                ToolRequest(name="soul_apply_patch", args={"patch": patch, "reason": "test"}),
                ctx,
            )
            self.assertTrue(applied.ok)
            payload = json.loads(applied.output)
            self.assertTrue(payload.get("changed"))
            self.assertTrue(str(payload.get("history_path") or "").endswith(".md"))
            self.assertTrue(str(payload.get("version_id") or ""))
            status = SoulStore(root).load_profile()
            self.assertEqual(status.style.emoji, "off")
            versions = store.list_soul_versions(session_id="sess-1", limit=10)
            self.assertEqual(len(versions), 1)
            self.assertEqual(str(versions[0].get("changed_by") or ""), "7")


if __name__ == "__main__":
    unittest.main()
