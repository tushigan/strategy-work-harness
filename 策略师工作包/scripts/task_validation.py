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


def is_records_path(relative):
    """F06 / F09：只追加的记录 = project/records/ 下直接的 .jsonl（前缀判定、改稿预算等共用这一条路径规则）。"""
    parts=Path(relative).parts if isinstance(relative,str) else ()
    return len(parts)==3 and parts[:2]==('project','records') and parts[2].endswith('.jsonl')


def records_prefix(path, relative, expected):
    """F06 / F09：project/records/ 下的 .jsonl 只追加。整文件指纹不符时由调用方再问这里：
    按行边界（每个换行之后）逐步算前缀指纹，某个前缀等于登记指纹即“登记部分未改，之后追加了 N 条”，
    返回 {"length": 登记部分字节数, "added": N}；前缀都对不上（改写、删行、不在换行处）返回 None，仍按原规则报指纹不符；
    之后只多了空白行（N=0）也返回 None。流式读，不整份进内存。"""
    if not is_records_path(relative) or not isinstance(expected,str):
        return None
    h=hashlib.sha256();matched=h.hexdigest()==expected;added=0;length=0
    with Path(path).open('rb') as stream:
        for line in stream:
            if matched:
                added+=bool(line.strip());continue
            h.update(line);length+=len(line)
            matched=line.endswith(b'\n') and h.hexdigest()==expected
    return {"length":length,"added":added} if matched and added else None


def appended_records(path, relative, expected):
    """F06（v1.7.3）：登记指纹是当前文件按行边界的某个前缀 → 返回之后追加的条数，否则 None（见 records_prefix）。"""
    found=records_prefix(path,relative,expected)
    return found["added"] if found else None
