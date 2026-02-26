import os
from pathlib import Path
from typing import Optional

from codex_telegram_bot.agent_core.capabilities import MarkdownCapabilityRegistry
from codex_telegram_bot.agent_core.memory import resolve_memory_config
from codex_telegram_bot.execution.local_shell import LocalShellRunner
from codex_telegram_bot.execution.profiles import ExecutionProfileResolver
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.codex_cli import CodexCliProvider
from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.providers.registry import ProviderRegistry
from codex_telegram_bot.providers.responses_api import ResponsesApiProvider
from codex_telegram_bot.providers.router import ProviderRouter, ProviderRouterConfig
from codex_telegram_bot.services.access_control import AccessController
from codex_telegram_bot.services.capability_router import CapabilityRouter
from codex_telegram_bot.services.probe_loop import ProbeLoop
from codex_telegram_bot.services.repo_context import RepositoryContextRetriever
from codex_telegram_bot.services.session_retention import SessionRetentionPolicy
from codex_telegram_bot.services.workspace_manager import WorkspaceManager
from codex_telegram_bot.services.agent_service import AgentService
from codex_telegram_bot.tools import build_default_tool_registry


def build_agent_service(state_db_path: Optional[Path] = None) -> AgentService:
    workspace_root = _read_workspace_root_env("EXECUTION_WORKSPACE_ROOT", Path.cwd())
    capabilities_root = _read_workspace_root_env("CAPABILITIES_DIR", workspace_root / "capabilities")
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
    provider_backend = _read_provider_backend()
    primary = _build_primary_provider(provider_backend, runner)
    provider_registry = ProviderRegistry()
    _register_providers(provider_registry, provider_backend, primary)
    capability_router = CapabilityRouter(provider_registry)

    fallback_mode = (os.environ.get("PROVIDER_FALLBACK_MODE", "none") or "none").strip().lower()
    fallback = EchoFallbackProvider() if fallback_mode == "echo" else None
    router_cfg = ProviderRouterConfig(
        retry_attempts=_read_int_env("PROVIDER_RETRY_ATTEMPTS", 1),
        failure_threshold=_read_int_env("PROVIDER_FAILURE_THRESHOLD", 2),
        recovery_sec=_read_int_env("PROVIDER_RECOVERY_SEC", 30),
    )
    provider = ProviderRouter(primary=provider_registry, fallback=fallback, config=router_cfg)
    memory_cfg = resolve_memory_config(_read_int_env)
    capability_registry = MarkdownCapabilityRegistry(capabilities_root)

    workspace_manager = WorkspaceManager(
        root=session_workspaces_root,
        max_disk_bytes=_read_int_env("WORKSPACE_MAX_DISK_BYTES", 100 * 1024 * 1024),
        max_file_count=_read_int_env("WORKSPACE_MAX_FILE_COUNT", 5000),
    )
    access_controller = AccessController()

    # Build the shared tool registry (email tool included only when ENABLE_EMAIL_TOOL=true).
    tool_registry = build_default_tool_registry(provider_registry=provider_registry)

    # Wire up the ProbeLoop when ENABLE_PROBE_LOOP=true.
    probe_loop: Optional[ProbeLoop] = None
    if (os.environ.get("ENABLE_PROBE_LOOP", "").strip().lower() == "true"):
        probe_loop = ProbeLoop(provider=provider, tool_registry=tool_registry)

    common_kwargs = dict(
        provider=provider,
        execution_runner=runner,
        repo_retriever=repo_retriever,
        session_max_messages=memory_cfg.max_messages,
        session_compact_keep=memory_cfg.keep_recent_messages,
        tool_loop_max_steps=_read_int_env("TOOL_LOOP_MAX_STEPS", 3),
        approval_ttl_sec=_read_int_env("APPROVAL_TTL_SEC", 900),
        max_pending_approvals_per_user=_read_int_env("MAX_PENDING_APPROVALS_PER_USER", 3),
        session_workspaces_root=session_workspaces_root,
        capability_registry=capability_registry,
        tool_registry=tool_registry,
        probe_loop=probe_loop,
        workspace_manager=workspace_manager,
        access_controller=access_controller,
        capability_router=capability_router,
    )

    if state_db_path is None:
        return AgentService(**common_kwargs)
    run_store = SqliteRunStore(db_path=state_db_path)
    event_bus = EventBus()
    retention_policy = SessionRetentionPolicy(
        store=run_store,
        archive_after_idle_days=_read_int_env("SESSION_ARCHIVE_AFTER_IDLE_DAYS", 30),
        delete_after_days=_read_int_env("SESSION_DELETE_AFTER_DAYS", 90),
    )
    return AgentService(
        **common_kwargs,
        run_store=run_store,
        event_bus=event_bus,
        retention_policy=retention_policy,
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


def _build_primary_provider(backend: str, runner: LocalShellRunner):
    """Instantiate the primary provider based on PROVIDER_BACKEND."""
    if backend == "codex-cli":
        return CodexCliProvider(runner=runner)
    if backend == "responses-api":
        return ResponsesApiProvider()
    if backend == "codex-exec-fallback":
        # Alias for codex-cli kept for compatibility
        return CodexCliProvider(runner=runner)
    raise ValueError(
        f"Unsupported PROVIDER_BACKEND: '{backend}'. "
        "Supported values: codex-cli, responses-api, codex-exec-fallback."
    )


def _register_providers(registry: ProviderRegistry, backend: str, primary) -> None:
    """Register the primary provider and optional secondary providers."""
    if backend == "responses-api":
        registry.register("responses_api", primary, make_active=True)
        # Also register codex_cli as a secondary (if codex is in PATH)
        try:
            from codex_telegram_bot.execution.local_shell import LocalShellRunner as _LSR
            from codex_telegram_bot.execution.profiles import ExecutionProfileResolver as _EPR
            _runner = _LSR(profile_resolver=_EPR(Path.cwd()))
            cli_provider = CodexCliProvider(runner=_runner)
            registry.register("codex_cli", cli_provider, make_active=False)
        except Exception:
            pass
    else:
        registry.register("codex_cli", primary, make_active=True)


def _read_provider_backend() -> str:
    raw = (os.environ.get("PROVIDER_BACKEND") or "codex-cli").strip().lower()
    aliases = {
        "codex_cli": "codex-cli",
        "codex": "codex-cli",
        "responses_api": "responses-api",
        "openai": "responses-api",
        "codex_exec_fallback": "codex-exec-fallback",
    }
    return aliases.get(raw, raw)
