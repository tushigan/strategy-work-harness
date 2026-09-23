"""Re-entrant macOS process lock for formal project writes."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_depth = threading.local()


def _local_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _guard:
        return _locks.setdefault(key, threading.RLock())


@contextmanager
def serialized(root):
    """Serialize a full read-check-write transaction; nested calls are safe."""
    path = Path(root).resolve() / "project/records/.workspace-write.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _local_lock(path)
    lock.acquire()
    depths = getattr(_depth, "values", {})
    key = str(path)
    current = depths.get(key, 0)
    depths[key] = current + 1
    _depth.values = depths
    stream = None
    try:
        if current == 0:
            stream = path.open("a+", encoding="utf-8")
            try:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - macOS supplies fcntl.
                pass
        yield
    finally:
        if current == 0 and stream is not None:
            try:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            stream.close()
        depths[key] = current
        lock.release()


def lock_path(root) -> str:
    return os.fspath(Path(root).resolve() / "project/records/.workspace-write.lock")
