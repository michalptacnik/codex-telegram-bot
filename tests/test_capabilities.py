import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.agent_core.capabilities import MarkdownCapabilityRegistry


class TestCapabilityRegistry(unittest.TestCase):
    def test_registry_selects_relevant_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "system.md").write_text("# System\n- baseline\n", encoding="utf-8")
            (root / "git.md").write_text("# Git\n- inspect status and diffs\n", encoding="utf-8")
            (root / "files.md").write_text("# Files\n- safe file ops\n", encoding="utf-8")

            registry = MarkdownCapabilityRegistry(root)
            items = registry.summarize_for_prompt("please check git status and branch", max_capabilities=2)

            self.assertTrue(items)
            self.assertIn("git", [it.name for it in items])
