import asyncio
import logging
import os
from typing import Optional, List

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from codex_telegram_bot.app_container import build_agent_service
from codex_telegram_bot.services.agent_service import AgentService
from .util import chunk_text

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 6000
MAX_OUTPUT_CHARS = 3800


def is_allowed(user_id: int, allowlist: Optional[List[int]]) -> bool:
    if allowlist is None:
        return True
    return user_id in allowlist


async def handle_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("✅")
    except Exception as exc:
        logger.exception("Ping handler error: %s", exc)


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("Stateless mode. Nothing to reset.")
    except Exception as exc:
        logger.exception("Reset handler error: %s", exc)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        agent_service = context.bot_data.get("agent_service")
        version = await agent_service.provider_version()
        cwd = os.getcwd()
        allowlist_active = "yes" if context.bot_data.get("allowlist") else "no"
        msg = f"Codex version: {version}\nCWD: {cwd}\nAllowlist active: {allowlist_active}"
        await update.message.reply_text(msg)
    except Exception as exc:
        logger.exception("Status handler error: %s", exc)


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
        agent_service = context.bot_data.get("agent_service")
        output = await agent_service.run_prompt(text)
        output = output.strip() if output else ""
        if not output:
            output = "(no output)"

        for chunk in chunk_text(output, MAX_OUTPUT_CHARS):
            await update.message.reply_text(chunk)
    except Exception as exc:
        logger.exception("Message handler error: %s", exc)


async def handle_reinstall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.message.from_user.id if update.message and update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        context.application.bot_data.get("reinstall_callback")()
        await update.message.reply_text("Reinstall scheduled. Restarting now.")
        await asyncio.sleep(0.5)
        context.application.bot_data.get("restart_callback")()
    except Exception as exc:
        logger.exception("Reinstall handler error: %s", exc)


async def handle_purge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.message.from_user.id if update.message and update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        context.application.bot_data.get("purge_callback")()
        await update.message.reply_text("Purged .env. Restarting now.")
        await asyncio.sleep(0.5)
        context.application.bot_data.get("restart_callback")()
    except Exception as exc:
        logger.exception("Purge handler error: %s", exc)


async def handle_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.message.from_user.id if update.message and update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        await update.message.reply_text("Restarting now.")
        await asyncio.sleep(0.5)
        context.application.bot_data.get("restart_callback")()
    except Exception as exc:
        logger.exception("Restart handler error: %s", exc)


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        logger.error("Telegram error: %s", context.error)
    except Exception:
        pass


def build_application(
    token: str,
    allowlist: Optional[List[int]],
    callbacks: dict,
    agent_service: Optional[AgentService] = None,
):
    if agent_service is None:
        agent_service = build_agent_service()
    app = ApplicationBuilder().token(token).build()
    app.bot_data["allowlist"] = allowlist
    app.bot_data["agent_service"] = agent_service
    app.bot_data.update(callbacks)

    app.add_handler(CommandHandler("ping", handle_ping))
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("reinstall", handle_reinstall))
    app.add_handler(CommandHandler("purge", handle_purge))
    app.add_handler(CommandHandler("restart", handle_restart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(handle_error)
    return app
