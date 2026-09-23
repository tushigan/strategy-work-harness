"""Read, summarize and persist handoff records without rewriting their format."""
from __future__ import annotations
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from handoff_inventory import inventory_digest, is_ignored, relative


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def recent_records(root: Path) -> dict[str, object]:
    """Summarize append-only logs without treating a cache as current truth."""
    records_dir = root / "project/records"
    result: dict[str, object] = {}
    if not records_dir.is_dir():
        return result
    for path in sorted(records_dir.glob("*.jsonl")):
        count = 0
        latest: dict[str, object] | None = None
        try:
            for line in path.read_text(encoding="utf-8").split("\n"):
                if not line.strip():
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    count += 1
                    latest = {
                        key: value[key] for key in (
                            "record_id", "event", "record_type", "created_at", "status",
                            "target", "key", "task_id",
                        ) if key in value
                    }
                    # Bound every retained field before it can enter a report or manifest.
                    pending = [(child, 1) for child in latest.values()]
                    while pending:
                        child, depth = pending.pop()
                        if isinstance(child, (dict, list)):
                            if depth > 64:
                                raise ValueError("记录摘要嵌套超过 64 层，先人工核对")
                            children = child.values() if isinstance(child, dict) else child
                            pending.extend((item, depth + 1) for item in children)
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            result[relative(root, path)] = {"status": "unreadable", "error": str(exc)}
            continue
        result[relative(root, path)] = {"count": count, "latest": latest}
    return result


def pending_markers(root: Path) -> list[dict[str, str]]:
    """Find embedded pending markers such as HTML alignment or external writes."""
    found: list[dict[str, str]] = []

    def walk(value: object, path: str, source: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                if key in {"pending_body_alignment", "sync_pending"} and child:
                    found.append({"path": source, "field": child_path, "status": "pending"})
                if key == "status" and child in {"pending", "unknown", "needs_auth"}:
                    found.append({"path": source, "field": child_path, "status": str(child)})
                walk(child, child_path, source)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", source)

    records_dir = root / "project/records"
    if not records_dir.is_dir():
        return found
    for path in sorted(records_dir.glob("*.json")):
        if is_ignored(root, path):
            continue
        try:
            walk(read_json(path), "", relative(root, path))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, RecursionError):
            continue
    return found


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_event(root: Path, event: dict[str, object]) -> None:
    path = root / "project/records/handoff-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - macOS supplies fcntl.
            pass
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover
            pass


def read_manifest(path: Path) -> dict[str, object]:
    manifest = read_json(path)
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or manifest.get("kind") != "project_handoff"
            or not isinstance(manifest.get("manifest_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", manifest["manifest_id"])
            or manifest.get("manifest_history_path") != f"project/records/handoffs/{manifest['manifest_id']}.json"
            or not isinstance(manifest.get("inventory"), list)
            or inventory_digest(manifest["inventory"]) != manifest.get("inventory_sha256")):
        raise ValueError("交接清单结构、编号、历史路径或指纹无效")
    return manifest
