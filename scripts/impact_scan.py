#!/usr/bin/env python3
"""Read dependency changes transitively; append observations without editing history."""
import argparse
from pathlib import Path

from phase2_store import atomic_json, local, read_json, sha
from phase6_events import append, events, run_cli, valid_file
from phase6_targets import catalog, identity, last_review, resolve
from review_gate import gate
from workspace_lock import serialized


def history_records(root):
    from phase2_store import events as p2_events
    from phase3_events import events as p3_events
    from phase5_common import events as p5_events
    collected = []
    for name in ("reviews", "confirmations"):
        for event in p2_events(root, name):
            if not event.get("target"):
                continue
            target = {"space": "phase2", "key": event["target"]["kind"], **event["target"]}
            collected.append((name, event, target))
    for name in ("phase3-reviews", "phase3-confirmations", "phase4-reviews"):
        collected.extend((name, event, event["target"]) for event in p3_events(root, name)
                         if event.get("target"))
    for name in ("review", "confirmation", "design_review"):
        collected.extend((name, event, event["target"]) for event in p5_events(root, name)
                         if event.get("target"))
    collected.extend((event["event"], event, event["target"]) for event in events(root)
                     if event["event"] in {"control_review", "confirmation"} and event.get("target"))
    return collected


def scan(root, record=False):
    with serialized(root):
        return _scan_unlocked(root, record)


def _scan_unlocked(root, record=False):
    items = catalog(root)
    changed, affected, invalid = [], [], []
    stale = {}
    for marker, item in items.items():
        problems = []
        for f in item["files"]:
            if not valid_file(root, f):
                path = local(root, f["path"])
                changed.append({"owner": item["target"], "path": f["path"],
                                "expected": f["sha256"],
                                "observed": sha(path) if path.is_file() else None})
                problems.append(f"文件改变或丢失：{f['path']}")
        for dep in item["dependencies"]:
            current = items.get(identity(dep))
            if not current or current["target"] != dep:
                problems.append(f"直接上游 {identity(dep)} 已改变或缺失")
        try:
            resolve(root, item["target"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problems.append(str(exc))
        if problems:
            stale[marker] = list(dict.fromkeys(problems))
    for marker, item in items.items():
        if item["target"]["space"] == "source":
            continue
        try:
            review = last_review(root, item["target"])
        except ValueError:
            review = None
        reasons = list(stale.get(marker, []))
        if review:
            try:
                gate(root, item["target"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                reasons.append(str(exc))
        if reasons:
            affected.append({"target": item["target"], "task_id": item["task_id"],
                "status": "needs_alignment" if marker in stale else "needs_review",
                "reasons": list(dict.fromkeys(reasons)),
                "next_steps": ["保留旧稿、反馈、失败和黄灯",
                               "核对上游变化并取得受影响范围的对齐授权",
                               "登记新版本或补齐缺失依据，由不同实例重新检核",
                               "重新取得所需策略师及客户确认，不沿用旧版批准"]})
    for kind, event, target in history_records(root):
        item = items.get(identity(target))
        reasons = []
        if not item or item["target"] != target:
            reasons.append("只保留为历史版本记录，不能批准当前版本")
        elif identity(target) in stale:
            reasons.extend(stale[identity(target)])
        if not valid_file(root, event.get("evidence")):
            reasons.append("原始报告或确认依据改变/丢失")
        if item and item["target"] == target:
            try:
                latest = last_review(root, target)
                review_id = event.get("review_id", event.get("review", {}).get("record_id"))
                if "confirmation" in kind and review_id != latest["record_id"]:
                    reasons.append("确认未绑定最新检核")
                if kind == "phase3-confirmations" and event.get("review_sha256") != (
                        latest.get("evidence", {}).get("sha256")):
                    reasons.append("确认缺少或不匹配最新检核指纹，须重新确认")
                if "review" in kind and event["record_id"] != latest["record_id"]:
                    reasons.append("同版已有更新检核，旧报告仍保留")
            except ValueError:
                reasons.append("缺少对应检核")
        if reasons:
            invalid.append({"record_id": event["record_id"], "record_type": kind,
                            "target": target, "reasons": list(dict.fromkeys(reasons))})
    from closure_impact import history_impact
    workflow_invalid, workflow_affected = history_impact(root)
    from task_receipts import inspect as inspect_receipts
    receipt_status = inspect_receipts(root)
    workflow_affected.extend({
        "record_id": item["record_id"], "record_type": "task_relation",
        "task_id": item["task_id"], "item_id": None,
        "status": "needs_alignment", "reasons": [item["issue"]],
        "next_steps": ["核对上游当前交付版本", "更新下游任务关系后再继续交付"],
    } for item in receipt_status["attention"])
    result = {"changed_files": changed, "affected": affected,
              "affected_workflows": workflow_affected,
              "invalidated_records": invalid + workflow_invalid,
              "automatic_rewrite": False, "all_history_retained": True}
    if record:
        state = read_json(local(root, "project/state.json"))
        if items and state.get("project_id"):
            saved = append(root, "impact_scan", result, unique=True)
            state["phase6_impact"] = {"record_id": saved["record_id"], "affected": affected,
                                    "affected_workflows": workflow_affected}
            atomic_json(local(root, "project/state.json"), state)
            refresh_counts(root)
            from project_closure import refresh
            refresh(root)
            result = {**result, "record_id": saved["record_id"]}
    return result


def refresh_counts(root):
    from phase2_store import registry as reg2
    from phase3_io import registry as reg3
    from phase5_common import registry as reg5
    from phase6_targets import control_registry
    path = local(root, "project/records/revision-counts.json")
    value = read_json(path) if path.exists() else {"schema_version": "0.1", "director_rework_rounds": {}}
    value["automatic_revision_rounds"] = {
        f"{space}/{key}": max(0, len(history) - 1)
        for space, registry in (("phase2", reg2), ("phase3", reg3), ("phase5", reg5),
                                ("control", control_registry))
        for key, history in registry(root)["artifacts"].items()}
    value["counting_note"] = "产出登记修订累计，不等于总监人工返工；仅为缓存，不能在此清零门禁。"
    atomic_json(path, value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    return scan(args.workspace, args.record)


if __name__ == "__main__":
    run_cli(main)
