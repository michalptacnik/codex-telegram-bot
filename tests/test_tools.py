import tempfile
import unittest
import subprocess
from unittest.mock import patch
from pathlib import Path

from codex_telegram_bot.tools import build_default_tool_registry
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.browser import HeadlessChromiumTool
from codex_telegram_bot.tools.files import ReadFileTool, WriteFileTool


class TestFileTools(unittest.TestCase):
    def test_read_file_tool_reads_workspace_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "note.txt"
            path.write_text("hello", encoding="utf-8")
            tool = ReadFileTool()

            res = tool.run(
                ToolRequest(name="read_file", args={"path": "note.txt"}),
                ToolContext(workspace_root=root),
            )

            self.assertTrue(res.ok)
            self.assertEqual(res.output, "hello")

    def test_write_file_tool_blocks_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = WriteFileTool()

            res = tool.run(
                ToolRequest(name="write_file", args={"path": "../oops.txt", "content": "x"}),
                ToolContext(workspace_root=root),
            )

            self.assertFalse(res.ok)
            self.assertIn("escapes workspace", res.output)

    def test_write_file_tool_returns_absolute_verified_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = WriteFileTool()

            res = tool.run(
                ToolRequest(name="write_file", args={"path": "note.txt", "content": "hi"}),
                ToolContext(workspace_root=root),
            )

            self.assertTrue(res.ok)
            self.assertIn(str(root / "note.txt"), res.output)
            self.assertIn("Verified file exists", res.output)

    def test_write_file_tool_allows_outside_workspace_in_trusted_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir(parents=True, exist_ok=True)
            external = Path(tmp) / "outside.txt"
            tool = WriteFileTool()

            res = tool.run(
                ToolRequest(name="write_file", args={"path": str(external), "content": "hello"}),
                ToolContext(workspace_root=root, policy_profile="trusted"),
            )

            self.assertTrue(res.ok)
            self.assertTrue(external.exists())
            self.assertEqual(external.read_text(encoding="utf-8"), "hello")


class TestToolRegistry(unittest.TestCase):
    def test_default_registry_contains_expected_tools(self):
        registry = build_default_tool_registry()
        names = registry.names()
        self.assertIn("read_file", names)
        self.assertIn("write_file", names)
        self.assertIn("git_status", names)
        self.assertIn("headless_chromium", names)

    def test_email_tool_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            registry = build_default_tool_registry()
            self.assertIsNone(registry.get("send_email_smtp"))

    def test_email_tool_enabled_by_env(self):
        with patch.dict("os.environ", {"ENABLE_EMAIL_TOOL": "1"}, clear=True):
            registry = build_default_tool_registry()
            self.assertIsNotNone(registry.get("send_email_smtp"))


class TestHeadlessChromiumTool(unittest.TestCase):
    def test_requires_url(self):
        tool = HeadlessChromiumTool()
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(ToolRequest(name="headless_chromium", args={}), ToolContext(workspace_root=Path(tmp)))
        self.assertFalse(res.ok)
        self.assertIn("missing required arg 'url'", res.output)

    def test_requires_http_or_https_url(self):
        tool = HeadlessChromiumTool()
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="headless_chromium", args={"url": "file:///tmp/x.html"}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertFalse(res.ok)
        self.assertIn("http(s)", res.output)

    @patch("codex_telegram_bot.tools.browser.shutil.which", return_value=None)
    def test_reports_missing_binary(self, _which):
        tool = HeadlessChromiumTool()
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="headless_chromium", args={"url": "https://example.com"}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertFalse(res.ok)
        self.assertIn("no Chromium binary found", res.output)

    @patch("codex_telegram_bot.tools.browser.shutil.which", return_value="/usr/bin/chromium")
    @patch("codex_telegram_bot.tools.browser.subprocess.run")
    def test_fetches_dom_on_success(self, mocked_run, _which):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["chromium"],
            returncode=0,
            stdout="<html><head><title>Example</title></head><body>Hello</body></html>",
            stderr="",
        )
        tool = HeadlessChromiumTool()
        with tempfile.TemporaryDirectory() as tmp:
            res = tool.run(
                ToolRequest(name="headless_chromium", args={"url": "https://example.com"}),
                ToolContext(workspace_root=Path(tmp)),
            )
        self.assertTrue(res.ok)
        self.assertIn("Fetched: https://example.com", res.output)
        self.assertIn("Title: Example", res.output)

    def test_email_tool_enabled_when_smtp_env_present(self):
        with patch.dict(
            "os.environ",
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_USER": "bot@example.com",
                "SMTP_APP_PASSWORD": "app-password",
            },
            clear=True,
        ):
            registry = build_default_tool_registry()
            self.assertIsNotNone(registry.get("send_email_smtp"))
