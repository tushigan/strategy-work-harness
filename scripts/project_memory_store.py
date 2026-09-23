"""Portable append-only project facts with revision and source checks."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import urlsplit

from phase2_store import WorkflowError, local, now
from workspace_lock import serialized

JOURNAL = "project/records/project-memory.jsonl"
STATUSES = {"confirmed", "needs_confirmation", "retired"}
REQUEST_FIELDS = {"memory_id", "request_id", "expected_revision", "project_id", "entity_type",
                  "entity_id", "topic", "current_value", "replaces", "scope",
                  "source_refs", "reason", "status"}
EVENT_FIELDS = (REQUEST_FIELDS - {"expected_revision"}) | {
    "schema_version", "request_hash", "revision", "timestamp", "actor"
}


def _text(value, name, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise WorkflowError(f"{name} 必须为{'可空' if empty else '非空'}文字")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise WorkflowError(f"{name} 含非法控制字符")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise WorkflowError(f"{name} 须使用字母、数字、短横线或下划线")
    return value


def _hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _project_identity(root, required=False):
    path = Path(root) / "project/state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise WorkflowError("项目身份文件无法读取，不能采用项目记忆") from exc
    fields = ("project_id", "project_name", "client_name", "brand_name")
    ready = isinstance(state, dict) and all(
        isinstance(state.get(name), str) and state[name].strip() for name in fields)
    if required and not ready:
        raise WorkflowError("保存项目记忆前须先登记项目编号、项目名、客户名和品牌名")
    return {name: state.get(name) for name in fields} if ready else None


def _storage(root):
    root = Path(root).resolve()
    for name in ("project", "project/records"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise WorkflowError("项目记忆存储目录必须为工作包内普通目录")
    for name in (JOURNAL, "project/records/.workspace-write.lock"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise WorkflowError("项目记忆日志与锁必须为普通文件")
    return root / JOURNAL


def _reference_path(root, value, stored=False):
    posix = PurePosixPath(value) if isinstance(value, str) else None
    if (posix is None or not value.startswith("project/") or posix.as_posix() != value
            or ".." in posix.parts or "\\" in value or any(ord(c) < 32 for c in value)
            or value == JOURNAL):
        raise WorkflowError("本地来源须在 project/ 内，不能引用项目记忆日志自身")
    if stored:
        return Path(root) / value
    candidate = Path(root)
    for part in posix.parts:
        candidate /= part
        if candidate.is_symlink():
            raise WorkflowError("项目记忆来源不能通过符号链接引用")
    return local(root, value)


def _source_refs(root, values, stored=False):
    if not isinstance(values, list):
        raise WorkflowError("source_refs 必须为列表")
    result = []
    for item in values:
        if not isinstance(item, dict):
            raise WorkflowError("每个来源必须为对象")
        _text(item.get("label"), "来源说明")
        if set(item) == ({"label", "path", "sha256"} if stored else {"label", "path"}):
            path = _reference_path(root, item["path"], stored)
            if stored:
                if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                    raise WorkflowError("来源文件指纹无效")
                result.append(dict(item))
            else:
                if not path.is_file():
                    raise WorkflowError("本地来源文件不存在，请先存入工作包")
                result.append({**item, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        elif set(item) == ({"label", "url", "cached", "network_verified"}
                           if stored else {"label", "url"}):
            url = urlsplit(item["url"])
            if (url.scheme not in {"http", "https", "thread"} or not url.netloc
                    or url.username or url.password or any(c.isspace() for c in item["url"])):
                raise WorkflowError("外部来源链接无效，不允许账号密码")
            if stored and (item["cached"] is not False or item["network_verified"] is not False):
                raise WorkflowError("外部来源只能标记为未缓存、未联网核验")
            result.append({"label": item["label"], "url": item["url"],
                           "cached": False, "network_verified": False})
        else:
            raise WorkflowError("来源只能包含 label 与 path，或 label 与 url")
    return result


def _validate_request(root, request, actor):
    if not isinstance(request, dict) or set(request) != REQUEST_FIELDS:
        raise WorkflowError("项目记忆请求字段不完整或含未支持字段")
    _identifier(request["memory_id"], "memory_id")
    _identifier(request["request_id"], "request_id")
    _text(actor, "记录者")
    for name in ("project_id", "entity_type", "entity_id", "topic", "current_value", "scope", "reason"):
        _text(request[name], name)
    identity = _project_identity(root, required=True)
    if request["project_id"] != identity["project_id"]:
        raise WorkflowError("项目记忆的 project_id 与当前工作包身份不一致")
    if request["replaces"] is not None:
        _text(request["replaces"], "replaces")
    if not isinstance(request["status"], str) or request["status"] not in STATUSES:
        raise WorkflowError("项目记忆状态无效")
    if type(request["expected_revision"]) is not int or request["expected_revision"] < 0:
        raise WorkflowError("expected_revision 须为非负整数")
    value = dict(request)
    value["source_refs"] = _source_refs(root, request["source_refs"])
    if request["status"] == "confirmed" and not value["source_refs"]:
        raise WorkflowError("confirmed 项目事实必须附可追溯来源")
    return value


def _validate_event(root, item, revisions, request_ids):
    if not isinstance(item, dict) or set(item) != EVENT_FIELDS:
        raise ValueError("事件字段不完整")
    _identifier(item["memory_id"], "memory_id")
    _identifier(item["request_id"], "request_id")
    expected = revisions.get(item["memory_id"], 0) + 1
    if (type(item["schema_version"]) is not int or item["schema_version"] != 1
            or type(item["revision"]) is not int
            or item["revision"] != expected or item["request_id"] in request_ids
            or not isinstance(item["request_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["request_hash"])):
        raise ValueError("版本序列或请求编号无效")
    for name in ("project_id", "entity_type", "entity_id", "topic", "current_value", "scope", "reason", "actor"):
        _text(item[name], name)
    if item["replaces"] is not None:
        _text(item["replaces"], "replaces")
    if not isinstance(item["status"], str) or item["status"] not in STATUSES:
        raise ValueError("状态无效")
    if not isinstance(item["timestamp"], str) or datetime.fromisoformat(item["timestamp"]).tzinfo is None:
        raise ValueError("时间无效")
    _source_refs(root, item["source_refs"], stored=True)
    if item["status"] == "confirmed" and not item["source_refs"]:
        raise ValueError("confirmed 项目事实缺少来源")


def history(root):
    path = _storage(root)
    if not path.exists():
        return [], b""
    raw = path.read_bytes()
    if not raw.strip():
        raise WorkflowError("项目记忆日志为空，可能被截断；不能当作无记忆")
    events, revisions, request_ids = [], {}, set()
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            _validate_event(root, item, revisions, request_ids)
            prior = next((e for e in reversed(events) if e["memory_id"] == item["memory_id"]), None)
            if prior and any(item[key] != prior[key]
                             for key in ("project_id", "entity_type", "entity_id", "topic")):
                raise ValueError("同一记忆编号的对象或主题改变")
            events.append(item)
            revisions[item["memory_id"]] = item["revision"]
            request_ids.add(item["request_id"])
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise WorkflowError("项目记忆日志损坏或乱序，请核对原件；未采用其中事实") from exc
    return events, raw


def save(root, request, actor):
    root = Path(root).resolve()
    _storage(root)
    value = _validate_request(root, request, actor)
    request_hash = _hash({"request": request, "actor": actor})
    with serialized(root):
        events, raw = history(root)
        prior_request = next((e for e in events if e["request_id"] == request["request_id"]), None)
        if prior_request:
            if prior_request["request_hash"] != request_hash:
                raise WorkflowError("request_id 已用于不同内容，保留原项目记忆")
            return next(e for e in reversed(events) if e["memory_id"] == prior_request["memory_id"])
        current = next((e for e in reversed(events) if e["memory_id"] == request["memory_id"]), None)
        revision = current["revision"] if current else 0
        if request["expected_revision"] != revision:
            raise WorkflowError("项目记忆版本已变化，请重新读取；不能覆盖其它对话的修订")
        identity = (request["project_id"], request["entity_type"], request["entity_id"],
                    request["topic"], request["scope"])
        for event in events:
            other = (event["project_id"], event["entity_type"], event["entity_id"],
                     event["topic"], event["scope"])
            if event["memory_id"] != request["memory_id"] and other == identity:
                raise WorkflowError("同一对象、主题和范围已属于另一个 memory_id")
        if current and request["current_value"] != current["current_value"]:
            if request["replaces"] != current["current_value"]:
                raise WorkflowError("更正当前值时 replaces 必须精确记录被替代值")
        event = {key: value[key] for key in REQUEST_FIELDS if key != "expected_revision"}
        event.update(schema_version=1, request_hash=request_hash, revision=revision + 1,
                     timestamp=now(), actor=actor)
        path = _storage(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".writing-project-memory-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw + (b"\n" if raw and not raw.endswith(b"\n") else b""))
                stream.write((json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            if (path.read_bytes() if path.exists() else b"") != raw:
                raise WorkflowError("保存期间项目记忆被修改，请重读；未覆盖原件")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return event


def _reference_issues(root, event):
    issues = []
    for ref in event["source_refs"]:
        if "url" in ref:
            continue
        try:
            path = _reference_path(root, ref["path"])
            if not path.is_file():
                issues.append(f"{ref['path']} 文件缺失")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
                issues.append(f"{ref['path']} 文件已改变，需核对并追加修订")
        except (OSError, ValueError):
            issues.append(f"{ref['path']} 不可读取或引用不安全")
    return issues


def inspect(root, include_retired=False):
    root = Path(root).resolve()
    result = {"status": "empty", "project_id": None, "current_facts": [], "needs_confirmation": [],
              "needs_attention": [], "errors": [], "memory_version": 0,
              "digest": hashlib.sha256(b"").hexdigest(), "record_path": JOURNAL}
    try:
        path = _storage(root)
        raw = path.read_bytes() if path.exists() else b""
        result["digest"] = hashlib.sha256(raw).hexdigest()
        events, _ = history(root)
        identity = _project_identity(root, required=bool(events))
        result["project_id"] = identity["project_id"] if identity else None
        if identity and any(event["project_id"] != identity["project_id"] for event in events):
            raise WorkflowError("项目记忆含其他项目记录，已停止采用")
    except (OSError, ValueError, RecursionError) as exc:
        result.update(status="blocked", errors=[str(exc)], memory_version=None)
        return result
    result["memory_version"] = len(events)
    latest = {event["memory_id"]: event for event in events}
    for event in sorted(latest.values(), key=lambda item: item["memory_id"]):
        item = dict(event)
        issues = _reference_issues(root, event)
        if issues:
            item.update(effective_status="needs_attention", file_issues=issues)
            result["needs_attention"].append(item)
            result["errors"].extend(issues)
        elif event["status"] == "confirmed":
            result["current_facts"].append(item)
        elif event["status"] == "needs_confirmation":
            result["needs_confirmation"].append(item)
        elif include_retired:
            result.setdefault("retired", []).append(item)
    if events:
        result["status"] = "attention" if result["errors"] else "recorded"
    return result


def context(root):
    result = inspect(root)
    if result["errors"]:
        raise WorkflowError("；".join(result["errors"]))
    return {"current_facts": result["current_facts"],
            "memory_version": result["memory_version"]}
