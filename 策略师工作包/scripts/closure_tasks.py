"""Delivery coverage of the signed plan, including manually executed services."""
from phase2_store import WorkflowError, local, read_json
from phase3_store import plan_ref, task
from phase6_events import actor_check, append, archive, decision_valid, events
from phase6_targets import catalog, identity, review_stamp
from review_gate import gate
from task_validation import completion_check, reuse

NATIVE = {"调研分析", "品牌屋初稿", "内部提案"}


def current_brief(root, task_id):
    brief = catalog(root).get(f"phase3/{task_id}::brief")
    if not brief:
        raise WorkflowError("任务执行前须有策略简报")
    reuse(gate, root, brief["target"])
    data = read_json(local(root, brief["meta"]["path"]))
    if any(x["blocks_completion"] for x in data["gaps"]):
        raise WorkflowError("简报存在阻断缺口")
    return brief["target"], review_stamp(root, brief["target"])


def native_completion(root, planned, seen=()):
    from phase5_tasks import completion
    record = completion(root, planned["task_id"], seen)
    outputs = list(record["outputs"])
    if record.get("html"):
        outputs.append(record["html"])
    elif planned["title"] == "品牌屋初稿":
        from brand_house_review import gate as html_gate
        outputs.append(reuse(html_gate, root, planned["task_id"])["target"])
    for ref in outputs:
        reuse(gate, root, ref)
    return {"task_id": planned["task_id"], "record_id": record["record_id"],
            "outputs": outputs, "reviews": [review_stamp(root, ref) for ref in outputs],
            "status": "delivered", "strategy_confirmation": "native_version_bound"}


def validate_outputs(root, planned, outputs):
    if not isinstance(outputs, list) or not outputs:
        raise WorkflowError("须指定实际交付版本，不能凭任务名或简报完成")
    if len({identity(x) for x in outputs}) != len(outputs):
        raise WorkflowError("同一产出不能指定多个版本")
    items = catalog(root)
    for target in outputs:
        reuse(gate, root, target, human=True)
        meta = items[identity(target)]["meta"]
        if meta.get("task_id") != planned["task_id"]:
            raise WorkflowError("交付必须属于此合同任务")
        if target["space"] != "design-submission" and not (
                target["space"] == "control" and meta.get("kind") == "external-deliverable"):
            raise WorkflowError("人工任务须实际设计提交或外部交付登记，简报不是交付")
    if planned.get("task_type") == "品牌设计" and not any(
            x["space"] == "design-submission" for x in outputs):
        raise WorkflowError("设计任务须包含经逐页策略检核的设计提交")
    return [review_stamp(root, ref) for ref in outputs]


@completion_check
def completion(root, task_id, seen=()):
    seen = tuple(seen)
    if task_id in seen:
        raise WorkflowError("交付任务依赖存在循环")
    planned = task(root, task_id)
    if planned.get("status") in {"暂停", "已取消"}:
        raise WorkflowError("暂停或取消任务不等于合同列项已交付；先确认范围变更")
    if planned["title"] in NATIVE:
        return native_completion(root, planned, seen)
    if planned["title"] == "客户确认":
        from project_closure import customer_completion
        return customer_completion(root, planned, seen + (task_id,))
    values = [e for e in events(root, "task_delivery") if e.get("task_id") == task_id]
    record = values[-1] if values else {}
    if not decision_valid(root, record) or record.get("plan") != plan_ref(root):
        raise WorkflowError("缺少当前计划的实际交付记录")
    brief, brief_review = current_brief(root, task_id)
    if record.get("brief") != brief or record.get("brief_review") != brief_review:
        raise WorkflowError("交付未绑定当前简报及最新检核；须重新对齐并登记，旧记录仅供追溯")
    reviews = validate_outputs(root, planned, record["outputs"])
    if reviews != record.get("reviews"):
        raise WorkflowError("交付依据已有新检核；须重新确认并登记交付")
    for dep in planned["dependencies"]:
        completion(root, dep, seen + (task_id,))
    return {"task_id": task_id, "record_id": record["record_id"], "outputs": record["outputs"],
            "reviews": reviews, "brief": brief, "brief_review": brief_review,
            "status": "delivered", "strategy_confirmation": "version_bound"}


def deliver(root, task_id, outputs, evidence, actor, simulation=False):
    actor_check(root, actor, simulation)
    planned = task(root, task_id)
    if planned["title"] in NATIVE | {"客户确认"}:
        raise WorkflowError("已实现工作流须走原任务完成检查，客户确认须单独登记")
    if planned.get("status") in {"暂停", "已取消"}:
        raise WorkflowError("不能交付暂停/取消的任务")
    gate(root, plan_ref(root), human=True)
    brief, brief_review = current_brief(root, task_id)
    for dep in planned["dependencies"]:
        completion(root, dep, (task_id,))
    reviews = validate_outputs(root, planned, outputs)
    items = catalog(root)
    for target in outputs:
        if brief not in items[identity(target)]["dependencies"]:
            raise WorkflowError("交付产出尚未绑定本任务当前简报；须登记新版并独立检核")
    return append(root, "task_delivery", {"task_id": task_id, "outputs": outputs, "reviews": reviews,
        "brief": brief, "brief_review": brief_review,
        "plan": plan_ref(root), "actor": actor, "simulation": simulation,
        "evidence": archive(root, evidence)}, unique=True)
