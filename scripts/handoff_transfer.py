"""Create immutable manifests and record acceptance under the workspace lock."""
from __future__ import annotations
import json
import uuid
from pathlib import Path

from phase2_store import local
from workspace_lock import serialized
from handoff_inventory import BOOKKEEPING, file_inventory, inventory_digest, relative, sha256_bytes
from handoff_records import append_event, now, read_manifest, write_json_atomic
from handoff_inspection import inspect_workspace, unsafe_storage_report


def build_manifest(root: Path, report: dict[str, object], actor: str) -> dict[str, object]:
    entries = file_inventory(root)
    return {
        "schema_version": 1,
        "manifest_id": uuid.uuid4().hex,
        "kind": "project_handoff",
        "created_at": now(),
        "created_by": actor,
        "workspace": root.name,
        "project": report["project"],
        "current_stage": report["project"].get("current_stage"),
        "next_action": report["project"].get("next_action"),
        "status_at_creation": report["status"],
        "pending_transactions": report["pending_transactions"],
        "embedded_pending": report["embedded_pending"],
        "state_attention": report["state_attention"],
        "recent_records": report["recent_records"],
        "external_dependencies": report["external_dependencies"],
        "roles": report["roles"],
        "subagent_policy": report["subagent_policy"],
        "inventory": entries,
        "inventory_sha256": inventory_digest(entries),
        "excluded_from_inventory": sorted(BOOKKEEPING),
        "history_directory": "project/records/handoffs",
        "receiver_must_recheck": [
            "本机 Python、Node、浏览器及第三方依赖",
            "飞书和云端大脑账号、项目权限及连接回执",
            "Codex 是否支持并能实际调用独立 fresh Agent",
            "真实客户资料的保密和分发授权",
        ],
    }


def prepare(root: Path, actor: str) -> dict[str, object]:
    root = root.resolve()
    unsafe = unsafe_storage_report(root)
    if unsafe:
        return {"status": "blocked", "report": unsafe, "written": False}
    with serialized(root):
        return _prepare_unlocked(root, actor)


def _prepare_unlocked(root: Path, actor: str) -> dict[str, object]:
    report = inspect_workspace(root)
    if not report["handoff_ready"]:
        return {"status": "blocked", "report": report, "written": False}
    manifest = build_manifest(root, report, actor)
    path = root / "project/records/handoff-manifest.json"
    previous = None
    if path.is_file():
        previous = sha256_bytes(path.read_bytes())
        try:
            old_manifest = read_manifest(path)
            old_id = old_manifest["manifest_id"]
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            return {"status": "blocked", "report": report, "written": False,
                    "errors": [f"已有交接清单损坏，不能覆盖；先人工恢复或归档: {exc}"]}
        history_path = root / "project/records/handoffs" / f"{old_id}.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        if history_path.exists() and history_path.read_bytes() != path.read_bytes():
            return {"status": "blocked", "report": report, "written": False,
                    "errors": [f"交接清单历史版本冲突: {relative(root, history_path)}"]}
        if not history_path.exists():
            history_path.write_bytes(path.read_bytes())
    new_history_path = root / "project/records/handoffs" / f"{manifest['manifest_id']}.json"
    new_history_path.parent.mkdir(parents=True, exist_ok=True)
    manifest["manifest_history_path"] = relative(root, new_history_path)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if new_history_path.exists() and new_history_path.read_bytes() != manifest_bytes:
        return {"status": "blocked", "report": report, "written": False,
                "errors": [f"新交接清单历史版本已存在且内容冲突: {relative(root, new_history_path)}"]}
    if not new_history_path.exists():
        new_history_path.write_bytes(manifest_bytes)
    write_json_atomic(path, manifest)
    append_event(root, {
        "event": "handoff_prepared",
        "event_id": uuid.uuid4().hex,
        "at": manifest["created_at"],
        "actor": actor,
        "manifest_id": manifest["manifest_id"],
        "manifest_sha256": sha256_bytes(path.read_bytes()),
        "manifest_history_path": manifest["manifest_history_path"],
        "previous_manifest_sha256": previous,
        "inventory_sha256": manifest["inventory_sha256"],
        "status": report["status"],
    })
    return {"status": report["status"], "report": report, "manifest": manifest, "written": True}


