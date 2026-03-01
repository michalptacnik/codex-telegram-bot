import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_telegram_bot.tools import build_default_tool_registry
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.files import ReadFileTool, WriteFileTool
from codex_telegram_bot.tools.web import WebSearchTool, WebFetchTool


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

    def test_write_file_tool_blocks_direct_soul_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = WriteFileTool()
            res = tool.run(
                ToolRequest(name="write_file", args={"path": "memory/SOUL.md", "content": "x"}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)
            self.assertIn("SOUL.md is immutable", res.output)


class TestToolRegistry(unittest.TestCase):
    def test_default_registry_contains_expected_tools(self):
        registry = build_default_tool_registry()
        names = registry.names()
        self.assertIn("read_file", names)
        self.assertIn("write_file", names)
        self.assertIn("soul_get", names)
        self.assertIn("soul_propose_patch", names)
        self.assertIn("soul_apply_patch", names)
        self.assertIn("git_status", names)
        self.assertIn("web_search", names)

    def test_email_tool_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            registry = build_default_tool_registry()
            self.assertIsNone(registry.get("send_email_smtp"))

    def test_email_tool_enabled_by_env(self):
        with patch.dict("os.environ", {"ENABLE_EMAIL_TOOL": "1"}, clear=True):
            registry = build_default_tool_registry()
            self.assertIsNotNone(registry.get("send_email_smtp"))

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

    def test_web_search_tool_can_be_disabled_by_env(self):
        with patch.dict("os.environ", {"ENABLE_WEB_SEARCH_TOOL": "0"}, clear=True):
            registry = build_default_tool_registry()
            self.assertIsNone(registry.get("web_search"))


class TestWebSearchTool(unittest.TestCase):
    def test_web_search_prefers_google_by_default(self):
        tool = WebSearchTool(
            fetch_fn=lambda _q, _t: {"RelatedTopics": []},
            fallback_fn=lambda _q, _t, _k: [],
            google_fetch_fn=lambda _q, _t, _k: [
                {"title": "Google Hit", "url": "https://example.com/google", "snippet": "from google"}
            ],
        )
        res = tool.run(
            ToolRequest(name="web_search", args={"query": "test query", "k": 3}),
            ToolContext(workspace_root=Path.cwd()),
        )
        self.assertTrue(res.ok)
        self.assertIn("source: Google Custom Search", res.output)
        self.assertIn("https://example.com/google", res.output)

    def test_web_search_formats_sources(self):
        def _fake_fetch(_query: str, _timeout: int):
            return {
                "Heading": "Test Topic",
                "AbstractText": "Abstract snippet.",
                "AbstractURL": "https://example.com/abstract",
                "RelatedTopics": [
                    {"Text": "Result One - snippet one", "FirstURL": "https://example.com/one"},
                    {"Text": "Result Two - snippet two", "FirstURL": "https://example.com/two"},
                ],
            }

        tool = WebSearchTool(fetch_fn=_fake_fetch)
        res = tool.run(
            ToolRequest(name="web_search", args={"query": "test query", "k": 3}),
            ToolContext(workspace_root=Path.cwd()),
        )
        self.assertTrue(res.ok)
        self.assertIn("Web results for", res.output)
        self.assertIn("https://example.com/one", res.output)
        self.assertIn("source: DuckDuckGo", res.output)

    def test_web_search_uses_html_fallback_when_instant_answer_empty(self):
        tool = WebSearchTool(
            fetch_fn=lambda _q, _t: {},
            fallback_fn=lambda _q, _t, _k: [
                {
                    "title": "DeepSeek",
                    "url": "https://www.deepseek.com/",
                    "snippet": "Official DeepSeek website.",
                }
            ],
        )
        res = tool.run(
            ToolRequest(name="web_search", args={"query": "DeepSeek company", "k": 3}),
            ToolContext(workspace_root=Path.cwd()),
        )
        self.assertTrue(res.ok)
        self.assertIn("source: DuckDuckGo HTML fallback", res.output)
        self.assertIn("https://www.deepseek.com/", res.output)

    def test_web_search_falls_back_to_duckduckgo_when_google_unavailable(self):
        tool = WebSearchTool(
            fetch_fn=lambda _q, _t: {
                "RelatedTopics": [
                    {"Text": "Result One - snippet one", "FirstURL": "https://example.com/one"},
                ]
            },
            fallback_fn=lambda _q, _t, _k: [],
            google_fetch_fn=lambda _q, _t, _k: (_ for _ in ()).throw(RuntimeError("google down")),
        )
        res = tool.run(
            ToolRequest(name="web_search", args={"query": "test query", "k": 3}),
            ToolContext(workspace_root=Path.cwd()),
        )
        self.assertTrue(res.ok)
        self.assertIn("source: DuckDuckGo", res.output)
        self.assertIn("https://example.com/one", res.output)

    def test_web_search_requires_query(self):
        tool = WebSearchTool(fetch_fn=lambda _q, _t: {})
        res = tool.run(
            ToolRequest(name="web_search", args={}),
            ToolContext(workspace_root=Path.cwd()),
        )
        self.assertFalse(res.ok)
        self.assertIn("query is required", res.output)


class _FakeHeaders(dict):
    def get_content_charset(self):
        return "utf-8"


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str, final_url: str):
        self._body = body
        self.headers = _FakeHeaders({"Content-Type": content_type})
        self._final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _n: int = -1):
        return self._body

    def geturl(self):
        return self._final_url


class _FakeOpener:
    def __init__(self, response):
        self._response = response

    def open(self, _req, timeout=10):
        return self._response


class TestWebFetchTool(unittest.TestCase):
    def test_web_fetch_blocks_localhost_targets(self):
        tool = WebFetchTool()
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            res = tool.run(
                ToolRequest(name="web_fetch", args={"url": "http://127.0.0.1/"}),
                ToolContext(workspace_root=Path.cwd()),
            )
        self.assertFalse(res.ok)
        self.assertIn("blocked", res.output.lower())

    def test_web_fetch_rejects_non_text_content_type(self):
        tool = WebFetchTool()
        fake_response = _FakeResponse(body=b"\x89PNG", content_type="image/png", final_url="https://example.com/img.png")
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            with patch("urllib.request.build_opener", return_value=_FakeOpener(fake_response)):
                res = tool.run(
                    ToolRequest(name="web_fetch", args={"url": "https://example.com/img.png"}),
                    ToolContext(workspace_root=Path.cwd()),
                )
        self.assertFalse(res.ok)
        self.assertIn("unsupported content type", res.output.lower())

    def test_web_fetch_extracts_readable_text(self):
        html = b"""\
<!doctype html><html><head><title>Article Title</title></head>
<body><nav>ignore me</nav><main><h1>Article Title</h1><p>Hello world from article body.</p></main></body></html>
"""
        tool = WebFetchTool()
        fake_response = _FakeResponse(body=html, content_type="text/html", final_url="https://example.com/article")
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            with patch("urllib.request.build_opener", return_value=_FakeOpener(fake_response)):
                res = tool.run(
                    ToolRequest(name="web_fetch", args={"url": "https://example.com/article", "max_chars": 200}),
                    ToolContext(workspace_root=Path.cwd()),
                )
        self.assertTrue(res.ok, msg=res.output)
        payload = __import__("json").loads(res.output)
        self.assertEqual(payload["title"], "Article Title")
        self.assertIn("Hello world", payload["text"])
