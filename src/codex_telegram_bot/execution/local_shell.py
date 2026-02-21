import asyncio
from typing import Sequence

from codex_telegram_bot.domain.contracts import CommandResult, ExecutionRunner


class LocalShellRunner(ExecutionRunner):
    async def run(
        self,
        argv: Sequence[str],
        stdin_text: str = "",
        timeout_sec: int = 60,
    ) -> CommandResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_text.encode()),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return CommandResult(returncode=124, stdout="", stderr="Execution timeout.")

        return CommandResult(
            returncode=proc.returncode or 0,
            stdout=stdout.decode(errors="replace") if stdout else "",
            stderr=stderr.decode(errors="replace") if stderr else "",
        )