def accept(root: Path, actor: str, manifest_id: str) -> dict[str, object]:
    root = root.resolve()
    unsafe = unsafe_storage_report(root)
    if unsafe:
        return {"status": "blocked", "report": unsafe, "accepted": False, "errors": unsafe["errors"]}
    with serialized(root):
        return _accept_unlocked(root, actor, manifest_id)


def _accept_unlocked(root: Path, actor: str, manifest_id: str) -> dict[str, object]:
    report = inspect_workspace(root)
    if report.get("storage_blocked"):
        return {"status": "blocked", "report": report, "accepted": False, "errors": report["errors"]}
    path = root / "project/records/handoff-manifest.json"
    if not path.is_file():
        return {"status": "blocked", "report": report, "accepted": False,
                "errors": ["缺少 handoff-manifest.json；先由移交方生成交接清单"]}
    try:
        manifest = read_manifest(path)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        return {"status": "blocked", "report": report, "accepted": False,
                "errors": [f"交接清单无法读取: {exc}"]}
    entries = file_inventory(root)
    actual_digest = inventory_digest(entries)
    errors = list(report["errors"])
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        errors.append("接收必须绑定移交方提供的 manifest_id")
    elif manifest.get("manifest_id") != manifest_id:
        errors.append("当前交接清单不是接收方指定的 manifest_id")
    history_path = manifest.get("manifest_history_path")
    if not isinstance(history_path, str):
        errors.append("交接清单缺少不可覆盖的历史版本路径")
    else:
        try:
            historical = local(root, history_path)
            if not historical.is_file() or sha256_bytes(historical.read_bytes()) != sha256_bytes(path.read_bytes()):
                errors.append("当前交接清单与不可覆盖历史版本不一致")
        except (OSError, ValueError):
            errors.append("交接清单历史版本路径无效")
    prepared_events = []
    events_path = root / "project/records/handoff-events.jsonl"
    if events_path.is_file():
        try:
            prepared_events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
                               if line.strip()]
            if any(not isinstance(item, dict) for item in prepared_events):
                prepared_events = []
                raise ValueError("交接事件必须是对象")
        except (OSError, UnicodeError, ValueError, RecursionError):
            errors.append("交接事件记录无法读取")
    prepared = next((item for item in reversed(prepared_events)
                     if item.get("event") == "handoff_prepared"
                     and item.get("manifest_id") == manifest.get("manifest_id")), None)
    if not prepared or prepared.get("manifest_sha256") != sha256_bytes(path.read_bytes()):
        errors.append("交接清单没有匹配的准备事件或清单指纹已变化")
    if actual_digest != manifest.get("inventory_sha256"):
        errors.append("交接后工作区文件内容或文件清单已变化，不能静默接收")
    expected = manifest.get("inventory")
    if entries != expected:
        errors.append("交接后文件列表、文件指纹或符号链接已变化")
    accepted = not errors
    event = {
        "event": "handoff_accepted" if accepted else "handoff_rejected",
        "event_id": uuid.uuid4().hex,
        "at": now(),
        "actor": actor,
        "manifest_id": manifest.get("manifest_id"),
        "manifest_sha256": sha256_bytes(path.read_bytes()),
        "inventory_sha256": actual_digest,
        "errors": errors,
        "warnings": report["warnings"],
    }
    append_event(root, event)
    return {
        "status": "accepted" if accepted else "blocked",
        "report": report,
        "accepted": accepted,
        "errors": errors,
        "warnings": report["warnings"],
        "manifest_id": manifest.get("manifest_id"),
    }
