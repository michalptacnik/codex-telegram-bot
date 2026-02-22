import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.repo_context import RepositoryContextRetriever


class TestRepositoryContextRetriever(unittest.TestCase):
    def test_retrieves_relevant_file_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(parents=True, exist_ok=True)
            (root / "README.md").write_text("General docs\n", encoding="utf-8")
            (root / "src" / "agent_service.py").write_text(
                "class AgentService:\n"
                "    def run_prompt_with_tool_loop(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            retriever = RepositoryContextRetriever(root=root)
            res = retriever.retrieve("tool loop in agent service", limit=3)
            self.assertTrue(len(res) >= 1)
            self.assertIn("agent_service.py", res[0].path)
            self.assertIn("run_prompt_with_tool_loop", res[0].snippet)

    def test_symbol_weighting_and_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(parents=True, exist_ok=True)
            target = root / "src" / "planner.py"
            target.write_text(
                "class PlannerEngine:\n"
                "    def build_execution_plan(self):\n"
                "        return []\n",
                encoding="utf-8",
            )
            retriever = RepositoryContextRetriever(root=root, auto_refresh_sec=9999)
            res = retriever.retrieve("build execution plan", limit=2)
            self.assertTrue(res)
            self.assertIn("planner.py", res[0].path)
            self.assertIn("symbols:", res[0].snippet)

            target.write_text(
                "class PlannerEngine:\n"
                "    def build_execution_plan(self):\n"
                "        return ['changed']\n",
                encoding="utf-8",
            )
            refresh = retriever.refresh_index(force=True)
            self.assertGreaterEqual(refresh["changed_files"], 1)
            stats = retriever.stats()
            self.assertGreaterEqual(stats["indexed_files"], 1)
