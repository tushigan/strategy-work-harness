"""Rebuild the Phase 2 status from versioned evidence, never from a cached approval."""
import argparse
from pathlib import Path

from phase2_store import (WorkflowError, atomic_json, current, events, local, now, output_json,
                         read_json, ref, registry, test_mode)
from workspace_lock import serialized


def import_state(root, meta=None):
    values = registry(root)["artifacts"].get("gantt", [])
    meta = meta or (values[-1] if values else None)
    if not meta:
        return {"status": "not_applicable", "latest_import_id": None}
    matching = [e for e in events(root, "system-imports") if e.get("target") == ref(meta)]
    event = matching[-1] if matching else {}
    allowed = {"not_uploaded", "pending_manual_upload", "succeeded", "failed", "unknown"}
    status = event.get("status", "not_uploaded")
    return {"status": status if status in allowed else "unknown", "latest_import_id": event.get("record_id")}


def inspect(root):
    from phase2_review import gate
    artifacts = registry(root)["artifacts"]
    result = {}
    for kind in ("contract", "plan", "gantt"):
        if not artifacts.get(kind):
            result[kind] = {"status": "not_started"}
            continue
        meta = artifacts[kind][-1]
        item = {"target": ref(meta), "path": meta["path"], "status": "draft"}
        try:
            current(root, kind)
        except (WorkflowError, OSError, ValueError, KeyError) as exc:
            item.update({"status": "stale", "reason": str(exc)})
            result[kind] = item
            continue
        try:
            gate(root, kind)
            item["status"] = ("test_ready_not_for_upload" if test_mode(root)
                              else "ready_for_manual_upload") if kind == "gantt" else "confirmed"
        except (WorkflowError, OSError, ValueError, KeyError) as exc:
            item["reason"] = str(exc)
        result[kind] = item
    result["system_import"] = import_state(root)["status"]
    return result


def refresh(root):
    with serialized(root):
        return _refresh_unlocked(root)


def _refresh_unlocked(root):
    path = local(root, "project/state.json")
    state = read_json(path)
    state["phase2"] = inspect(root)
    kinds = [k for k in ("contract", "plan", "gantt") if state["phase2"][k]["status"] != "not_started"]
    if kinds:
        kind = kinds[-1]
        item = state["phase2"][kind]
        approved = item["status"] in {"confirmed", "ready_for_manual_upload", "test_ready_not_for_upload"}
        state["artifact_status"] = {"status": "approved" if approved else item["status"],
                                    "current_artifact_id": kind,
                                    "latest_artifact_version": item["target"]["version"]}
        for name, field, default in (("reviews", "machine_review", "not_started"),
                                     ("confirmations", "human_confirmation", "not_requested")):
            matching = [e for e in events(root, name) if e.get("target") == item["target"]]
            latest = matching[-1] if matching else {}
            key = "latest_review_id" if name == "reviews" else "latest_confirmation_id"
            effective = approved
            if name == "reviews" and not effective:
                from phase2_review import gate
                try:
                    gate(root, kind, human=False)
                    effective = True
                except (WorkflowError, OSError, ValueError, KeyError):
                    pass
            recorded = latest.get("status", default)
            effective_status = recorded
            if not effective and recorded in {"passed", "passed_with_yellow", "confirmed"}:
                effective_status = "not_started" if name == "reviews" else "superseded"
            state[field] = {"status": effective_status, "recorded_status": recorded,
                            "approval_effective": effective, key: latest.get("record_id")}
        state["next_action"] = ("仅供测试，不得上传" if test_mode(root) else "由策略师核对系统预览后手动上传") if (
            approved and kind == "gantt") else item.get("reason", {"contract": "整理任务排期", "plan": "生成 Excel 候选", "gantt": "检核实际文件"}[kind])
        state["current_stage"] = "contract_scope"
        state["system_import"] = import_state(root)
        if kind == "gantt" and state["system_import"]["status"] in {"succeeded", "failed", "unknown", "pending_manual_upload"}:
            state["next_action"] = ("已有实际导入成功记录，不重复上传" if state["system_import"]["status"] == "succeeded"
                                    else "先核查项管系统现状与已有导入记录，不盲目重传")
    state["last_updated"] = now()
    atomic_json(path, state)
    if local(root, "project/records/phase3-artifacts.json").exists():
        from phase3_tasks import refresh as refresh_phase3
        refresh_phase3(root)
    return state["phase2"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true", help="同步重建状态入口，不产生业务批准")
    args = parser.parse_args()
    output_json(refresh(args.workspace) if args.refresh else inspect(args.workspace))


if __name__ == "__main__":
    main()
