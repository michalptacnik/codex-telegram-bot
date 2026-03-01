import os
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from codex_telegram_bot.agent_core.capabilities import MarkdownCapabilityRegistry
from codex_telegram_bot.agent_core.memory import resolve_memory_config
from codex_telegram_bot.domain.contracts import ExecutionRunner
from codex_telegram_bot.execution.docker_sandbox import DockerSandboxRunner
from codex_telegram_bot.execution.local_shell import LocalShellRunner
from codex_telegram_bot.execution.process_manager import ProcessManager
from codex_telegram_bot.execution.profiles import ExecutionProfileResolver
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.anthropic_provider import AnthropicProvider
from codex_telegram_bot.providers.codex_cli import CodexCliProvider
from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.providers.gemini_provider import GeminiProvider
from codex_telegram_bot.providers.openai_compatible import OpenAICompatibleProvider
from codex_telegram_bot.providers.responses_api import ResponsesApiProvider
from codex_telegram_bot.providers.registry import ProviderRegistry
from codex_telegram_bot.providers.router import ProviderRouter, ProviderRouterConfig
from codex_telegram_bot.services.access_control import AccessController
from codex_telegram_bot.services.capability_router import CapabilityRouter
from codex_telegram_bot.services.probe_loop import ProbeLoop
from codex_telegram_bot.services.repo_context import RepositoryContextRetriever
from codex_telegram_bot.services.session_retention import SessionRetentionPolicy
from codex_telegram_bot.services.workspace_manager import WorkspaceManager
from codex_telegram_bot.services.agent_service import AgentService
from codex_telegram_bot.services.mcp_bridge import McpBridge, _mcp_enabled
from codex_telegram_bot.services.skill_manager import SkillManager
from codex_telegram_bot.services.skill_pack import SkillPackLoader
from codex_telegram_bot.services.tool_policy import ToolPolicyEngine
from codex_telegram_bot.services.toolchain import agent_toolchain_status
from codex_telegram_bot.tools import build_default_tool_registry


logger = logging.getLogger(__name__)


def build_agent_service(state_db_path: Optional[Path] = None, config_dir: Optional[Path] = None) -> AgentService:
    _log_agent_toolchain_status()
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
    provider_runner = LocalShellRunner(profile_resolver=ExecutionProfileResolver(workspace_root))
    execution_runner = _build_execution_runner(workspace_root=workspace_root)
    provider_backend = _read_provider_backend()
    provider_registry = _build_provider_registry(runner=provider_runner, preferred_backend=provider_backend)
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
    resolved_config_dir = (
        config_dir.expanduser().resolve()
        if config_dir is not None
        else ((state_db_path.parent if state_db_path else (Path.home() / ".config" / "codex-telegram-bot")).expanduser().resolve())
    )
    skill_manager = SkillManager(config_dir=resolved_config_dir)

    # MCP bridge (Issue #103)
    mcp_bridge: Optional[McpBridge] = None
    if _mcp_enabled():
        mcp_bridge = McpBridge(workspace_root=workspace_root)

    # Skill pack loader (Issue #104)
    skill_pack_loader = SkillPackLoader(
        bundled_dir=workspace_root / "skills" / "bundled",
        global_dir=resolved_config_dir / "skills" / "packs",
        workspace_dir=workspace_root / ".skills",
    )

    # Tool policy engine (Issue #107)
    tool_policy_engine = ToolPolicyEngine()

    if state_db_path is not None:
        logger.info("state_db_path=%s", str(state_db_path.expanduser().resolve()))
    run_store = SqliteRunStore(db_path=state_db_path) if state_db_path is not None else None
    process_manager = ProcessManager(run_store=run_store)

    # Build tool registry with optional run_store/process manager (db-backed path).
    tool_registry = build_default_tool_registry(
        provider_registry=provider_registry,
        run_store=run_store,
        mcp_bridge=mcp_bridge,
        process_manager=process_manager,
    )
    probe_loop: Optional[ProbeLoop] = None
    if (os.environ.get("ENABLE_PROBE_LOOP") or "").strip().lower() in {"1", "true", "yes", "on"}:
        probe_loop = ProbeLoop(provider=provider, tool_registry=tool_registry)

    approval_ttl_sec = _read_int_env("APPROVAL_TTL_SEC", 900)
    common_kwargs = dict(
        provider=provider,
        execution_runner=execution_runner,
        repo_retriever=repo_retriever,
        session_max_messages=memory_cfg.max_messages,
        session_compact_keep=memory_cfg.keep_recent_messages,
        tool_loop_max_steps=_read_int_env("TOOL_LOOP_MAX_STEPS", 3),
        approval_ttl_sec=approval_ttl_sec,
        max_pending_approvals_per_user=_read_int_env("MAX_PENDING_APPROVALS_PER_USER", 3),
        session_workspaces_root=session_workspaces_root,
        provider_registry=provider_registry,
        capability_registry=capability_registry,
        tool_registry=tool_registry,
        probe_loop=probe_loop,
        workspace_manager=workspace_manager,
        access_controller=access_controller,
        capability_router=capability_router,
        skill_manager=skill_manager,
        mcp_bridge=mcp_bridge,
        skill_pack_loader=skill_pack_loader,
        tool_policy_engine=tool_policy_engine,
        process_manager=process_manager,
    )

    if run_store is None:
        return AgentService(**common_kwargs)
    run_store.recover_interrupted_runs()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(60, approval_ttl_sec))
    run_store.expire_tool_approvals_before(cutoff.isoformat())
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


