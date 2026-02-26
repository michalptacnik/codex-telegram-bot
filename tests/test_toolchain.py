import unittest

from codex_telegram_bot.services.toolchain import agent_toolchain_status
from codex_telegram_bot.services.toolchain import apt_packages_for_missing_commands
from codex_telegram_bot.services.toolchain import find_missing_agent_toolchain_commands
from codex_telegram_bot.services.toolchain import required_agent_toolchain_commands


class TestToolchain(unittest.TestCase):
    def test_required_commands_uses_defaults(self):
        required = required_agent_toolchain_commands(env={})
        self.assertIn("python3", required)
        self.assertIn("fd|fdfind", required)

    def test_required_commands_reads_env_override(self):
        required = required_agent_toolchain_commands(env={"AGENT_TOOLCHAIN_COMMANDS": "git, rg ,git, jq"})
        self.assertEqual(required, ["git", "rg", "jq"])

    def test_missing_detection_supports_alternatives(self):
        def _which(name: str):
            if name == "fdfind":
                return "/usr/bin/fdfind"
            return None

        missing = find_missing_agent_toolchain_commands(["fd|fdfind", "git"], which=_which)
        self.assertEqual(missing, ["git"])

    def test_apt_package_mapping_for_missing(self):
        packages = apt_packages_for_missing_commands(["rg", "fd|fdfind", "ssh"])
        self.assertEqual(packages, ["ripgrep", "fd-find", "openssh-client"])

    def test_agent_toolchain_status_ready_false_when_missing(self):
        status = agent_toolchain_status(env={"AGENT_TOOLCHAIN_COMMANDS": "definitely_missing_binary"})
        self.assertFalse(status["ready"])
        self.assertEqual(status["missing"], ["definitely_missing_binary"])

