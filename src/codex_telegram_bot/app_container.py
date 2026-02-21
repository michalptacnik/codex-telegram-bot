import os
from pathlib import Path
from typing import Optional

from codex_telegram_bot.execution.local_shell import LocalShellRunner
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.codex_cli import CodexCliProvider
from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.providers.router import ProviderRouter, ProviderRouterConfig
from codex_telegram_bot.services.agent_service import AgentService


def build_agent_service(state_db_path: Optional[Path] = None) -> AgentService:
    runner = LocalShellRunner()
    primary = CodexCliProvider(runner=runner)

    fallback_mode = (os.environ.get("PROVIDER_FALLBACK_MODE", "none") or "none").strip().lower()
    fallback = EchoFallbackProvider() if fallback_mode == "echo" else None
    router_cfg = ProviderRouterConfig(
        retry_attempts=_read_int_env("PROVIDER_RETRY_ATTEMPTS", 1),
        failure_threshold=_read_int_env("PROVIDER_FAILURE_THRESHOLD", 2),
        recovery_sec=_read_int_env("PROVIDER_RECOVERY_SEC", 30),
    )
    provider = ProviderRouter(primary=primary, fallback=fallback, config=router_cfg)

    if state_db_path is None:
        return AgentService(provider=provider)
    run_store = SqliteRunStore(db_path=state_db_path)
    event_bus = EventBus()
    return AgentService(provider=provider, run_store=run_store, event_bus=event_bus)


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)
