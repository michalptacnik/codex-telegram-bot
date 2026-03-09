"""Telegram transport adapter.

Wraps ``python-telegram-bot`` polling into the channel-agnostic Transport
protocol so the agent runtime treats Telegram as one of many possible
message channels.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from codex_telegram_bot.transports.base import (
    InboundMessage,
    MessageHandler,
    OutboundMessage,
    Transport,
)

logger = logging.getLogger(__name__)

try:
    from telegram import Bot, Update
    from telegram.constants import ChatAction
    from telegram.ext import (
        ApplicationBuilder,
        ContextTypes,
        MessageHandler as TGMessageHandler,
        filters,
    )
    _PTB_AVAILABLE = True
except ImportError:
    _PTB_AVAILABLE = False


class TelegramTransport:
    """Transport implementation for Telegram using python-telegram-bot."""

    def __init__(
        self,
        token: str,
        allowlist: Optional[List[int]] = None,
    ) -> None:
        if not _PTB_AVAILABLE:
            raise ImportError("python-telegram-bot is required for TelegramTransport")
        self._token = token
        self._allowlist = set(allowlist or [])
        self._app = None
        self._handler: Optional[MessageHandler] = None
        self._running = False

    @property
    def channel_name(self) -> str:
        return "telegram"

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler
        self._app = ApplicationBuilder().token(self._token).build()

        async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.message is None or update.message.text is None:
                return
            user_id = update.effective_user.id if update.effective_user else 0
            chat_id = update.effective_chat.id if update.effective_chat else 0

            if self._allowlist and user_id not in self._allowlist:
                return

            inbound = InboundMessage(
                channel="telegram",
                channel_chat_id=str(chat_id),
                channel_user_id=str(user_id),
                text=update.message.text,
                raw=update,
            )

            if self._handler:
                try:
                    response = await self._handler(inbound)
                    if response and update.message:
                        await update.message.reply_text(response[:4096])
                except Exception:
                    logger.exception("telegram.handler_error chat=%s", chat_id)

        self._app.add_handler(TGMessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        self._running = True

    async def stop(self) -> None:
        if self._app and self._running:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._running = False

    async def send(self, message: OutboundMessage) -> Optional[str]:
        if self._app is None:
            raise RuntimeError("TelegramTransport not started")
        chat_id = int(message.channel_chat_id)
        text = message.text[:4096]
        sent = await self._app.bot.send_message(chat_id=chat_id, text=text)
        return str(sent.message_id)

    async def health(self) -> Dict[str, Any]:
        if self._app is None:
            return {"ok": False, "error": "not started"}
        try:
            me = await self._app.bot.get_me()
            return {"ok": True, "bot_username": me.username}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
