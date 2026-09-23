"""Portable, append-only artifact versions for the contract workflow."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from workspace_lock import serialized as workspace_write_lock


class WorkflowError(ValueError):
    pass




def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(data):
    return hashlib.sha256(json_bytes(data)).hexdigest()


def local(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise WorkflowError("项目引用必须使用非空相对路径和 / 分隔符")
    if Path(relative).is_absolute() or PureWindowsPath(relative).drive or relative.startswith("~"):
        raise WorkflowError("项目引用不能使用绝对路径")
    target = (Path(root) / relative).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise WorkflowError("项目引用越界")
    return target


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json_bytes(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def append(root, name, event):
    with workspace_write_lock(root):
        return _append_unlocked(root, name, event)


def _append_unlocked(root, name, event):
    path = local(root, f"project/records/{name}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"record_id": str(uuid.uuid4()), "created_at": now(),
              "record_type": name, "actor": "local-script", **event}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if name in {"artifacts", "reviews", "confirmations", "export-checks", "system-imports", "template-events"}:
        from workflow_status import refresh
        refresh(root)
    return record


def events(root, name):
    path = local(root, f"project/records/{name}.jsonl")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def registry(root):
    path = local(root, "project/records/phase2-artifacts.json")
    return read_json(path) if path.exists() else {"schema_version": "0.2", "artifacts": {}}


def latest(root, kind):
    values = registry(root)["artifacts"].get(kind, [])
    if not values:
        raise WorkflowError(f"缺少 {kind} 产出")
    return values[-1]


def current(root, kind, seen=None):
    meta = latest(root, kind)
    seen = set(seen or ())
    if kind in seen:
        raise WorkflowError("版本依赖存在循环")
    seen.add(kind)
    for field in ("path", "snapshot_path"):
        path = local(root, meta[field])
        if not path.is_file() or sha(path) != meta["sha256"]:
            raise WorkflowError(f"{kind} 内容已改变或丢失，请登记新版本并重新检核")
    for dep in meta["dependencies"]:
        upstream = current(root, dep["kind"], seen)
        if upstream["sha256"] != dep["sha256"] or upstream["version"] != dep["version"]:
            raise WorkflowError(f"{kind} 上游 {dep['kind']} 已改变，需重新整理")
    for ref in meta.get("source_files", []):
        path = local(root, ref["path"])
        if not path.is_file() or sha(path) != ref["sha256"]:
            raise WorkflowError(f"{kind} 来源文件改变或丢失")
    return meta


def ref(meta):
    return {key: meta[key] for key in ("kind", "version", "sha256")}


def archive(root, source, category):
    source = Path(source)
    if not source.is_file():
        raise WorkflowError("输入文件不存在")
    if category not in {"contracts", "templates", "evidence"}:
        raise WorkflowError("无效的归档类别")
    relative = f"project/inputs/{category}/{sha(source)}{source.suffix.lower()}"
    target = local(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst)
    if sha(target) != sha(source):
        raise WorkflowError("归档文件指纹不一致")
    return {"path": relative, "sha256": sha(target), "name": source.name}


def test_mode(root):
    return read_json(local(root, "project/state.json")).get("test_mode") is True


def revision_budget(root, kind):
    count = max(0, len(registry(root)["artifacts"].get(kind, [])) - 1)
    allowance = 2
    for event in events(root, "phase2-decisions"):
        if event.get("kind") == kind and event.get("status") == "allow_more_revisions":
            evidence = event.get("evidence", {})
            path = local(root, evidence.get("path", "missing"))
            if path.is_file() and sha(path) == evidence.get("sha256"):
                if not event.get("simulation") or test_mode(root):
                    allowance = max(allowance, event["allowed_total"])
    return count, allowance


def ensure_revision_allowed(root, kind):
    count, allowance = revision_budget(root, kind)
    if registry(root)["artifacts"].get(kind) and count >= allowance:
        raise WorkflowError(f"{kind} 已达自动修订上限 {allowance}，保留历史并请策略师决定")
    if count:
        from phase2_review_guard import registered_review
        registered_review(root, latest(root, kind))


def next_version(root, kind, history):
    folder = local(root, f"project/outputs/versions/{kind}")
    used = [int(p.stem[1:]) for p in folder.glob("v*")
            if p.is_file() and re.fullmatch(r"v\d+", p.stem)]
    return max([m["version"] for m in history] + used + [0]) + 1


def preserve_conflict(root, meta):
    pointer = local(root, meta["path"])
    if meta["path"] == meta["snapshot_path"] or not pointer.is_file() or sha(pointer) == meta["sha256"]:
        return
    digest = sha(pointer)
    relative = f"project/outputs/conflicts/{meta['kind']}/{digest}.json"
    target = local(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with pointer.open("rb") as source, target.open("xb") as dest:
            shutil.copyfileobj(source, dest)
    raise WorkflowError(f"{meta['kind']} 有未登记的人工修改，已保留在 {relative}；"
                        "请对比登记版和人工版，再用 resolve_conflict.py 记录选择，不能覆盖")


def publish(root, kind, payload, author, dependencies=(), source_files=()):
    with workspace_write_lock(root):
        return _publish_unlocked(root, kind, payload, author, dependencies, source_files)


def _publish_unlocked(root, kind, payload, author, dependencies=(), source_files=()):
    if kind not in {"contract", "plan", "gantt"} or not author.strip():
        raise WorkflowError("无效产出类别或作者实例")
    reg = registry(root)
    history = reg["artifacts"].setdefault(kind, [])
    if history:
        preserve_conflict(root, history[-1])
    data = json_bytes(payload) if not isinstance(payload, bytes) else payload
    digest = hashlib.sha256(data).hexdigest()
    dependencies = list(dependencies)
    source_files = list(source_files)
    # Repeating an unchanged contract resolution only adds a redundant decision input.
    repeated_resolution = (history and kind == "contract" and source_files
                           and source_files[:-1] == history[-1]["source_files"]
                           and source_files[-1]["path"].startswith("project/inputs/evidence/"))
    if (history and history[-1]["sha256"] == digest and history[-1]["dependencies"] == dependencies
            and (history[-1]["source_files"] == source_files or repeated_resolution)):
        return current(root, kind)
    ensure_revision_allowed(root, kind)
    version = next_version(root, kind, history)
    suffix = "xlsx" if kind == "gantt" else "json"
    snapshot = f"project/outputs/versions/{kind}/v{version:04d}.{suffix}"
    names = {"contract": "contract-items", "plan": "task-plan"}
    pointer = f"project/tasks/{names[kind]}.json" if kind != "gantt" else snapshot
    target = local(root, snapshot)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(data)
    if pointer != snapshot:
        atomic_json(local(root, pointer), payload)
    meta = {"kind": kind, "artifact_id": kind, "version": version, "path": pointer,
            "snapshot_path": snapshot, "sha256": digest, "author_instance": author,
            "created_at": now(), "dependencies": dependencies, "source_files": list(source_files)}
    history.append(meta)
    atomic_json(local(root, "project/records/phase2-artifacts.json"), reg)
    append(root, "artifacts", {"record_type": "artifact", "actor": author, "status": "draft", **meta})
    return meta


def publish_file(root, kind, source_path, author, dependencies=(), source_files=()):
    """Publish an already-created binary artifact while retaining the same pointer rules."""
    with workspace_write_lock(root):
        return _publish_file_unlocked(root, kind, source_path, author, dependencies, source_files)


def _publish_file_unlocked(root, kind, source_path, author, dependencies=(), source_files=()):
    source_path = Path(source_path)
    if kind != "gantt" or not source_path.is_file() or not author.strip():
        raise WorkflowError("只允许发布存在的甘特 Excel 文件")
    reg = registry(root)
    history = reg["artifacts"].setdefault(kind, [])
    digest = sha(source_path)
    dependencies = list(dependencies)
    if (history and history[-1]["sha256"] == digest and history[-1]["dependencies"] == dependencies
            and history[-1]["source_files"] == list(source_files)):
        return current(root, kind)
    ensure_revision_allowed(root, kind)
    version = next_version(root, kind, history)
    snapshot = f"project/outputs/versions/{kind}/v{version:04d}.xlsx"
    target = local(root, snapshot)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as source, target.open("xb") as dest:
        shutil.copyfileobj(source, dest)
    meta = {"kind": kind, "artifact_id": kind, "version": version, "path": snapshot,
            "snapshot_path": snapshot, "sha256": digest, "author_instance": author,
            "created_at": now(), "dependencies": dependencies, "source_files": list(source_files)}
    history.append(meta)
    atomic_json(local(root, "project/records/phase2-artifacts.json"), reg)
    append(root, "artifacts", {"record_type": "artifact", "actor": author, "status": "draft", **meta})
    return meta


def output_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
