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
    BrowserOpenTool,
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

    def test_browser_action_requires_action_or_steps(self):
        bridge = BrowserBridge(heartbeat_ttl_sec=60)
        bridge.register_client(instance_id="client-1", label="Chrome")
        tool = BrowserActionTool(bridge=bridge)
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="browser_action", args={"wait": False}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertFalse(res.ok)
        self.assertIn("action is required", res.output.lower())

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
