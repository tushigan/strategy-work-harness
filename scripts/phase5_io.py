"""Recoverable local writes for Phase 5; snapshots never overwrite history."""
import os
import shutil

from phase2_store import WorkflowError, workspace_write_lock

PENDING = "project/records/phase5-pending.json"


def recover(root):
    with workspace_write_lock(root):
        return _recover_unlocked(root)


def _recover_unlocked(root):
    from phase5_common import (INDEX, atomic_bytes, atomic_json, local, read_json,
                               registry, sha)
    pending = local(root, PENDING)
    if not pending.exists():
        return {"status": "nothing_pending"}
    transaction = read_json(pending)
    meta, previous = transaction["meta"], transaction["previous"]
    reg = registry(root)
    history = reg["artifacts"].setdefault(meta["key"], [])
    already = bool(history and history[-1] == meta)
    if not already and (history[-1] if history else None) != previous:
        raise WorkflowError("版本库出现另一笔修改，不能自动恢复")
    old = {x["path"]: x["sha256"] for x in (previous or {}).get("current_files", [])}
    pairs = list(zip(meta["current_files"], meta["snapshot_files"], strict=True))
    for pointer, snapshot in pairs:
        source, dest = local(root, snapshot["path"]), local(root, pointer["path"])
        if not source.is_file() or sha(source) != snapshot["sha256"]:
            raise WorkflowError("待恢复的历史快照丢失或改变")
        actual = sha(dest) if dest.exists() else None
        if actual not in {old.get(pointer["path"]), pointer["sha256"]}:
            kept = local(root, f"project/outputs/conflicts/phase5/{actual}{dest.suffix}")
            kept.parent.mkdir(parents=True, exist_ok=True)
            if not kept.exists():
                shutil.copyfile(dest, kept)
            raise WorkflowError("遇到未登记人工修改；已保留冲突，不覆盖")
    for pointer, snapshot in pairs:
        atomic_bytes(local(root, pointer["path"]), local(root, snapshot["path"]).read_bytes())
    if not already:
        history.append(meta)
        atomic_json(local(root, INDEX), reg)
    pending.unlink()
    return meta


def write_transaction(root, meta, previous, files):
    with workspace_write_lock(root):
        return _write_transaction_unlocked(root, meta, previous, files)


def _write_transaction_unlocked(root, meta, previous, files):
    from phase5_common import atomic_json, local
    pending = local(root, PENDING)
    if pending.exists():
        raise WorkflowError("上次写入未结束；请先恢复")
    for snapshot, raw in files:
        path = local(root, snapshot)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    atomic_json(pending, {"meta": meta, "previous": previous})
    return recover(root)
