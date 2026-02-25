import argparse
import logging
import os
import sys
from pathlib import Path

from codex_telegram_bot.app_container import build_agent_service
from .config import (
    DEFAULT_CONFIG_DIR,
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
from .util import redact


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
    parser.add_argument("--host", default="127.0.0.1", help="Control Center bind host")
    parser.add_argument("--port", type=int, default=8765, help="Control Center bind port")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))

    args = parser.parse_args()
    config_dir = Path(args.config_dir).expanduser().resolve()

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

    config = load_config(config_dir)

    callbacks = {
        "reinstall_callback": lambda: reinstall_env(config_dir),
        "purge_callback": lambda: purge_env(config_dir),
        "restart_callback": _restart_self,
    }

    state_db_path = config.config_dir / "state.db"
    agent_service = build_agent_service(state_db_path=state_db_path, config_dir=config.config_dir)

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
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
