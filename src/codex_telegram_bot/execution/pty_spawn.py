from __future__ import annotations

import errno
import os
import select
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

OutputCallback = Callable[[bytes], None]


@dataclass
class SpawnedProcess:
    """Managed subprocess with PTY or pipe-backed IO streaming."""

    process: subprocess.Popen
    pty_enabled: bool
    output_cb: OutputCallback
    master_fd: int | None = None
    _threads: list[threading.Thread] = field(default_factory=list)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _stdin_lock: threading.Lock = field(default_factory=threading.Lock)

    def start_readers(self) -> None:
        if self.pty_enabled:
            thread = threading.Thread(target=self._read_pty_output, daemon=True, name=f"pty-reader-{self.process.pid}")
            self._threads.append(thread)
            thread.start()
            return

        if self.process.stdout is not None:
            t_out = threading.Thread(
                target=self._read_pipe_output,
                args=(self.process.stdout,),
                daemon=True,
                name=f"stdout-reader-{self.process.pid}",
            )
            self._threads.append(t_out)
            t_out.start()
        if self.process.stderr is not None:
            t_err = threading.Thread(
                target=self._read_pipe_output,
                args=(self.process.stderr,),
                daemon=True,
                name=f"stderr-reader-{self.process.pid}",
            )
            self._threads.append(t_err)
            t_err.start()

    def poll(self) -> int | None:
        return self.process.poll()

    def write_stdin(self, text: str) -> None:
        payload = (text or "").encode("utf-8", errors="replace")
        if not payload:
            return
        with self._stdin_lock:
            if self.pty_enabled:
                if self.master_fd is None:
                    return
                os.write(self.master_fd, payload)
                return
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.write(payload)
                self.process.stdin.flush()

    def interrupt(self) -> None:
        self._signal_group(signal.SIGINT)

    def terminate(self) -> None:
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        self._signal_group(signal.SIGKILL)

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def close(self) -> None:
        self._stop_event.set()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        try:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
        except Exception:
            pass
        try:
            if self.process.stdout is not None and not self.process.stdout.closed:
                self.process.stdout.close()
        except Exception:
            pass
        try:
            if self.process.stderr is not None and not self.process.stderr.closed:
                self.process.stderr.close()
        except Exception:
            pass
        current = threading.current_thread()
        for thread in self._threads:
            if thread is current:
                continue
            thread.join(timeout=0.1)

    def _read_pty_output(self) -> None:
        if self.master_fd is None:
            return
        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.2)
            except OSError:
                break
            if not ready:
                if self.process.poll() is not None:
                    # Drain one extra cycle after process exit.
                    try:
                        data = os.read(self.master_fd, 4096)
                    except OSError:
                        data = b""
                    if data:
                        self.output_cb(data)
                        continue
                    break
                continue
            try:
                data = os.read(self.master_fd, 4096)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    break
                continue
            if not data:
                if self.process.poll() is not None:
                    break
                continue
            self.output_cb(data)

    def _read_pipe_output(self, stream) -> None:
        fd = stream.fileno()
        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except OSError:
                break
            if not ready:
                if self.process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    continue
                break
            if not chunk:
                break
            self.output_cb(chunk)

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            return
        except Exception:
            # Fallback to direct process signaling if pgid kill fails.
            try:
                self.process.send_signal(sig)
            except Exception:
                return


def spawn_process(
    argv: Sequence[str],
    cwd: Path,
    pty_enabled: bool,
    output_cb: OutputCallback,
    env: Optional[Mapping[str, str]] = None,
) -> SpawnedProcess:
    """Spawn process with PTY (preferred) and fallback to pipes."""
    if pty_enabled:
        try:
            return _spawn_pty(argv=argv, cwd=cwd, output_cb=output_cb, env=env)
        except Exception:
            return _spawn_pipes(argv=argv, cwd=cwd, output_cb=output_cb, env=env)
    return _spawn_pipes(argv=argv, cwd=cwd, output_cb=output_cb, env=env)


def _spawn_pty(
    argv: Sequence[str],
    cwd: Path,
    output_cb: OutputCallback,
    env: Optional[Mapping[str, str]],
) -> SpawnedProcess:
    master_fd, slave_fd = os.openpty()
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            shell=False,
            close_fds=True,
            start_new_session=True,
            env=(dict(env) if env is not None else None),
        )
    finally:
        os.close(slave_fd)

    os.set_blocking(master_fd, False)
    spawned = SpawnedProcess(
        process=proc,
        pty_enabled=True,
        output_cb=output_cb,
        master_fd=master_fd,
    )
    spawned.start_readers()
    return spawned


def _spawn_pipes(
    argv: Sequence[str],
    cwd: Path,
    output_cb: OutputCallback,
    env: Optional[Mapping[str, str]],
) -> SpawnedProcess:
    proc = subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
        start_new_session=True,
        env=(dict(env) if env is not None else None),
    )
    if proc.stdout is not None:
        os.set_blocking(proc.stdout.fileno(), False)
    if proc.stderr is not None:
        os.set_blocking(proc.stderr.fileno(), False)
    spawned = SpawnedProcess(
        process=proc,
        pty_enabled=False,
        output_cb=output_cb,
    )
    spawned.start_readers()
    return spawned
