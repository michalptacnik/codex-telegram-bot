import argparse
import asyncio
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from codex_telegram_bot.app_container import build_agent_service
from .config import (
    DEFAULT_CONFIG_DIR,
    apply_env_defaults,
    load_config,
    load_env_with_fallback,
    purge_env,
    reinstall_env,
    get_env_path,
    get_env_value,
    TOKEN_KEY,
    ALLOWLIST_KEY,
    parse_allowlist,
)
from .telegram_bot import build_application
from codex_telegram_bot.services.process_singleton import ProcessSingletonLockError, hold_process_singleton
from .util import redact


def _preflight_codex_cli() -> None:
    """Warn early if the codex CLI is missing or unusable.

    The bot will still start so operators can open the Control Center and
    use ``GET /api/onboarding/readiness`` for guided remediation, but every
    prompt will fail until codex is installed and reachable in PATH.
    """
    if not shutil.which("codex"):
        print(
            "\n"
            "  ┌──────────────────────────────────────────────────────────┐\n"
            "  │  WARNING: 'codex' CLI not found in PATH                  │\n"
            "  │                                                           │\n"
            "  │  The bot will start, but every prompt will return an      │\n"
            "  │  error until codex is installed and available in PATH.    │\n"
            "  │                                                           │\n"
            "  │  Install:  npm install -g @openai/codex                   │\n"
            "  │  Verify:   codex --version                                │\n"
            "  │  Diagnose: GET /api/onboarding/readiness (Control Center) │\n"
            "  └──────────────────────────────────────────────────────────┘\n",
            file=sys.stderr,
        )
        return
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            print(
                f"\n  WARNING: 'codex --version' exited with code {result.returncode}.\n"
                f"  The CLI may not work correctly. Run 'codex --version' to diagnose.\n",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"\n  WARNING: Could not run 'codex --version': {exc}\n", file=sys.stderr)


def _configure_logging(level: str) -> None:
    level = (level or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _restart_self() -> None:
    os.execv(sys.executable, [sys.executable, "-m", "codex_telegram_bot.cli"] + sys.argv[1:])


def _print_config(config_dir: Path) -> None:
    env_file = load_env_with_fallback(config_dir)
    token = get_env_value(TOKEN_KEY, env_file)
    allowlist_raw = get_env_value(ALLOWLIST_KEY, env_file)
    allowlist = parse_allowlist(allowlist_raw)

    print(f"Config dir: {config_dir}")
    print(f"Env file: {get_env_path(config_dir)}")
    print(f"Token present: {'yes' if token else 'no'}")
    print(f"Allowlist active: {'yes' if allowlist else 'no'}")
    if allowlist:
        print(f"Allowlist count: {len(allowlist)}")


def _build_cron_agent(config_dir: Path, agent_service=None):
    """Build a CronHeartbeatAgent with environment-driven configuration."""
    from codex_telegram_bot.services.cron_agent import CronHeartbeatAgent, CronAgentConfig

    workspace_root = Path(
        (os.environ.get("EXECUTION_WORKSPACE_ROOT") or "").strip() or str(Path.cwd())
    ).expanduser().resolve()

    health_file_raw = (os.environ.get("CRON_AGENT_HEALTH_FILE") or "").strip()
    health_file = Path(health_file_raw).expanduser().resolve() if health_file_raw else None

    cfg = CronAgentConfig(
        workspace_root=workspace_root,
        health_file=health_file,
    )

    delivery_fn = None
    if agent_service is not None:
        # Build a delivery function that fans out to all active sessions
        async def _deliver_to_all_sessions(payload: dict) -> dict:
            """Deliver proactive messages to all registered transports via ProactiveMessenger."""
            messenger = getattr(agent_service, "_proactive_messenger", None)
            if messenger is None:
                return {"attempted": [], "delivered": [], "failed": {}}
            # Get active sessions to determine chat_ids
            sessions = agent_service.list_recent_sessions(limit=20)
            results = []
            seen_chats = set()
            for session in sessions:
                chat_id = getattr(session, "chat_id", 0)
                if not chat_id or chat_id in seen_chats:
                    continue
                seen_chats.add(chat_id)
                enriched = dict(payload)
                enriched["chat_id"] = chat_id
                result = await messenger.deliver(enriched)
                results.append(result)
            return {"sessions_notified": len(seen_chats), "results": results}

        delivery_fn = _deliver_to_all_sessions

    return CronHeartbeatAgent(config=cfg, delivery_fn=delivery_fn)


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex Telegram CLI bot")
    parser.add_argument(
        "--config-dir",
        default=str(DEFAULT_CONFIG_DIR),
        help="Directory to store .env config (default: ~/.config/codex-telegram-bot)",
    )
    parser.add_argument("--print-config", action="store_true", help="Print active config summary")
    parser.add_argument("--reinstall", action="store_true", help="Clear token and re-onboard")
    parser.add_argument("--purge", action="store_true", help="Delete .env and re-onboard")
    parser.add_argument("--restart", action="store_true", help="Restart the bot process")
    parser.add_argument(
        "--control-center",
        action="store_true",
        help="Run local Control Center web UI instead of Telegram polling mode",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run only the background agent daemon (heartbeat, cron, watchers) without Telegram",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Control Center bind host")
    parser.add_argument("--port", type=int, default=8765, help="Control Center bind port")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))

    args = parser.parse_args()
    config_dir = Path(args.config_dir).expanduser().resolve()
    env_file = load_env_with_fallback(config_dir)
    apply_env_defaults(env_file)

    if "--log-level" not in sys.argv and (os.environ.get("LOG_LEVEL") or "").strip():
        args.log_level = os.environ.get("LOG_LEVEL", args.log_level)

    _configure_logging(args.log_level)

    if args.print_config:
        _print_config(config_dir)
        return

    if args.purge:
        purge_env(config_dir)
        print("Purged .env. Restart the bot to re-onboard.", file=sys.stderr)
        return

    if args.reinstall:
        reinstall_env(config_dir)
        print("Cleared token. Restarting for re-onboarding.", file=sys.stderr)
        _restart_self()

    if args.restart:
        print("Restarting bot.", file=sys.stderr)
        _restart_self()

    # Preflight: warn if codex CLI is missing before entering serving mode.
    _preflight_codex_cli()

    config = load_config(config_dir)
    callbacks = {
        "reinstall_callback": lambda: reinstall_env(config_dir),
        "purge_callback": lambda: purge_env(config_dir),
        "restart_callback": _restart_self,
    }
    if args.daemon:
        lock_scope = "daemon"
    elif args.control_center:
        lock_scope = f"control-center-{args.host}-{args.port}"
    else:
        lock_scope = "telegram-polling"
    try:
        with hold_process_singleton(config_dir=config_dir, scope=lock_scope):
            state_db_path = config.config_dir / "state.db"
            agent_service = build_agent_service(state_db_path=state_db_path, config_dir=config.config_dir)

            # --daemon: standalone background agent (no Telegram)
            if args.daemon:
                cron_agent = _build_cron_agent(config_dir, agent_service=agent_service)
                print(
                    "  ┌──────────────────────────────────────────────────────────┐\n"
                    "  │  Daemon mode: background agent running                    │\n"
                    "  │  Heartbeat, cron jobs, and system watchers active.        │\n"
                    "  │  Press Ctrl+C to stop.                                    │\n"
                    "  └──────────────────────────────────────────────────────────┘",
                    file=sys.stderr,
                )
                asyncio.run(cron_agent.run())
                return

            if args.control_center:
                from codex_telegram_bot.control_center.app import create_app_with_config
                import uvicorn

                app = create_app_with_config(agent_service, config_dir=config_dir)
                uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
                return

            app = build_application(
                config.token,
                config.allowlist,
                callbacks,
                agent_service=agent_service,
            )

            # Start the CronHeartbeatAgent alongside Telegram polling
            cron_agent = _build_cron_agent(config_dir, agent_service=agent_service)
            _run_polling_with_cron_agent(app, cron_agent)
            try:
                asyncio.run(agent_service.shutdown())
            except Exception:
                logging.getLogger(__name__).exception("agent_service.shutdown failed")
    except ProcessSingletonLockError as exc:
        print(f"Startup blocked: {exc}", file=sys.stderr)
        return


