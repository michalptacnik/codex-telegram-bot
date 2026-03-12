from __future__ import annotations

import errno
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


class ProcessSingletonLockError(RuntimeError):
    pass


class ProcessSingletonLock:
    def __init__(self, *, path: Path, label: str) -> None:
        self._path = Path(path).expanduser().resolve()
        self._label = str(label or "").strip() or "runtime"
        self._fh = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+", encoding="utf-8")
        if fcntl is None:
            raise ProcessSingletonLockError("process locking requires fcntl on this platform.")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ProcessSingletonLockError(
                    f"another '{self._label}' process is already running (lock: {self._path})"
                )
            raise ProcessSingletonLockError(str(exc))
        self._fh.seek(0)
        self._fh.truncate(0)
        self._fh.write(f"{os.getpid()}\n{self._label}\n")
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except Exception:
            pass

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __enter__(self) -> "ProcessSingletonLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _singleton_lock_path(*, config_dir: Path, scope: str) -> Path:
    safe = re.sub(r"[^a-z0-9._-]+", "-", str(scope or "runtime").strip().lower()).strip("-")
    if not safe:
        safe = "runtime"
    return config_dir.expanduser().resolve() / ".locks" / f"{safe}.lock"


@contextmanager
def hold_process_singleton(*, config_dir: Path, scope: str) -> Iterator[ProcessSingletonLock]:
    lock = ProcessSingletonLock(path=_singleton_lock_path(config_dir=config_dir, scope=scope), label=scope)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
