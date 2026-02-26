"""Tests for SendEmailTool with safety gating (PRODUCT BAR)."""
import smtplib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.email import SendEmailTool, is_email_tool_enabled


# ---------------------------------------------------------------------------
# is_email_tool_enabled
# ---------------------------------------------------------------------------


class TestIsEmailToolEnabled:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ENABLE_EMAIL_TOOL", raising=False)
        assert is_email_tool_enabled() is False

    def test_enabled_when_true(self, monkeypatch):
        monkeypatch.setenv("ENABLE_EMAIL_TOOL", "true")
        assert is_email_tool_enabled() is True

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ENABLE_EMAIL_TOOL", "TRUE")
        assert is_email_tool_enabled() is True

    def test_disabled_for_other_values(self, monkeypatch):
        for val in ("1", "yes", "on", "false", ""):
            monkeypatch.setenv("ENABLE_EMAIL_TOOL", val)
            assert is_email_tool_enabled() is False


# ---------------------------------------------------------------------------
# SendEmailTool.run() — gating
# ---------------------------------------------------------------------------


class TestSendEmailToolGating:
    def _ctx(self, tmp_path):
        return ToolContext(workspace_root=tmp_path)

    def test_disabled_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ENABLE_EMAIL_TOOL", raising=False)
        tool = SendEmailTool(email_from="sender@example.com")
        req = ToolRequest(name="send_email", args={"to": "a@b.com", "subject": "Hi", "body": "Hello"})
        result = tool.run(req, self._ctx(tmp_path))
        assert result.ok is False
        assert "disabled" in result.output.lower()

    def test_enabled_without_email_from_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_EMAIL_TOOL", "true")
        monkeypatch.delenv("EMAIL_FROM", raising=False)
        tool = SendEmailTool()  # no email_from
        req = ToolRequest(name="send_email", args={"to": "a@b.com", "subject": "S", "body": "B"})
        result = tool.run(req, self._ctx(tmp_path))
        assert result.ok is False
        assert "EMAIL_FROM" in result.output

    def test_missing_required_args(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_EMAIL_TOOL", "true")
        tool = SendEmailTool(email_from="s@example.com")
        # missing 'body'
        req = ToolRequest(name="send_email", args={"to": "a@b.com", "subject": "Hi"})
        result = tool.run(req, self._ctx(tmp_path))
        assert result.ok is False
        assert "body" in result.output.lower() or "required" in result.output.lower()


# ---------------------------------------------------------------------------
# SendEmailTool.run() — dry_run
# ---------------------------------------------------------------------------


class TestSendEmailToolDryRun:
    def _ctx(self, tmp_path):
        return ToolContext(workspace_root=tmp_path)

    def test_dry_run_does_not_send(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_EMAIL_TOOL", "true")
        tool = SendEmailTool(email_from="sender@example.com")
        req = ToolRequest(
            name="send_email",
            args={"to": "rx@example.com", "subject": "Test", "body": "Hello", "dry_run": True},
        )
        with patch("smtplib.SMTP") as mock_smtp:
            result = tool.run(req, self._ctx(tmp_path))
        mock_smtp.assert_not_called()
        assert result.ok is True
        assert "dry_run" in result.output.lower() or "[dry_run]" in result.output

    def test_dry_run_shows_email_details(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_EMAIL_TOOL", "true")
        tool = SendEmailTool(email_from="me@example.com")
        req = ToolRequest(
            name="send_email",
            args={
                "to": "them@example.com",
                "subject": "Hello World",
                "body": "This is a test.",
                "dry_run": True,
            },
        )
        result = tool.run(req, self._ctx(tmp_path))
        assert result.ok is True
        assert "them@example.com" in result.output
        assert "Hello World" in result.output


# ---------------------------------------------------------------------------
# SendEmailTool.run() — SMTP send
# ---------------------------------------------------------------------------


class TestSendEmailToolSmtp:
    def _ctx(self, tmp_path):
        return ToolContext(workspace_root=tmp_path)

    def test_successful_send(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_EMAIL_TOOL", "true")
        tool = SendEmailTool(
            smtp_host="smtp.example.com",
            smtp_port=587,
            email_from="bot@example.com",
        )
        req = ToolRequest(
            name="send_email",
            args={"to": "user@example.com", "subject": "Alert", "body": "Something happened."},
        )
        mock_smtp_instance = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_smtp_instance) as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp_instance)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = tool.run(req, self._ctx(tmp_path))
        assert result.ok is True
        assert "user@example.com" in result.output

    def test_smtp_failure_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_EMAIL_TOOL", "true")
        tool = SendEmailTool(email_from="bot@example.com")
        req = ToolRequest(
            name="send_email",
            args={"to": "a@b.com", "subject": "S", "body": "B"},
        )
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
            result = tool.run(req, self._ctx(tmp_path))
        assert result.ok is False
        assert "Error" in result.output


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


class TestSendEmailToolMeta:
    def test_name(self):
        assert SendEmailTool.name == "send_email"

    def test_requires_approval_flag(self):
        assert SendEmailTool.requires_approval is True

    def test_init_from_env(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "mail.example.com")
        monkeypatch.setenv("SMTP_PORT", "465")
        monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
        tool = SendEmailTool()
        assert tool._host == "mail.example.com"
        assert tool._port == 465
        assert tool._from == "noreply@example.com"
