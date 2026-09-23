"""Build a semantic schedule from independently reviewed and confirmed scope."""
import argparse
from pathlib import Path

from compare_import_template import binding
from gantt_model import COMMON, TASK, PROJECT_FIELDS, project_common, skeleton, to_row
from phase2_review import gate
from phase2_store import WorkflowError, archive, local, output_json, publish, read_json, ref
from template_profile import default_effects
from validate_gantt import ready_contract, validate_payload


def build(root, context_path, actor):
    contract_meta = gate(root, "contract")
    contract = read_json(local(root, contract_meta["path"]))
    if not ready_contract(contract):
        raise WorkflowError("合同识别存在未解决问题，不能进入排期")
    context = read_json(context_path)
    if context.get("contract_ref") != ref(contract_meta):
        raise WorkflowError("排期输入必须绑定已确认的当前合同版本")
    if set(context.get("common", {})) - set(COMMON):
        raise WorkflowError("排期输入包含未知的项目公共字段")
    projects = context.get("projects", {})
    if not isinstance(projects, dict) or any(not isinstance(p, dict) or set(p) - PROJECT_FIELDS for p in projects.values()):
        raise WorkflowError("系统项目分组只能填写项目名称、类型、状态、负责公司和背景")
    tasks = skeleton(contract["items"])
    schedules = context.get("tasks", {})
    if set(schedules) - {t["task_id"] for t in tasks}:
        raise WorkflowError("排期输入含不存在的任务编号")
    allowed = set(TASK) | {"date_basis", "date_status", "execution_route", "dependencies", "project_key"}
    for task in tasks:
        override = schedules.get(task["task_id"], {})
        if set(override) - allowed:
            raise WorkflowError(f"{task['task_id']}: 未知任务字段")
        task.update(override)
    template = binding(root)
    plan = {"schema_version": "0.2", "contract_ref": ref(contract_meta),
            "common": context.get("common", {}), "projects": projects, "tasks": tasks,
            "project_start": context.get("project_start"), "hard_deadline": context.get("hard_deadline"),
            "status": "draft", "template_checked_sha256": template["profile"]["sha256"],
            "template_semantic_sha256": template["profile"]["semantic_sha256"],
            "default_effects": [
                {"task_id": task["task_id"], **effect} for task in tasks
                for effect in default_effects(template["profile"], to_row(project_common(context, task), task))]}
    check = validate_payload(plan, contract, template["profile"])
    plan["pending_questions"] = check["errors"]
    plan["status"] = "pending_review" if check["valid"] and template["approved"] else "draft"
    meta = publish(root, "plan", plan, actor, dependencies=[ref(contract_meta)],
                   source_files=[archive(root, context_path, "evidence"), template["source"]])
    return {"artifact": meta, "status": plan["status"], "pending_questions": plan["pending_questions"],
            "default_effects": plan["default_effects"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    args = parser.parse_args()
    output_json(build(args.workspace, args.context, args.actor))


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
