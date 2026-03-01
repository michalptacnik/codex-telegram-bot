import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.services.agent_service import AgentService
from codex_telegram_bot.services.thin_memory import MEMORY_INDEX_MAX_CHARS, ThinMemoryStore, parse_index


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


if __name__ == "__main__":
    unittest.main()
