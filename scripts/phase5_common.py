"""Shared version, dependency and review gates for Phase 5 local work."""
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from phase2_store import WorkflowError
from workspace_lock import serialized

INDEX = "project/records/phase5-artifacts.json"
EVENTS = "project/records/phase5-events.jsonl"
SUBMISSIONS = "project/records/design-submissions.json"
PASS = {"passed", "passed_with_yellow"}


def now():
    return datetime.now(timezone.utc).isoformat()


def local(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise WorkflowError("项目引用必须使用非空相对路径和 / 分隔符")
    if Path(relative).is_absolute() or PureWindowsPath(relative).drive or relative.startswith("~"):
        raise WorkflowError("项目引用不能使用绝对路径")
    target = (Path(root) / relative).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise WorkflowError("项目引用越界")
    return target


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha(path):
    return sha_bytes(Path(path).read_bytes())


def atomic_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".phase5-writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def atomic_json(path, value):
    atomic_bytes(path, json_bytes(value))


def registry(root):
    path = local(root, INDEX)
    return read_json(path) if path.exists() else {"schema_version": "0.1", "artifacts": {}}


def events(root, name=None):
    path = local(root, EVENTS)
    if not path.exists():
        return []
    result = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [item for item in result if name is None or item.get("event") == name]


def append_event(root, event, payload):
    with serialized(root):
        return _append_event_unlocked(root, event, payload)


def _append_event_unlocked(root, event, payload):
    path = local(root, EVENTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {**payload, "record_id": str(uuid.uuid4()), "created_at": now(), "event": event}
    previous = path.read_bytes() if path.exists() else b""
    events(root)
    raw = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    atomic_bytes(path, previous + raw)
    return record


def test_mode(root):
    try:
        return read_json(local(root, "project/state.json")).get("test_mode") is True
    except (OSError, ValueError, KeyError):
        return False


def ref(meta):
    return {"space": "phase5", "key": meta["key"], "version": meta["version"], "sha256": meta["sha256"]}


def phase3_ref(meta):
    return {"space": "phase3", "key": meta["key"], "version": meta["version"], "sha256": meta["sha256"]}


def latest(root, key):
    history = registry(root).get("artifacts", {}).get(key, [])
    if not history:
        raise WorkflowError(f"缺少 Phase 5 产出 {key}")
    return history[-1]


def _phase3_current(root, dependency):
    from phase3_store import current
    from phase3_store import ref as make_ref
    meta = current(root, dependency["key"])
    if make_ref(meta) != dependency:
        raise WorkflowError(f"上游 {dependency['key']} 已更新，当前稿需重新整理")
    return meta


def resolve(root, dependency, seen=()):
    if not isinstance(dependency, dict) or dependency.get("space") not in {
            "phase3", "phase5", "design-submission"}:
        raise WorkflowError("Phase 5 只接受 phase3、phase5 或 design-submission 版本引用")
    marker = (dependency["space"], dependency["key"])
    if marker in seen:
        raise WorkflowError("Phase 5 版本依赖存在循环")
    if dependency["space"] == "phase3":
        return _phase3_current(root, dependency)
    if dependency["space"] == "phase5":
        meta = current(root, dependency["key"], seen)
        if ref(meta) != dependency:
            raise WorkflowError(f"下游依赖 {dependency['key']} 已更新")
        return meta
    return submission_current(root, dependency)


def check_files(root, meta):
    for item in meta.get("source_files", []):
        if not local(root, item["path"]).is_file() or sha(local(root, item["path"])) != item["sha256"]:
            raise WorkflowError("修订授权依据丢失或改变")
    for item in meta.get("current_files", []):
        path = local(root, item["path"])
        if not path.is_file() or sha(path) != item["sha256"]:
            raise WorkflowError(f"{meta['key']} 存在未登记修改或文件丢失；先保留修改，不覆盖")
    for item in meta.get("snapshot_files", []):
        path = local(root, item["path"])
        if not path.is_file() or sha(path) != item["sha256"]:
            raise WorkflowError(f"{meta['key']} 历史快照丢失或被修改")


def current(root, key, seen=()):
    from phase5_io import PENDING
    if local(root, PENDING).exists():
        raise WorkflowError("上次写入未结束；请先恢复")
    meta = latest(root, key)
    marker = ("phase5", key)
    if marker in seen:
        raise WorkflowError("Phase 5 版本依赖存在循环")
    check_files(root, meta)
    for dependency in meta.get("dependencies", []):
        resolve(root, dependency, seen + (marker,))
    return meta


def submission_registry(root):
    path = local(root, SUBMISSIONS)
    return read_json(path) if path.exists() else {"schema_version": "0.1", "submissions": {}}


def submission_current(root, dependency):
    item = submission_registry(root).get("submissions", {}).get(dependency["key"])
    if not item or item.get("sha256") != dependency.get("sha256"):
        raise WorkflowError(f"设计稿 {dependency.get('key')} 已更新、缺失或未登记")
    path = local(root, item["path"])
    if not path.is_file() or sha(path) != item["sha256"]:
        raise WorkflowError("设计稿文件指纹不一致，不能使用旧检核")
    return item


def required_sources(root, meta, include_current=True):
    values = list(meta.get("source_files", []))
    if include_current:
        values.extend(meta.get("current_files", []))
        values.extend(meta.get("snapshot_files", []))
    for dependency in meta.get("dependencies", []):
        if dependency["space"] == "phase3":
            from phase3_review import required_sources as phase3_sources
            upstream = _phase3_current(root, dependency)
            values.extend(phase3_sources(root, upstream))
        elif dependency["space"] == "phase5":
            values.extend(required_sources(root, current(root, dependency["key"])))
        else:
            item = submission_current(root, dependency)
            values.append({"path": item["path"], "sha256": item["sha256"]})
    return [{"path": path, "sha256": digest} for path, digest in sorted(
        {(x["path"], x["sha256"]) for x in values})]


def _version_number(root, key, folder):
    history = registry(root).get("artifacts", {}).get(key, [])
    used = [item["version"] for item in history]
    for path in local(root, folder).glob("v*") if local(root, folder).exists() else []:
        if path.is_file() and path.stem[1:].isdigit():
            used.append(int(path.stem[1:]))
    return max(used + [0]) + 1


def publish(root, *, key, kind, task_id, author, payload, markdown, dependencies,
            file_layout, revision=None):
    with serialized(root):
        return _publish_unlocked(root, key=key, kind=kind, task_id=task_id, author=author,
                                 payload=payload, markdown=markdown, dependencies=dependencies,
                                 file_layout=file_layout, revision=revision)


def _publish_unlocked(root, *, key, kind, task_id, author, payload, markdown, dependencies,
                      file_layout, revision=None):
    from phase5_io import PENDING, write_transaction
    if local(root, PENDING).exists():
        raise WorkflowError("上次写入未结束；请先恢复")
    if not isinstance(author, str) or not author.strip():
        raise WorkflowError("必须提供实际作者执行实例")
    if not isinstance(payload, dict):
        raise WorkflowError("Phase 5 产出必须是 JSON 对象")
    if kind not in {"proposal-script", "html-deck"} or key != f"{task_id}::{kind}":
        raise WorkflowError("产出编号与任务、类别必须一致，不能改名清零轮次")
    dependencies = list(dependencies)
    if any(not isinstance(item, dict) for item in dependencies):
        raise WorkflowError("上游须为版本引用对象列表")
    for dependency in dependencies:
        resolve(root, dependency, (("phase5", key),))
    reg = registry(root)
    history = reg.setdefault("artifacts", {}).setdefault(key, [])
    previous = history[-1] if history else None
    payload = {k: v for k, v in payload.items() if k not in {"version", "revision"}}
    content_hash = sha_bytes(json_bytes(payload))
    if previous:
        check_files(root, previous)
        if previous.get("content_hash") == content_hash and previous.get(
                "dependencies") == dependencies:
            return current(root, key)
    from phase5_decisions import revision_details
    revision, source_files = revision_details(root, previous, history, revision)
    version = _version_number(root, key, file_layout["folder"])
    payload["version"] = version
    if revision:
        payload["revision"] = revision
    content = json_bytes(payload)
    digest = sha_bytes(content)
    snapshot_files = []
    current_files = []
    files = []
    for role, suffix, raw in file_layout["files"](version, content, markdown):
        snapshot = file_layout["snapshot"](version, role, suffix)
        pointer = file_layout["current"](role, suffix)
        snapshot_path = local(root, snapshot)
        if snapshot_path.exists():
            raise WorkflowError("版本快照已存在，拒绝覆盖历史")
        files.append((snapshot, raw))
        snapshot_files.append({"role": role, "path": snapshot, "sha256": sha_bytes(raw)})
        current_files.append({"role": role, "path": pointer, "sha256": sha_bytes(raw)})
    meta = {"key": key, "kind": kind, "task_id": task_id, "version": version,
            "author_instance": author, "created_at": now(), "sha256": digest,
            "content_hash": content_hash, "dependencies": dependencies,
            "current_files": current_files, "snapshot_files": snapshot_files,
            "revision": revision, "source_files": source_files}
    return write_transaction(root, meta, previous, files)


def last_review(root, target):
    from phase5_review import last_review as impl
    return impl(root, target)


def valid_event_file(root, item):
    from phase5_review import valid_event_file as impl
    return impl(root, item)


def review_checks(root, meta, report, *, required_scope=()):
    from phase5_review import review_checks as impl
    return impl(root, meta, report, required_scope=required_scope)


def record_review(root, key, report_path, required_scope=()):
    from phase5_review import record_review as impl
    return impl(root, key, report_path, required_scope)


def gate(root, key, *, human=False):
    from phase5_review import gate as impl
    return impl(root, key, human=human)


def confirm(root, key, evidence_path, actor, simulation=False, status="confirmed"):
    from phase5_review import confirm as impl
    return impl(root, key, evidence_path, actor, simulation, status)


def status_for(root, key):
    from phase5_review import status_for as impl
    return impl(root, key)
def all_status(root):
    return {key: status_for(root, key) for key in registry(root).get("artifacts", {})}
