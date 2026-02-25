import unittest

from codex_telegram_bot.telegram_bot import (
    _COMMAND_HANDLERS,
    _build_command_registry,
    _is_valid_command_name,
    _sanitize_command_name,
    _validate_command_registry,
    _parse_contact_spec,
    _parse_email_check_spec,
    _parse_email_command_spec,
    _parse_email_template_spec,
    _parse_gh_command_spec,
    _parse_template_spec,
)


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


class TestEmailOpsParsers(unittest.TestCase):
    def test_parse_email_check(self):
        spec, err = _parse_email_check_spec(["user@example.com"])
        self.assertEqual(err, "")
        self.assertEqual(spec["name"], "email_validate")

    def test_parse_contact_add(self):
        spec, err = _parse_contact_spec(["add", "user@example.com", "John", "Doe"])
        self.assertEqual(err, "")
        self.assertEqual(spec["name"], "contact_upsert")

    def test_parse_template_save(self):
        spec, err = _parse_template_spec(["save", "welcome", "|", "Hello", "|", "Body"])
        self.assertEqual(err, "")
        self.assertEqual(spec["name"], "template_upsert")

    def test_parse_email_template(self):
        spec, err = _parse_email_template_spec(["--dry-run", "welcome", "user@example.com"])
        self.assertEqual(err, "")
        self.assertEqual(spec["name"], "send_email_template")
        self.assertTrue(spec["args"]["dry_run"])


class TestCommandRegistryValidation(unittest.TestCase):
    def test_all_registered_commands_are_valid(self):
        for command_name, _handler_name in _COMMAND_HANDLERS:
            self.assertTrue(_is_valid_command_name(command_name), command_name)

    def test_registry_rejects_hyphenated_command(self):
        with self.assertRaises(RuntimeError):
            _validate_command_registry([("email-check", object())])

    def test_sanitize_command_name_normalizes_hyphen(self):
        self.assertEqual(_sanitize_command_name("email-check"), "email_check")

    def test_build_registry_normalizes_and_skips_broken_entries(self):
        registry = _build_command_registry()
        self.assertTrue(any(name == "email_check" for name, _ in registry))
