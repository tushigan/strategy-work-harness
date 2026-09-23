"""Resume existing planned tasks; no background scheduler and no date rewriting."""
from datetime import date

from phase2_review import gate as plan_gate
from phase2_store import WorkflowError, atomic_json, local, now, read_json
from phase3_events import append, events
from phase3_io import registry
from phase3_review import gate, last_review
from phase3_store import current, key_for, plan_ref, ref, task
from workspace_lock import serialized
from task_validation import completion_check, evaluation, reuse


def required(task_data):
    if task_data["title"] == "调研分析":
        return ["brief", "research-plan", "evidence", "analysis"], ["analysis"]
    if task_data["title"] == "品牌屋初稿":
        return ["brief", "brand-house", "derivation"], ["brand-house", "derivation"]
    return [], []


def execution_hold(task_data):
    status = task_data.get("status")
    if status in {"暂停", "已取消", "已完成"}:
        return f"任务计划状态为{status}，不能自动重新执行；先核实已有成果或更新授权"
    return None


@completion_check
def completion(root, task_id, seen=()):
    seen = tuple(seen)
    if task_id in seen:
        raise WorkflowError("任务前置关系循环")
    task_data = task(root, task_id)
    if task_data.get("status") in {"暂停", "已取消"}:
        raise WorkflowError("暂停或取消任务不能作为已完成前置；先核实范围变更")
    if not required(task_data)[0]:
        from closure_tasks import completion as delivery_completion
        return delivery_completion(root, task_id, seen)
    matches = [e for e in events(root, "phase3-tasks")
               if e.get("task_id") == task_id and e.get("status") == "completed"]
    if not matches or matches[-1].get("plan") != plan_ref(root):
        raise WorkflowError(f"前置任务 {task_id} 尚未有效完成")
    recorded = matches[-1]
    for output in recorded["outputs"]:
        meta = reuse(gate, root, output["key"], human=output["key"].split("::")[-1] in required(task_data)[1])
        if output != ref(meta):
            raise WorkflowError(f"任务 {task_id} 的完成依据已更新")
    from brand_house_review import task_gate
    reuse(task_gate, root, task_id, recorded)
    for upstream in task_data["dependencies"]:
        completion(root, upstream, seen + (task_id,))
    return recorded


def start(root, task_id, actor, as_of=None):
    with serialized(root):
        return _start_unlocked(root, task_id, actor, as_of)


def _start_unlocked(root, task_id, actor, as_of=None):
    plan_gate(root, "plan")
    task_data = task(root, task_id)
    if execution_hold(task_data):
        raise WorkflowError(execution_hold(task_data))
    try:
        return completion(root, task_id)
    except WorkflowError:
        pass
    today = as_of or date.today().isoformat()
    date.fromisoformat(today)
    if today < task_data["start"]:
        raise WorkflowError("尚未到计划开始日；可准备简报，但不自动提前执行")
    if not actor.strip():
        raise WorkflowError("缺执行实例")
    for upstream in task_data["dependencies"]:
        completion(root, upstream, (task_id,))
    brief = gate(root, key_for(task_id, "brief"))
    payload = read_json(local(root, brief["path"]))
    if any(g["blocks_completion"] for g in payload["gaps"]):
        raise WorkflowError("简报仍有关键缺口；仅可做已列明的准备工作")
    if not required(task_data)[0]:
        raise WorkflowError("本阶段只起草该任务简报，实际执行留人工或后续阶段")
    matching = [e for e in events(root, "phase3-tasks")
                if e.get("task_id") == task_id and e.get("plan") == plan_ref(root)
                and e.get("status") == "in_progress" and e.get("brief") == ref(brief)]
    if matching:
        return matching[-1]
    return append(root, "phase3-tasks", {
        "task_id": task_id, "status": "in_progress", "actor": actor,
        "plan": plan_ref(root), "brief": ref(brief), "as_of": today,
        "committed_dates": [task_data["start"], task_data["end"]]})


