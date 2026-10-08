"""Compare and archive a template; this never modifies a task plan."""
import argparse
from pathlib import Path

from phase2_store import (WorkflowError, append, archive, atomic_json, local, output_json,
                         read_json, sha, test_mode)
from template_profile import BASELINE_SHA, compare_profiles, inspect_template
from gantt_model import ADAPTER_VERSION, COMMON, TASK
from workbook_validations import adaptation_signature
from workspace_lock import serialized


def supported_adaptation(profile):
    baseline_path = Path(__file__).resolve().parents[1] / ".agents/skills/project-import-template/assets/openclaw-gantt-import-template.xlsx"
    if not baseline_path.is_file() or sha(baseline_path) != BASELINE_SHA:
        raise WorkflowError("随包基线丢失或改变，无法确定适配依据")
    baseline = inspect_template(baseline_path)
    if set(profile["headers"]) != set(baseline["headers"]):
        return False
    for key in ("sheets", "rules", "comments", "options", "dictionary", "instructions"):
        if profile[key] != baseline[key]:
            return False
    # Existing list rules can move with their columns, but may not silently change.
    return adaptation_signature(profile) == adaptation_signature(baseline)


def register_template(root, source, accept_evidence=None):
    with serialized(root):
        return _register_template_unlocked(root, source, accept_evidence)


def _register_template_unlocked(root, source, accept_evidence=None):
    source_ref = archive(root, source, "templates")
    profile = inspect_template(local(root, source_ref["path"]))
    path = local(root, "project/records/import-template.json")
    previous = binding(root, refresh_legacy=True) if path.exists() else None
    diff = compare_profiles(previous["profile"], profile) if previous else None
    approved = profile["sha256"] == BASELINE_SHA
    evidence = None
    rule_reference = source_ref
    if not approved and previous and not diff["rules_changed"]:
        approved = previous["approved"]
        evidence = previous.get("adaptation_evidence")
        rule_reference = previous["rule_reference"]
    if accept_evidence:
        acceptance = read_json(accept_evidence)
        if (acceptance.get("template_sha256") != profile["sha256"]
                or acceptance.get("adapter_version") != ADAPTER_VERSION
                or acceptance.get("status") != "passed"
                or not acceptance.get("author_instance")
                or not acceptance.get("reviewer_instance")
                or acceptance["author_instance"] == acceptance["reviewer_instance"]
                or not acceptance.get("confirmed_by") or not acceptance.get("confirmation_text")
                or not acceptance.get("rule_assessment")):
            raise WorkflowError("新版适配依据需绑定模板、适配版本、独立检核及策略师确认原文")
        if profile["issues"] or set(profile["headers"]) != set(COMMON.values()) | set(TASK.values()):
            raise WorkflowError("新增/删除/未知字段或规则冲突尚未实现适配，不能以确认跳过")
        if not supported_adaptation(profile):
            raise WorkflowError("新版业务规则尚未实现适配；确认不能替代修改适配器和重新测试")
        if acceptance.get("simulation") and not test_mode(root):
            raise WorkflowError("正式项目不能采用模拟模板适配确认")
        evidence = archive(root, accept_evidence, "evidence")
        approved = True
        rule_reference = source_ref
    if profile["issues"]:
        approved = False
    value = {"schema_version": "0.3", "source": source_ref, "profile": profile,
             "approved": approved, "adaptation_evidence": evidence,
             "rule_reference": rule_reference,
             "changes": diff, "adapter_version": ADAPTER_VERSION, "system_import_verified": False}
    record_path = local(root, f"project/records/template-versions/{profile['sha256']}.json")
    if not record_path.exists():
        atomic_json(record_path, value)
    atomic_json(path, value)
    append(root, "template-events", {"status": "approved" if approved else "needs_adaptation",
                                    "template": value})
    return value


def binding(root, refresh_legacy=False):
    path = local(root, "project/records/import-template.json")
    if not path.is_file():
        raise WorkflowError("未登记当前导入模板")
    value = read_json(path)
    source = local(root, value["source"]["path"])
    actual = inspect_template(source)
    if "native_validations" not in value["profile"]:
        if not refresh_legacy or actual["sha256"] != value["source"]["sha256"]:
            raise WorkflowError("旧模板快照缺完整校验属性，请重新登记原模板并重新检核排期")
        # Re-read archived evidence; never inherit an old, incomplete rule approval.
        return {**value, "profile": actual, "approved": False,
                "adaptation_evidence": None, "rule_reference": value["source"]}
    if actual != value["profile"] or actual["sha256"] != value["source"]["sha256"]:
        raise WorkflowError("模板或规则快照改变，必须重新比较适配")
    rule_source = value.get("rule_reference", {})
    rule_path = local(root, rule_source.get("path", "missing"))
    if not rule_path.is_file() or sha(rule_path) != rule_source.get("sha256"):
        raise WorkflowError("模板规则批准依据缺失或改变")
    if inspect_template(rule_path)["semantic_sha256"] != actual["semantic_sha256"]:
        raise WorkflowError("模板规则与批准依据不一致")
    if value.get("adaptation_evidence"):
        evidence = value["adaptation_evidence"]
        path = local(root, evidence["path"])
        if not path.is_file() or sha(path) != evidence["sha256"]:
            raise WorkflowError("模板适配依据改变或丢失")
        acceptance = read_json(path)
        if (acceptance.get("template_sha256") != rule_source["sha256"]
                or acceptance.get("adapter_version") != ADAPTER_VERSION
                or acceptance.get("simulation") and not test_mode(root)):
            raise WorkflowError("模板适配确认不适用于本次运行")
        if not supported_adaptation(actual):
            raise WorkflowError("当前适配器不支持这份已变更规则")
    elif value["approved"] and rule_source["sha256"] != BASELINE_SHA:
        raise WorkflowError("未知模板缺适配确认")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--against", type=Path)
    parser.add_argument("--accept-evidence", type=Path, help="策略师对新版规则的明确确认原文；不是自动批准")
    args = parser.parse_args()
    if args.workspace:
        result = register_template(args.workspace, args.template, args.accept_evidence)
    elif args.against:
        result = compare_profiles(inspect_template(args.against), inspect_template(args.template))
    else:
        result = inspect_template(args.template)
    output_json(result)


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
