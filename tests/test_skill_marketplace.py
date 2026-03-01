import base64
import json
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.services.agent_service import AgentService
from codex_telegram_bot.services.skill_manager import SkillManager
from codex_telegram_bot.services.skill_marketplace import SkillMarketplace, SkillSource


class TestSkillMarketplace(unittest.TestCase):
    def test_catalog_parsing_and_cache_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SqliteRunStore(db_path=root / "state.db")
            manager = SkillManager(config_dir=root / "cfg")
            market = SkillMarketplace(
                store=store,
                skill_manager=manager,
                workspace_root=root,
                config_dir=root / "cfg",
            )
            market._sources = [SkillSource(name="demo", type="github_repo", repo="acme/skills", path="skills", ref="main")]
            md = (
                "---\n"
                "skill_id: writer\n"
                "name: Writer\n"
                "description: Writing helper\n"
                "keywords: [write, draft]\n"
                "tools: []\n"
                "---\n"
                "Long body that should stay on disk.\n"
            )
            encoded = base64.b64encode(md.encode("utf-8")).decode("ascii")

            def _fake_http(url: str) -> str:
                if "/contents/skills?" in url:
                    return json.dumps([{"type": "dir", "name": "writer"}])
                if "/contents/skills/writer/SKILL.md" in url:
                    return json.dumps({"content": encoded})
                raise AssertionError(f"unexpected url {url}")

            market._http_get_text = _fake_http  # type: ignore[assignment]
            stats = market.refresh(source_name="demo", force=True)
            self.assertEqual(stats["entries"], 1)
            rows = store.list_skill_catalog_entries(query="", source_name="demo", limit=10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "demo:writer")

            # Cached search should return quickly without force.
            rows2 = market.search(query="writer", source="demo", refresh=False)
            self.assertEqual(len(rows2), 1)

    def test_install_and_enable_hash_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SqliteRunStore(db_path=root / "state.db")
            manager = SkillManager(config_dir=root / "cfg")
            market = SkillMarketplace(
                store=store,
                skill_manager=manager,
                workspace_root=root,
                config_dir=root / "cfg",
            )
            entry = {
                "id": "demo:writer",
                "source_name": "demo",
                "skill_name": "writer",
                "version": "1.0.0",
                "description": "Writer skill",
                "tags": ["writing"],
                "install_ref": {
                    "type": "github_repo_skill",
                    "repo": "acme/skills",
                    "path": "skills/writer",
                    "ref": "main",
                },
                "last_fetched_at": "2026-03-01T00:00:00+00:00",
            }
            store.upsert_skill_catalog_entries("demo", [entry])

            skill_text = (
                "---\n"
                "skill_id: writer\n"
                "name: Writer\n"
                "description: Writing helper\n"
                "keywords: [write, draft]\n"
                "tools: []\n"
                "---\n"
                "Some body content.\n"
            )
            market._fetch_skill_bundle = lambda install_ref: {  # type: ignore[assignment]
                "SKILL.md": skill_text.encode("utf-8"),
                "assets/tips.md": b"tips",
            }
            installed = market.install(skill_ref="demo:writer", target="workspace")
            self.assertEqual(installed["skill_id"], "writer")

            # Hash verification failure on tamper.
            skill_path = Path(installed["installed_path"]) / "SKILL.md"
            skill_path.write_text("tampered", encoding="utf-8")
            with self.assertRaises(ValueError):
                market.enable("writer")

    def test_progressive_disclosure_prompt_not_bloated_by_market_skill_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SkillManager(config_dir=root / "cfg")
            manager.upsert_instruction_skill(
                skill_id="writer",
                name="Writer",
                description="Instruction-only writer skill",
                keywords=["draft"],
                source="market:demo",
                enabled=True,
            )
            service = AgentService(
                provider=EchoFallbackProvider(),
                skill_manager=manager,
                session_workspaces_root=root / "ws",
            )
            session_id = "sess-market-skill"
            service.initialize_session_workspace(session_id=session_id)
            prompt = service.build_session_prompt(session_id=session_id, user_prompt="hello")
            self.assertNotIn("Some body content.", prompt)
            self.assertIn("Skill usage contract:", prompt)


if __name__ == "__main__":
    unittest.main()
