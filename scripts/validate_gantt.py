"""Check semantic task coverage and actual template requirements."""
import argparse
import math
import re
from datetime import date
from pathlib import Path

from compare_import_template import binding
from gantt_model import COMMON, TASK, NODES, PROJECT_FIELDS, project_common, to_row
from phase2_store import WorkflowError, current, local, output_json, read_json
from template_profile import DATE_FIELDS, default_effects, required_fields

EMPTY = (None, "", [])
PLACEHOLDER = re.compile(r"示例|待确认|待补|TODO|TBD|请填写", re.I)


def empty(value):
    return value in EMPTY or isinstance(value, str) and not value.strip()


def shape_errors(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("common", {}), dict):
        return ["计划及公共字段必须为对象"]
    tasks = plan.get("tasks", [])
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks):
        return ["任务必须为对象列表"]
    errors = []
    projects = plan.get("projects", {})
    if not isinstance(projects, dict) or any(not isinstance(p, dict) or set(p) - PROJECT_FIELDS for p in projects.values()):
        return ["系统项目分组结构非法"]
    if any(v is not None and not isinstance(v, str) for p in projects.values() for v in p.values()):
        return ["项目分组字段必须是文字"]
    for key, value in plan.get("common", {}).items():
        if key != "amount" and value is not None and not isinstance(value, (str, list)):
            errors.append(f"公共字段 {key} 类型非法")
        if isinstance(value, list) and key not in {"contract_manager", "assistant_manager"}:
            errors.append(f"公共字段 {key} 不能为列表")
    allowed = set(TASK) | {"task_id", "source_item_ids", "dependencies", "date_basis", "date_status", "execution_route", "project_key"}
    for task in tasks:
        if set(task) - allowed:
            errors.append("任务有未知字段，不能静默忽略")
        for key in ("source_item_ids", "dependencies"):
            if not isinstance(task.get(key, []), list) or any(not isinstance(v, str) for v in task.get(key, [])):
                errors.append(f"{key} 必须为编号列表")
        if not isinstance(task.get("task_id"), str):
            errors.append("任务编号必须为字符串")
        if task.get("project_key") is not None and (not isinstance(task["project_key"], str) or task["project_key"] not in projects):
            errors.append("任务引用的系统项目分组不存在")
    return errors


def date_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期必须为 YYYY-MM-DD")
    return date.fromisoformat(value)


def ready_contract(contract):
    return (contract.get("source_review_complete") is True
            and bool(contract.get("items"))
            and all(i.get("disposition") in {"included", "not_service"} for i in contract["items"])
            and any(i.get("disposition") == "included" for i in contract["items"])
            and all(i.get("resolved") is True and not i.get("hard_block") for i in contract["issues"]))


