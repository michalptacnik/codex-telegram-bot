"""Tests for SessionMemoryFiles (PRODUCT BAR)."""
from pathlib import Path

import pytest

from codex_telegram_bot.services.session_memory_files import SessionMemoryFiles


class TestSessionMemoryFilesReadWrite:
    def test_read_facts_empty_when_no_file(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        assert mem.read_facts() == ""

    def test_write_and_read_facts(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        mem.write_facts("Project: codex-telegram-bot\nLanguage: Python")
        result = mem.read_facts()
        assert "codex-telegram-bot" in result
        assert "Python" in result

    def test_write_facts_overwrites(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        mem.write_facts("Old content")
        mem.write_facts("New content")
        assert mem.read_facts() == "New content"

    def test_read_worklog_empty_when_no_file(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        assert mem.read_worklog() == ""

    def test_append_worklog_creates_file(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        mem.append_worklog("Task completed: listed files.")
        result = mem.read_worklog()
        assert "Task completed" in result

    def test_append_worklog_accumulates(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        mem.append_worklog("Entry 1")
        mem.append_worklog("Entry 2")
        result = mem.read_worklog()
        assert "Entry 1" in result
        assert "Entry 2" in result

    def test_append_worklog_has_timestamp(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        mem.append_worklog("something")
        result = mem.read_worklog()
        assert "##" in result  # markdown heading with timestamp

    def test_facts_path_property(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        assert mem.facts_path == tmp_path / "facts.md"

    def test_worklog_path_property(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        assert mem.worklog_path == tmp_path / "worklog.md"

    def test_creates_parent_dirs(self, tmp_path):
        ws = tmp_path / "nested" / "workspace"
        mem = SessionMemoryFiles(workspace=ws)
        mem.write_facts("test")
        assert (ws / "facts.md").exists()


class TestSessionMemoryFilesInjectContext:
    def test_empty_when_no_files(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        assert mem.inject_context() == ""

    def test_facts_only(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        mem.write_facts("API_KEY=abc123\nDomain: example.com")
        ctx = mem.inject_context()
        assert "Session facts" in ctx
        assert "API_KEY" in ctx

    def test_worklog_only(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        mem.append_worklog("Deployed v1.0")
        ctx = mem.inject_context()
        assert "task log" in ctx.lower() or "worklog" in ctx.lower() or "Deployed" in ctx

    def test_both_files_combined(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        mem.write_facts("fact: something")
        mem.append_worklog("outcome: something else")
        ctx = mem.inject_context()
        assert "fact" in ctx
        assert "outcome" in ctx

    def test_inject_respects_budget(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        # Write a very large facts file
        mem.write_facts("x" * 10_000)
        ctx = mem.inject_context()
        # inject_context caps the injected content
        assert len(ctx) < 2000  # well under the raw file size

    def test_whitespace_only_files_not_injected(self, tmp_path):
        mem = SessionMemoryFiles(workspace=tmp_path)
        mem.write_facts("   \n  ")
        assert mem.inject_context() == ""
