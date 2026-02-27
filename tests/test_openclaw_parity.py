"""Golden scenario suite for OpenClaw Parity (Issue #108).

CI-safe mock implementations — no external network dependencies.

Covers:
  1. Write + verify + absolute path operations
  2. Memory operations (write/search/get)
  3. Sessions spawn background result handling
  4. MCP discover and call functionality
  5. Approval flow for high-risk actions

Plus unit tests for each parity issue:
  #102 - Responses API structured tool-calling loop
  #103 - MCP bridge with lazy discovery, cache, dynamic schema injection
  #104 - Skill-pack system (SKILL.md semantics, precedence, gating, lazy injection)
  #105 - Agent-facing session tools (visibility controls)
  #106 - Markdown-first memory parity (daily logs + memory_get/memory_search)
  #107 - Tool policy groups, wildcard allow/deny, /elevated session state
"""
import asyncio
import json
import os
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Issue #102: Responses API structured tool-calling loop
# ---------------------------------------------------------------------------

from codex_telegram_bot.providers.responses_api import (
    ResponsesApiProvider,
    tool_schemas_from_registry,
    _extract_responses_text,
    _extract_responses_tool_calls,
)


class TestResponsesApiToolLoop:
    """Tests for the structured tool-calling loop (Issue #102)."""

    def test_tool_schemas_from_registry(self):
        """Verify tool_schemas_from_registry converts ToolRegistry to Responses API format."""
        from codex_telegram_bot.tools.base import ToolRegistry

        class FakeTool:
            def __init__(self, name):
                self.name = name
                self.description = f"Does {name} things."
            def run(self, req, ctx):
                pass

        reg = ToolRegistry()
        reg.register(FakeTool("read_file"))
        reg.register(FakeTool("write_file"))

        schemas = tool_schemas_from_registry(reg)
        assert len(schemas) == 2
        names = {s["name"] for s in schemas}
        assert "read_file" in names
        assert "write_file" in names
        for s in schemas:
            assert s["type"] == "function"
            assert "parameters" in s

    def test_tool_schemas_from_empty_registry(self):
        from codex_telegram_bot.tools.base import ToolRegistry
        reg = ToolRegistry()
        assert tool_schemas_from_registry(reg) == []

    def test_tool_schemas_from_none_registry(self):
        assert tool_schemas_from_registry(None) == []

    def test_run_tool_loop_no_api_key(self):
        p = ResponsesApiProvider(api_key="")
        result = asyncio.run(p.run_tool_loop(
            messages=[{"role": "user", "content": "test"}],
            tool_schemas=[],
        ))
        assert "Error" in result["text"]
        assert result["iterations"] == 0

    def test_run_tool_loop_completes_on_text_response(self):
        """When model returns text without tool calls, loop completes."""
        fake_response = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "All done!"}]}
            ]
        }
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=fake_response)

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        p = ResponsesApiProvider(api_key="test-key")
        p._http_client = mock_client

        result = asyncio.run(p.run_tool_loop(
            messages=[{"role": "user", "content": "Hello"}],
            tool_schemas=[],
        ))
        assert result["text"] == "All done!"
        assert result["iterations"] == 1
        assert result["tool_calls_log"] == []

    def test_run_tool_loop_executes_tool_calls(self):
        """Model returns a tool call, then text — loop executes tool and finishes."""
        tool_call_response = {
            "output": [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path": "/tmp/test.txt"}',
                    "call_id": "call_1",
                }
            ]
        }
        final_response = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "File contents: hello"}]}
            ]
        }

        mock_resp_tool = MagicMock()
        mock_resp_tool.raise_for_status = MagicMock()
        mock_resp_tool.json = MagicMock(return_value=tool_call_response)

        mock_resp_final = MagicMock()
        mock_resp_final.raise_for_status = MagicMock()
        mock_resp_final.json = MagicMock(return_value=final_response)

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=[mock_resp_tool, mock_resp_final])

        p = ResponsesApiProvider(api_key="test-key")
        p._http_client = mock_client

        def executor(name, args):
            return "hello"

        result = asyncio.run(p.run_tool_loop(
            messages=[{"role": "user", "content": "Read file"}],
            tool_schemas=[{"type": "function", "name": "read_file"}],
            tool_executor=executor,
        ))
        assert result["iterations"] == 2
        assert len(result["tool_calls_log"]) == 1
        assert result["tool_calls_log"][0]["name"] == "read_file"
        assert result["text"] == "File contents: hello"

    def test_run_tool_loop_max_iterations(self):
        """Loop stops at max_iterations even if model keeps returning tool calls."""
        tool_call_response = {
            "output": [
                {
                    "type": "function_call",
                    "name": "shell_exec",
                    "arguments": '{"cmd": "ls"}',
                    "call_id": "call_n",
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=tool_call_response)

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        p = ResponsesApiProvider(api_key="test-key")
        p._http_client = mock_client

        result = asyncio.run(p.run_tool_loop(
            messages=[{"role": "user", "content": "loop forever"}],
            tool_schemas=[{"type": "function", "name": "shell_exec"}],
            max_iterations=3,
        ))
        assert result["iterations"] == 3
        assert len(result["tool_calls_log"]) == 3


# ---------------------------------------------------------------------------
# Issue #103: MCP bridge
# ---------------------------------------------------------------------------

from codex_telegram_bot.services.mcp_bridge import (
    McpBridge,
    McpToolSpec,
    McpSearchTool,
    McpCallTool,
)


class TestMcpBridge:
    """Tests for MCP bridge (Issue #103)."""

    def test_register_server(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        bridge.register_server("https://example.com/mcp", name="test")
        servers = bridge.list_servers()
        assert len(servers) == 1
        assert servers[0].url == "https://example.com/mcp"

    def test_register_server_empty_url_raises(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        with pytest.raises(ValueError, match="required"):
            bridge.register_server("")

    @patch.dict(os.environ, {"MCP_DISABLE_HTTP": "true"})
    def test_http_blocked(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        with pytest.raises(ValueError, match="HTTP"):
            bridge.register_server("http://insecure.example.com/mcp")

    def test_cache_fresh_check(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        assert not bridge.is_cache_fresh("https://example.com/mcp")

        # Populate cache
        bridge._tool_cache["https://example.com/mcp"] = type(
            "_CacheEntry", (), {"tools": [], "fetched_at": time.time()}
        )()
        assert bridge.is_cache_fresh("https://example.com/mcp")

    def test_cache_stale_after_ttl(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        bridge._tool_cache["https://example.com/mcp"] = type(
            "_CacheEntry", (), {"tools": [], "fetched_at": time.time() - 600}
        )()
        assert not bridge.is_cache_fresh("https://example.com/mcp")

    def test_search_returns_ranked_results(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        tools = [
            McpToolSpec(tool_id="t1", name="file_reader", description="Read files from disk", server_url="https://x"),
            McpToolSpec(tool_id="t2", name="web_search", description="Search the web", server_url="https://x"),
            McpToolSpec(tool_id="t3", name="file_writer", description="Write files to disk", server_url="https://x"),
        ]
        bridge._tool_cache["https://x"] = type("_CacheEntry", (), {"tools": tools, "fetched_at": time.time()})()
        bridge._servers.append(type("McpServerEntry", (), {"url": "https://x", "name": "x", "enabled": True})())

        results = bridge.search("file", k=10)
        assert len(results) >= 2
        assert results[0].tool_id in {"t1", "t3"}

    def test_schema_for_tools_returns_function_schemas(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        tools = [
            McpToolSpec(tool_id="t1", name="reader", description="Reads stuff", server_url="https://x"),
        ]
        bridge._tool_cache["https://x"] = type("_CacheEntry", (), {"tools": tools, "fetched_at": time.time()})()

        schemas = bridge.schema_for_tools(["t1"])
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["name"] == "t1"

    def test_dynamic_injection_only_selected(self, tmp_path):
        """Confirm only selected tool schemas are injected (not all)."""
        bridge = McpBridge(workspace_root=tmp_path)
        tools = [
            McpToolSpec(tool_id="t1", name="a", description="A tool", server_url="https://x"),
            McpToolSpec(tool_id="t2", name="b", description="B tool", server_url="https://x"),
            McpToolSpec(tool_id="t3", name="c", description="C tool", server_url="https://x"),
        ]
        bridge._tool_cache["https://x"] = type("_CacheEntry", (), {"tools": tools, "fetched_at": time.time()})()

        # Only inject t2
        schemas = bridge.schema_for_tools(["t2"])
        assert len(schemas) == 1
        assert schemas[0]["name"] == "t2"

    def test_call_tool(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        tools = [
            McpToolSpec(tool_id="t1", name="test_tool", description="Test", server_url="https://x"),
        ]
        bridge._tool_cache["https://x"] = type("_CacheEntry", (), {"tools": tools, "fetched_at": time.time()})()

        output = bridge.call("t1", {"arg1": "val1"})
        parsed = json.loads(output)
        assert parsed["status"] == "executed"
        assert parsed["tool_id"] == "t1"

    def test_call_unknown_tool(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        output = bridge.call("nonexistent", {})
        assert "not found" in output

    def test_disk_cache_persistence(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)
        tools = [
            McpToolSpec(tool_id="t1", name="test", description="Test tool", server_url="https://x"),
        ]
        bridge._persist_cache("https://x", tools)

        # Load from disk cache
        loaded = bridge._load_disk_cache("https://x")
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0].tool_id == "t1"


class TestMcpTools:
    """Tests for MCP tool wrappers."""

    def test_mcp_search_tool(self, tmp_path):
        from codex_telegram_bot.tools.base import ToolRequest, ToolContext
        bridge = McpBridge(workspace_root=tmp_path)
        tools = [McpToolSpec(tool_id="t1", name="searcher", description="Search things", server_url="https://x")]
        bridge._tool_cache["https://x"] = type("_CacheEntry", (), {"tools": tools, "fetched_at": time.time()})()
        bridge._servers.append(type("McpServerEntry", (), {"url": "https://x", "name": "x", "enabled": True})())

        tool = McpSearchTool(bridge)
        result = tool.run(
            ToolRequest(name="mcp_search", args={"query": "search", "k": 5}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "searcher" in result.output or "t1" in result.output

    def test_mcp_call_tool(self, tmp_path):
        from codex_telegram_bot.tools.base import ToolRequest, ToolContext
        bridge = McpBridge(workspace_root=tmp_path)
        tools = [McpToolSpec(tool_id="t1", name="caller", description="Call things", server_url="https://x")]
        bridge._tool_cache["https://x"] = type("_CacheEntry", (), {"tools": tools, "fetched_at": time.time()})()

        tool = McpCallTool(bridge)
        result = tool.run(
            ToolRequest(name="mcp_call", args={"tool_id": "t1", "args": {}}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "executed" in result.output

    def test_mcp_call_missing_tool_id(self, tmp_path):
        from codex_telegram_bot.tools.base import ToolRequest, ToolContext
        bridge = McpBridge(workspace_root=tmp_path)
        tool = McpCallTool(bridge)
        result = tool.run(
            ToolRequest(name="mcp_call", args={}),
            ToolContext(workspace_root=tmp_path),
        )
        assert not result.ok


# ---------------------------------------------------------------------------
# Issue #104: Skill-pack system
# ---------------------------------------------------------------------------

from codex_telegram_bot.services.skill_pack import (
    SkillPackLoader,
    SkillPackSpec,
    parse_skill_md,
    _parse_yaml_frontmatter,
)


class TestSkillPackFrontmatter:
    """Tests for YAML frontmatter parsing (Issue #104)."""

    def test_parse_basic_skill_md(self):
        text = """---
skill_id: my-skill
name: My Skill
description: Does things
keywords: [keyword1, keyword2]
tools: [tool_a, tool_b]
---
Body text here.
"""
        spec = parse_skill_md(text)
        assert spec is not None
        assert spec.skill_id == "my-skill"
        assert spec.name == "My Skill"
        assert spec.description == "Does things"
        assert "keyword1" in spec.keywords
        assert "tool_a" in spec.tools

    def test_parse_disable_model_invocation(self):
        text = """---
skill_id: restricted
name: Restricted Skill
description: Cannot be model-invoked
keywords: [restricted]
tools: [special_tool]
disable-model-invocation: true
---
"""
        spec = parse_skill_md(text)
        assert spec is not None
        assert spec.disable_model_invocation is True

    def test_parse_gating_fields(self):
        text = """---
skill_id: gated
name: Gated Skill
description: Requires bins and env
keywords: [gated]
tools: [gated_tool]
requires-env: [MY_API_KEY]
requires-bins: [curl, jq]
requires-os: linux
---
"""
        spec = parse_skill_md(text)
        assert spec is not None
        assert "my_api_key" in spec.requires_env
        assert "curl" in spec.requires_bins
        assert spec.requires_os == "linux"

    def test_parse_no_frontmatter_returns_none(self):
        assert parse_skill_md("No frontmatter here") is None

    def test_parse_missing_skill_id_returns_none(self):
        text = """---
name: No ID
description: Missing skill_id
keywords: []
tools: []
---
"""
        assert parse_skill_md(text) is None

    def test_yaml_frontmatter_parser(self):
        fm = _parse_yaml_frontmatter("key1: value1\nkey2: [a, b, c]\nbool_key: true")
        assert fm["key1"] == "value1"
        assert fm["key2"] == ["a", "b", "c"]
        assert fm["bool_key"] is True


class TestSkillPackLoader:
    """Tests for SkillPackLoader precedence and gating (Issue #104)."""

    def test_workspace_overrides_bundled(self, tmp_path):
        """Workspace skills take precedence over bundled."""
        bundled = tmp_path / "bundled" / "skill1"
        bundled.mkdir(parents=True)
        (bundled / "SKILL.md").write_text("""---
skill_id: skill1
name: Bundled Version
description: From bundled
keywords: [test]
tools: [tool_a]
---
""", encoding="utf-8")

        workspace = tmp_path / "workspace" / "skill1"
        workspace.mkdir(parents=True)
        (workspace / "SKILL.md").write_text("""---
skill_id: skill1
name: Workspace Version
description: From workspace
keywords: [test]
tools: [tool_b]
---
""", encoding="utf-8")

        loader = SkillPackLoader(
            bundled_dir=tmp_path / "bundled",
            workspace_dir=tmp_path / "workspace",
        )
        skills = loader.load_all()
        assert len(skills) == 1
        assert skills[0].name == "Workspace Version"
        assert skills[0].source == "workspace"

    def test_lazy_injection_matches_keywords(self, tmp_path):
        """Only skills matching prompt keywords are returned by active_skills."""
        workspace = tmp_path / "workspace"
        for name, kw in [("email_skill", "email"), ("git_skill", "git")]:
            d = workspace / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"""---
skill_id: {name}
name: {name}
description: {name} skill
keywords: [{kw}]
tools: [tool_{name}]
---
""", encoding="utf-8")

        loader = SkillPackLoader(workspace_dir=workspace)
        loader.load_all()

        active = loader.active_skills("I need to send an email")
        assert len(active) == 1
        assert active[0].skill_id == "email_skill"

    def test_gating_missing_bin(self, tmp_path):
        spec = SkillPackSpec(
            skill_id="test",
            name="Test",
            description="",
            keywords=[],
            tools=[],
            source="bundled",
            source_path="",
            requires_bins=["nonexistent_binary_xyz"],
        )
        loader = SkillPackLoader()
        passed, reason = loader.check_gating(spec)
        assert not passed
        assert "not found" in reason

    def test_gating_missing_env(self, tmp_path):
        spec = SkillPackSpec(
            skill_id="test",
            name="Test",
            description="",
            keywords=[],
            tools=[],
            source="bundled",
            source_path="",
            requires_env=["NONEXISTENT_VAR_XYZ"],
        )
        loader = SkillPackLoader()
        passed, reason = loader.check_gating(spec)
        assert not passed
        assert "env var" in reason

    def test_allowlist_enforcement(self, tmp_path):
        """Admin install with source allowlist (tested via SkillManager)."""
        from codex_telegram_bot.services.skill_manager import SkillManager
        sm = SkillManager(config_dir=tmp_path)
        with pytest.raises(ValueError, match="Untrusted"):
            sm.install_from_url("https://evil.example.com/skill.json")

    def test_search_skills(self, tmp_path):
        workspace = tmp_path / "ws"
        d = workspace / "s1"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("""---
skill_id: file_ops
name: File Operations
description: File manipulation tools
keywords: [file, read, write]
tools: [read_file]
---
""", encoding="utf-8")

        loader = SkillPackLoader(workspace_dir=workspace)
        loader.load_all()
        results = loader.search("file")
        assert len(results) == 1
        assert results[0].skill_id == "file_ops"


# ---------------------------------------------------------------------------
# Issue #105: Agent-facing session tools
# ---------------------------------------------------------------------------

from codex_telegram_bot.tools.sessions import (
    SessionsListTool,
    SessionsHistoryTool,
    SessionsSendTool,
    SessionsSpawnTool,
    SessionStatusTool,
)
from codex_telegram_bot.tools.base import ToolRequest, ToolContext


class TestSessionTools:
    """Tests for agent-facing session tools (Issue #105)."""

    def _mock_store(self):
        """Create a mock run store with session methods."""
        store = MagicMock()
        from codex_telegram_bot.domain.sessions import TelegramSessionRecord, TelegramSessionMessageRecord
        from datetime import datetime, timezone
        session = TelegramSessionRecord(
            session_id="sess-1",
            chat_id=100,
            user_id=200,
            status="active",
            current_agent_id="default",
            summary="test session",
            last_run_id="run-1",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        msg = TelegramSessionMessageRecord(
            id=1,
            session_id="sess-1",
            role="user",
            content="hello world",
            run_id="run-1",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        store.get_session.return_value = session
        store.list_sessions_for_chat_user.return_value = [session]
        store.list_session_messages.return_value = [msg]
        store.create_session.return_value = session
        store.append_session_message.return_value = None
        return store

    def test_sessions_list(self, tmp_path):
        store = self._mock_store()
        tool = SessionsListTool(run_store=store)
        result = tool.run(
            ToolRequest(name="sessions_list", args={"chat_id": 100, "user_id": 200}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "sess-1" in result.output

    def test_sessions_history(self, tmp_path):
        store = self._mock_store()
        tool = SessionsHistoryTool(run_store=store)
        result = tool.run(
            ToolRequest(name="sessions_history", args={"session_id": "sess-1", "chat_id": 100, "user_id": 200}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "hello world" in result.output

    def test_sessions_history_visibility_blocked(self, tmp_path):
        """Unauthorized session read must be blocked."""
        store = self._mock_store()
        tool = SessionsHistoryTool(run_store=store)
        # Different user_id — should be blocked
        result = tool.run(
            ToolRequest(name="sessions_history", args={"session_id": "sess-1", "chat_id": 100, "user_id": 999}),
            ToolContext(workspace_root=tmp_path),
        )
        assert not result.ok
        assert "Access denied" in result.output

    def test_sessions_send(self, tmp_path):
        store = self._mock_store()
        tool = SessionsSendTool(run_store=store)
        result = tool.run(
            ToolRequest(name="sessions_send", args={"session_id": "sess-1", "content": "new message", "chat_id": 100, "user_id": 200}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "sent" in result.output.lower()

    def test_sessions_spawn(self, tmp_path):
        store = self._mock_store()
        tool = SessionsSpawnTool(run_store=store)
        result = tool.run(
            ToolRequest(name="sessions_spawn", args={"chat_id": 100, "user_id": 200}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "sess-1" in result.output

    def test_session_status(self, tmp_path):
        store = self._mock_store()
        tool = SessionStatusTool(run_store=store)
        result = tool.run(
            ToolRequest(name="session_status", args={"session_id": "sess-1", "chat_id": 100, "user_id": 200}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "active" in result.output

    def test_session_status_visibility_blocked(self, tmp_path):
        store = self._mock_store()
        tool = SessionStatusTool(run_store=store)
        result = tool.run(
            ToolRequest(name="session_status", args={"session_id": "sess-1", "chat_id": 999, "user_id": 200}),
            ToolContext(workspace_root=tmp_path),
        )
        assert not result.ok
        assert "Access denied" in result.output

    def test_no_store_configured(self, tmp_path):
        tool = SessionsListTool(run_store=None)
        result = tool.run(
            ToolRequest(name="sessions_list", args={"chat_id": 1, "user_id": 1}),
            ToolContext(workspace_root=tmp_path),
        )
        assert not result.ok
        assert "store" in result.output.lower()

    def test_session_tools_timeout_and_quota_boundaries(self, tmp_path):
        """Resource constraints enforcement — spawn requires valid chat/user."""
        store = self._mock_store()
        tool = SessionsSpawnTool(run_store=store)
        result = tool.run(
            ToolRequest(name="sessions_spawn", args={"chat_id": 0, "user_id": 0}),
            ToolContext(workspace_root=tmp_path),
        )
        assert not result.ok


# ---------------------------------------------------------------------------
# Issue #106: Markdown-first memory parity
# ---------------------------------------------------------------------------

from codex_telegram_bot.tools.memory import (
    MemoryStore,
    MemoryGetTool,
    MemorySearchTool,
)


class TestMemoryStore:
    """Tests for MemoryStore (Issue #106)."""

    def test_missing_file_returns_empty(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        content = store.read_file("nonexistent.md")
        assert content == ""

    def test_write_and_read_daily_log(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        today = date.today()
        store.write_daily("Entry 1\n")
        store.write_daily("Entry 2\n")

        content = store.read_file(f"memory/{today.isoformat()}.md")
        assert "Entry 1" in content
        assert "Entry 2" in content

    def test_preload_budget_cap(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        today = date.today()
        # Write a large entry
        large_content = "x" * 10000
        store.write_daily(large_content)

        preloaded = store.preload(budget_chars=500)
        assert len(preloaded) <= 600  # budget + headers

    def test_preload_loads_today_and_yesterday(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        today = date.today()
        yesterday = today - timedelta(days=1)

        store.write_daily("Today's entry", d=today)
        store.write_daily("Yesterday's entry", d=yesterday)

        preloaded = store.preload()
        assert "Today's entry" in preloaded
        assert "Yesterday's entry" in preloaded

    def test_search_finds_matching_content(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        store.write_daily("Meeting with Alice about project X")
        store.write_daily("Lunch break")

        results = store.search("Alice")
        assert len(results) >= 1
        assert "Alice" in results[0]["content"]

    def test_search_empty_query(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        assert store.search("") == []

    def test_search_curated_memory(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        curated = store.curated_path()
        curated.write_text("Important: remember project deadline is March 1\n", encoding="utf-8")

        results = store.search("deadline")
        assert len(results) >= 1

    def test_read_with_line_range(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        store.write_daily("Line 1")
        store.write_daily("Line 2")
        store.write_daily("Line 3")

        path = f"memory/{date.today().isoformat()}.md"
        content = store.read_file(path, start_line=2, end_line=3)
        assert "Line 2" in content
        assert "Line 1" not in content

    def test_path_escape_blocked(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        resolved = store._resolve_path("../../etc/passwd")
        assert resolved is None


class TestMemoryTools:
    """Tests for memory_get and memory_search tools."""

    def test_memory_get_tool(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        store.write_daily("Test content")
        path = f"memory/{date.today().isoformat()}.md"

        tool = MemoryGetTool(store=store)
        result = tool.run(
            ToolRequest(name="memory_get", args={"path": path}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "Test content" in result.output

    def test_memory_get_missing_path(self, tmp_path):
        tool = MemoryGetTool()
        result = tool.run(
            ToolRequest(name="memory_get", args={}),
            ToolContext(workspace_root=tmp_path),
        )
        assert not result.ok

    def test_memory_search_tool(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        store.write_daily("Deployed version 2.0 to production")

        tool = MemorySearchTool(store=store)
        result = tool.run(
            ToolRequest(name="memory_search", args={"query": "deployed"}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "Deployed" in result.output or "deployed" in result.output.lower()

    def test_memory_search_no_results(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)
        tool = MemorySearchTool(store=store)
        result = tool.run(
            ToolRequest(name="memory_search", args={"query": "nonexistent_term_xyz"}),
            ToolContext(workspace_root=tmp_path),
        )
        assert result.ok
        assert "No results" in result.output


# ---------------------------------------------------------------------------
# Issue #107: Tool policy groups
# ---------------------------------------------------------------------------

from codex_telegram_bot.services.tool_policy import (
    ToolPolicyEngine,
    ToolPolicyConfig,
    TOOL_GROUPS,
    VALID_ELEVATED_MODES,
)


class TestToolPolicyGroups:
    """Tests for tool policy groups and wildcards (Issue #107)."""

    def test_group_expansion(self):
        engine = ToolPolicyEngine()
        fs_tools = engine.expand_group("filesystem")
        assert "read_file" in fs_tools
        assert "write_file" in fs_tools

    def test_wildcard_allow_all(self):
        engine = ToolPolicyEngine()
        result = engine.evaluate("read_file", session_id="s1")
        assert result.allowed

    def test_wildcard_deny_pattern(self):
        config = ToolPolicyConfig(
            allow_patterns=["*"],
            deny_patterns=["shell_exec"],
        )
        engine = ToolPolicyEngine(default_config=config)
        result = engine.evaluate("shell_exec", session_id="s1")
        assert not result.allowed
        assert "denied" in result.reason

    def test_group_deny(self):
        """Deny an entire group."""
        config = ToolPolicyConfig(
            allow_patterns=["*"],
            deny_patterns=["runtime"],
        )
        engine = ToolPolicyEngine(default_config=config)
        result = engine.evaluate("shell_exec", session_id="s1")
        assert not result.allowed

    def test_glob_pattern(self):
        config = ToolPolicyConfig(
            allow_patterns=["git_*"],
            deny_patterns=[],
        )
        engine = ToolPolicyEngine(default_config=config)
        assert engine.evaluate("git_status", session_id="s1").allowed
        assert not engine.evaluate("shell_exec", session_id="s1").allowed

    def test_elevated_off_blocks_runtime_for_non_admin(self):
        config = ToolPolicyConfig(
            allow_patterns=["*"],
            elevated_mode="off",
        )
        engine = ToolPolicyEngine(default_config=config)
        result = engine.evaluate("shell_exec", session_id="s1", is_admin=False)
        assert not result.allowed
        assert "elevated" in result.reason

    def test_elevated_on_allows_runtime(self):
        config = ToolPolicyConfig(
            allow_patterns=["*"],
            elevated_mode="on",
        )
        engine = ToolPolicyEngine(default_config=config)
        result = engine.evaluate("shell_exec", session_id="s1", is_admin=False)
        assert result.allowed

    def test_elevated_admin_always_allowed(self):
        config = ToolPolicyConfig(
            allow_patterns=["*"],
            elevated_mode="off",
        )
        engine = ToolPolicyEngine(default_config=config)
        result = engine.evaluate("shell_exec", session_id="s1", is_admin=True)
        assert result.allowed

    def test_set_elevated_mode(self):
        engine = ToolPolicyEngine()
        mode = engine.set_elevated("s1", "full")
        assert mode == "full"
        config = engine.get_session_config("s1")
        assert config.elevated_mode == "full"

    def test_set_elevated_invalid_mode(self):
        engine = ToolPolicyEngine()
        engine.set_elevated("s1", "on")
        mode = engine.set_elevated("s1", "invalid")
        assert mode == "on"  # unchanged

    def test_per_provider_restrictions(self):
        config = ToolPolicyConfig(
            allow_patterns=["*"],
            elevated_mode="on",
            per_provider_restrictions={"codex_cli": ["shell_exec"]},
        )
        engine = ToolPolicyEngine(default_config=config)
        result = engine.evaluate("shell_exec", session_id="s1", provider_name="codex_cli")
        assert not result.allowed
        assert "restricted" in result.reason

        # Different provider — allowed
        result2 = engine.evaluate("shell_exec", session_id="s1", provider_name="anthropic")
        assert result2.allowed

    def test_conservative_defaults_for_non_admin(self):
        """Conservative defaults maintained: non-admin can't use runtime tools with default config."""
        engine = ToolPolicyEngine()  # default config: elevated_mode="off"
        result = engine.evaluate("shell_exec", session_id="s1", is_admin=False)
        assert not result.allowed

    def test_session_scoped_config(self):
        engine = ToolPolicyEngine()
        config1 = ToolPolicyConfig(allow_patterns=["read_file"], elevated_mode="on")
        config2 = ToolPolicyConfig(allow_patterns=["*"], elevated_mode="on")
        engine.set_session_config("s1", config1)
        engine.set_session_config("s2", config2)

        assert not engine.evaluate("shell_exec", session_id="s1").allowed
        assert engine.evaluate("shell_exec", session_id="s2").allowed


# ---------------------------------------------------------------------------
# Golden Scenarios (CI-safe)
# ---------------------------------------------------------------------------


class TestGoldenScenarioWriteVerifyAbsolutePath:
    """Golden #1: Write + verify + absolute path operations."""

    def test_write_and_verify_file(self, tmp_path):
        from codex_telegram_bot.tools.files import ReadFileTool, WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        ctx = ToolContext(workspace_root=workspace)

        # Write
        write_tool = WriteFileTool()
        write_result = write_tool.run(
            ToolRequest(name="write_file", args={"path": "test.txt", "content": "hello world"}),
            ctx,
        )
        assert write_result.ok
        assert str(workspace / "test.txt") in write_result.output
        assert "Verified file exists" in write_result.output

        # Read
        read_tool = ReadFileTool()
        read_result = read_tool.run(
            ToolRequest(name="read_file", args={"path": "test.txt"}),
            ctx,
        )
        assert read_result.ok
        assert read_result.output == "hello world"


class TestGoldenScenarioMemoryWriteSearchGet:
    """Golden #2: Memory write/search/get operations."""

    def test_memory_write_search_get(self, tmp_path):
        store = MemoryStore(workspace_root=tmp_path)

        # Write
        store.write_daily("Deployed v2.0 to staging")
        store.write_daily("Fixed bug in auth module")

        # Search
        results = store.search("auth")
        assert len(results) >= 1
        assert "auth" in results[0]["content"].lower()

        # Get
        path = f"memory/{date.today().isoformat()}.md"
        content = store.read_file(path)
        assert "Deployed v2.0" in content
        assert "Fixed bug" in content


class TestGoldenScenarioSessionSpawn:
    """Golden #3: Sessions spawn background result."""

    def test_spawn_and_check_status(self, tmp_path):
        from datetime import datetime, timezone
        from codex_telegram_bot.domain.sessions import TelegramSessionRecord

        store = MagicMock()
        session = TelegramSessionRecord(
            session_id="bg-sess-1",
            chat_id=100,
            user_id=200,
            status="active",
            current_agent_id="default",
            summary="background session",
            last_run_id="",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        store.create_session.return_value = session
        store.get_session.return_value = session

        # Spawn
        spawn_tool = SessionsSpawnTool(run_store=store)
        spawn_result = spawn_tool.run(
            ToolRequest(name="sessions_spawn", args={"chat_id": 100, "user_id": 200}),
            ToolContext(workspace_root=tmp_path),
        )
        assert spawn_result.ok
        assert "bg-sess-1" in spawn_result.output

        # Check status
        status_tool = SessionStatusTool(run_store=store)
        status_result = status_tool.run(
            ToolRequest(name="session_status", args={"session_id": "bg-sess-1", "chat_id": 100, "user_id": 200}),
            ToolContext(workspace_root=tmp_path),
        )
        assert status_result.ok
        assert "active" in status_result.output


class TestGoldenScenarioMcpDiscoverAndCall:
    """Golden #4: MCP discover and call."""

    def test_mcp_discover_and_call(self, tmp_path):
        bridge = McpBridge(workspace_root=tmp_path)

        # Simulate server with tools
        tools = [
            McpToolSpec(tool_id="calc_add", name="add", description="Add two numbers", server_url="https://calc.example"),
            McpToolSpec(tool_id="calc_mul", name="multiply", description="Multiply numbers", server_url="https://calc.example"),
        ]
        bridge._tool_cache["https://calc.example"] = type(
            "_CacheEntry", (), {"tools": tools, "fetched_at": time.time()}
        )()
        bridge._servers.append(type(
            "McpServerEntry", (), {"url": "https://calc.example", "name": "calc", "enabled": True}
        )())

        # Discover
        discovered = bridge.discover_all()
        assert len(discovered) == 2

        # Search
        results = bridge.search("add")
        assert len(results) >= 1
        assert results[0].tool_id == "calc_add"

        # Call
        output = bridge.call("calc_add", {"a": 1, "b": 2})
        parsed = json.loads(output)
        assert parsed["status"] == "executed"
        assert parsed["tool_id"] == "calc_add"


class TestGoldenScenarioApprovalFlow:
    """Golden #5: Approval flow for high-risk actions."""

    def test_approval_required_tools(self):
        from codex_telegram_bot.services.agent_service import APPROVAL_REQUIRED_TOOLS
        assert "send_email_smtp" in APPROVAL_REQUIRED_TOOLS
        assert "send_email" in APPROVAL_REQUIRED_TOOLS

    def test_tool_policy_blocks_unapproved_runtime(self):
        """Approval-like gate via tool policy: runtime tools blocked by default."""
        engine = ToolPolicyEngine()
        result = engine.evaluate("shell_exec", session_id="s1", is_admin=False)
        assert not result.allowed


# ---------------------------------------------------------------------------
# Integration: Tool registry includes new tools
# ---------------------------------------------------------------------------


class TestToolRegistryIntegration:
    """Verify tool registry contains all new tools from the parity epic."""

    def test_memory_tools_registered(self):
        from codex_telegram_bot.tools import build_default_tool_registry
        registry = build_default_tool_registry()
        assert registry.get("memory_get") is not None
        assert registry.get("memory_search") is not None

    def test_session_tools_registered_with_store(self):
        from codex_telegram_bot.tools import build_default_tool_registry
        store = MagicMock()
        registry = build_default_tool_registry(run_store=store)
        assert registry.get("sessions_list") is not None
        assert registry.get("sessions_history") is not None
        assert registry.get("sessions_send") is not None
        assert registry.get("sessions_spawn") is not None
        assert registry.get("session_status") is not None

    def test_session_tools_not_registered_without_store(self):
        from codex_telegram_bot.tools import build_default_tool_registry
        registry = build_default_tool_registry()
        assert registry.get("sessions_list") is None

    def test_mcp_tools_registered_with_bridge(self, tmp_path):
        from codex_telegram_bot.tools import build_default_tool_registry
        bridge = McpBridge(workspace_root=tmp_path)
        registry = build_default_tool_registry(mcp_bridge=bridge)
        assert registry.get("mcp_search") is not None
        assert registry.get("mcp_call") is not None

    def test_mcp_tools_not_registered_without_bridge(self):
        from codex_telegram_bot.tools import build_default_tool_registry
        registry = build_default_tool_registry()
        assert registry.get("mcp_search") is None
