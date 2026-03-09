"""Channel-agnostic transport protocol and data types.

Every channel connector (Telegram, Discord, WhatsApp, etc.) implements the
``Transport`` protocol so the agent runtime can send/receive messages without
knowing which chat platform is in use.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message data types (channel-agnostic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundMessage:
    """A message received from any channel.

    ``channel`` identifies the transport (e.g. "telegram", "discord").
    ``channel_chat_id`` is the platform-specific chat/channel/DM identifier.
    ``channel_user_id`` is the platform-specific user identifier.
    ``text`` is the message body.
    ``attachments`` holds file metadata (platform-specific dicts).
    ``raw`` holds the original platform event for advanced use.
    """
    channel: str
    channel_chat_id: str
    channel_user_id: str
    text: str
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    raw: Any = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class OutboundMessage:
    """A message to send through a channel.

    ``channel_chat_id`` identifies where to send (platform-specific).
    ``text`` is the message body (may contain markdown).
    ``reply_to_message_id`` optionally references a prior message.
    ``attachments`` holds files to send (list of dicts with ``path`` or ``url``).
    """
    channel_chat_id: str
    text: str
    reply_to_message_id: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------

# Callback type: receives InboundMessage, returns response text (or None).
MessageHandler = Callable[[InboundMessage], Awaitable[Optional[str]]]


class Transport(Protocol):
    """Channel transport interface.

    Implementors bind to a specific chat platform and translate between
    platform-specific APIs and the channel-agnostic message types.
    """

    @property
    def channel_name(self) -> str:
        """Short identifier for this transport (e.g. "telegram", "discord")."""
        ...

    async def start(self, handler: MessageHandler) -> None:
        """Start the transport, routing inbound messages to ``handler``.

        This should be non-blocking (start polling/webhook listeners in a
        background task).  Implementations should catch and log transport
        errors without crashing.
        """
        ...

    async def stop(self) -> None:
        """Gracefully shut down the transport."""
        ...

    async def send(self, message: OutboundMessage) -> Optional[str]:
        """Send an outbound message.  Returns platform-specific message ID."""
        ...

    async def health(self) -> Dict[str, Any]:
        """Return transport health status."""
        ...


# ---------------------------------------------------------------------------
# Transport registry
# ---------------------------------------------------------------------------


class TransportRegistry:
    """Manages multiple transport instances.

    Usage::

        registry = TransportRegistry()
        registry.register(telegram_transport)
        registry.register(discord_transport)
        await registry.start_all(handler)
        await registry.broadcast(OutboundMessage(...))
    """

    def __init__(self) -> None:
        self._transports: Dict[str, Transport] = {}
        self._handler: Optional[MessageHandler] = None

    def register(self, transport: Transport) -> None:
        name = transport.channel_name
        if not name:
            raise ValueError("transport.channel_name must not be empty")
        self._transports[name] = transport

    def get(self, channel_name: str) -> Optional[Transport]:
        return self._transports.get(channel_name)

    def all(self) -> List[Transport]:
        return list(self._transports.values())

    def channel_names(self) -> List[str]:
        return sorted(self._transports.keys())

    async def start_all(self, handler: MessageHandler) -> None:
        """Start all registered transports with the given message handler."""
        self._handler = handler
        for name, transport in self._transports.items():
            try:
                await transport.start(handler)
                logger.info("transport.started channel=%s", name)
            except Exception:
                logger.exception("transport.start_failed channel=%s", name)

    async def stop_all(self) -> None:
        """Stop all registered transports."""
        for name, transport in self._transports.items():
            try:
                await transport.stop()
                logger.info("transport.stopped channel=%s", name)
            except Exception:
                logger.exception("transport.stop_failed channel=%s", name)

    async def send(self, channel_name: str, message: OutboundMessage) -> Optional[str]:
        """Send a message through a specific transport."""
        transport = self._transports.get(channel_name)
        if transport is None:
            raise ValueError(f"transport '{channel_name}' not registered")
        return await transport.send(message)

    async def broadcast(self, message: OutboundMessage) -> Dict[str, Optional[str]]:
        """Send a message through all transports.  Returns {channel: message_id}."""
        results: Dict[str, Optional[str]] = {}
        for name, transport in self._transports.items():
            try:
                msg_id = await transport.send(message)
                results[name] = msg_id
            except Exception:
                logger.exception("transport.broadcast_failed channel=%s", name)
                results[name] = None
        return results

    async def health_all(self) -> Dict[str, Dict[str, Any]]:
        """Collect health from all transports."""
        out: Dict[str, Dict[str, Any]] = {}
        for name, transport in self._transports.items():
            try:
                out[name] = await transport.health()
            except Exception as exc:
                out[name] = {"ok": False, "error": str(exc)}
        return out

    def __len__(self) -> int:
        return len(self._transports)
