"""Streaming CLI-like feedback for Telegram (EPIC 4, issue #67).

The StreamingUpdater drives a "live typing" experience in Telegram:
  1. Sends an initial placeholder message ("⏳ Thinking…").
  2. Feeds an async generator of text chunks from the provider.
  3. Periodically edits the message to show accumulated text.
  4. Throttles edits to respect Telegram's rate limits (~4 edits/sec).

Usage::

    from codex_telegram_bot.services.streaming import StreamingUpdater

    async for chunk in provider.generate_stream(messages):
        ...  # handled internally by the updater

    updater = StreamingUpdater(bot=context.bot, chat_id=chat_id)
    final_text = await updater.run(provider.generate_stream(messages))

The updater also accepts a plain coroutine (non-streaming) and will show a
spinner placeholder while it completes, then send the final result – so callers
can use the same interface for both streaming and non-streaming providers.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable, Optional, Union

logger = logging.getLogger(__name__)

# Telegram allows ~30 messages/sec per bot globally; for edits on a single
# message, a 0.3 s interval is safe and visually smooth.
_DEFAULT_EDIT_INTERVAL_SEC = 0.3
_PLACEHOLDER = "⏳ …"
_MAX_TELEGRAM_MSG_CHARS = 4096
_CHUNK_DISPLAY_THRESHOLD = 20   # chars accumulated before forcing an edit


class StreamingUpdater:
    """Edit a Telegram message in real-time as chunks arrive.

    Parameters
    ----------
    bot:
        ``telegram.Bot`` instance.
    chat_id:
        Target chat ID.
    message_id:
        If provided, edits an existing message instead of sending a new one.
    edit_interval_sec:
        Minimum seconds between message edits (rate-limit guard).
    suffix:
        Appended to the message while streaming (e.g. a cursor "▌").
    on_final:
        Optional async callback called with the final text.
    """

    def __init__(
        self,
        bot: "telegram.Bot",  # type: ignore[name-defined]
        chat_id: int,
        message_id: Optional[int] = None,
        edit_interval_sec: float = _DEFAULT_EDIT_INTERVAL_SEC,
        suffix: str = "▌",
        on_final: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._edit_interval = edit_interval_sec
        self._suffix = suffix
        self._on_final = on_final
        self._last_edit_time: float = 0.0
        self._current_text: str = ""

    async def run(
        self,
        source: Union[AsyncIterator[str], "asyncio.Future[str]"],
    ) -> str:
        """Stream chunks from ``source`` into the Telegram message.

        ``source`` may be:
          - an async generator / async iterator yielding text chunks, or
          - a coroutine / Future returning a complete string (non-streaming).

        Returns the full accumulated text.
        """
        # Send placeholder if we don't yet have a message to edit
        if self._message_id is None:
            sent = await self._bot.send_message(
                chat_id=self._chat_id,
                text=_PLACEHOLDER,
                parse_mode=None,
            )
            self._message_id = sent.message_id

        if asyncio.iscoroutine(source) or asyncio.isfuture(source):
            # Non-streaming: show placeholder until done
            result = await source  # type: ignore[misc]
            await self._update(result, final=True)
            return result

        # Streaming path
        self._current_text = ""
        accumulated_since_edit = 0

        async for chunk in source:  # type: ignore[union-attr]
            if not isinstance(chunk, str):
                continue
            self._current_text += chunk
            accumulated_since_edit += len(chunk)

            now = asyncio.get_event_loop().time()
            time_since_edit = now - self._last_edit_time
            if (accumulated_since_edit >= _CHUNK_DISPLAY_THRESHOLD
                    or time_since_edit >= self._edit_interval):
                await self._update(self._current_text, final=False)
                accumulated_since_edit = 0

        # Final edit without cursor suffix
        await self._update(self._current_text, final=True)
        if self._on_final:
            try:
                self._on_final(self._current_text)
            except Exception:
                pass
        return self._current_text

    async def _update(self, text: str, *, final: bool) -> None:
        display = text if final else text + self._suffix
        # Truncate to Telegram's per-message limit
        if len(display) > _MAX_TELEGRAM_MSG_CHARS:
            display = display[-_MAX_TELEGRAM_MSG_CHARS:]
        if not display:
            display = _PLACEHOLDER
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=display,
                parse_mode=None,
            )
            self._last_edit_time = asyncio.get_event_loop().time()
        except Exception as exc:
            # "Message not modified" is benign; log others
            msg = str(exc).lower()
            if "not modified" not in msg and "message to edit not found" not in msg:
                logger.debug("StreamingUpdater edit failed: %s", exc)


# ---------------------------------------------------------------------------
# Convenience: run a prompt with streaming if the provider supports it
# ---------------------------------------------------------------------------

async def stream_prompt_to_telegram(
    provider: "ProviderAdapter",  # type: ignore[name-defined]
    messages: list,
    bot: "telegram.Bot",  # type: ignore[name-defined]
    chat_id: int,
    message_id: Optional[int] = None,
    correlation_id: str = "",
    edit_interval_sec: float = _DEFAULT_EDIT_INTERVAL_SEC,
) -> str:
    """High-level helper: generates with streaming (if supported) and
    pushes live updates to a Telegram message.

    Falls back to buffered generation if the provider does not support streaming.
    """
    caps = getattr(provider, "capabilities", lambda: {})()
    supports_streaming = isinstance(caps, dict) and caps.get("supports_streaming", False)

    updater = StreamingUpdater(
        bot=bot,
        chat_id=chat_id,
        message_id=message_id,
        edit_interval_sec=edit_interval_sec,
    )

    if supports_streaming and hasattr(provider, "generate_stream"):
        source = provider.generate_stream(messages, correlation_id=correlation_id)
    else:
        source = provider.generate(messages, correlation_id=correlation_id)  # type: ignore[assignment]

    return await updater.run(source)


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from telegram import Bot
    from codex_telegram_bot.domain.contracts import ProviderAdapter
