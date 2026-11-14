"""One machine-wide lock shared by every Hearth frontend that can own the AMD NPU."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

if os.name == "nt":
    _STATE_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Hearth"
else:
    _STATE_ROOT = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "hearth"
_LOCK_PATH = _STATE_ROOT / "npu-request.lock"


def _try_lock(handle: IO[bytes]) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: IO[bytes]) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire(timeout: float | None = None) -> IO[bytes]:
    """Acquire the cross-process NPU lock, optionally with a bounded wait.

    Generation paths intentionally wait for ownership, but interactive management
    operations can pass a timeout so a leaked/very long stream cannot make the UI
    spin forever.
    """
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = _LOCK_PATH.open("a+b")
    if os.name == "nt" and handle.seek(0, 2) == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = None if timeout is None else time.monotonic() + max(0, timeout)
    while True:
        try:
            _try_lock(handle)
            return handle
        except OSError:
            if deadline is not None and time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError(f"NPU is busy after waiting {timeout:g} seconds")
            time.sleep(0.1)


def release(handle: IO[bytes] | None) -> None:
    if handle is None:
        return
    try:
        _unlock(handle)
    finally:
        handle.close()
