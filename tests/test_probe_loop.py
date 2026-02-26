"""Tests for ProbeLoop and probe-step parsing (PRODUCT BAR)."""
import asyncio
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from codex_telegram_bot.services.probe_loop import (
    ProbeLoop,
    ProbeResult,
    build_tool_catalog,
    build_tool_schemas,
    probe,
    _parse_probe_output,
    _parse_tool_directive,
    _format_tool_results,
)
from codex_telegram_bot.tools.base import ToolContext, ToolRegistry, ToolRequest, ToolResult


# ---------------------------------------------------------------------------
# Fake tool & registry helpers
# ---------------------------------------------------------------------------


class FakeTool:
    def __init__(self, name: str, ok: bool = True, output: str = "ok"):
        self.name = name
        self._ok = ok
        self._output = output

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        return ToolResult(ok=self._ok, output=self._output)


def _make_registry(*tools) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _make_provider(responses: List[str]):
    """Create a mock ProviderAdapter that returns responses in sequence."""
    provider = MagicMock()
    provider.generate = AsyncMock(side_effect=responses)
    return provider


# ---------------------------------------------------------------------------
# build_tool_catalog
# ---------------------------------------------------------------------------


class TestBuildToolCatalog:
    def test_empty_registry(self):
        reg = _make_registry()
        assert build_tool_catalog(reg) == "(none)"

    def test_single_tool(self):
        reg = _make_registry(FakeTool("read_file"))
        assert build_tool_catalog(reg) == "read_file"

    def test_multiple_tools_joined_by_comma(self):
        reg = _make_registry(FakeTool("read_file"), FakeTool("write_file"), FakeTool("shell_exec"))
        catalog = build_tool_catalog(reg)
        assert "read_file" in catalog
        assert "write_file" in catalog

    def test_respects_budget(self):
        # Names sum to > 10 chars → should truncate
        tools = [FakeTool(f"tool_{i:03d}") for i in range(20)]
        reg = _make_registry(*tools)
        catalog = build_tool_catalog(reg, budget=20)
        assert len(catalog) <= 24  # budget + a bit for "..."


# ---------------------------------------------------------------------------
# _parse_probe_output
# ---------------------------------------------------------------------------


class TestParseProbeOutput:
    def test_no_tools_plain(self):
        result = _parse_probe_output("NO_TOOLS\nThe sky is blue.")
        assert result.kind == "NO_TOOLS"
        assert "sky" in result.answer

    def test_no_tools_extra_whitespace(self):
        result = _parse_probe_output("NO_TOOLS\n\n  Paris is the capital of France.  ")
        assert result.kind == "NO_TOOLS"
        assert "Paris" in result.answer

    def test_need_tools_valid_json(self):
        raw = 'NEED_TOOLS {"tools":["read_file","shell_exec"],"goal":"List files","max_steps":2,"done_when":"files listed"}'
        result = _parse_probe_output(raw)
        assert result.kind == "NEED_TOOLS"
        assert "read_file" in result.tools
        assert "shell_exec" in result.tools
        assert result.goal == "List files"
        assert result.max_steps == 2
        assert result.done_when == "files listed"

    def test_need_tools_malformed_json_falls_back(self):
        result = _parse_probe_output("NEED_TOOLS {bad json}")
        assert result.kind == "NO_TOOLS"  # malformed → fallback

    def test_unexpected_format_treated_as_direct_answer(self):
        result = _parse_probe_output("Here is the answer directly.")
        assert result.kind == "NO_TOOLS"
        assert "answer" in result.answer

    def test_need_tools_empty_tools_list(self):
        raw = 'NEED_TOOLS {"tools":[],"goal":"nothing","max_steps":1,"done_when":""}'
        result = _parse_probe_output(raw)
        assert result.kind == "NEED_TOOLS"
        assert result.tools == []

    def test_max_steps_zero_uses_default(self):
        # 0 is falsy → treated as "not set" → defaults to 3
        raw = 'NEED_TOOLS {"tools":["t"],"goal":"g","max_steps":0,"done_when":""}'
        result = _parse_probe_output(raw)
        assert result.max_steps == 3

    def test_max_steps_positive_preserved(self):
        raw = 'NEED_TOOLS {"tools":["t"],"goal":"g","max_steps":5,"done_when":"done"}'
        result = _parse_probe_output(raw)
        assert result.max_steps == 5


# ---------------------------------------------------------------------------
# _parse_tool_directive
# ---------------------------------------------------------------------------


class TestParseToolDirective:
    def test_valid_directive(self):
        result = _parse_tool_directive('!tool {"name": "read_file", "args": {"path": "/tmp/x"}}')
        assert result is not None
        assert result["name"] == "read_file"
        assert result["args"] == {"path": "/tmp/x"}

    def test_wrong_prefix(self):
        assert _parse_tool_directive("!exec ls") is None

    def test_malformed_json(self):
        assert _parse_tool_directive("!tool {bad}") is None

    def test_missing_name(self):
        assert _parse_tool_directive('!tool {"args": {}}') is None

    def test_non_dict_args(self):
        # args must be dict
        assert _parse_tool_directive('!tool {"name": "t", "args": [1,2]}') is None

    def test_tool_alias_accepted(self):
        result = _parse_tool_directive('!tool {"tool": "shell_exec", "args": {"cmd": "ls"}}')
        assert result is not None
        assert result["name"] == "shell_exec"


# ---------------------------------------------------------------------------
# _format_tool_results
# ---------------------------------------------------------------------------


