from dataclasses import dataclass
from typing import Callable

DEFAULT_MEMORY_TURNS = 20


@dataclass(frozen=True)
class MemoryConfig:
    max_turns: int
    max_messages: int
    keep_recent_messages: int


def resolve_memory_config(read_int_env: Callable[[str, int], int]) -> MemoryConfig:
    max_turns = max(1, int(read_int_env("SESSION_MAX_TURNS", DEFAULT_MEMORY_TURNS)))
    default_messages = max_turns * 2
    max_messages = max(2, int(read_int_env("SESSION_MAX_MESSAGES", default_messages)))
    keep_recent = int(read_int_env("SESSION_COMPACT_KEEP", min(20, max_messages)))
    keep_recent = max(2, min(keep_recent, max_messages))
    return MemoryConfig(
        max_turns=max_turns,
        max_messages=max_messages,
        keep_recent_messages=keep_recent,
    )
