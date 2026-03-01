import json
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
from codex_telegram_bot.services.proactive_messenger import ProactiveMessenger
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
    resolved_config_dir = (
        config_dir.expanduser().resolve()
        if config_dir is not None
        else ((state_db_path.parent if state_db_path else (Path.home() / ".config" / "codex-telegram-bot")).expanduser().resolve())
    )
    provider_runner = LocalShellRunner(profile_resolver=ExecutionProfileResolver(workspace_root))
    execution_runner = _build_execution_runner(workspace_root=workspace_root)
    provider_backend = _read_provider_backend()
    provider_specs = _load_provider_specs(workspace_root=workspace_root, config_dir=resolved_config_dir)
    provider_registry = _build_provider_registry(
        runner=provider_runner,
        preferred_backend=provider_backend,
        provider_specs=provider_specs,
    )
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

    proactive_messenger = ProactiveMessenger()

    # Build tool registry with optional run_store/process manager (db-backed path).
    tool_registry = build_default_tool_registry(
        provider_registry=provider_registry,
        run_store=run_store,
        mcp_bridge=mcp_bridge,
        process_manager=process_manager,
        access_controller=access_controller,
        proactive_messenger=proactive_messenger,
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
        proactive_messenger=proactive_messenger,
        config_dir=resolved_config_dir,
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


def _build_provider_registry(
    runner: ExecutionRunner,
    preferred_backend: str,
    provider_specs: Optional[Dict[str, Dict[str, str]]] = None,
) -> ProviderRegistry:
    specs = dict(provider_specs or _default_provider_specs())
    active = _registry_name_for_backend(preferred_backend)
    default_name = active if active in specs else (next(iter(specs.keys()), "codex_cli"))
    registry = ProviderRegistry(default_provider_name=default_name)
    registered_names: List[str] = []
    for name, spec in specs.items():
        provider = _instantiate_provider_from_spec(name=name, spec=spec, runner=runner)
        if provider is None:
            continue
        registry.register(name, provider, make_active=(name == default_name))
        registered_names.append(name)
    if not registered_names:
        registry.register("codex_cli", CodexCliProvider(runner=runner), make_active=True)
    return registry


def _load_provider_specs(workspace_root: Path, config_dir: Optional[Path]) -> Dict[str, Dict[str, str]]:
    candidates: List[Path] = []
    candidates.append((workspace_root / "providers.json").expanduser().resolve())
    home_cfg = (Path.home() / ".config" / "codex-telegram-bot" / "providers.json").expanduser().resolve()
    candidates.append(home_cfg)
    env_path = (os.environ.get("PROVIDERS_CONFIG") or "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser().resolve())
    if config_dir is not None:
        cfg_candidate = (config_dir / "providers.json").expanduser().resolve()
        if cfg_candidate not in candidates:
            candidates.insert(0, cfg_candidate)

    for path in candidates:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("providers config load failed path=%s error=%s", str(path), exc)
            continue
        if not isinstance(raw, dict):
            logger.error("providers config invalid root path=%s", str(path))
            continue
        specs: Dict[str, Dict[str, str]] = {}
        for name, spec in raw.items():
            normalized = _normalize_provider_name(str(name))
            if not normalized or not isinstance(spec, dict):
                continue
            normalized_spec = {str(k): str(v) for k, v in spec.items() if v is not None}
            specs[normalized] = normalized_spec
        if specs:
            logger.info("providers config loaded path=%s providers=%s", str(path), ",".join(sorted(specs.keys())))
            return specs
    return _default_provider_specs()


def _default_provider_specs() -> Dict[str, Dict[str, str]]:
    base: Dict[str, Dict[str, str]] = {
        "codex_cli": {"type": "codex_cli"},
        "anthropic": {"type": "anthropic"},
        "openai": {
            "type": "openai_compatible",
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
        },
        "deepseek": {
            "type": "openai_compatible",
            "api_key_env": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        },
        "qwen": {
            "type": "openai_compatible",
            "api_key_env": "QWEN_API_KEY",
            "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
        },
        "gemini": {"type": "gemini"},
        "responses_api": {"type": "responses_api"},
    }
    for extra in _read_extra_openai_compatible_specs():
        base[extra["name"]] = {
            "type": "openai_compatible",
            "api_key_env": extra["api_key_env"],
            "base_url": extra["base_url"],
            "model": extra["model"],
            "model_env": extra["model_env"],
            "base_url_env": extra["base_url_env"],
            "timeout_env": extra["timeout_env"],
        }
    return base


def _instantiate_provider_from_spec(
    name: str,
    spec: Dict[str, str],
    runner: ExecutionRunner,
):
    kind = (spec.get("type") or "").strip().lower()
    if kind == "codex_cli":
        return CodexCliProvider(runner=runner)
    if kind == "anthropic":
        return AnthropicProvider(
            model=spec.get("model") or None,
        )
    if kind == "responses_api":
        return ResponsesApiProvider(
            model=spec.get("model") or None,
            api_base=spec.get("api_base") or None,
        )
    if kind == "gemini":
        api_key_env = (spec.get("api_key_env") or "").strip()
        return GeminiProvider(
            api_key=(os.environ.get(api_key_env) if api_key_env else None),
            model=spec.get("model") or None,
        )
    if kind in {"openai_compatible", "openai-compatible"}:
        api_key_env = (spec.get("api_key_env") or "").strip()
        base_url = (spec.get("base_url") or "").strip().rstrip("/")
        model = (spec.get("model") or "").strip()
        if not api_key_env or not base_url or not model:
            logger.error(
                "provider config invalid name=%s reason=missing_required_fields type=%s",
                name,
                kind,
            )
            return None
        api_key = (os.environ.get(api_key_env) or "").strip()
        if not api_key and name == "qwen":
            api_key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
        return OpenAICompatibleProvider(
            provider_name=name,
            api_key_env=api_key_env,
            default_base_url=base_url,
            default_model=model,
            model_env=spec.get("model_env") or None,
            base_url_env=spec.get("base_url_env") or None,
            timeout_env=spec.get("timeout_env") or None,
            api_key=api_key or None,
        )
    logger.error("provider config invalid name=%s reason=unknown_type type=%s", name, kind)
    return None


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
