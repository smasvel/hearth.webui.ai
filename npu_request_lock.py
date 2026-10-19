"""One machine-wide lock shared by every Hearth frontend that can own the AMD NPU."""

from __future__ import annotations

import fcntl
import time
from pathlib import Path
from typing import IO

_LOCK_PATH = Path("/tmp/hearth-npu-request.lock")


def acquire(timeout: float | None = None) -> IO[bytes]:
    """Acquire the cross-process NPU lock, optionally with a bounded wait.

    Generation paths intentionally wait for ownership, but interactive management
    operations can pass a timeout so a leaked/very long stream cannot make the UI
    spin forever.
    """
    handle = _LOCK_PATH.open("a+b")
    if timeout is None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle
    deadline = time.monotonic() + max(0, timeout)
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError(f"NPU is busy after waiting {timeout:g} seconds")
            time.sleep(0.1)


def release(handle: IO[bytes] | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
