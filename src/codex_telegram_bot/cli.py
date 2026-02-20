import argparse
import logging
import os
import sys
from pathlib import Path

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

    app = build_application(config.token, config.allowlist, callbacks)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
