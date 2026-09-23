"""Independent review for the current version of an independent task."""
import argparse
from pathlib import Path

from phase2_store import WorkflowError, local, read_json, test_mode
from phase6_events import append, archive, decision_valid, run_cli, valid_file
from phase6_targets import catalog, identity, last_review, resolve, sources


PASS = {"passed", "passed_with_yellow"}


def _required(root, target):
    meta = resolve(root, target)
    item = catalog(root)[identity(target)]
    if target["space"] != "standalone":
        raise WorkflowError("该入口只检核独立任务版本")
    return meta, item, sources(root, target)


def checks(root, target, report):
    _, item, expected_sources = _required(root, target)
    fields = {"target", "reviewer_instance", "status", "scope", "checked_sources",
              "uncovered", "findings", "summary", "simulation"}
    if not isinstance(report, dict) or set(report) != fields or report.get("target") != target:
        raise WorkflowError("独立检核报告字段不完整，或没有绑定当前任务版本")
    reviewer = report["reviewer_instance"]
    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer in item["authors"]:
        raise WorkflowError("任务记录者或作者不能自审，须实际使用不同实例")
    if type(report["simulation"]) is not bool or report["simulation"] and not test_mode(root):
        raise WorkflowError("模拟检核只允许用于 test_mode 合成项目")
    if report["status"] not in PASS | {"returned", "insufficient_evidence"}:
        raise WorkflowError("独立检核状态无效")
    for name in ("scope", "checked_sources", "uncovered", "findings"):
        if not isinstance(report[name], list):
            raise WorkflowError(f"{name} 必须为列表")
    if (not report["scope"] or any(not isinstance(value, str) or not value.strip()
            for value in report["scope"] + report["uncovered"])):
        raise WorkflowError("检核范围和未覆盖项须为明确文字")
    if not isinstance(report["summary"], str) or not report["summary"].strip():
        raise WorkflowError("检核报告须有结论摘要")
    for finding in report["findings"]:
        if (not isinstance(finding, dict) or set(finding) != {"level", "location", "evidence", "action"}
                or finding["level"] not in {"red", "yellow", "green"}
                or any(not isinstance(finding[name], str) or not finding[name].strip()
                       for name in ("location", "evidence", "action"))):
            raise WorkflowError("检核意见缺等级、定位、证据或动作")
    if any(not valid_file(root, value) for value in report["checked_sources"]):
        raise WorkflowError("已检查文件缺失或指纹不符")
    actual = {(value["path"], value["sha256"]) for value in report["checked_sources"]}
    missing = {f"source:{value['path']}" for value in expected_sources
               if (value["path"], value["sha256"]) not in actual}
    if missing and not missing.issubset(set(report["uncovered"])):
        raise WorkflowError("未检查的来源或交付文件必须逐项列入 uncovered")
    if report["status"] in PASS:
        if missing or report["uncovered"] or any(x["level"] == "red" for x in report["findings"]):
            raise WorkflowError("存在红灯或未覆盖内容，不能判定通过")
        yellow = any(x["level"] == "yellow" for x in report["findings"])
        if yellow != (report["status"] == "passed_with_yellow"):
            raise WorkflowError("黄灯意见与整体结论不一致")
    return item


def record_review(root, target, report_path):
    report = read_json(report_path)
    checks(root, target, report)
    evidence = archive(root, report_path)
    if read_json(local(root, evidence["path"])) != report:
        raise WorkflowError("检核报告归档时发生变化")
    checks(root, target, report)
    return append(root, "standalone_review", {**report, "evidence": evidence})


def gate(root, key):
    item = catalog(root).get(f"standalone/{key}")
    if not item:
        raise WorkflowError("找不到当前独立任务版本")
    target = item["target"]
    resolve(root, target)
    record = last_review(root, target)
    if not decision_valid(root, record):
        raise WorkflowError("独立检核报告原件失效")
    report = read_json(local(root, record["evidence"]["path"]))
    checks(root, target, report)
    if report["status"] not in PASS:
        raise WorkflowError("当前独立任务版本未获独立检核通过")
    return item["meta"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sources", "review", "gate"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    item = catalog(args.workspace).get(f"standalone/{args.task_id}")
    if not item:
        raise WorkflowError("找不到当前独立任务版本")
    if args.action == "sources":
        return {"target": item["target"], "checked_sources_required":
                sources(args.workspace, item["target"]), "author_instances": item["authors"]}
    if args.action == "gate":
        return gate(args.workspace, args.task_id)
    if not args.report:
        raise WorkflowError("review 缺少 --report")
    return record_review(args.workspace, item["target"], args.report)


if __name__ == "__main__":
    run_cli(main)
