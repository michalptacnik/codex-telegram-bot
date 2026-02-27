from __future__ import annotations

import asyncio
import random
from typing import Any, Dict

try:
    import httpx as _httpx  # type: ignore[import]
except Exception:  # pragma: no cover
    _httpx = None  # type: ignore[assignment]


def build_httpx_client(
    *,
    base_url: str,
    headers: Dict[str, str],
    connect_timeout_sec: float,
    read_timeout_sec: float,
    max_connections: int = 30,
    max_keepalive_connections: int = 10,
) -> Any:
    if _httpx is None:
        raise RuntimeError("httpx is required")
    timeout = _httpx.Timeout(connect=connect_timeout_sec, read=read_timeout_sec, write=read_timeout_sec, pool=5.0)
    limits = _httpx.Limits(
        max_connections=max(1, int(max_connections)),
        max_keepalive_connections=max(1, int(max_keepalive_connections)),
    )
    return _httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, limits=limits)


async def post_json_with_retries(
    client: Any,
    *,
    path: str,
    payload: Dict[str, Any],
    attempts: int = 3,
    base_backoff_sec: float = 0.5,
) -> Any:
    last_exc: Exception | None = None
    max_attempts = max(1, int(attempts))
    for idx in range(max_attempts):
        try:
            resp = await client.post(path, json=payload)
            if resp.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
                if idx + 1 >= max_attempts:
                    resp.raise_for_status()
                await _sleep_backoff(idx, base_backoff_sec)
                continue
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if idx + 1 >= max_attempts or (not _is_transient_error(exc)):
                raise
            await _sleep_backoff(idx, base_backoff_sec)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("post_json_with_retries exhausted without result")


def _is_transient_error(exc: Exception) -> bool:
    text = type(exc).__name__.lower() + " " + str(exc).lower()
    transient_markers = ["timeout", "readerror", "connecterror", "network", "tempor", "name or service not known"]
    return any(marker in text for marker in transient_markers)


async def _sleep_backoff(attempt_idx: int, base_backoff_sec: float) -> None:
    # bounded exponential backoff with jitter
    delay = min(8.0, max(0.05, float(base_backoff_sec)) * (2 ** attempt_idx))
    delay = delay * (0.8 + random.random() * 0.4)
    await asyncio.sleep(delay)