class TestFormatToolResults:
    def test_empty(self):
        assert _format_tool_results([]) == "(none)"

    def test_ok_result(self):
        out = _format_tool_results([{"tool": "read_file", "ok": True, "output": "file contents"}])
        assert "read_file" in out
        assert "ok" in out

    def test_error_result(self):
        out = _format_tool_results([{"tool": "broken", "error": "timeout"}])
        assert "ERROR" in out
        assert "timeout" in out

    def test_failed_result(self):
        out = _format_tool_results([{"tool": "shell_exec", "ok": False, "output": "rc=1"}])
        assert "failed" in out


# ---------------------------------------------------------------------------
# probe() function
# ---------------------------------------------------------------------------


class TestProbeFunction:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_no_tools_response(self):
        provider = _make_provider(["NO_TOOLS\nThe answer is 42."])
        reg = _make_registry(FakeTool("read_file"))
        result = self._run(probe("What is 6*7?", provider, "read_file"))
        assert result.kind == "NO_TOOLS"
        assert "42" in result.answer

    def test_need_tools_response(self):
        raw = 'NEED_TOOLS {"tools":["read_file"],"goal":"read hosts","max_steps":1,"done_when":"file read"}'
        provider = _make_provider([raw])
        reg = _make_registry(FakeTool("read_file"))
        result = self._run(probe("Read /etc/hosts", provider, "read_file"))
        assert result.kind == "NEED_TOOLS"
        assert "read_file" in result.tools


# ---------------------------------------------------------------------------
# ProbeLoop.run()
# ---------------------------------------------------------------------------


class TestProbeLoopRun:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_no_tools_returns_direct_answer(self, tmp_path):
        provider = _make_provider(["NO_TOOLS\nParis is the capital of France."])
        reg = _make_registry(FakeTool("read_file"))
        loop = ProbeLoop(provider=provider, tool_registry=reg)
        result = self._run(loop.run("What is the capital of France?", workspace_root=tmp_path))
        assert result.answer == "Paris is the capital of France."
        assert result.tool_results == []

    def test_need_tools_executes_tool_and_returns_final_answer(self, tmp_path):
        probe_resp = 'NEED_TOOLS {"tools":["read_file"],"goal":"read file","max_steps":1,"done_when":"file read"}'
        tool_resp = '!tool {"name": "read_file", "args": {"path": "/tmp/test.txt"}}'
        final_resp = "DONE: The file contains 'hello world'."
        provider = _make_provider([probe_resp, tool_resp, final_resp])
        reg = _make_registry(FakeTool("read_file", ok=True, output="hello world"))
        loop = ProbeLoop(provider=provider, tool_registry=reg)
        result = self._run(loop.run("Read /tmp/test.txt", workspace_root=tmp_path))
        assert "hello world" in result.answer or result.tool_results

    def test_blocked_tool_not_in_allowed(self, tmp_path):
        """Tool returned by model is not in allowed_tools → blocked."""
        probe_resp = 'NEED_TOOLS {"tools":["shell_exec"],"goal":"run cmd","max_steps":1,"done_when":"done"}'
        # Model asks for a different tool (not in allowed)
        tool_resp = '!tool {"name": "write_file", "args": {"path": "/x", "content": "x"}}'
        final_resp = "Done: nothing ran."
        provider = _make_provider([probe_resp, tool_resp, final_resp])
        reg = _make_registry(FakeTool("shell_exec"), FakeTool("write_file"))
        loop = ProbeLoop(provider=provider, tool_registry=reg)
        result = self._run(loop.run("Run something", workspace_root=tmp_path))
        # write_file is blocked — a tool_result with error should appear
        blocked = [r for r in result.tool_results if "BLOCKED" in r.get("error", "")]
        assert len(blocked) >= 1

    def test_no_valid_tools_registered_generates_direct_answer(self, tmp_path):
        probe_resp = 'NEED_TOOLS {"tools":["nonexistent_tool"],"goal":"do thing","max_steps":1,"done_when":"done"}'
        direct_resp = "I cannot do that without tools."
        provider = _make_provider([probe_resp, direct_resp])
        reg = _make_registry()  # empty registry
        loop = ProbeLoop(provider=provider, tool_registry=reg)
        result = self._run(loop.run("Do something", workspace_root=tmp_path))
        assert result.warning != ""  # warning about no valid tools

    def test_repair_attempted_on_invalid_directive(self, tmp_path):
        """Model returns garbage first, valid directive after repair."""
        probe_resp = 'NEED_TOOLS {"tools":["read_file"],"goal":"read","max_steps":1,"done_when":"done"}'
        garbage_resp = "I would like to use the read_file tool please."  # not !tool format
        repair_resp = '!tool {"name": "read_file", "args": {"path": "/x"}}'
        final_resp = "DONE: file contents found."
        provider = _make_provider([probe_resp, garbage_resp, repair_resp, final_resp])
        reg = _make_registry(FakeTool("read_file", ok=True, output="contents"))
        loop = ProbeLoop(provider=provider, tool_registry=reg)
        result = self._run(loop.run("Read /x", workspace_root=tmp_path))
        # Should have run read_file after REPAIR
        ran = [r for r in result.tool_results if r.get("tool") == "read_file"]
        assert len(ran) >= 1

    def test_tool_catalog_uses_registry_names(self):
        reg = _make_registry(FakeTool("alpha"), FakeTool("beta"))
        provider = MagicMock()
        loop = ProbeLoop(provider=provider, tool_registry=reg)
        catalog = loop.tool_catalog()
        assert "alpha" in catalog
        assert "beta" in catalog
