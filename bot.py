#!/usr/bin/env python3
import argparse
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

ENV_PATH = Path(__file__).with_name('.env')
TOKEN_KEY = 'TELEGRAM_BOT_TOKEN'
ALLOWLIST_KEY = 'ALLOWLIST'
MAX_INPUT_CHARS = 6000
MAX_OUTPUT_CHARS = 3800
CODEX_TIMEOUT_SEC = 60
VERSION_TIMEOUT_SEC = 10

REDACT_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")


def redact(text: str) -> str:
    return REDACT_RE.sub("sk-REDACTED", text)


def load_env_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            data[k.strip()] = v.strip()
    except Exception as exc:
        print(f"Failed to read .env: {exc}", file=sys.stderr)
    return data


def write_env_file(path: Path, data: Dict[str, str]) -> None:
    try:
        lines = [f"{k}={v}" for k, v in data.items()]
        path.write_text("\n".join(lines) + "\n", encoding='utf-8')
    except Exception as exc:
        print(f"Failed to write .env: {exc}", file=sys.stderr)


def get_env_value(key: str, env_file: Dict[str, str]) -> Optional[str]:
    return os.environ.get(key) or env_file.get(key)


def parse_allowlist(raw: Optional[str]) -> Optional[List[int]]:
    if not raw:
        return None
    ids: List[int] = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids or None


def ensure_onboarding() -> Dict[str, str]:
    env_file = load_env_file(ENV_PATH)
    token = get_env_value(TOKEN_KEY, env_file)
    allowlist = get_env_value(ALLOWLIST_KEY, env_file)

    updated = False

    if not token:
        token = input("Enter Telegram Bot Token: ").strip()
        env_file[TOKEN_KEY] = token
        updated = True

    if allowlist is None:
        allowlist_input = input(
            "Enter allowed Telegram user ID(s), comma separated. Leave blank to allow everyone: "
        ).strip()
        if not allowlist_input:
            print(
                "WARNING: No allowlist set. Anyone who finds your bot can use your Codex CLI and burn tokens."
            )
            confirm = input("Type YES to continue: ").strip()
            if confirm != "YES":
                print("Aborted.")
                sys.exit(1)
        env_file[ALLOWLIST_KEY] = allowlist_input
        updated = True

    if updated:
        write_env_file(ENV_PATH, env_file)

    return env_file


def purge_env() -> None:
    try:
        if ENV_PATH.exists():
            ENV_PATH.unlink()
    except Exception as exc:
        print(f"Failed to purge .env: {exc}", file=sys.stderr)


def reinstall_env() -> None:
    env_file = load_env_file(ENV_PATH)
    if TOKEN_KEY in env_file:
        del env_file[TOKEN_KEY]
        write_env_file(ENV_PATH, env_file)


def restart_self() -> None:
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])


async def run_codex(prompt: str) -> str:
    prompt = redact(prompt)
    try:
        print("Running: codex exec - --color never", file=sys.stderr)
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "exec",
            "-",
            "--color",
            "never",
            "--skip-git-repo-check",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=CODEX_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "Execution timeout."

        out = stdout.decode(errors="replace") if stdout else ""
        err = stderr.decode(errors="replace") if stderr else ""
        if out:
            print("Codex stdout:", file=sys.stderr)
            print(out, file=sys.stderr)
        if err:
            print("Codex stderr:", file=sys.stderr)
            print(err, file=sys.stderr)
        if proc.returncode != 0:
            msg = f"Error: codex exited with code {proc.returncode}."
            tail = (err.strip() or out.strip())[:300]
            if tail:
                msg += f" {tail}"
            return redact(msg)
        return redact(out) if out.strip() else "(no output)"
    except FileNotFoundError:
        return "Error: codex CLI not found."
    except Exception as exc:
        print(f"Codex execution error: {exc}", file=sys.stderr)
        return "Error: failed to run codex."


async def get_codex_version() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=VERSION_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "unknown"
        if proc.returncode != 0:
            return "unknown"
        return redact((stdout.decode(errors="replace").strip() or "unknown"))
    except Exception:
        return "unknown"


def is_allowed(user_id: int, allowlist: Optional[List[int]]) -> bool:
    if allowlist is None:
        return True
    return user_id in allowlist


async def handle_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("✅")
    except Exception as exc:
        print(f"Ping handler error: {exc}", file=sys.stderr)


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("Stateless mode. Nothing to reset.")
    except Exception as exc:
        print(f"Reset handler error: {exc}", file=sys.stderr)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        version = await get_codex_version()
        cwd = os.getcwd()
        allowlist_active = "yes" if context.bot_data.get("allowlist") else "no"
        msg = f"Codex version: {version}\nCWD: {cwd}\nAllowlist active: {allowlist_active}"
        await update.message.reply_text(msg)
    except Exception as exc:
        print(f"Status handler error: {exc}", file=sys.stderr)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.message.text:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return

        text = update.message.text
        if len(text) > MAX_INPUT_CHARS:
            await update.message.reply_text("Input too long.")
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        output = await run_codex(text)
        output = output.strip() if output else ""
        if not output:
            output = "(no output)"

        for i in range(0, len(output), MAX_OUTPUT_CHARS):
            await update.message.reply_text(output[i:i + MAX_OUTPUT_CHARS])
    except Exception as exc:
        print(f"Message handler error: {exc}", file=sys.stderr)


async def handle_reinstall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.message.from_user.id if update.message and update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        reinstall_env()
        await update.message.reply_text("Reinstall scheduled. Restarting now.")
        await asyncio.sleep(0.5)
        restart_self()
    except Exception as exc:
        print(f"Reinstall handler error: {exc}", file=sys.stderr)


async def handle_purge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.message.from_user.id if update.message and update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        purge_env()
        await update.message.reply_text("Purged .env. Restarting now.")
        await asyncio.sleep(0.5)
        restart_self()
    except Exception as exc:
        print(f"Purge handler error: {exc}", file=sys.stderr)


async def handle_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.message.from_user.id if update.message and update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        await update.message.reply_text("Restarting now.")
        await asyncio.sleep(0.5)
        restart_self()
    except Exception as exc:
        print(f"Restart handler error: {exc}", file=sys.stderr)


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        print(f"Telegram error: {context.error}", file=sys.stderr)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex Telegram CLI bot")
    parser.add_argument("--reinstall", action="store_true", help="Clear token and re-onboard")
    parser.add_argument("--purge", action="store_true", help="Delete .env and re-onboard")
    parser.add_argument("--restart", action="store_true", help="Restart the bot process")
    args = parser.parse_args()

    if args.purge:
        purge_env()
        print("Purged .env. Restart the bot to re-onboard.", file=sys.stderr)
        sys.exit(0)

    if args.reinstall:
        reinstall_env()
        print("Cleared token. Restarting for re-onboarding.", file=sys.stderr)
        restart_self()

    if args.restart:
        print("Restarting bot.", file=sys.stderr)
        restart_self()

    env_file = ensure_onboarding()
    token = get_env_value(TOKEN_KEY, env_file)
    if not token:
        print("Missing TELEGRAM_BOT_TOKEN.", file=sys.stderr)
        sys.exit(1)

    allowlist_raw = get_env_value(ALLOWLIST_KEY, env_file)
    allowlist = parse_allowlist(allowlist_raw)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["allowlist"] = allowlist

    app.add_handler(CommandHandler("ping", handle_ping))
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("reinstall", handle_reinstall))
    app.add_handler(CommandHandler("purge", handle_purge))
    app.add_handler(CommandHandler("restart", handle_restart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(handle_error)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
