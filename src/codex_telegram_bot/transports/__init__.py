"""Multi-channel transport abstraction layer.

Provides a channel-agnostic interface for message transport so that the
agent runtime can communicate over Telegram, Discord, WhatsApp, or any
future channel without coupling to a specific API.
"""
from codex_telegram_bot.transports.base import (
    InboundMessage,
    OutboundMessage,
    Transport,
    TransportRegistry,
)

__all__ = [
    "InboundMessage",
    "OutboundMessage",
    "Transport",
    "TransportRegistry",
]
