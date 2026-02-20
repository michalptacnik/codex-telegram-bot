import re
from typing import Iterable, List

REDACT_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")


def redact(text: str) -> str:
    return REDACT_RE.sub("sk-REDACTED", text)


def chunk_text(text: str, max_len: int) -> List[str]:
    if max_len <= 0:
        return [text]
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]


def iter_lines(text: str) -> Iterable[str]:
    for line in text.splitlines():
        yield line