def _run_polling_with_cron_agent(app, cron_agent) -> None:
    """Run Telegram polling with the CronHeartbeatAgent as a co-task."""
    original_post_init = getattr(app, "_post_init_callback", None)

    async def _post_init_with_cron(application) -> None:
        await cron_agent.start()
        application.bot_data["cron_agent"] = cron_agent

    async def _post_shutdown_with_cron(application) -> None:
        agent = application.bot_data.pop("cron_agent", None)
        if agent is not None:
            await agent.stop()

    # Register cron agent lifecycle with the Telegram application
    old_post_init = app.post_init
    old_post_shutdown = app.post_shutdown

    async def _combined_post_init(application) -> None:
        if old_post_init:
            await old_post_init(application)
        await _post_init_with_cron(application)

    async def _combined_post_shutdown(application) -> None:
        await _post_shutdown_with_cron(application)
        if old_post_shutdown:
            await old_post_shutdown(application)

    app.post_init = _combined_post_init
    app.post_shutdown = _combined_post_shutdown

    _run_polling_with_retry(app)


def _run_polling_with_retry(app) -> None:
    logger = logging.getLogger(__name__)
    max_attempts = max(1, int(os.environ.get("TELEGRAM_BOOTSTRAP_RETRIES", "5") or 5))
    attempt = 0
    while True:
        try:
            app.run_polling(close_loop=False)
            return
        except Exception as exc:
            attempt += 1
            transient = _is_transient_bootstrap_error(exc)
            if (not transient) or attempt >= max_attempts:
                raise
            delay = min(20.0, 1.0 * (2 ** max(0, attempt - 1)))
            delay = delay * (0.8 + random.random() * 0.4)
            logger.warning(
                "telegram.bootstrap.retry attempt=%s/%s delay=%.2fs kind=%s",
                attempt,
                max_attempts,
                delay,
                type(exc).__name__,
            )
            time.sleep(delay)


def _is_transient_bootstrap_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    markers = [
        "readerror",
        "connecterror",
        "timeout",
        "temporary failure",
        "name or service not known",
        "dns",
    ]
    return any(marker in text for marker in markers)


if __name__ == "__main__":
    main()
