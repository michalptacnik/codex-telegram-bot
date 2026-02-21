from codex_telegram_bot.app_container import build_agent_service

_service = build_agent_service()


async def run_codex(prompt: str) -> str:
    """Backward-compatible wrapper around the new service layer."""
    return await _service.run_prompt(prompt)


async def get_codex_version() -> str:
    """Backward-compatible wrapper around the new service layer."""
    return await _service.provider_version()
