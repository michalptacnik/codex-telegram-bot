import json
import tempfile
import time
import unittest
from pathlib import Path

from codex_telegram_bot.services.browser_bridge import BrowserBridge
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.browser import (
    BrowserActionTool,
    BrowserExtractTool,
    BrowserNavigateTool,
    BrowserOpenTool,
    BrowserSnapshotTool,
    BrowserScriptTool,
    BrowserStatusTool,
)


class TestBrowserBridge(unittest.TestCase):
    def test_register_and_status(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        client = bridge.register_client(instance_id="client-1", label="Chrome")
        self.assertEqual(client["instance_id"], "client-1")

        status = bridge.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["active_clients"], 1)
        self.assertEqual(status["total_clients"], 1)

    def test_register_exposes_extension_capabilities(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(
            instance_id="client-1",
            label="Chrome",
            extension_version="2.0.0",
            supported_commands=["open_url", "run_script", "snapshot", "screenshot"],
        )
        status = bridge.status()
        self.assertEqual(status["active_clients"], 1)
        self.assertIn("snapshot", status.get("supported_commands", []))
        self.assertEqual(status["clients"][0].get("extension_version"), "2.0.0")
        self.assertIn("screenshot", status["clients"][0].get("supported_commands", []))
        self.assertEqual(status["clients"][0].get("capability_source"), "reported")

    def test_capability_inference_uses_client_version_when_extension_metadata_missing(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(
            instance_id="client-1",
            label="Chrome",
            version="2.0.0",
        )
        status = bridge.status()
        self.assertIn("snapshot", status.get("supported_commands", []))
        self.assertIn("screenshot", status.get("supported_commands", []))
        client = status["clients"][0]
        self.assertEqual(client.get("extension_version"), "")
        self.assertEqual(client.get("capability_source"), "inferred_v2_from_client_version")
        self.assertIn("snapshot", client.get("effective_supported_commands", []))

    def test_capability_inference_falls_back_to_baseline_for_legacy_versions(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(
            instance_id="client-legacy",
            label="Chrome",
            version="0.1.1",
        )
        status = bridge.status()
        self.assertIn("run_script", status.get("supported_commands", []))
        self.assertNotIn("snapshot", status.get("supported_commands", []))
        client = status["clients"][0]
        self.assertEqual(client.get("capability_source"), "inferred_baseline_from_client_version")
        self.assertNotIn("snapshot", client.get("effective_supported_commands", []))

    def test_enqueue_requires_active_client(self):
        bridge = BrowserBridge()
        result = bridge.enqueue_command(command_type="open_url", payload={"url": "https://example.com"})
        self.assertFalse(result["ok"])
        self.assertIn("No active Chrome extension", result["error"])

    def test_queue_poll_complete_wait(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")

        queued = bridge.enqueue_command(
            command_type="open_url",
            payload={"url": "https://example.com"},
            wait=False,
        )
        self.assertTrue(queued["ok"])
        command = queued["command"]
        command_id = command["command_id"]

        polled = bridge.poll_commands(instance_id="client-1", limit=5)
        self.assertEqual(len(polled), 1)
        self.assertEqual(polled[0]["command_id"], command_id)

        completed = bridge.complete_command(
            instance_id="client-1",
            command_id=command_id,
            ok=True,
            output="Opened",
            data={"tab_id": 7},
        )
        self.assertTrue(completed["ok"])

        waited = bridge.wait_for_result(command_id=command_id, timeout_sec=2)
        self.assertTrue(waited["ok"])
        self.assertEqual(waited["command"]["status"], "completed")
        self.assertEqual(waited["command"]["data"]["tab_id"], 7)

    def test_enqueue_with_invalid_requested_client_falls_back_to_active(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        queued = bridge.enqueue_command(
            command_type="open_url",
            payload={"url": "https://example.com"},
            client_id="0",
            wait=False,
        )
        self.assertTrue(queued["ok"])
        self.assertEqual(queued["command"]["client_id"], "client-1")

    def test_wait_timeout_returns_pending_and_command_is_redispatched(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60, dispatch_lease_sec=1)
        bridge.register_client(instance_id="client-1", label="Chrome")
        queued = bridge.enqueue_command(
            command_type="open_url",
            payload={"url": "https://example.com"},
            wait=False,
        )
        self.assertTrue(queued["ok"])
        command_id = queued["command"]["command_id"]

        first_poll = bridge.poll_commands(instance_id="client-1", limit=5)
        self.assertEqual(len(first_poll), 1)
        self.assertEqual(first_poll[0]["command_id"], command_id)
        self.assertEqual(int(first_poll[0].get("dispatch_count") or 0), 1)

        timed_out = bridge.wait_for_result(command_id=command_id, timeout_sec=1)
        self.assertFalse(timed_out["ok"])
        self.assertTrue(timed_out.get("pending"))
        self.assertEqual(timed_out["command"]["status"], "dispatched")

        time.sleep(1.05)
        second_poll = bridge.poll_commands(instance_id="client-1", limit=5)
        self.assertEqual(len(second_poll), 1)
        self.assertEqual(second_poll[0]["command_id"], command_id)
        self.assertGreaterEqual(int(second_poll[0].get("dispatch_count") or 0), 2)

        completed = bridge.complete_command(
            instance_id="client-1",
            command_id=command_id,
            ok=True,
            output="Opened",
            data={"tab_id": 9},
        )
        self.assertTrue(completed["ok"])
        waited = bridge.wait_for_result(command_id=command_id, timeout_sec=1)
        self.assertTrue(waited["ok"])
        self.assertEqual(waited["command"]["status"], "completed")

    def test_extension_supports_command_uses_capability_metadata(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(
            instance_id="client-1",
            label="Chrome",
            extension_version="1.0.0",
            supported_commands=["open_url", "navigate_url", "run_script"],
        )
        self.assertFalse(bridge.extension_supports_command("snapshot"))
        self.assertTrue(bridge.extension_supports_command("run_script"))

    def test_extension_supports_snapshot_for_v2_client_version_without_extension_version(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome", version="2.0.0")
        self.assertTrue(bridge.extension_supports_command("snapshot"))


class TestBrowserTools(unittest.TestCase):
    def test_browser_open_rejects_localhost(self):
        bridge = BrowserBridge()
        tool = BrowserOpenTool(bridge=bridge)

        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="browser_open", args={"url": "http://localhost:8765"}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertFalse(res.ok)
        self.assertIn("http(s)", res.output)

    def test_browser_open_queues_command(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserOpenTool(bridge=bridge)

        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_open",
                    args={
                        "url": "https://example.com",
                        "wait": False,
                        "client_id": "client-1",
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"]["command_type"], "open_url")
        self.assertEqual(payload["command"]["payload"]["url"], "https://example.com")

    def test_browser_open_uses_active_tab_url_when_url_is_missing(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(
            instance_id="client-1",
            label="Chrome",
            active_tab_url="https://x.com/home",
        )
        tool = BrowserOpenTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="browser_open", args={"wait": False, "client_id": "client-1"}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"]["payload"]["url"], "https://x.com/home")

    def test_browser_navigate_uses_active_tab_url_when_url_is_missing(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(
            instance_id="client-1",
            label="Chrome",
            active_tab_url="https://x.com/home",
        )
        tool = BrowserNavigateTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="browser_navigate", args={"wait": False, "client_id": "client-1"}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"]["payload"]["url"], "https://x.com/home")

    def test_browser_open_treats_placeholder_client_id_as_auto(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserOpenTool(bridge=bridge)

        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_open",
                    args={
                        "url": "https://example.com",
                        "wait": False,
                        "client_id": "0",
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"]["client_id"], "client-1")

    def test_browser_status_tool_json(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserStatusTool(bridge=bridge)

        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(ToolRequest(name="browser_status", args={}), ToolContext(workspace_root=Path(tmp)))
        self.assertTrue(res.ok)
        payload = json.loads(res.output)
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["active_clients"], 1)

    def test_browser_script_queues_command(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserScriptTool(bridge=bridge)

        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_script",
                    args={
                        "script": "return document.title;",
                        "wait": False,
                        "client_id": "client-1",
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"]["command_type"], "run_script")

    def test_browser_extract_queues_script_command(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserExtractTool(bridge=bridge)

        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_extract",
                    args={
                        "wait": False,
                        "client_id": "client-1",
                        "max_chars": 4000,
                        "include_links": True,
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"]["command_type"], "run_script")

    def test_browser_action_queues_script_command(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserActionTool(bridge=bridge)

        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_action",
                    args={
                        "steps": [{"action": "click", "selector": "body"}],
                        "wait": False,
                        "client_id": "client-1",
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"]["command_type"], "run_script")
        self.assertIn("const plan=", payload["command"]["payload"]["script"])

    def test_browser_action_empty_args_falls_back_to_extract(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserActionTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="browser_action", args={"wait": False}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"]["command_type"], "run_script")
        script = str(payload["command"]["payload"].get("script") or "")
        self.assertIn('"action":"extract"', script)

    def test_browser_action_supports_operation_alias(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserActionTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_action",
                    args={
                        "operation": "set_text",
                        "selector": "div[data-testid='tweetTextarea_0']",
                        "text": "hello world",
                        "wait": False,
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        script = str(payload["command"]["payload"].get("script") or "")
        self.assertIn('"action":"type"', script)
        self.assertNotIn('"action":"extract"', script)

    def test_browser_action_expands_x_compose_selector_aliases(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserActionTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_action",
                    args={
                        "action": "click",
                        "selector": "a[href='/compose/post']",
                        "wait": False,
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        script = str(payload["command"]["payload"].get("script") or "")
        self.assertIn("SideNav_NewTweet_Button", script)
        self.assertIn("[data-testid='tweetButton']", script)
        self.assertIn("AppTabBar_NewTweet_Link", script)

    def test_browser_action_script_prefers_interactable_targets(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserActionTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_action",
                    args={
                        "action": "click",
                        "selector": "div[data-testid='tweetButton']",
                        "wait": False,
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        script = str(payload["command"]["payload"].get("script") or "")
        self.assertIn("isInteractable", script)
        self.assertIn("maybeComposeTargets", script)

    def test_browser_action_script_supports_contenteditable_type(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserActionTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_action",
                    args={
                        "operation": "set_text",
                        "selector": "div[data-testid='tweetTextarea_0']",
                        "text": "hello world",
                        "wait": False,
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        script = str(payload["command"]["payload"].get("script") or "")
        self.assertIn("setContentEditable", script)
        self.assertIn("el.isContentEditable", script)

    def test_browser_action_retries_without_stale_tab_id(self):
        class _RetryBridge:
            def __init__(self):
                self.calls = []

            def enqueue_command(self, **kwargs):
                payload = dict(kwargs.get("payload") or {})
                self.calls.append(payload)
                if "tab_id" in payload:
                    return {"ok": False, "error": "Script failed on tab 123: No tab with id: 123"}
                return {
                    "ok": True,
                    "command": {
                        "command_id": "cmd-1",
                        "status": "queued",
                        "command_type": "run_script",
                        "payload": payload,
                    },
                }

        bridge = _RetryBridge()
        tool = BrowserActionTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_action",
                    args={
                        "action": "click",
                        "selector": "body",
                        "tab_id": 123,
                        "wait": False,
                    },
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        self.assertEqual(len(bridge.calls), 2)
        self.assertIn("tab_id", bridge.calls[0])
        self.assertNotIn("tab_id", bridge.calls[1])

    def test_browser_action_normalizes_completed_result(self):
        class _CompletedBridge:
            def enqueue_command(self, **kwargs):
                return {
                    "ok": True,
                    "command": {
                        "command_id": "cmd-1",
                        "status": "completed",
                        "command_type": "run_script",
                        "data": {
                            "tab_id": 7,
                            "result": {
                                "ok": True,
                                "url": "https://example.com",
                                "title": "Example",
                                "steps": [{"index": 0, "action": "click", "ok": True}],
                            },
                        },
                    },
                }

        tool = BrowserActionTool(bridge=_CompletedBridge())
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(
                    name="browser_action",
                    args={"action": "click", "selector": "body", "wait": True},
                ),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tab_id"], 7)
        self.assertEqual(payload["title"], "Example")
        self.assertEqual(payload["command_id"], "cmd-1")

    def test_browser_snapshot_emulates_via_run_script_when_native_snapshot_is_unsupported(self):
        class _LegacySnapshotBridge:
            def __init__(self):
                self.last_command_type = ""
                self.ref_map = {}

            def extension_supports_command(self, command):
                return str(command or "").strip().lower() != "snapshot"

            def enqueue_command(self, **kwargs):
                self.last_command_type = str(kwargs.get("command_type") or "")
                return {
                    "ok": True,
                    "command": {
                        "command_id": "cmd-snap-1",
                        "status": "completed",
                        "command_type": self.last_command_type,
                        "data": {
                            "tab_id": 7,
                            "result": {
                                "url": "https://example.com",
                                "title": "Example",
                                "total_elements_on_page": 1,
                                "truncated": False,
                                "elements": [
                                    {
                                        "ref": 1,
                                        "tag": "button",
                                        "role": "button",
                                        "name": "Post",
                                        "text": "Post",
                                        "selector": "button[data-testid='tweetButtonInline']",
                                    }
                                ],
                                "ref_map": {"1": "button[data-testid='tweetButtonInline']"},
                            },
                        },
                    },
                }

            def set_snapshot_ref_map(self, ref_map):
                self.ref_map = dict(ref_map or {})

        bridge = _LegacySnapshotBridge()
        tool = BrowserSnapshotTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="browser_snapshot", args={"wait": True}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok, msg=res.output)
        payload = json.loads(res.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload.get("mode"), "emulated_via_run_script")
        self.assertEqual(int(payload.get("element_count") or 0), 1)
        self.assertEqual(bridge.last_command_type, "run_script")
        self.assertEqual(bridge.ref_map.get("1"), "button[data-testid='tweetButtonInline']")

    def test_browser_script_reports_outdated_extension_message(self):
        class _UnsupportedBridge:
            def enqueue_command(self, **kwargs):
                return {"ok": False, "error": "Unsupported command type: run_script"}

        tool = BrowserScriptTool(bridge=_UnsupportedBridge())
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="browser_script", args={"script": "return 1;", "wait": True}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertFalse(res.ok)
        self.assertIn("outdated", res.output.lower())
