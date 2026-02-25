import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_telegram_bot.services.skill_manager import SkillManager
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.email import SendEmailSmtpTool


class TestSkillManager(unittest.TestCase):
    def test_seeded_skills_and_auto_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(config_dir=Path(tmp))
            skills = mgr.list_skills()
            self.assertTrue(any(s.skill_id == "smtp_email" for s in skills))

            old = {k: os.environ.get(k) for k in ["SMTP_HOST", "SMTP_USER", "SMTP_APP_PASSWORD"]}
            os.environ["SMTP_HOST"] = "smtp.example.com"
            os.environ["SMTP_USER"] = "bot@example.com"
            os.environ["SMTP_APP_PASSWORD"] = "app-password"
            try:
                active = mgr.auto_activate("Please send an email update")
                self.assertTrue(any(s.skill_id == "smtp_email" for s in active))
                tools = mgr.tools_for_skills(active)
                self.assertIn("send_email_smtp", tools)
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_existing_registry_gets_builtin_skill_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            reg = cfg / "skills" / "registry.json"
            reg.parent.mkdir(parents=True, exist_ok=True)
            reg.write_text(
                '{"skills":{"smtp_email":{"skill_id":"smtp_email","name":"SMTP Email","description":"","keywords":[],"tools":["send_email_smtp"],"requires_env":[],"enabled":true,"source":"builtin","trusted":true}}}\n',
                encoding="utf-8",
            )
            mgr = SkillManager(config_dir=cfg)
            rows = {s.skill_id for s in mgr.list_skills()}
            self.assertIn("email_ops", rows)


class TestEmailTool(unittest.TestCase):
    def test_dry_run(self):
        tool = SendEmailSmtpTool()
        req = ToolRequest(
            name="send_email_smtp",
            args={
                "to": "user@example.com",
                "subject": "Hello",
                "body": "World",
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_user": "bot@example.com",
                "smtp_password": "secret",
                "from": "bot@example.com",
                "dry_run": True,
            },
        )
        out = tool.run(req, ToolContext(workspace_root=Path("."), policy_profile="trusted"))
        self.assertTrue(out.ok)
        self.assertIn("Would send email", out.output)

    def test_password_whitespace_is_normalized(self):
        tool = SendEmailSmtpTool()
        req = ToolRequest(
            name="send_email_smtp",
            args={
                "to": "user@example.com",
                "subject": "Hello",
                "body": "World",
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_user": "bot@example.com",
                "smtp_password": "gdqd sswj qfyg bkna",
                "from": "bot@example.com",
            },
        )
        with patch("smtplib.SMTP") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            out = tool.run(req, ToolContext(workspace_root=Path("."), policy_profile="trusted"))
            self.assertTrue(out.ok)
            smtp.login.assert_called_once_with("bot@example.com", "gdqdsswjqfygbkna")

    def test_transient_dns_error_retries_then_succeeds(self):
        tool = SendEmailSmtpTool()
        req = ToolRequest(
            name="send_email_smtp",
            args={
                "to": "user@example.com",
                "subject": "Hello",
                "body": "World",
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_user": "bot@example.com",
                "smtp_password": "secret",
                "from": "bot@example.com",
            },
        )
        with patch("smtplib.SMTP") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            smtp.starttls.side_effect = [socket.gaierror(-3, "Temporary failure in name resolution"), None]
            out = tool.run(req, ToolContext(workspace_root=Path("."), policy_profile="trusted"))
            self.assertTrue(out.ok)
            self.assertGreaterEqual(smtp.starttls.call_count, 2)
