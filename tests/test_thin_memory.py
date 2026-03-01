import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.agent_service import AgentService
from codex_telegram_bot.services.thin_memory import (
    MEMORY_INDEX_MAX_CHARS,
    ThinMemoryStore,
    parse_index,
    render_index,
)
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.memory import MemoryIndexGetTool, MemoryPointerOpenTool


class TestThinMemoryLayout(unittest.TestCase):
    def test_thin_memory_layout_bootstraps_on_workspace_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AgentService(
                provider=EchoFallbackProvider(),
                session_workspaces_root=Path(tmp) / "workspaces",
            )
            info = service.initialize_session_workspace(session_id="sess-memory-layout")
            ws = Path(info["workspace_root"])

            self.assertTrue((ws / "memory").is_dir())
            self.assertTrue((ws / "memory" / "MEMORY_INDEX.md").is_file())
            self.assertTrue((ws / "memory" / "daily").is_dir())
            self.assertTrue((ws / "memory" / "pages").is_dir())
            text = (ws / "memory" / "MEMORY_INDEX.md").read_text(encoding="utf-8")
            self.assertIn("# MEMORY_INDEX v1", text)
            self.assertLessEqual(len(text), MEMORY_INDEX_MAX_CHARS)
            parsed = parse_index(text)
            self.assertEqual(parsed.identity, {})

    def test_thin_memory_store_write_respects_size_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ThinMemoryStore(workspace_root=Path(tmp), max_index_chars=1024)
            index = store.load_index()
            index.identity["preferred_name"] = "x" * 5000
            with self.assertRaises(ValueError):
                store.save_index(index)

    def test_index_parse_and_render_are_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ThinMemoryStore(workspace_root=Path(tmp))
            patch = {
                "identity": {"set": {"preferred_name": "Michal", "timezone": "Europe/Amsterdam"}},
                "active_projects": {
                    "upsert": [
                        {
                            "project_id": "P001",
                            "title": "OpenClaw parity",
                            "path": "memory/pages/projects/openclaw_parity.md",
                        }
                    ]
                },
                "obligations": {
                    "upsert": [
                        {
                            "obligation_id": "O014",
                            "text": "Send invoice",
                            "due": "2026-03-05",
                            "ref": "memory/pages/tasks.md#invoices",
                        }
                    ]
                },
                "preferences": {"set": {"style": "scientific_pushback"}},
                "pointers": {"set": {"P001": "memory/pages/projects/openclaw_parity.md"}},
            }
            store.update_index_patch(patch)
            first = store.read_index_text()
            reparsed = parse_index(first)
            second = render_index(reparsed)
            self.assertEqual(first, second)

    def test_pointer_open_and_index_get_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ThinMemoryStore(workspace_root=root)
            page = root / "memory" / "pages" / "projects" / "openclaw_parity.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# OpenClaw Parity\n\n## Notes\nhello\n", encoding="utf-8")
            store.update_index_patch(
                {
                    "pointers": {"set": {"P001": "memory/pages/projects/openclaw_parity.md#notes"}},
                }
            )
            index_tool = MemoryIndexGetTool()
            pointer_tool = MemoryPointerOpenTool()
            ctx = ToolContext(workspace_root=root)
            idx = index_tool.run(ToolRequest(name="memory_index_get", args={}), ctx)
            self.assertTrue(idx.ok)
            self.assertIn("# MEMORY_INDEX v1", idx.output)
            opened = pointer_tool.run(
                ToolRequest(
                    name="memory_pointer_open",
                    args={"pointer_id": "P001", "max_chars": 2000},
                ),
                ctx,
            )
            self.assertTrue(opened.ok, opened.output)
            self.assertIn("target: memory/pages/projects/openclaw_parity.md#notes", opened.output)
            self.assertIn("## Notes", opened.output)

    def test_index_patch_rejects_section_overflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ThinMemoryStore(workspace_root=Path(tmp))
            with self.assertRaises(ValueError):
                store.update_index_patch(
                    {
                        "active_projects": {
                            "set_all": [
                                {
                                    "project_id": f"P{i:03d}",
                                    "title": f"Project {i}",
                                    "path": "memory/pages/projects/p.md",
                                }
                                for i in range(1, 30)
                            ]
                        }
                    }
                )

    def test_pointer_target_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ThinMemoryStore(workspace_root=Path(tmp))
            with self.assertRaises(ValueError):
                store.update_index_patch(
                    {
                        "pointers": {"set": {"BAD": "../secrets.md"}},
                    }
                )

    def test_prompt_includes_thin_index_not_daily_logs_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AgentService(
                provider=EchoFallbackProvider(),
                session_workspaces_root=root / "workspaces",
            )
            session_id = "sess-prompt-memory"
            service.initialize_session_workspace(session_id=session_id)
            ws = service.session_workspace(session_id=session_id)
            daily = ws / "memory" / "daily" / "2026-03-01.md"
            daily.parent.mkdir(parents=True, exist_ok=True)
            daily.write_text("# Daily\nsecret-daily-detail", encoding="utf-8")
            prompt = service.build_session_prompt(session_id=session_id, user_prompt="hello")
            self.assertIn("Thin memory index:", prompt)
            self.assertIn("Memory usage contract:", prompt)
            self.assertNotIn("secret-daily-detail", prompt)

    def test_message_compaction_updates_obligations_and_keeps_index_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SqliteRunStore(db_path=root / "state.db")
            service = AgentService(
                provider=EchoFallbackProvider(),
                run_store=store,
                session_workspaces_root=root / "workspaces",
            )
            session = service.get_or_create_session(chat_id=5, user_id=6)
            service.append_session_user_message(
                session.session_id,
                "TODO: Send invoice to client due 2026-03-05",
            )
            service.append_session_assistant_message(
                session.session_id,
                "I added that to obligations.",
            )
            tm = ThinMemoryStore(workspace_root=service.session_workspace(session.session_id))
            index = tm.load_index()
            self.assertTrue(any("Send invoice to client" in o.text for o in index.obligations))
            self.assertLessEqual(len(tm.read_index_text()), MEMORY_INDEX_MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