def complete(root, task_id, actor):
    with serialized(root):
        return _complete_unlocked(root, task_id, actor)


def _complete_unlocked(root, task_id, actor):
    plan_gate(root, "plan")
    task_data = task(root, task_id)
    if execution_hold(task_data):
        raise WorkflowError(execution_hold(task_data))
    kinds, human = required(task_data)
    if not kinds or not actor.strip():
        raise WorkflowError("此任务实际交付不在 Phase 3 范围，不能凭简报宣称完成")
    started = [e for e in events(root, "phase3-tasks") if e.get("task_id") == task_id
               and e.get("status") == "in_progress" and e.get("plan") == plan_ref(root)]
    if not started:
        raise WorkflowError("未完成任务启动检查")
    if started[-1].get("brief") != ref(current(root, key_for(task_id, "brief"))):
        raise WorkflowError("简报已更新，先重新执行 start 检查再完成")
    for dep in task_data["dependencies"]:
        completion(root, dep, (task_id,))
    outputs = []
    for kind in kinds:
        meta = gate(root, key_for(task_id, kind), human=kind in human)
        if any(g["blocks_completion"] for g in read_json(local(root, meta["path"]))["gaps"]):
            raise WorkflowError("产出存在阻断缺口，不能完成任务")
        outputs.append(ref(meta))
    from brand_house_review import task_gate
    html = task_gate(root, task_id)
    try:
        previous = completion(root, task_id)
        if previous["outputs"] == outputs:
            return previous
    except WorkflowError:
        pass
    return append(root, "phase3-tasks", {"task_id": task_id, "status": "completed",
                  "actor": actor, "plan": plan_ref(root), "outputs": outputs,
                  **({"html": html} if html else {}),
                  "scope": "internal_deliverables_only_not_client_confirmation"})


@evaluation
def inspect(root, as_of=None):
    today = as_of or date.today().isoformat()
    date.fromisoformat(today)
    result = {"as_of": today, "tasks": [], "artifacts": {}, "plan_error": None,
              "background_scheduler": False}
    try:
        plan = plan_gate(root, "plan")
        tasks = read_json(local(root, plan["path"]))["tasks"]
    except (WorkflowError, OSError, ValueError, KeyError) as exc:
        result["plan_error"] = str(exc)
        tasks = []
    for key, history in registry(root)["artifacts"].items():
        meta = history[-1]
        item = {"target": ref(meta), "status": "draft", "path": meta["markdown_path"]}
        try:
            current(root, key)
        except (WorkflowError, OSError, ValueError, KeyError) as exc:
            item.update(status="stale", reason=str(exc))
        else:
            try:
                reuse(gate, root, key, human=False)
                item["status"] = "reviewed"
            except (WorkflowError, OSError, ValueError, KeyError) as exc:
                item["reason"] = str(exc)
        result["artifacts"][key] = item
    for task_data in tasks:
        task_id = task_data["task_id"]
        item = {"task_id": task_id, "title": task_data["title"],
                "start": task_data["start"], "end": task_data["end"],
                "due": task_data["start"] <= today, "overdue": task_data["end"] < today,
                "status": "not_started", "blockers": [], "can_do": "准备或补充本任务简报"}
        if execution_hold(task_data):
            item.update(status="blocked", blockers=[execution_hold(task_data)],
                        can_do="核对任务计划与已有成果，不自动重启任务")
            result["tasks"].append(item)
            continue
        try:
            completion(root, task_id)
            item.update(status="completed", overdue=False, can_do="无需重复执行；有反馈则登记修订")
        except (WorkflowError, OSError, ValueError, KeyError):
            for dep in task_data["dependencies"]:
                try:
                    completion(root, dep, (task_id,))
                except (WorkflowError, OSError, ValueError, KeyError) as exc:
                    item["blockers"].append(str(exc))
            try:
                brief = reuse(gate, root, key_for(task_id, "brief"), human=False)
                data = read_json(local(root, brief["path"]))
                item["blockers"].extend(g["cannot_do"] for g in data["gaps"] if g["blocks_completion"])
            except (WorkflowError, OSError, ValueError, KeyError) as exc:
                item["blockers"].append(str(exc))
            if not required(task_data)[0]:
                item["blockers"].append("实际执行不在 Phase 3 范围；只可完成策略简报")
            if item["blockers"]:
                item["status"] = "blocked"
            elif item["due"]:
                item.update(status="ready", can_do="可调用 start 后执行；保持原排期")
                started = [e for e in events(root, "phase3-tasks") if
                           e.get("task_id") == task_id and e.get("status") == "in_progress"
                           and e.get("plan") == plan_ref(root) and e.get("brief") == ref(brief)]
                if started:
                    item.update(status="in_progress", can_do="继续当前任务；不重复启动或改变原排期")
        result["tasks"].append(item)
    return result


