from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass
class _MessageState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_text: str = ""
    last_hash: str = ""
    task: Optional[asyncio.Task] = None


class MessageUpdater:
    """Serializes and de-duplicates edits for a Telegram message."""

    def __init__(self, debounce_sec: float = 0.35) -> None:
        self._debounce_sec = max(0.0, float(debounce_sec))
        self._states: Dict[Tuple[int, int], _MessageState] = {}

    async def update(
        self,
        *,
        bot: Any,
        chat_id: int,
        message_id: int,
        text: str,
        fallback_send: bool = False,
    ) -> None:
        key = (int(chat_id), int(message_id))
        state = self._states.setdefault(key, _MessageState())
        state.pending_text = str(text or "")[:3900]
        if state.task and not state.task.done():
            return
        state.task = asyncio.create_task(
            self._drain_updates(
                key=key,
                state=state,
                bot=bot,
                fallback_send=fallback_send,
            )
        )

    async def flush(self, *, chat_id: int, message_id: int) -> None:
        key = (int(chat_id), int(message_id))
        state = self._states.get(key)
        if state is None or state.task is None:
            return
        try:
            await state.task
        except Exception:
            return

    async def _drain_updates(
        self,
        *,
        key: Tuple[int, int],
        state: _MessageState,
        bot: Any,
        fallback_send: bool,
    ) -> None:
        while True:
            candidate = state.pending_text
            state.pending_text = ""
            if not candidate:
                break
            if self._debounce_sec > 0:
                await asyncio.sleep(self._debounce_sec)
                # Coalesce bursts by preferring latest value.
                if state.pending_text:
                    continue
            digest = hashlib.sha256(candidate.encode("utf-8", errors="ignore")).hexdigest()
            if digest == state.last_hash:
                if not state.pending_text:
                    break
                continue
            async with state.lock:
                try:
                    await bot.edit_message_text(chat_id=key[0], message_id=key[1], text=candidate)
                    state.last_hash = digest
                except Exception as exc:
                    if _is_not_modified(exc):
                        state.last_hash = digest
                    elif fallback_send:
                        try:
                            await bot.send_message(chat_id=key[0], text=candidate)
                        except Exception:
                            pass
            if not state.pending_text:
                break


def _is_not_modified(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "message is not modified" in text

