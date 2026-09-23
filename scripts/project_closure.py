#!/usr/bin/env python3
"""Check every signed item and exact customer decision before closing a project."""
import argparse
from pathlib import Path

from phase2_store import WorkflowError, atomic_json, fingerprint, local, read_json
from phase3_store import plan_ref
from phase6_events import actor_check, append, archive, decision_valid, events, run_cli
from phase6_targets import catalog, control_ref
from review_gate import gate
from closure_tasks import completion, deliver
from workspace_lock import serialized
from task_validation import evaluation, reuse


def basis(root):
    items = catalog(root)
    refs = [items[f"phase2/{kind}"]["target"] for kind in ("contract", "plan")]
    metas = [reuse(gate, root, ref, human=True) for ref in refs]
    contract, plan = [read_json(local(root, m["path"])) for m in metas]
    return refs, contract, plan


@evaluation
def item_bundle(root, item, tasks, refs, seen=()):
    deliveries, blockers = [], []
    relevant = [t for t in tasks if item["item_id"] in t["source_item_ids"]]
    execution = [t for t in relevant if t["title"] != "客户确认"]
    if not execution:
        blockers.append("该签约列项没有实际交付任务")
    for planned in execution:
        try:
            deliveries.append(completion(root, planned["task_id"], seen))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            blockers.append(f"{planned['task_id']}: {exc}")
    bundle = {"item_id": item["item_id"], "basis": refs, "deliveries": deliveries,
              "task_ids": [t["task_id"] for t in relevant]}
    return bundle, blockers


def customer_decision(root, bundle):
    values = [e for e in events(root, "customer_decision") if e.get("item_id") == bundle["item_id"]]
    decision = values[-1] if values else {}
    valid = decision_valid(root, decision) and decision.get("bundle") == bundle
    return decision if valid else None


def customer_completion(root, planned, seen):
    refs, contract, plan = basis(root)
    item_ids = planned.get("source_item_ids")
    if not isinstance(item_ids, list) or not item_ids or any(
            not isinstance(item_id, str) for item_id in item_ids) or len(set(item_ids)) != len(item_ids):
        raise WorkflowError("客户确认任务须明确绑定当前合同列项")
    included = {item["item_id"]: item for item in contract["items"] if item["disposition"] == "included"}
    decisions, checked_tasks = [], set()
    # Check this task's items only. A project-wide inspect would recurse into
    # unrelated downstream items which themselves depend on this confirmation.
    for item_id in item_ids:
        if item_id not in included:
            raise WorkflowError(f"客户确认任务的列项 {item_id} 不在当前签约范围")
        bundle, blockers = item_bundle(root, included[item_id], plan["tasks"], refs, seen)
        if blockers:
            raise WorkflowError(f"{item_id} 交付尚未齐全：" + "；".join(blockers))
        decision = customer_decision(root, bundle)
        if not decision or decision.get("status") != "confirmed":
            reason = "客户已退回当前交付版本" if decision and decision.get("status") == "rejected" else (
                "当前交付版本缺有效客户确认")
            raise WorkflowError(f"{item_id}：{reason}")
        decisions.append({"item_id": item_id, "record_id": decision["record_id"],
                          "evidence": decision["evidence"]})
        checked_tasks.update(delivery["task_id"] for delivery in bundle["deliveries"])
    for dependency in planned["dependencies"]:
        if dependency not in checked_tasks:
            completion(root, dependency, seen)
    return {"task_id": planned["task_id"], "status": "completed", "plan": refs[1],
            "customer_decisions": decisions}


def confirm_item(root, item_id, evidence, actor, simulation=False, status="confirmed"):
    actor_check(root, actor, simulation)
    if status not in {"confirmed", "rejected"}:
        raise WorkflowError("客户决定不合法")
    refs, contract, plan = basis(root)
    items = [x for x in contract["items"] if x["item_id"] == item_id and x["disposition"] == "included"]
    if len(items) != 1:
        raise WorkflowError("须指定当前已确认合同中的签约列项")
    bundle, blockers = item_bundle(root, items[0], plan["tasks"], refs)
    if status == "confirmed" and blockers:
        raise WorkflowError("交付尚未齐全：" + "；".join(blockers))
    if not Path(evidence).read_text(encoding="utf-8").strip():
        raise WorkflowError("须保存策略师提供的客户确认原文")
    return append(root, "customer_decision", {"item_id": item_id, "bundle": bundle,
        "actor": actor, "status": status, "simulation": simulation,
        "evidence": archive(root, evidence)}, unique=True)


