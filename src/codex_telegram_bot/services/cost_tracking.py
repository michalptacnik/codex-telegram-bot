from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_DEFAULT_PRICES_PER_1M: Dict[str, Dict[str, float]] = {
    "openai/gpt-4.1-mini": {"prompt": 0.15, "completion": 0.60},
    "responses_api/gpt-4o": {"prompt": 2.50, "completion": 10.00},
    "deepseek/deepseek-chat": {"prompt": 0.27, "completion": 1.10},
    "qwen/qwen-plus": {"prompt": 0.40, "completion": 1.20},
    "anthropic/claude-opus-4-6": {"prompt": 15.00, "completion": 75.00},
    "gemini/gemini-2.0-flash": {"prompt": 0.10, "completion": 0.40},
}


def normalize_usage(usage: Dict[str, Any]) -> Tuple[int, int, int]:
    u = usage or {}
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    completion = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    total = int(u.get("total_tokens") or (prompt + completion))
    return max(0, prompt), max(0, completion), max(0, total)


def explicit_cost_from_usage(usage: Dict[str, Any]) -> Optional[float]:
    for key in ("cost_usd", "total_cost_usd", "cost"):
        raw = usage.get(key) if isinstance(usage, dict) else None
        if raw is None:
            continue
        try:
            return max(0.0, float(raw))
        except Exception:
            continue
    return None


def estimate_cost_usd(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    usage: Optional[Dict[str, Any]] = None,
    config_dir: Optional[Path] = None,
) -> Optional[float]:
    explicit = explicit_cost_from_usage(usage or {})
    if explicit is not None:
        return explicit
    table = _load_price_table(config_dir=config_dir)
    key_candidates = [
        f"{provider}/{model}",
        f"{provider}/{(model or '').strip().lower()}",
        model or "",
    ]
    spec = None
    for key in key_candidates:
        if key in table:
            spec = table[key]
            break
    if not spec:
        return None
    try:
        prompt_rate = float(spec.get("prompt", 0.0))
        completion_rate = float(spec.get("completion", 0.0))
    except Exception:
        return None
    return ((prompt_tokens / 1_000_000.0) * prompt_rate) + ((completion_tokens / 1_000_000.0) * completion_rate)


def _load_price_table(config_dir: Optional[Path] = None) -> Dict[str, Dict[str, float]]:
    table: Dict[str, Dict[str, float]] = dict(_DEFAULT_PRICES_PER_1M)
    candidates = []
    if config_dir is not None:
        candidates.append((config_dir / "prices.json").expanduser().resolve())
    candidates.append((Path.cwd() / "prices.json").expanduser().resolve())
    candidates.append((Path.home() / ".config" / "codex-telegram-bot" / "prices.json").expanduser().resolve())
    env_path = (os.environ.get("PRICES_CONFIG") or "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser().resolve())
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            try:
                p = float(value.get("prompt", 0.0))
                c = float(value.get("completion", 0.0))
            except Exception:
                continue
            table[str(key)] = {"prompt": max(0.0, p), "completion": max(0.0, c)}
    return table

