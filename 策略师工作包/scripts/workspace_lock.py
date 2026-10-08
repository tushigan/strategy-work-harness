"""Reentrant business lock with one total, monotonic five-second budget."""
from __future__ import annotations
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_depth = threading.local()
WAIT_SECONDS = 5.0

class WorkspaceBusy(ValueError):
    status = "busy"
    reason_code = "workspace_lock_timeout"
    def __init__(self):
        super().__init__("工作区正在被其他操作占用，稍后继续")

def _local_lock(path):
    with _guard:
        return _locks.setdefault(str(path), threading.RLock())

@contextmanager
def serialized(root, write=True):
    path = Path(root).resolve() / "project/records/.workspace-write.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("工作区锁不能为符号链接")
    lock = _local_lock(path)
    start = time.monotonic(); deadline = start + WAIT_SECONDS
    if not lock.acquire(timeout=max(0, deadline-time.monotonic())):
        try:
            from yunxing_rizhi import note
            note('workspace_lock','wait',duration_ms=(time.monotonic()-start)*1000,
                 status='busy',reason_code='lock_contention')
        except Exception:pass
        raise WorkspaceBusy()
    depths = getattr(_depth, "values", {}); key = str(path)
    current = depths.get(key, 0); _depth.values = depths
    stream = None; acquired = False; waited = False; cache_scope = None
    try:
        if current == 0:
            import fcntl
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            stream = os.fdopen(fd, "a+")
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True; break
                except BlockingIOError:
                    waited = True
                    remaining = deadline-time.monotonic()
                    if remaining <= 0: raise WorkspaceBusy()
                    threading.Event().wait(min(0.025, remaining))
        wait_ms=(time.monotonic()-start)*1000
        depths[key] = current + 1
        if waited or wait_ms > 1:
            from yunxing_rizhi import note
            note('workspace_lock','wait',duration_ms=wait_ms,reason_code='lock_contention')
        if write:
            from task_validation import mutation
            cache_scope=mutation();cache_scope.__enter__()
        yield
    finally:
        if cache_scope is not None:cache_scope.__exit__(None,None,None)
        if current == 0 and stream is not None:
            if acquired:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        depths[key] = current
        lock.release()
        if waited and not acquired:
            try:
                from yunxing_rizhi import note
                note("workspace_lock", "wait", duration_ms=locals().get("wait_ms", (time.monotonic()-start)*1000),
                     reason_code="lock_contention", status="ok" if acquired else "busy")
            except Exception: pass

def lock_path(root):
    return os.fspath(Path(root).resolve() / "project/records/.workspace-write.lock")


def read_serialized(function):
    from functools import wraps
    @wraps(function)
    def call(root,*args,**kwargs):
        with serialized(root,write=False):return function(root,*args,**kwargs)
    return call