def _log_agent_toolchain_status() -> None:
    status = agent_toolchain_status()
    missing = [str(x) for x in list(status.get("missing") or [])]
    if not missing:
        return
    hints = [str(x) for x in list(status.get("missing_packages_hint") or [])]
    logger.warning("Agent toolchain missing commands: %s", ", ".join(missing))
    if hints:
        logger.warning(
            "Install missing packages (Ubuntu): sudo apt-get install -y %s",
            " ".join(hints),
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


def _read_provider_backend() -> str:
    raw = (os.environ.get("PROVIDER_BACKEND") or "codex-cli").strip().lower()
    aliases = {
        "codex_cli": "codex-cli",
        "codex": "codex-cli",
        "codex_exec_fallback": "codex-exec-fallback",
        "responses_api": "responses-api",
        "quen": "qwen",
        "qwen-openai": "qwen",
        "deepseek-openai": "deepseek",
    }
    return aliases.get(raw, raw)


def _build_provider_registry(runner: ExecutionRunner, preferred_backend: str) -> ProviderRegistry:
    active = _registry_name_for_backend(preferred_backend)
    extra_specs = _read_extra_openai_compatible_specs()
    known_names = {
        "codex_cli",
        "anthropic",
        "openai",
        "deepseek",
        "qwen",
        "gemini",
        "responses_api",
        *[spec["name"] for spec in extra_specs],
    }
    default_name = active if active in known_names else "codex_cli"
    registry = ProviderRegistry(default_provider_name=default_name)
    registry.register("codex_cli", CodexCliProvider(runner=runner), make_active=(default_name == "codex_cli"))
    registry.register("anthropic", AnthropicProvider(), make_active=(default_name == "anthropic"))
    registry.register(
        "openai",
        OpenAICompatibleProvider(
            provider_name="openai",
            api_key_env="OPENAI_API_KEY",
            default_base_url="https://api.openai.com/v1",
            default_model="gpt-4.1-mini",
        ),
        make_active=(default_name == "openai"),
    )
    registry.register(
        "deepseek",
        OpenAICompatibleProvider(
            provider_name="deepseek",
            api_key_env="DEEPSEEK_API_KEY",
            default_base_url="https://api.deepseek.com/v1",
            default_model="deepseek-chat",
        ),
        make_active=(default_name == "deepseek"),
    )
    registry.register(
        "qwen",
        OpenAICompatibleProvider(
            provider_name="qwen",
            api_key_env="QWEN_API_KEY",
            default_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            default_model="qwen-plus",
            api_key=os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or "",
        ),
        make_active=(default_name == "qwen"),
    )
    registry.register("gemini", GeminiProvider(), make_active=(default_name == "gemini"))
    registry.register("responses_api", ResponsesApiProvider(), make_active=(default_name == "responses_api"))
    for spec in extra_specs:
        name = spec["name"]
        registry.register(
            name,
            OpenAICompatibleProvider(
                provider_name=name,
                api_key_env=spec["api_key_env"],
                default_base_url=spec["base_url"],
                default_model=spec["model"],
                model_env=spec["model_env"],
                base_url_env=spec["base_url_env"],
                timeout_env=spec["timeout_env"],
            ),
            make_active=(default_name == name),
        )
    return registry


def _build_execution_runner(workspace_root: Path) -> ExecutionRunner:
    resolver = ExecutionProfileResolver(workspace_root)
    backend = (os.environ.get("EXECUTION_BACKEND") or "local").strip().lower()
    if backend in {"docker", "docker-sandbox", "sandbox"}:
        logger.info("Execution backend: docker sandbox")
        return DockerSandboxRunner(profile_resolver=resolver)
    logger.info("Execution backend: local shell")
    return LocalShellRunner(profile_resolver=resolver)


def _registry_name_for_backend(value: str) -> str:
    if value in {"codex-cli", "codex-exec-fallback"}:
        return "codex_cli"
    if value == "responses-api":
        return "responses_api"
    return (value or "").strip().lower().replace("-", "_")


def _read_extra_openai_compatible_specs() -> List[Dict[str, str]]:
    raw = (os.environ.get("OPENAI_COMPATIBLE_PROVIDERS") or "").strip()
    if not raw:
        return []
    names: List[str] = []
    for chunk in raw.split(","):
        name = _normalize_provider_name(chunk)
        if not name or name in names:
            continue
        names.append(name)
    specs: List[Dict[str, str]] = []
    for name in names:
        env_prefix = name.upper()
        model_env = f"{env_prefix}_MODEL"
        base_url_env = f"{env_prefix}_BASE_URL"
        timeout_env = f"{env_prefix}_TIMEOUT_SEC"
        specs.append(
            {
                "name": name,
                "api_key_env": f"{env_prefix}_API_KEY",
                "model_env": model_env,
                "base_url_env": base_url_env,
                "timeout_env": timeout_env,
                "model": (os.environ.get(model_env) or "gpt-4.1-mini").strip(),
                "base_url": (os.environ.get(base_url_env) or "").strip().rstrip("/"),
            }
        )
    return specs


def _normalize_provider_name(value: str) -> str:
    name = (value or "").strip().lower().replace("-", "_")
    if not name:
        return ""
    clean = []
    for ch in name:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_":
            clean.append(ch)
    return "".join(clean)