def validate_payload(plan, contract, profile):
    errors, warnings, rows = [], [], []
    errors.extend(shape_errors(plan))
    if errors:
        return {"valid": False, "errors": errors, "warnings": [], "rows": [], "task_count": 0}
    if not ready_contract(contract):
        errors.append("合同识别仍有未处理问题或列项")
    if profile.get("issues"):
        errors.append("模板存在未处理规则问题")
    if set(profile["headers"]) != set(COMMON.values()) | set(TASK.values()):
        errors.append("模板字段不受当前适配器支持，需要更新适配器")
    tasks = plan.get("tasks", [])
    if not tasks:
        errors.append("任务为空；不能删除任务绕过校验")
    common = plan.get("common", {})
    unknown = set(common) - set(COMMON)
    if unknown:
        errors.append(f"未映射项目字段: {sorted(unknown)}")
    ids = [t.get("task_id") for t in tasks]
    if len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids):
        errors.append("任务编号缺失或重复")
    included = {i["item_id"]: i for i in contract["items"] if i["disposition"] == "included"}
    covered, moments, by_id = set(), {}, {t.get("task_id"): t for t in tasks}
    project_start = plan.get("project_start")
    deadline = plan.get("hard_deadline")
    for value, label in ((project_start, "项目开始"), (deadline, "硬性截止")):
        if value not in EMPTY:
            try:
                date_value(value)
            except ValueError:
                errors.append(f"{label}日期非法")
    for index, task in enumerate(tasks, 1):
        tid = task.get("task_id", str(index))
        source = task.get("source_item_ids", [])
        if not source or any(s not in included for s in source):
            errors.append(f"{tid}: 缺合同对应关系或引用无效")
        covered.update(source)
        if any(included[s]["service_kind"] != "brand_house" for s in source if s in included):
            route = task.get("execution_route")
            if not isinstance(route, str) or not route.strip() or re.search(r"待明确|待定|待确认|待补|TODO|TBD", route, re.I):
                errors.append(f"{tid}: 其他服务的人工或外部执行安排尚未明确")
        try:
            row = to_row(project_common(plan, task), task)
        except ValueError as exc:
            errors.append(f"{tid}: {exc}")
            continue
        rows.append(row)
        for field in required_fields(profile):
            if empty(row.get(field)):
                errors.append(f"{tid}: 缺必填 {field}")
        for field, values in profile["options"].items():
            if row.get(field) not in EMPTY and row[field] not in values:
                errors.append(f"{tid}: {field} 非法选项 {row[field]!r}")
        for effect in default_effects(profile, row):
            errors.append(f"{tid}: {effect['field']} 留空会默认 {effect['default']}，需明确填写")
        for key in ("deliverable", "acceptance", "date_basis"):
            if not isinstance(task.get(key), str) or not task[key].strip():
                errors.append(f"{tid}: 缺业务必要信息 {key}")
        if task.get("date_status") not in {"suggested", "agreed"}:
            errors.append(f"{tid}: 日期需区分建议/已约定")
        elif task["date_status"] == "suggested" and "建议" not in str(task.get("date_basis")):
            errors.append(f"{tid}: 估算排期的依据必须明确写建议")
        for field, value in row.items():
            if field != "合同金额（元）" and value is not None and not isinstance(value, str):
                errors.append(f"{tid}: {field} 必须为文字")
            if isinstance(value, str) and PLACEHOLDER.search(value):
                errors.append(f"{tid}: {field} 有示例或占位文字")
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "@", "-")):
                errors.append(f"{tid}: {field} 有公式/危险开头，请明确改成普通文字")
        parsed = {}
        for field in DATE_FIELDS:
            if row.get(field) in EMPTY:
                continue
            try:
                parsed[field] = date_value(row[field])
            except ValueError:
                errors.append(f"{tid}: {field} 日期非法")
        if "任务开始" in parsed and "任务截止" in parsed:
            start = (parsed["任务开始"], {"上午": 0, "下午": 1}.get(row["任务开始半天"], -1))
            end = (parsed["任务截止"], {"上午": 0, "下午": 1}.get(row["任务截止半天"], -1))
            moments[tid] = (start, end)
            if start > end:
                errors.append(f"{tid}: 日期或半天顺序倒置")
            if isinstance(project_start, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", project_start) and row["任务开始"] < project_start:
                errors.append(f"{tid}: 早于项目开始")
            if isinstance(deadline, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline) and row["任务截止"] > deadline:
                errors.append(f"{tid}: 超过硬性截止")
        if parsed.get("合同签约时间", date.min) > parsed.get("合同完成时间", date.max):
            errors.append(f"{tid}: 合同起止日期倒置")
        amount = row["合同金额（元）"]
        if amount not in EMPTY and (isinstance(amount, bool) or not isinstance(amount, (int, float))
                                    or not math.isfinite(amount) or amount < 0):
            errors.append(f"{tid}: 合同金额必须为非负数值或留空")
        if row["任务等级"] == "S" and empty(row["最终拍板人"]):
            errors.append(f"{tid}: S级任务必须填写最终拍板人")
    if set(included) - covered:
        errors.append(f"合同漏项: {sorted(set(included) - covered)}")
    common_by_project = {}
    for row in rows:
        key = tuple(row[k] for k in ("客户", "品牌", "合同", "项目"))
        values = {label: row[label] for label in COMMON.values()}
        if key in common_by_project and common_by_project[key] != values:
            errors.append("同项目公共字段不一致")
        common_by_project[key] = values
    for iid, item in included.items():
        related = [t for t in tasks if iid in t.get("source_item_ids", [])]
        if item["service_kind"] == "brand_house":
            expected = [(f"{iid}-{n[0]}", n[1]) for n in NODES]
            if [(t.get("task_id"), t.get("title")) for t in related] != expected:
                errors.append(f"{iid}: 品牌屋必须保持四个关键节点及稳定编号")
            for prior, after in zip(related, related[1:]):
                if prior["task_id"] not in after.get("dependencies", []):
                    errors.append(f"{iid}: 品牌屋节点缺前置依赖")
    visiting, visited = set(), set()

    def visit(tid):
        if tid in visiting:
            errors.append(f"{tid}: 依赖循环")
            return
        if tid in visited:
            return
        visiting.add(tid)
        for dep in by_id[tid].get("dependencies", []):
            if dep not in by_id:
                errors.append(f"{tid}: 依赖不存在 {dep}")
            else:
                visit(dep)
                if tid in moments and dep in moments and moments[tid][0] <= moments[dep][1]:
                    errors.append(f"{tid}: 必须在前置任务 {dep} 结束后的半天开始")
        visiting.remove(tid)
        visited.add(tid)
    for tid in by_id:
        visit(tid)
    return {"valid": not errors, "errors": list(dict.fromkeys(errors)), "warnings": warnings,
            "rows": rows, "task_count": len(tasks)}


def validate_current(root, plan_file=None):
    template = binding(root)
    contract_meta = current(root, "contract")
    contract = read_json(local(root, contract_meta["path"]))
    plan = read_json(plan_file or local(root, current(root, "plan")["path"]))
    result = validate_payload(plan, contract, template["profile"])
    if not template["approved"]:
        result["valid"] = False
        result["errors"].append("当前模板未完成适配确认")
    if plan.get("template_semantic_sha256") != template["profile"]["semantic_sha256"]:
        result["errors"].append("模板规则已改变，需保留原任务并重新核对新规则")
    from phase2_store import ref
    if plan.get("contract_ref") != ref(contract_meta):
        result["errors"].append("任务计划未绑定当前合同版本")
    result["valid"] = not result["errors"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--plan-file", type=Path, help="仅检查候选，不建立确认记录")
    args = parser.parse_args()
    result = validate_current(args.workspace, args.plan_file)
    output_json(result)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
