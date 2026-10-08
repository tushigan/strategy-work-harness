"""Atomic event append for the single-controller Phase 3 workflow."""
import json
import uuid

from phase2_store import WorkflowError, events, local, now
from phase3_io import atomic_bytes
from workspace_lock import serialized


def append(root, name, event):
    with serialized(root):
        return _append_unlocked(root, name, event)


def _append_unlocked(root, name, event):
    path = local(root, f"project/records/{name}.jsonl")
    old = path.read_bytes() if path.exists() else b""
    try:
        for line in old.decode("utf-8").splitlines():
            if line.strip():
                json.loads(line)
    except (UnicodeError, ValueError) as exc:
        raise WorkflowError("已有事件记录损坏；保留原文件并人工核对，不追加掩盖") from exc
    record = {"record_id": str(uuid.uuid4()), "created_at": now(),
              "record_type": name, "actor": "local-script", **event}
    line = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    atomic_bytes(path, old + (b"\n" if old and not old.endswith(b"\n") else b"") + line)
    return record
