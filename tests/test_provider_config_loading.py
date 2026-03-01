import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_telegram_bot.app_container import build_agent_service


class TestProviderConfigLoading(unittest.TestCase):
    def test_loads_openai_compatible_provider_from_providers_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "providers.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "codex_cli": {"type": "codex_cli"},
                        "routerx": {
                            "type": "openai_compatible",
                            "api_key_env": "ROUTERX_API_KEY",
                            "base_url": "https://openrouter.ai/api/v1",
                            "model": "openai/gpt-4o-mini",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"PROVIDERS_CONFIG": str(cfg_path), "ROUTERX_API_KEY": "test-key"}, clear=False):
                service = build_agent_service(state_db_path=None, config_dir=Path(tmp))
                names = service.available_provider_names()
                self.assertIn("routerx", names)

    def test_invalid_provider_entry_is_skipped_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "providers.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "codex_cli": {"type": "codex_cli"},
                        "bad_router": {
                            "type": "openai_compatible",
                            "base_url": "https://example.invalid/v1",
                            "model": "foo/bar",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"PROVIDERS_CONFIG": str(cfg_path)}, clear=False):
                service = build_agent_service(state_db_path=None, config_dir=Path(tmp))
                names = service.available_provider_names()
                self.assertIn("codex_cli", names)
                self.assertNotIn("bad_router", names)


if __name__ == "__main__":
    unittest.main()

