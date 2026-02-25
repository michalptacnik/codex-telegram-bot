import unittest

from codex_telegram_bot.telegram_bot import _parse_email_command_spec, _parse_gh_command_spec


class TestEmailCommandParser(unittest.TestCase):
    def test_parse_email_success(self):
        spec, err = _parse_email_command_spec(["user@example.com", "|", "Hello", "|", "Body"])
        self.assertEqual(err, "")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["name"], "send_email_smtp")
        self.assertEqual(spec["args"]["to"], "user@example.com")

    def test_parse_email_dry_run(self):
        spec, err = _parse_email_command_spec(["--dry-run", "user@example.com", "|", "S", "|", "B"])
        self.assertEqual(err, "")
        self.assertTrue(spec["args"]["dry_run"])


class TestGhCommandParser(unittest.TestCase):
    def test_parse_gh_comment(self):
        spec, err = _parse_gh_command_spec(["comment", "owner/repo", "12", "Looks", "good"])
        self.assertEqual(err, "")
        self.assertEqual(spec["name"], "github_comment")
        self.assertEqual(spec["args"]["issue"], 12)

    def test_parse_gh_create(self):
        spec, err = _parse_gh_command_spec(["create", "owner/repo", "My title", "|", "Body"])
        self.assertEqual(err, "")
        self.assertEqual(spec["name"], "github_create_issue")

    def test_parse_gh_close(self):
        spec, err = _parse_gh_command_spec(["close", "owner/repo", "14", "not_planned"])
        self.assertEqual(err, "")
        self.assertEqual(spec["name"], "github_close_issue")
