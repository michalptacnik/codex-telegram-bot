import os
from pathlib import Path
from typing import Optional

from codex_telegram_bot.execution.local_shell import LocalShellRunner
from codex_telegram_bot.execution.profiles import ExecutionProfileResolver
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.codex_cli import CodexCliProvider
from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.providers.router import ProviderRouter, ProviderRouterConfig
from codex_telegram_bot.services.repo_context import RepositoryContextRetriever
from codex_telegram_bot.services.agent_service import AgentService


def build_agent_service(state_db_path: Optional[Path] = None) -> AgentService:
    workspace_root = _read_workspace_root_env("EXECUTION_WORKSPACE_ROOT", Path.cwd())
    session_workspaces_root = _read_workspace_root_env(
        "SESSION_WORKSPACES_ROOT",
        workspace_root / ".session_workspaces",
    )
    repo_retriever = RepositoryContextRetriever(
        root=workspace_root,
        max_scan_files=_read_int_env("REPO_SCAN_MAX_FILES", 3000),
        max_file_bytes=_read_int_env("REPO_SCAN_MAX_FILE_BYTES", 120000),
        auto_refresh_sec=_read_int_env("REPO_INDEX_AUTO_REFRESH_SEC", 30),
    )
    runner = LocalShellRunner(profile_resolver=ExecutionProfileResolver(workspace_root))
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
        return AgentService(
            provider=provider,
            execution_runner=runner,
            repo_retriever=repo_retriever,
            session_max_messages=_read_int_env("SESSION_MAX_MESSAGES", 60),
            session_compact_keep=_read_int_env("SESSION_COMPACT_KEEP", 20),
            tool_loop_max_steps=_read_int_env("TOOL_LOOP_MAX_STEPS", 3),
            approval_ttl_sec=_read_int_env("APPROVAL_TTL_SEC", 900),
            max_pending_approvals_per_user=_read_int_env("MAX_PENDING_APPROVALS_PER_USER", 3),
            session_workspaces_root=session_workspaces_root,
        )
    run_store = SqliteRunStore(db_path=state_db_path)
    event_bus = EventBus()
    return AgentService(
        provider=provider,
        run_store=run_store,
        event_bus=event_bus,
        execution_runner=runner,
        repo_retriever=repo_retriever,
        session_max_messages=_read_int_env("SESSION_MAX_MESSAGES", 60),
        session_compact_keep=_read_int_env("SESSION_COMPACT_KEEP", 20),
        tool_loop_max_steps=_read_int_env("TOOL_LOOP_MAX_STEPS", 3),
        approval_ttl_sec=_read_int_env("APPROVAL_TTL_SEC", 900),
        max_pending_approvals_per_user=_read_int_env("MAX_PENDING_APPROVALS_PER_USER", 3),
        session_workspaces_root=session_workspaces_root,
    )


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def _read_workspace_root_env(name: str, default: Path) -> Path:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default.expanduser().resolve()
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return default.expanduser().resolve()
