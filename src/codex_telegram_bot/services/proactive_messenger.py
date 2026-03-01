from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable, Dict

logger = logging.getLogger(__name__)

TransportHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class ProactiveMessenger:
    """Fan-out dispatcher for proactive outbound deliveries."""

    def __init__(self) -> None:
        self._handlers: Dict[str, TransportHandler] = {}
        self._lock = threading.Lock()

    def register(self, name: str, handler: TransportHandler) -> None:
        normalized = str(name or "").strip().lower()
        if not normalized:
            raise ValueError("transport name is required")
        if not callable(handler):
            raise ValueError("transport handler must be callable")
        with self._lock:
            self._handlers[normalized] = handler

    def unregister(self, name: str) -> None:
        normalized = str(name or "").strip().lower()
        if not normalized:
            return
        with self._lock:
            self._handlers.pop(normalized, None)

    async def deliver(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            handlers = dict(self._handlers)
        delivered = []
        failed: Dict[str, str] = {}
        for name, handler in handlers.items():
            try:
                await handler(dict(payload))
                delivered.append(name)
            except Exception as exc:
                failed[name] = str(exc)
                logger.exception("proactive transport failed: %s", name)
        return {
            "attempted": sorted(handlers.keys()),
            "delivered": delivered,
            "failed": failed,
        }
