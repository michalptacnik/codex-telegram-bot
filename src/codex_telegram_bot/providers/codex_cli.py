import logging
from typing import Any, Dict

from codex_telegram_bot.domain.contracts import ExecutionRunner, ProviderAdapter
from codex_telegram_bot.observability.structured_log import log_json
from codex_telegram_bot.util import redact

logger = logging.getLogger(__name__)

EXEC_TIMEOUT_SEC = 60
VERSION_TIMEOUT_SEC = 10


class CodexCliProvider(ProviderAdapter):
    def __init__(self, runner: ExecutionRunner):
        self._runner = runner

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
                timeout_sec=EXEC_TIMEOUT_SEC,
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
                timeout_sec=VERSION_TIMEOUT_SEC,
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
                timeout_sec=VERSION_TIMEOUT_SEC,
            )
        except Exception:
            return {
                "provider": "codex_cli",
                "status": "unhealthy",
                "reason": "version_check_exception",
            }
        if result.returncode != 0:
            return {
                "provider": "codex_cli",
                "status": "unhealthy",
                "reason": "version_check_nonzero",
                "returncode": result.returncode,
            }
        return {
            "provider": "codex_cli",
            "status": "healthy",
            "version": redact((result.stdout.strip() or "unknown")),
        }
