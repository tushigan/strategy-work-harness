"""Compute local proposal completion without pretending customer approval or delivery."""
from datetime import date

import phase5_common as c
from phase2_review import gate as plan_gate
from phase3_review import gate as brief_gate
from phase3_store import plan_ref, ref as brief_ref, task
from phase3_tasks import completion as phase3_completion, execution_hold, inspect as phase3_inspect
from phase2_store import WorkflowError
from workspace_lock import serialized
from task_validation import completion_check, evaluation, reuse


@completion_check
def completion(root, task_id, seen=()):
    seen = tuple(seen)
    if task_id in seen:
        raise WorkflowError("任务前置关系循环")
    planned = task(root, task_id)
    if planned.get("status") in {"暂停", "已取消"}:
        raise WorkflowError("暂停或取消任务不能作为已完成前置；先核实范围变更")
    if planned["title"] != "内部提案":
        if planned["title"] not in {"调研分析", "品牌屋初稿"}:
            from closure_tasks import completion as delivery_completion
            return delivery_completion(root, task_id, seen)
        recorded = phase3_completion(root, task_id, seen)
        if planned["title"] == "品牌屋初稿":
            from brand_house_review import gate as html_gate
            reuse(html_gate, root, task_id)
        return recorded
    records = [x for x in c.events(root, "task_completed") if x.get("task_id") == task_id]
    recorded = records[-1] if records else {}
    if recorded.get("plan") != plan_ref(root):
        raise WorkflowError("内部提案尚未有效完成")
    brief = reuse(brief_gate, root, f"{task_id}::brief", human=False)
    outputs = [brief_ref(brief)]
    for kind in ("proposal-script", "html-deck"):
        outputs.append(c.ref(reuse(c.gate, root, f"{task_id}::{kind}", human=True)))
    if outputs != recorded.get("outputs"):
        raise WorkflowError("内部提案完成依据已更新，须重新对齐和确认")
    for upstream in planned["dependencies"]:
        completion(root, upstream, seen + (task_id,))
    return recorded


def start(root, task_id, actor, as_of=None):
    with serialized(root):
        return _start_unlocked(root, task_id, actor, as_of)


def _start_unlocked(root, task_id, actor, as_of=None):
    plan_gate(root, "plan")
    planned = task(root, task_id)
    if planned["title"] != "内部提案" or execution_hold(planned) or not isinstance(actor, str) or not actor.strip():
        raise WorkflowError("仅执行有效计划中的内部提案任务，须有实际执行者")
    today = as_of or date.today().isoformat()
    date.fromisoformat(today)
    if today < planned["start"]:
        raise WorkflowError("未到开始日，仅可准备候选内容")
    try:
        return completion(root, task_id)
    except WorkflowError:
        pass
    for upstream in planned["dependencies"]:
        completion(root, upstream, (task_id,))
    brief = brief_gate(root, f"{task_id}::brief")
    if any(g["blocks_completion"] for g in c.read_json(c.local(root, brief["path"]))["gaps"]):
        raise WorkflowError("任务简报仍有阻断缺口")
    payload = {"task_id": task_id, "actor": actor, "plan": plan_ref(root), "brief": brief_ref(brief),
               "committed_dates": [planned["start"], planned["end"]], "as_of": today}
    matching = [x for x in c.events(root, "task_started") if all(x.get(k) == v for k, v in payload.items())]
    return matching[-1] if matching else c.append_event(root, "task_started", payload)


def complete(root, task_id, actor):
    with serialized(root):
        return _complete_unlocked(root, task_id, actor)


