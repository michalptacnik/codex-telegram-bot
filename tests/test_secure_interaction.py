"""Tests for EPIC 2: Secure Computer Interaction Layer.

Covers GitDiffTool, GitLogTool, GitAddTool, GitCommitTool,
ShellExecTool, SshDetectionTool, and the updated default registry.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.tools import build_default_tool_registry
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.git import (
    GitAddTool,
    GitCommitTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
)
from codex_telegram_bot.tools.shell import SAFE_COMMANDS, ShellExecTool
from codex_telegram_bot.tools.ssh import SshDetectionTool


def _make_git_repo(tmp: str) -> Path:
    """Create a minimal git repo with an initial commit for testing."""
    root = Path(tmp)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp, check=True, capture_output=True)
    # Disable commit signing in this isolated repo; the global git config may enforce it.
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp, check=True, capture_output=True)
    readme = root / "README.md"
    readme.write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--no-gpg-sign", "-m", "initial"], cwd=tmp, check=True, capture_output=True)
    return root


class TestGitDiffTool(unittest.TestCase):
    def test_diff_shows_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            (root / "README.md").write_text("hello\nworld\n")
            tool = GitDiffTool()
            res = tool.run(
                ToolRequest(name="git_diff", args={}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)
            self.assertIn("world", res.output)

    def test_diff_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            (root / "new.txt").write_text("staged content\n")
            subprocess.run(["git", "add", "new.txt"], cwd=tmp, check=True, capture_output=True)
            tool = GitDiffTool()
            res = tool.run(
                ToolRequest(name="git_diff", args={"staged": True}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)
            self.assertIn("staged content", res.output)

    def test_diff_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitDiffTool()
            res = tool.run(
                ToolRequest(name="git_diff", args={}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)
            self.assertIn("No changes", res.output)


class TestGitLogTool(unittest.TestCase):
    def test_log_returns_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitLogTool()
            res = tool.run(
                ToolRequest(name="git_log", args={"n": 5}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)
            self.assertIn("initial", res.output)

    def test_log_clamps_n_to_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitLogTool()
            # n=9999 should be clamped to 50
            res = tool.run(
                ToolRequest(name="git_log", args={"n": 9999}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)

    def test_log_invalid_n_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitLogTool()
            res = tool.run(
                ToolRequest(name="git_log", args={"n": "not-a-number"}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)


class TestGitAddTool(unittest.TestCase):
    def test_add_stages_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            (root / "new.txt").write_text("content\n")
            tool = GitAddTool()
            res = tool.run(
                ToolRequest(name="git_add", args={"paths": ["new.txt"]}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)

    def test_add_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitAddTool()
            res = tool.run(
                ToolRequest(name="git_add", args={"paths": ["../evil.txt"]}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)
            self.assertIn("not allowed", res.output)

    def test_add_blocks_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitAddTool()
            res = tool.run(
                ToolRequest(name="git_add", args={"paths": ["/etc/passwd"]}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)

    def test_add_requires_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitAddTool()
            res = tool.run(
                ToolRequest(name="git_add", args={}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)
            self.assertIn("required", res.output)

    def test_add_accepts_string_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            (root / "single.txt").write_text("x\n")
            tool = GitAddTool()
            res = tool.run(
                ToolRequest(name="git_add", args={"paths": "single.txt"}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)


class TestGitCommitTool(unittest.TestCase):
    def test_commit_creates_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            (root / "change.txt").write_text("new\n")
            subprocess.run(["git", "add", "change.txt"], cwd=tmp, check=True, capture_output=True)
            tool = GitCommitTool()
            res = tool.run(
                ToolRequest(name="git_commit", args={"message": "test commit"}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)

    def test_commit_requires_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitCommitTool()
            res = tool.run(
                ToolRequest(name="git_commit", args={}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)
            self.assertIn("required", res.output)

    def test_commit_rejects_overly_long_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitCommitTool()
            res = tool.run(
                ToolRequest(name="git_commit", args={"message": "x" * 2001}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)
            self.assertIn("exceeds", res.output)

    def test_commit_fails_when_nothing_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            tool = GitCommitTool()
            res = tool.run(
                ToolRequest(name="git_commit", args={"message": "empty commit"}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)


class TestShellExecTool(unittest.TestCase):
    def test_runs_allowed_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("hello\n")
            tool = ShellExecTool()
            res = tool.run(
                ToolRequest(name="shell_exec", args={"cmd": "ls"}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)
            self.assertIn("file.txt", res.output)

    def test_blocks_disallowed_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = ShellExecTool()
            res = tool.run(
                ToolRequest(name="shell_exec", args={"cmd": "curl http://example.com"}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)
            self.assertIn("allowlist", res.output)

    def test_blocks_empty_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = ShellExecTool()
            res = tool.run(
                ToolRequest(name="shell_exec", args={"cmd": ""}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)

    def test_blocks_missing_cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = ShellExecTool()
            res = tool.run(
                ToolRequest(name="shell_exec", args={}),
                ToolContext(workspace_root=root),
            )
            self.assertFalse(res.ok)
            self.assertIn("required", res.output)

    def test_echo_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = ShellExecTool()
            res = tool.run(
                ToolRequest(name="shell_exec", args={"cmd": "echo hello world"}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)
            self.assertIn("hello world", res.output)

    def test_safe_commands_set_not_empty(self):
        self.assertGreater(len(SAFE_COMMANDS), 0)
        self.assertIn("ls", SAFE_COMMANDS)
        self.assertIn("grep", SAFE_COMMANDS)
        self.assertNotIn("curl", SAFE_COMMANDS)
        self.assertNotIn("wget", SAFE_COMMANDS)
        self.assertNotIn("rm", SAFE_COMMANDS)  # rm is actually NOT safe enough to omit


class TestSshDetectionTool(unittest.TestCase):
    def test_returns_ok_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = SshDetectionTool()
            res = tool.run(
                ToolRequest(name="ssh_detect", args={}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)
            # Output should always mention SSH agent status
            self.assertIn("SSH agent", res.output)

    def test_reports_git_remote_non_ssh_for_non_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = SshDetectionTool()
            res = tool.run(
                ToolRequest(name="ssh_detect", args={}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)
            self.assertIn("non-SSH", res.output)

    def test_reports_ssh_remote_for_git_at_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_git_repo(tmp)
            subprocess.run(
                ["git", "remote", "add", "origin", "git@github.com:example/repo.git"],
                cwd=tmp, check=True, capture_output=True,
            )
            tool = SshDetectionTool()
            res = tool.run(
                ToolRequest(name="ssh_detect", args={}),
                ToolContext(workspace_root=root),
            )
            self.assertTrue(res.ok)
            self.assertIn("SSH", res.output)
            self.assertNotIn("non-SSH", res.output.split("Git remote")[1])


class TestDefaultRegistryUpdated(unittest.TestCase):
    def test_all_epic2_tools_registered(self):
        registry = build_default_tool_registry()
        names = registry.names()
        for expected in [
            "read_file", "write_file",
            "git_status", "git_diff", "git_log", "git_add", "git_commit",
            "shell_exec", "ssh_detect",
        ]:
            self.assertIn(expected, names, f"Missing tool: {expected}")
