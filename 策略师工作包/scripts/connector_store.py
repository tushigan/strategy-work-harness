"""Connector-only atomic journal, serialized across processes on supported Mac."""
import fcntl
import json
import os
import uuid
from contextlib import contextmanager

import phase5_common as c
import connector_safety as s
from workspace_lock import serialized

EVENTS = "project/records/connector-events.jsonl"
LOCK = "project/records/.connector.lock"
STATUSES = {"pending", "synced", "unknown", "denied", "unavailable", "read",
            "partial", "empty", "blocked"}


def events(root):
    path = c.local(root, EVENTS)
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise c.WorkflowError("Connector journal is incomplete; manual investigation required")
    result, previous, ids = [], "", set()
    current_scope = s.scope(root)
    for line in raw.splitlines():
        item = json.loads(line)
        if not isinstance(item, dict):
            s.fail()
        checksum = item.get("event_sha256")
        payload = {k: v for k, v in item.items() if k != "event_sha256"}
        s.safe(payload)
        s.simulation_guard(root, payload)
        if (item.get("schema") != 1 or item.get("previous") != previous
                or checksum != s.digest(payload) or item.get("scope") != current_scope
                or item.get("status") not in STATUSES
                or not isinstance(item.get("event_id"), str) or item["event_id"] in ids):
            raise c.WorkflowError("Connector journal is invalid or belongs to another scope")
        ids.add(item["event_id"])
        previous = checksum
        result.append(item)
    return result


@contextmanager
def locked(root):
    s.scope(root)
    path = c.local(root, LOCK)
    c.local(root, EVENTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def append(root, payload):
    """Caller holds locked(root); failure leaves the preceding valid history intact."""
    with serialized(root):
        return _append_unlocked(root, payload)


def _append_unlocked(root, payload):
    history = events(root)
    record = {**payload, "schema": 1, "event_id": uuid.uuid4().hex, "time": c.now(),
              "previous": history[-1]["event_sha256"] if history else "",
              "simulation": s.simulated(root)}
    if record["status"] not in STATUSES or record["scope"] != s.scope(root):
        s.fail()
    s.safe(record)
    record["event_sha256"] = s.digest(record)
    path = c.local(root, EVENTS)
    raw = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
    c.atomic_bytes(path, (path.read_bytes() if path.exists() else b"") + raw)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return record


def result(item, *, duplicate=False):
    keys = ("provider", "operation", "request_id", "source_id", "scope", "status",
            "time", "sources", "simulation", "remote_result")
    return {**{k: item[k] for k in keys if k in item}, "duplicate": duplicate,
            "local_work_can_continue": True,
            "needs_remote_verification": item["status"] in {"pending", "unknown"},
            "next_action": "none" if item["status"] in {"synced", "read", "empty"} else
                           "use_local_evidence; investigate_or_explicitly_reconcile"}
