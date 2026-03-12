from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from codex_telegram_bot.services.process_singleton import hold_process_singleton


_CHILD_SNIPPET = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from codex_telegram_bot.services.process_singleton import hold_process_singleton, ProcessSingletonLockError

    cfg = Path(sys.argv[1])
    scope = sys.argv[2]
    try:
        with hold_process_singleton(config_dir=cfg, scope=scope):
            raise SystemExit(0)
    except ProcessSingletonLockError:
        raise SystemExit(41)
    """
)


class TestProcessSingleton(unittest.TestCase):
    def test_second_process_cannot_acquire_same_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            with hold_process_singleton(config_dir=config_dir, scope="telegram-polling"):
                child = subprocess.run(
                    [sys.executable, "-c", _CHILD_SNIPPET, str(config_dir), "telegram-polling"],
                    capture_output=True,
                    text=True,
                    env=dict(os.environ),
                )
            self.assertEqual(child.returncode, 41, msg=f"child stdout={child.stdout} stderr={child.stderr}")

    def test_second_process_can_acquire_different_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            with hold_process_singleton(config_dir=config_dir, scope="telegram-polling"):
                child = subprocess.run(
                    [sys.executable, "-c", _CHILD_SNIPPET, str(config_dir), "control-center-127.0.0.1-8765"],
                    capture_output=True,
                    text=True,
                    env=dict(os.environ),
                )
            self.assertEqual(child.returncode, 0, msg=f"child stdout={child.stdout} stderr={child.stderr}")

