"""Append-only cross-workflow decisions. Current state is a disposable cache."""
import uuid
from pathlib import Path

from phase2_store import WorkflowError, fingerprint, local, read_json, sha, test_mode
from phase5_common import atomic_bytes, json_bytes, now
from workspace_lock import serialized

LOG = "project/records/phase6-events.jsonl"


def events(root, event=None):
    import json
    path = local(root, LOG)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip()] if path.exists() else []
    if any(not isinstance(x, dict) for x in records):
        raise WorkflowError("第六阶段事件记录损坏；保留原件并核对")
    return [x for x in records if event is None or x.get("event") == event]


def append(root, event, payload, *, unique=False):
    with serialized(root):
        return _append_unlocked(root, event, payload, unique=unique)


def _append_unlocked(root, event, payload, *, unique=False):
    if {"record_id", "created_at", "event", "fingerprint"} & payload.keys():
        raise WorkflowError("事件包含系统保留字段")
    prior = events(root)
    digest = fingerprint({"event": event, "payload": payload})
    if unique:
        # Repeated state is idempotent only until a newer decision supersedes it.
        matches = [x for x in prior if x.get("event") == event]
        if matches and matches[-1].get("fingerprint") == digest:
            return matches[-1]
    record = {**payload, "event": event, "fingerprint": digest,
              "record_id": str(uuid.uuid4()), "created_at": now()}
    path = local(root, LOG)
    raw = path.read_bytes() if path.exists() else b""
    import json
    atomic_bytes(path, raw + (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode())
    return record


def valid_file(root, item):
    return isinstance(item, dict) and isinstance(item.get("path"), str) and (
        local(root, item["path"]).is_file() and sha(local(root, item["path"])) == item.get("sha256"))


def archive(root, path):
    path = Path(path)
    raw = path.read_bytes()
    if not raw.strip():
        raise WorkflowError("原文不能为空")
    relative = f"project/records/phase6-evidence/{sha(path)}{path.suffix.lower()}"
    target = local(root, relative)
    if target.exists() and target.read_bytes() != raw:
        raise WorkflowError("归档指纹冲突")
    if not target.exists():
        atomic_bytes(target, raw)
    return {"path": relative, "sha256": sha(target)}


def actor_check(root, actor, simulation):
    if not isinstance(actor, str) or not actor.strip() or type(simulation) is not bool:
        raise WorkflowError("须提供实际记录者及明确的 simulation")
    if simulation and not test_mode(root):
        raise WorkflowError("模拟决定仅限 test_mode 合成案例")


def decision_valid(root, record):
    return bool(record and valid_file(root, record.get("evidence")) and
                (not record.get("simulation") or test_mode(root)))


def run_cli(callback):
    try:
        value = callback()
        print(json_bytes(value).decode(), end="")
    except (WorkflowError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
