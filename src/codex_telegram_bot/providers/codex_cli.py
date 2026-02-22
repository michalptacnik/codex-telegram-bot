import logging
import os
from typing import Any, Dict, Sequence

from codex_telegram_bot.domain.contracts import ExecutionRunner, ProviderAdapter
from codex_telegram_bot.observability.structured_log import log_json
from codex_telegram_bot.util import redact

logger = logging.getLogger(__name__)

DEFAULT_EXEC_TIMEOUT_SEC = 180
DEFAULT_VERSION_TIMEOUT_SEC = 10


def _read_timeout_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


class CodexCliProvider(ProviderAdapter):
    def __init__(
        self,
        runner: ExecutionRunner,
        exec_timeout_sec: int | None = None,
        version_timeout_sec: int | None = None,
    ):
        self._runner = runner
        self._exec_timeout_sec = exec_timeout_sec or _read_timeout_env(
            "CODEX_EXEC_TIMEOUT_SEC",
            DEFAULT_EXEC_TIMEOUT_SEC,
        )
        self._version_timeout_sec = version_timeout_sec or _read_timeout_env(
            "CODEX_VERSION_TIMEOUT_SEC",
            DEFAULT_VERSION_TIMEOUT_SEC,
        )

    async def generate(
        self,
        messages: Sequence[Dict[str, str]],
        stream: bool = False,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        prompt = _messages_to_prompt(messages)
        if stream:
            # codex-cli provider is currently non-streaming; keep contract stable.
            stream = False
        return await self.execute(
            prompt=prompt,
            correlation_id=correlation_id,
            policy_profile=policy_profile,
        )

    async def execute(
        self,
        prompt: str,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        safe_prompt = redact(prompt)
        log_json(
            logger,
            "provider.execute.start",
            provider="codex_cli",
            run_id=correlation_id,
            policy_profile=policy_profile,
        )
        try:
            result = await self._runner.run(
                ["codex", "exec", "-", "--color", "never", "--skip-git-repo-check"],
                stdin_text=safe_prompt,
                timeout_sec=self._exec_timeout_sec,
                policy_profile=policy_profile,
            )
        except FileNotFoundError:
            log_json(
                logger,
                "provider.execute.error",
                provider="codex_cli",
                run_id=correlation_id,
                kind="cli_not_found",
            )
            return "Error: codex CLI not found."
        except Exception as exc:
            logger.exception("Codex execution error: %s", exc)
            log_json(
                logger,
                "provider.execute.error",
                provider="codex_cli",
                run_id=correlation_id,
                kind="unexpected_exception",
            )
            return "Error: failed to run codex."

        if result.stdout:
            logger.info("Codex stdout:\n%s", redact(result.stdout))
        if result.stderr:
            logger.info("Codex stderr:\n%s", redact(result.stderr))

        if result.returncode != 0:
            msg = f"Error: codex exited with code {result.returncode}."
            tail = (result.stderr.strip() or result.stdout.strip())[:300]
            if tail:
                msg += f" {tail}"
            log_json(
                logger,
                "provider.execute.finish",
                provider="codex_cli",
                run_id=correlation_id,
                returncode=result.returncode,
                status="failed",
                policy_profile=policy_profile,
            )
            return redact(msg)
        log_json(
            logger,
            "provider.execute.finish",
            provider="codex_cli",
            run_id=correlation_id,
            returncode=result.returncode,
            status="completed",
            policy_profile=policy_profile,
        )
        return redact(result.stdout) if result.stdout.strip() else "(no output)"

    async def version(self) -> str:
        try:
            result = await self._runner.run(
                ["codex", "--version"],
                timeout_sec=self._version_timeout_sec,
            )
        except Exception:
            return "unknown"
        if result.returncode != 0:
            return "unknown"
        return redact((result.stdout.strip() or "unknown"))

    async def health(self) -> Dict[str, Any]:
        try:
            result = await self._runner.run(
                ["codex", "--version"],
                timeout_sec=self._version_timeout_sec,
            )
        except Exception:
            return {
                "provider": "codex_cli",
                "status": "unhealthy",
                "reason": "version_check_exception",
                "capabilities": self.capabilities(),
            }
        if result.returncode != 0:
            return {
                "provider": "codex_cli",
                "status": "unhealthy",
                "reason": "version_check_nonzero",
                "returncode": result.returncode,
                "capabilities": self.capabilities(),
            }
        return {
            "provider": "codex_cli",
            "status": "healthy",
            "version": redact((result.stdout.strip() or "unknown")),
            "capabilities": self.capabilities(),
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "provider": "codex_cli",
            "supports_tool_calls": True,
            "supports_streaming": False,
            "max_context_chars": 120_000,
            "supported_policy_profiles": ["strict", "balanced", "trusted"],
            "reliability_tier": "primary",
        }


def _messages_to_prompt(messages: Sequence[Dict[str, str]]) -> str:
    lines: list[str] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower()
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            lines.append(content)
        else:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines).strip()
