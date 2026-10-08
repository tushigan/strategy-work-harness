"""Single-controller transactions with explicit crash recovery and conflict retention."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from phase2_store import (WorkflowError, atomic_json, json_bytes, local, read_json, sha,
                           workspace_write_lock)

INDEX = "project/records/phase3-artifacts.json"
PENDING = "project/records/phase3-pending.json"


def registry(root):
    path = local(root, INDEX)
    return read_json(path) if path.exists() else {"schema_version": "0.3", "artifacts": {}}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def preserve(root, relative):
    path = local(root, relative)
    if path.is_file():
        target = local(root, f"project/outputs/conflicts/phase3/{sha(path)}{path.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(path, target)
        return target.relative_to(Path(root).resolve()).as_posix()
    return None


def check_pointers(root, meta):
    for field, hash_field in (("path", "sha256"), ("markdown_path", "markdown_sha256")):
        path = local(root, meta[field])
        if not path.is_file() or sha(path) != meta[hash_field]:
            kept = preserve(root, meta[field])
            raise WorkflowError(f"存在未登记人工修改或文件丢失；不覆盖。保留位置：{kept}")


def write_transaction(root, meta, payload, markdown):
    with workspace_write_lock(root):
        return _write_transaction_unlocked(root, meta, payload, markdown)


def _write_transaction_unlocked(root, meta, payload, markdown):
    pending = local(root, PENDING)
    if pending.exists():
        raise WorkflowError("有未结束的写入；先运行 recover，不把半成品当当前版本")
    reg = registry(root)
    history = reg["artifacts"].get(meta["key"], [])
    previous = history[-1] if history else None
    if previous:
        check_pointers(root, previous)
    files = [(meta["snapshot_path"], json_bytes(payload)),
             (meta["markdown_snapshot"], markdown.encode("utf-8"))]
    for name, data in files:
        target = local(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    transaction = {"meta": meta, "previous": previous}
    atomic_json(pending, transaction)
    return _recover_unlocked(root)


def recover(root):
    with workspace_write_lock(root):
        return _recover_unlocked(root)


def _recover_unlocked(root):
    pending = local(root, PENDING)
    if not pending.exists():
        return {"status": "nothing_pending"}
    transaction = read_json(pending)
    meta, previous = transaction["meta"], transaction["previous"]
    reg = registry(root)
    history = reg["artifacts"].setdefault(meta["key"], [])
    already = bool(history and history[-1] == meta)
    if not already and (history[-1] if history else None) != previous:
        raise WorkflowError("恢复失败：版本库已发生另一笔修改，需人工合并")
    pairs = (("path", "snapshot_path", "sha256"),
             ("markdown_path", "markdown_snapshot", "markdown_sha256"))
    for pointer, snapshot, hash_field in pairs:
        source = local(root, meta[snapshot])
        if not source.is_file() or sha(source) != meta[hash_field]:
            raise WorkflowError("恢复失败：待写入快照丢失或指纹不符")
        dest = local(root, meta[pointer])
        actual = sha(dest) if dest.is_file() else None
        expected = previous[hash_field] if previous else None
        if actual not in {expected, meta[hash_field]}:
            kept = preserve(root, meta[pointer])
            raise WorkflowError(f"恢复遇到人工修改，已保留 {kept}，未覆盖")
    for pointer, snapshot, _ in pairs:
        atomic_bytes(local(root, meta[pointer]), local(root, meta[snapshot]).read_bytes())
    if not already:
        history.append(meta)
        atomic_json(local(root, INDEX), reg)
    pending.unlink()
    return meta
