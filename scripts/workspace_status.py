"""Read each implemented workflow without mutating the copied project."""
from task_validation import evaluation


@evaluation
def inspect_workflows(root, warnings):
    result = {}
    from standalone_tasks import inspect as standalone_status
    result["standalone"] = standalone_status(root, include_archived=True)
    warnings.extend(result["standalone"]["errors"])
    from project_memory_store import inspect as memory_status
    result["project_memory"] = memory_status(root)
    warnings.extend(f"项目长期记忆：{item}" for item in result["project_memory"]["errors"])
    from task_receipts import inspect as receipt_status
    result["task_receipts"] = receipt_status(root)
    warnings.extend(f"任务回执：{item}" for item in result["task_receipts"]["errors"])
    warnings.extend(
        f"任务关系 {item['record_id']}：{item['issue']}"
        for item in result["task_receipts"]["attention"]
    )
    if (root / "project/records/phase2-artifacts.json").exists():
        try:
            from workflow_status import inspect
            result["phase2"] = inspect(root)
        except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"第二阶段状态未能核验，请检查环境/记录: {exc}")
    if (root / "project/records/phase3-artifacts.json").exists():
        try:
            from phase3_tasks import inspect
            result["phase3"] = inspect(root)
        except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"第三阶段状态未能核验，请检查记录: {exc}")
    if any((root / f"project/records/{name}.json").exists()
           for name in ("phase5-artifacts", "design-submissions")):
        try:
            from phase5_common import all_status
            from phase5_tasks import inspect
            from design_expression import all_status as design_status
            tasks = inspect(root)
            result["phase5"] = {"artifacts": all_status(root), "tasks": tasks["tasks"],
                                "plan_error": tasks["plan_error"],
                                "submissions": design_status(root)}
        except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"第五阶段状态未能核验，请检查记录: {exc}")
    if any((root / f"project/records/{name}").exists() for name in (
            "phase2-artifacts.json", "control-artifacts.json", "phase6-events.jsonl")):
        try:
            from review_gate import inspect as review_status
            from impact_scan import scan
            from project_closure import closure_status, inspect as closure_check
            result["phase6"] = {"reviews": review_status(root), "impact": scan(root),
                                "closure": closure_status(root), "checklist": closure_check(root)}
        except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"跨阶段状态未能核验，请检查记录: {exc}")
    try:
        from knowledge_connectors import inspect as connector_status
        result["connectors"] = connector_status(root)
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        warnings.append(f"外部连接记录未能核验（未联网）: {exc}")
    return result


def project_progress(state, workflows):
    """Prefer the already checked workflow over the last persisted status hint."""
    project = {key: state.get(key) for key in (
        "project_id", "project_name", "client_name", "brand_name", "status", "current_stage", "next_action")}
    project["recorded_next_action"] = project["next_action"]
    project["progress_source"] = "recorded_state"
    standalone = workflows.get("standalone", {})
    independent = standalone.get("tasks", [])
    active_independent = [t for t in independent if t["status"] in {"planned", "in_progress", "waiting"}]
    attention_independent = [t for t in independent
                             if t["status"] != "archived" and t["file_issues"]]
    actionable_independent = [t for t in independent
                              if t in active_independent or t in attention_independent]
    project["independent_tasks"] = {
        "count": len(independent), "active_count": len(active_independent),
        "attention_count": len(attention_independent),
        "record_path": standalone.get("record_path"),
        "next_actions": [{"task_id": t["task_id"], "title": t["title"],
                          "status": t["effective_status"], "next_action": (
                              "先核对文件缺失或变化，再继续：" + t["next_action"]
                              if t["file_issues"] else t["next_action"])}
                         for t in actionable_independent],
    }
    memory = workflows.get("project_memory", {})
    project["project_memory"] = {
        "status": memory.get("status", "empty"),
        "confirmed_count": len(memory.get("current_facts", [])),
        "needs_confirmation_count": len(memory.get("needs_confirmation", [])),
        "attention_count": len(memory.get("needs_attention", [])),
        "memory_version": memory.get("memory_version", 0),
        "record_path": memory.get("record_path"),
    }
    receipts = workflows.get("task_receipts", {})
    project["task_receipts"] = {
        "status": receipts.get("status", "empty"),
        "receipt_count": len(receipts.get("receipts", [])),
        "relation_count": len(receipts.get("relations", [])),
        "formal_link_count": len(receipts.get("formal_links", [])),
        "attention_count": len(receipts.get("attention", [])),
        "record_path": receipts.get("record_path"),
    }
    tasks = workflows.get("phase5", workflows.get("phase3", {}))
    closure = workflows.get("phase6", {}).get("closure", {}).get("status")
    if closure in {"closed", "change_pending"}:
        project.update(current_stage="closure", status="closed" if closure == "closed" else "active",
                       progress_source="live_closure", next_action=(
                           "项目记录已结项；改稿将保留结项历史并重新核对" if closure == "closed" else
                           "结项后有变更；保留原结项并重新对齐、检核和客户确认"))
    elif tasks.get("plan_error"):
        project.update(next_action=tasks["plan_error"], current_stage="contract_scope",
                       progress_source="live_tasks")
    elif tasks.get("tasks"):
        active = next((item for item in tasks["tasks"] if item["status"] != "completed"), None)
        project.update(next_action=active["can_do"] if active else
                       "核对真实客户批准及合同交付，不自动结项", progress_source="live_tasks")
        if active:
            stages = {"调研分析": "research", "品牌屋初稿": "brand_house_draft",
                      "内部提案": "internal_proposal", "客户确认": "client_confirmation"}
            project["current_stage"] = stages.get(active["title"], project["current_stage"])
    elif actionable_independent and "phase2" not in workflows:
        project.update(next_action="；".join(
            f"独立任务 {t['task_id']}（{t['title']}）：{t['next_action']}"
            for t in project["independent_tasks"]["next_actions"]),
                       progress_source="live_standalone_tasks")
    return project
