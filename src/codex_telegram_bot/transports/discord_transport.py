"""Discord transport adapter.

Implements the channel-agnostic Transport protocol for Discord using the
``discord.py`` library.  Requires ``pip install discord.py`` (or the
``discord`` extra from pyproject.toml).

Environment variables
---------------------
``DISCORD_BOT_TOKEN``
    The Discord bot token.
``DISCORD_ALLOWLIST``
    Optional comma-separated list of allowed Discord user IDs.
``DISCORD_CHANNEL_IDS``
    Optional comma-separated list of channel IDs the bot should listen in.
    If empty, the bot listens in all channels it has access to.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Set

from codex_telegram_bot.transports.base import (
    InboundMessage,
    MessageHandler,
    OutboundMessage,
    Transport,
)

logger = logging.getLogger(__name__)

try:
    import discord
    _DISCORD_AVAILABLE = True
except ImportError:
    _DISCORD_AVAILABLE = False


def _parse_id_list(raw: str) -> Set[int]:
    out: Set[int] = set()
    for chunk in (raw or "").split(","):
        value = chunk.strip()
        if value.isdigit():
            out.add(int(value))
    return out


class DiscordTransport:
    """Transport implementation for Discord using discord.py."""

    def __init__(
        self,
        token: Optional[str] = None,
        allowlist: Optional[Set[int]] = None,
        channel_ids: Optional[Set[int]] = None,
    ) -> None:
        if not _DISCORD_AVAILABLE:
            raise ImportError(
                "discord.py is required for DiscordTransport. "
                "Install with: pip install 'codex-telegram-bot[discord]'"
            )
        self._token = token or (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
        if not self._token:
            raise ValueError("DISCORD_BOT_TOKEN is required")
        self._allowlist = allowlist or _parse_id_list(os.environ.get("DISCORD_ALLOWLIST") or "")
        self._channel_ids = channel_ids or _parse_id_list(os.environ.get("DISCORD_CHANNEL_IDS") or "")
        self._handler: Optional[MessageHandler] = None
        self._client: Optional[discord.Client] = None
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    @property
    def channel_name(self) -> str:
        return "discord"

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        transport = self  # capture for closures

        @self._client.event
        async def on_ready():
            logger.info("discord.ready user=%s", self._client.user)
            transport._ready.set()

        @self._client.event
        async def on_message(message: discord.Message):
            # Ignore bot's own messages.
            if message.author == self._client.user:
                return
            if message.author.bot:
                return

            # Allowlist check.
            if transport._allowlist and message.author.id not in transport._allowlist:
                return

            # Channel filter.
            if transport._channel_ids and message.channel.id not in transport._channel_ids:
                return

            if not message.content:
                return

            inbound = InboundMessage(
                channel="discord",
                channel_chat_id=str(message.channel.id),
                channel_user_id=str(message.author.id),
                text=message.content,
                raw=message,
            )

            if transport._handler:
                try:
                    response = await transport._handler(inbound)
                    if response:
                        # Discord has a 2000 char limit per message.
                        for chunk in _chunk_text(response, 2000):
                            await message.channel.send(chunk)
                except Exception:
                    logger.exception(
                        "discord.handler_error channel=%s user=%s",
                        message.channel.id,
                        message.author.id,
                    )

        # Start the client in a background task.
        self._task = asyncio.create_task(self._client.start(self._token))

    async def stop(self) -> None:
        if self._client and not self._client.is_closed():
            await self._client.close()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def send(self, message: OutboundMessage) -> Optional[str]:
        if self._client is None:
            raise RuntimeError("DiscordTransport not started")
        # Wait up to 10 seconds for the client to be ready.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            raise RuntimeError("Discord client not ready")

        channel_id = int(message.channel_chat_id)
        channel = self._client.get_channel(channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(channel_id)

        text = message.text
        sent_ids: List[str] = []
        for chunk in _chunk_text(text, 2000):
            sent = await channel.send(chunk)
            sent_ids.append(str(sent.id))
        return sent_ids[0] if sent_ids else None

    async def health(self) -> Dict[str, Any]:
        if self._client is None:
            return {"ok": False, "error": "not started"}
        if self._client.is_closed():
            return {"ok": False, "error": "client closed"}
        if not self._ready.is_set():
            return {"ok": False, "error": "not ready"}
        user = self._client.user
        return {
            "ok": True,
            "bot_username": str(user) if user else "unknown",
            "guild_count": len(self._client.guilds),
        }


def _chunk_text(text: str, max_len: int = 2000) -> List[str]:
    """Split text into chunks of at most ``max_len`` characters."""
    if not text:
        return []
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]
