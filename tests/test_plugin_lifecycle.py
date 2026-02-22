import json
import os
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.plugin_lifecycle import PluginLifecycleManager


def _manifest(plugin_id: str, version: str = "1.0.0", signature: str = "") -> dict:
    data = {
        "manifest_version": "1.0",
        "plugin_id": plugin_id,
        "name": f"Plugin {plugin_id}",
        "version": version,
        "requires_api_version": "v1",
        "entrypoint": {"type": "command", "argv": ["python3", "-m", plugin_id]},
        "capabilities": ["runs:read", "meta:read"],
    }
    if signature:
        data["signature"] = signature
    return data


class TestPluginLifecycleManager(unittest.TestCase):
    def test_install_enable_disable_update_uninstall_and_audit(self):
        old_policy = os.environ.get("PLUGIN_TRUST_POLICY")
        os.environ["PLUGIN_TRUST_POLICY"] = "require_signature"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_dir = Path(tmp)
                mgr = PluginLifecycleManager(config_dir=config_dir)

                manifest_a = config_dir / "a.json"
                manifest_a.write_text(json.dumps(_manifest("plugin_a")), encoding="utf-8")
                p = mgr.install_plugin(manifest_a, enable=False)
                self.assertEqual(p.plugin_id, "plugin_a")
                self.assertFalse(p.enabled)

                with self.assertRaises(ValueError):
                    mgr.enable_plugin("plugin_a")

                signed = _manifest("plugin_a", signature="sig-123")
                manifest_signed = config_dir / "a_signed.json"
                manifest_signed.write_text(json.dumps(signed), encoding="utf-8")
                p2 = mgr.update_plugin("plugin_a", manifest_signed)
                self.assertFalse(p2.enabled)

                p3 = mgr.enable_plugin("plugin_a")
                self.assertTrue(p3.enabled)

                p4 = mgr.disable_plugin("plugin_a")
                self.assertFalse(p4.enabled)

                updated = _manifest("plugin_a", version="1.1.0", signature="sig-123")
                manifest_updated = config_dir / "a_updated.json"
                manifest_updated.write_text(json.dumps(updated), encoding="utf-8")
                p5 = mgr.update_plugin("plugin_a", manifest_updated)
                self.assertEqual(p5.version, "1.1.0")

                removed = mgr.uninstall_plugin("plugin_a")
                self.assertTrue(removed)
                self.assertEqual(mgr.list_plugins(), [])

                audit = mgr.list_audit_events(limit=50)
                actions = [e.action for e in audit]
                self.assertIn("install", actions)
                self.assertIn("update", actions)
                self.assertIn("enable", actions)
                self.assertIn("disable", actions)
                self.assertIn("uninstall", actions)
        finally:
            if old_policy is None:
                os.environ.pop("PLUGIN_TRUST_POLICY", None)
            else:
                os.environ["PLUGIN_TRUST_POLICY"] = old_policy

    def test_allow_local_unsigned_policy(self):
        old_policy = os.environ.get("PLUGIN_TRUST_POLICY")
        os.environ["PLUGIN_TRUST_POLICY"] = "allow_local_unsigned"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_dir = Path(tmp)
                mgr = PluginLifecycleManager(config_dir=config_dir)
                manifest_path = config_dir / "unsigned.json"
                manifest_path.write_text(json.dumps(_manifest("plugin_unsigned")), encoding="utf-8")
                plugin = mgr.install_plugin(manifest_path, enable=True)
                self.assertTrue(plugin.enabled)
                self.assertEqual(plugin.trust_status, "trusted")
        finally:
            if old_policy is None:
                os.environ.pop("PLUGIN_TRUST_POLICY", None)
            else:
                os.environ["PLUGIN_TRUST_POLICY"] = old_policy