@evaluation
def inspect(root):
    result = {"ready": False, "items": [], "blockers": [], "basis": [],
              "notice": "仅检查记录完整性，不代替客户身份核验和实际批准。"}
    try:
        refs, contract, plan = basis(root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["blockers"].append(f"合同/排期未就绪：{exc}")
        return result
    result["basis"] = refs
    included = [x for x in contract["items"] if x["disposition"] == "included"]
    if not included:
        result["blockers"].append("没有已确认的签约列项")
    for item in included:
        bundle, blockers = item_bundle(root, item, plan["tasks"], refs)
        decision = customer_decision(root, bundle)
        delivered = not blockers
        confirmed = bool(delivered and decision and decision["status"] == "confirmed")
        if not confirmed:
            blockers.append("当前交付版本缺有效客户确认")
        result["items"].append({"item_id": item["item_id"], "title": item["title"],
            "bundle": bundle, "delivered": delivered,
            "client_confirmed": confirmed, "client_decision": decision,
            "blockers": blockers})
        result["blockers"].extend(f"{item['item_id']}: {s}" for s in blockers)
    result["ready"] = not result["blockers"]
    return result


def report(root, author, revision=None):
    from control_artifacts import publish
    checklist = inspect(root)
    if not checklist["ready"]:
        raise WorkflowError("结项条件不完整：" + "；".join(checklist["blockers"]))
    deps, source_paths = {}, []
    for ref in checklist["basis"]:
        if ref != plan_ref(root):
            deps[(ref["space"], ref["key"])] = ref
    for item in checklist["items"]:
        source_paths.append(item["client_decision"]["evidence"]["path"])
        for delivery in item["bundle"]["deliveries"]:
            for ref in delivery["outputs"]:
                deps[(ref["space"], ref["key"])] = ref
    text = "\n".join(f"{item['item_id']} {item['title']}：交付、独立检核、策略师确认及客户确认记录齐全。"
                     for item in checklist["items"])
    data = {"kind": "closure-report", "task_id": "project", "title": "项目结项核对",
            "checklist_sha256": fingerprint(checklist), "sections": [
                {"id": "coverage", "title": "合同覆盖", "text": text},
                {"id": "limits", "title": "确认边界", "text": checklist["notice"]}]}
    return publish(root, data, author, dependencies=list(deps.values()),
                   source_paths=source_paths, revision=revision)


def close(root, evidence, actor, simulation=False):
    with serialized(root):
        return _close_unlocked(root, evidence, actor, simulation)


def _close_unlocked(root, evidence, actor, simulation=False):
    from control_artifacts import latest
    actor_check(root, actor, simulation)
    checklist = inspect(root)
    if not checklist["ready"]:
        raise WorkflowError("尚不能结项：" + "；".join(checklist["blockers"]))
    meta = latest(root, "project::closure-report")
    gate(root, control_ref(meta), human=True)
    data = read_json(local(root, meta["files"][0]["path"]))
    if data.get("checklist_sha256") != fingerprint(checklist):
        raise WorkflowError("结项检查依据已变，须更新报告并重新独立检核和确认")
    from phase6_targets import review_stamp
    record = append(root, "project_closed", {"checklist": checklist, "report": control_ref(meta),
        "review": review_stamp(root, control_ref(meta)), "actor": actor, "simulation": simulation,
        "evidence": archive(root, evidence)}, unique=True)
    refresh(root)
    return record


def closure_status(root):
    closed = events(root, "project_closed")
    if not closed:
        return {"status": "not_closed", "last_closure_id": None}
    record = closed[-1]
    reasons = []
    current = inspect(root)
    if not current["ready"] or current != record["checklist"]:
        reasons.append("合同、交付、检核或客户决定已改变")
    try:
        gate(root, record["report"], human=True)
        from phase6_targets import review_stamp
        if review_stamp(root, record["report"]) != record["review"]:
            reasons.append("结项报告已重新检核")
        if not decision_valid(root, record):
            reasons.append("结项决定原文已失效")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reasons.append(str(exc))
    return {"status": "change_pending" if reasons else "closed",
            "last_closure_id": record["record_id"], "reasons": reasons}


def refresh(root):
    result = closure_status(root)
    if result["status"] == "not_closed":
        return result
    if result["status"] == "change_pending":
        append(root, "post_closure_change", {"closure_id": result["last_closure_id"],
            "reasons": result["reasons"], "checklist_sha256": fingerprint(inspect(root))}, unique=True)
    path = local(root, "project/state.json")
    state = read_json(path)
    state["closure"] = result
    state["status"] = "closed" if result["status"] == "closed" else "active"
    state["current_stage"] = "closure"
    state["next_action"] = ("项目记录已结项；改稿将保留结项历史并重新核对" if state["status"] == "closed"
                            else "结项后有变更；保留原结项并重新对齐、检核和客户确认")
    atomic_json(path, state)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "deliver", "client-confirm", "client-reject",
                                         "report", "close", "status", "refresh"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--actor", default="")
    parser.add_argument("--simulation", action="store_true")
    args = parser.parse_args()
    if args.action in {"inspect", "status", "refresh"}:
        return {"inspect": inspect, "status": closure_status, "refresh": refresh}[args.action](args.workspace)
    request = read_json(args.request) if args.request else {}
    if args.action == "report":
        return report(args.workspace, args.actor, request.get("revision"))
    if not args.evidence:
        raise WorkflowError("缺 --evidence 实际原文")
    if args.action == "close":
        return close(args.workspace, args.evidence, args.actor, args.simulation)
    if args.action == "deliver":
        return deliver(args.workspace, request["task_id"], request["outputs"],
                       args.evidence, args.actor, args.simulation)
    return confirm_item(args.workspace, request["item_id"], args.evidence, args.actor, args.simulation,
                        "confirmed" if args.action == "client-confirm" else "rejected")


if __name__ == "__main__":
    run_cli(main)
