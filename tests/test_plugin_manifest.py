import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.plugins.manifest import validate_manifest


def _valid_manifest() -> dict:
    return {
        "manifest_version": "1.0",
        "plugin_id": "plugin_reader",
        "name": "Plugin Reader",
        "version": "1.0.0",
        "requires_api_version": "v1",
        "entrypoint": {"type": "command", "argv": ["python3", "-m", "plugin_reader"]},
        "capabilities": ["runs:read", "jobs:read", "meta:read"],
        "permissions": [{"resource": "runs", "actions": ["read"]}],
    }


class TestPluginManifestValidation(unittest.TestCase):
    def test_valid_manifest(self):
        errors = validate_manifest(_valid_manifest())
        self.assertEqual(errors, [])

    def test_invalid_capability_and_semver(self):
        manifest = _valid_manifest()
        manifest["version"] = "v1"
        manifest["capabilities"] = ["runs:read", "unknown:capability"]
        errors = validate_manifest(manifest)
        self.assertTrue(any("semantic version" in e for e in errors))
        self.assertTrue(any("unsupported entries" in e for e in errors))

    def test_privileged_capability_requires_security_ack(self):
        manifest = _valid_manifest()
        manifest["capabilities"] = ["runs:read", "workspace:write"]
        errors = validate_manifest(manifest)
        self.assertTrue(any("security_acknowledged" in e for e in errors))

        manifest["security_acknowledged"] = True
        errors2 = validate_manifest(manifest)
        self.assertEqual(errors2, [])


class TestPluginManifestCli(unittest.TestCase):
    def test_cli_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(json.dumps(_valid_manifest()), encoding="utf-8")
            cmd = [
                "python3",
                "scripts/validate_plugin_manifest.py",
                str(manifest_path),
            ]
            result = subprocess.run(
                cmd,
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                env={"PYTHONPATH": "src"},
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("passed", result.stdout.lower())