def refresh(root, as_of=None):
    result = inspect(root, as_of)
    path = local(root, "project/state.json")
    state = read_json(path)
    state["phase3"] = result
    if result["artifacts"]:
        completed = [t["task_id"] for t in result["tasks"] if t["status"] == "completed"]
        remaining = [t for t in result["tasks"] if t["status"] != "completed"]
        active = remaining[0] if remaining else None
        state["task_status"] = {"status": "blocked" if result["plan_error"] or active and active["blockers"] else
                               "in_progress" if active else "completed",
                               "current_task_id": active["task_id"] if active else None,
                               "completed_task_ids": completed}
        state["current_stage"] = "brand_house_draft" if any(
            "::brand-house" in k for k in result["artifacts"]) else "research"
        state["next_action"] = result["plan_error"] or (active["can_do"] if active else
                               "本阶段成果已内部确认；后续阶段仍需另行实施")
        newest = max((v[-1] for v in registry(root)["artifacts"].values()), key=lambda m: m["created_at"])
        item = result["artifacts"][newest["key"]]
        state["artifact_status"] = {"status": "ready_for_review" if item["status"] == "draft" else
                                    "draft" if item["status"] == "reviewed" else "stale",
                                    "current_artifact_id": newest["key"],
                                    "latest_artifact_version": newest["version"]}
        reviews = [r for r in events(root, "phase3-reviews") if r.get("target") == ref(newest)]
        last = reviews[-1] if reviews else {}
        recorded_status = last.get("status", "not_started")
        effective_status = recorded_status if item["status"] == "reviewed" else (
            recorded_status if recorded_status in {"returned", "insufficient_evidence"}
            and item["status"] != "stale" else "not_started")
        state["machine_review"] = {"status": effective_status,
                                   "recorded_status": last.get("status", "not_started"),
                                   "latest_review_id": last.get("record_id")}
        confirmations = [e for e in events(root, "phase3-confirmations") if e.get("target") == ref(newest)]
        confirmation = confirmations[-1] if confirmations else {}
        human_status = confirmation.get("status", "not_requested")
        if human_status == "confirmed":
            try:
                gate(root, newest["key"], human=True)
            except (WorkflowError, OSError, ValueError, KeyError):
                human_status = "superseded"
        state["human_confirmation"] = {"status": human_status,
                                       "latest_confirmation_id": confirmation.get("record_id")}
    state["last_updated"] = now()
    atomic_json(path, state)
    dep_path = local(root, "project/records/dependencies.json")
    deps = read_json(dep_path) if dep_path.exists() else {"schema_version": "0.1", "dependencies": []}
    deps["phase3"] = [{"target": ref(v[-1]), "upstream": v[-1]["dependencies"],
                      "status": result["artifacts"][k]["status"]} for k, v in registry(root)["artifacts"].items()]
    atomic_json(dep_path, deps)
    return result