def _complete_unlocked(root, task_id, actor):
    plan_gate(root, "plan")
    planned = task(root, task_id)
    if planned["title"] != "内部提案" or execution_hold(planned) or not isinstance(actor, str) or not actor.strip():
        raise WorkflowError("仅可完成有效计划中的内部提案，不代客户批准")
    brief = brief_gate(root, f"{task_id}::brief")
    started = [x for x in c.events(root, "task_started") if x.get("task_id") == task_id and
               x.get("plan") == plan_ref(root) and x.get("brief") == brief_ref(brief)]
    if not started:
        raise WorkflowError("先运行内部提案启动检查")
    for upstream in planned["dependencies"]:
        completion(root, upstream, (task_id,))
    outputs = [brief_ref(brief)] + [c.ref(c.gate(root, f"{task_id}::{kind}", human=True))
                                 for kind in ("proposal-script", "html-deck")]
    try:
        previous = completion(root, task_id)
        if previous["outputs"] == outputs:
            return previous
    except WorkflowError:
        pass
    return c.append_event(root, "task_completed", {"task_id": task_id, "actor": actor,
        "plan": plan_ref(root), "outputs": outputs, "scope": "internal_proposal_not_client_approval"})


@evaluation
def inspect(root):
    from closure_tasks import NATIVE, completion as delivery_completion, current_brief
    from phase6_events import events
    result = phase3_inspect(root)
    for item in result["tasks"]:
        planned = task(root, item["task_id"])
        if execution_hold(planned):
            continue
        if planned["title"] == "内部提案":
            try:
                completion(root, item["task_id"])
                item.update(status="completed", blockers=[], overdue=False, can_do="内部提案已确认；客户确认另记")
            except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
                item.update(status="blocked", blockers=[str(exc)], can_do="按第五阶段说明对齐逐字稿和PPT")
        elif planned["title"] == "客户确认":
            try:
                delivery_completion(root, item["task_id"])
                item.update(status="completed", blockers=[], overdue=False,
                            can_do="当前合同列项及交付版本已获客户确认；无需重复登记")
            except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
                item.update(status="blocked", blockers=[str(exc)],
                            can_do="核对当前列项交付及客户反馈；补齐或重新确认当前版本")
        elif planned["title"] not in NATIVE:
            try:
                delivery_completion(root, item["task_id"])
                item.update(status="completed", blockers=[], overdue=False,
                            can_do="当前任务已登记有效交付；列项客户确认另行核对")
            except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
                blockers = []
                if any(e.get("task_id") == item["task_id"] for e in events(root, "task_delivery")):
                    blockers.append(str(exc))
                for dependency in planned["dependencies"]:
                    try:
                        delivery_completion(root, dependency, (item["task_id"],))
                    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as error:
                        blockers.append(str(error))
                try:
                    current_brief(root, item["task_id"])
                except (WorkflowError, OSError, ValueError, KeyError, TypeError) as error:
                    blockers.append(str(error))
                item.update(status="blocked" if blockers else "ready" if item["due"] else "not_started",
                            blockers=list(dict.fromkeys(blockers)), can_do=(
                                "先对齐任务前置、简报及交付记录，再由策略师组织人工交付" if blockers else
                                "由策略师组织人工或设计交付；独立检核和内部确认后登记实际版本" if item["due"] else
                                "尚未到开始日；可准备人工或设计交付，不提前登记完成"))
    return result


def refresh(root):
    inspected = inspect(root)
    result = {"artifacts": c.all_status(root), "tasks": inspected["tasks"],
              "plan_error": inspected["plan_error"]}
    from design_expression import all_status
    result["submissions"] = all_status(root)
    path = c.local(root, "project/state.json")
    state = c.read_json(path)
    state["phase5"] = result
    state["last_updated"] = c.now()
    if result["tasks"] or result["artifacts"] or result["plan_error"]:
        if result["artifacts"]:
            state["current_stage"] = "internal_proposal"
        state["task_status"]["completed_task_ids"] = [t["task_id"] for t in result["tasks"] if t["status"] == "completed"]
        remaining = [t for t in result["tasks"] if t["status"] != "completed"]
        active = remaining[0] if remaining else None
        if active and active["title"] == "客户确认":
            state["current_stage"] = "client_confirmation"
        blocked = result["plan_error"] or active and active["blockers"]
        state["task_status"].update(status="blocked" if blocked else
                                    "in_progress" if active else "completed",
                                    current_task_id=active["task_id"] if active else None)
        state["next_action"] = result["plan_error"] or (active["can_do"] if active else
                               "核对真实客户批准及合同交付，不自动结项")
    c.atomic_json(path, state)
    return result
