"""Explain invalid delivery and customer decisions without rewriting their history."""
from closure_tasks import completion
from phase6_events import decision_valid, events
from project_closure import closure_status, inspect

ERRORS = (OSError, ValueError, KeyError, TypeError)


def history_impact(root):
    records = [e for e in events(root) if e["event"] in {
        "task_delivery", "customer_decision", "project_closed"}]
    if not records:
        return [], []
    checklist = inspect(root)
    items = {i["item_id"]: i for i in checklist["items"]}
    status = closure_status(root)
    latest = {}
    for record in records:
        key = (record["event"], record.get("task_id", record.get("item_id", "project")))
        latest[key] = record["record_id"]
    invalid, affected, completions = [], [], {}
    for record in records:
        kind = record["event"]
        key = (kind, record.get("task_id", record.get("item_id", "project")))
        reasons = []
        if not decision_valid(root, record):
            reasons.append("决定原文改变、缺失或模拟范围失效")
        if record["record_id"] != latest[key]:
            reasons.append("已有后续决定；此记录只供历史追溯")
        if kind == "task_delivery":
            task_id = record["task_id"]
            if task_id not in completions:
                try:
                    completions[task_id] = (completion(root, task_id), None)
                except ERRORS as exc:
                    completions[task_id] = (None, str(exc))
            current, error = completions[task_id]
            if error:
                reasons.append(error)
            elif current["record_id"] != record["record_id"]:
                reasons.append("当前交付已重新登记")
            steps = ["核对任务简报及交付版本、最新检核",
                     "重新取得策略师确认并登记交付，再核对客户确认"]
        elif kind == "customer_decision":
            item = items.get(record["item_id"])
            current = item.get("client_decision") if item else None
            if not item:
                reasons.append("当前合同或排期不再覆盖此列项，须核对范围变更")
            elif not current or current["record_id"] != record["record_id"]:
                reasons.append("客户决定未绑定当前交付及有效检核")
                reasons.extend(item["blockers"])
            steps = ["先补齐当前合同列项的有效交付",
                     "保留客户原话，重新取得并登记当前交付的客户决定"]
        else:
            if (status["status"] != "closed" or
                    status["last_closure_id"] != record["record_id"]):
                reasons.extend(status.get("reasons") or ["结项记录只供历史追溯"])
            steps = ["重新核对合同、交付、检核及客户决定",
                     "更新结项报告并独立检核、人工确认，保留原结项记录"]
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            invalid.append({"record_id": record["record_id"], "record_type": kind,
                "task_id": record.get("task_id"), "item_id": record.get("item_id"),
                "reasons": reasons, "next_steps": steps})
        rejected = kind == "customer_decision" and record.get("status") == "rejected"
        if record["record_id"] == latest[key] and (reasons or rejected):
            affected.append({"record_id": record["record_id"], "record_type": kind,
                "task_id": record.get("task_id"), "item_id": record.get("item_id"),
                "status": "needs_alignment",
                "reasons": reasons or ["最新客户决定为退回，不能视为已确认"],
                "next_steps": steps})
    return invalid, affected
