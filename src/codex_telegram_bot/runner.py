import asyncio
import logging
from typing import Optional

from .util import redact

logger = logging.getLogger(__name__)

CODEX_TIMEOUT_SEC = 60
VERSION_TIMEOUT_SEC = 10


def _log_output(label: str, text: str) -> None:
    if text:
        logger.info("%s:\n%s", label, redact(text))


async def run_codex(prompt: str) -> str:
    prompt = redact(prompt)
    try:
        logger.info("Running: codex exec - --color never --skip-git-repo-check")
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "exec",
            "-",
            "--color",
            "never",
            "--skip-git-repo-check",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=CODEX_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "Execution timeout."

        out = stdout.decode(errors="replace") if stdout else ""
        err = stderr.decode(errors="replace") if stderr else ""
        _log_output("Codex stdout", out)
        _log_output("Codex stderr", err)
        if proc.returncode != 0:
            msg = f"Error: codex exited with code {proc.returncode}."
            tail = (err.strip() or out.strip())[:300]
            if tail:
                msg += f" {tail}"
            return redact(msg)
        return redact(out) if out.strip() else "(no output)"
    except FileNotFoundError:
        return "Error: codex CLI not found."
    except Exception as exc:
        logger.exception("Codex execution error: %s", exc)
        return "Error: failed to run codex."


async def get_codex_version() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=VERSION_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "unknown"
        if proc.returncode != 0:
            return "unknown"
        return redact((stdout.decode(errors="replace").strip() or "unknown"))
    except Exception:
        return "unknown"
