"""Reuse successful checks only within one read-only task evaluation."""
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

from phase2_store import WorkflowError

_checks = ContextVar("task_validation_checks", default=None)
_active = ContextVar("task_validation_active", default=None)


def evaluation(function):
    @wraps(function)
    def run(*args, **kwargs):
        if _checks.get() is not None:
            return function(*args, **kwargs)
        token = _checks.set({})
        active_token = _active.set(set())
        try:
            return function(*args, **kwargs)
        finally:
            _active.reset(active_token)
            _checks.reset(token)
    return run


def _key(value):
    if isinstance(value, dict):
        return tuple((k, _key(v)) for k, v in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_key(v) for v in value)
    return value


def _reuse(key, check):
    cache = _checks.get()
    if cache is None:
        return check()
    active = _active.get()
    if key in active:
        raise WorkflowError("检查依赖关系循环")
    if key not in cache:
        # Failures may depend on the traversal path. Never memoize them.
        active.add(key)
        try:
            cache[key] = deepcopy(check())
        finally:
            active.remove(key)
    return deepcopy(cache[key])


def reuse(function, *args, **kwargs):
    return _reuse((function, _key(args), _key(kwargs)), lambda: function(*args, **kwargs))


def read_check(function):
    """Reuse recursive gates only while a read-only evaluator owns the context."""
    @wraps(function)
    def run(*args, **kwargs):
        return reuse(function, *args, **kwargs)
    return run


def completion_check(function):
    @evaluation
    @wraps(function)
    def run(root, task_id, seen=()):
        if task_id in seen:
            raise WorkflowError("任务前置关系循环")
        # Only a fully checked subtree is reusable. Check the active path first.
        return _reuse((function, root, task_id), lambda: function(root, task_id, seen))
    return run


from contextlib import contextmanager
import hashlib
from pathlib import Path

@contextmanager
def mutation():
    """A writer never inherits read-only successes; clear prior cache on both sides."""
    old=_checks.get()
    if old is not None:old.clear()
    token=_checks.set(None); active_token=_active.set(None)
    try:yield
    finally:
        _active.reset(active_token);_checks.reset(token)
        if old is not None:old.clear()

def file_digest(path):
    path=Path(path)
    def signature():
        s=path.stat();return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    before=signature();cache=_checks.get();key=('stable_sha256',str(path.resolve()),before)
    if cache is not None and key in cache:return cache[key]
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    if signature()!=before:raise WorkflowError('文件在指纹检查期间改变，请核对当前版本')
    result=h.hexdigest()
    if cache is not None:cache[key]=result
    return result
