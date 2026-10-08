"""Shared native report checks, independent of downstream workflow gates."""
from phase2_store import WorkflowError, events, local, read_json, ref, registry, sha, test_mode

PASS = {"passed", "passed_with_yellow"}


def evidence_valid(root, evidence):
    if not isinstance(evidence, dict) or not evidence.get("path"):
        return False
    try:
        path = local(root, evidence["path"])
        return path.is_file() and sha(path) == evidence.get("sha256")
    except (OSError, ValueError, TypeError):
        return False


def report_checks(root, meta, report):
    if not isinstance(report, dict) or report.get("target") != ref(meta):
        raise WorkflowError("检核报告不是当前版本")
    actor = report.get("reviewer_instance")
    authors = {m["author_instance"] for m in registry(root)["artifacts"].get(meta["kind"], [])}
    authors.add(meta["author_instance"])
    if not isinstance(actor, str) or not actor.strip() or actor in authors:
        raise WorkflowError("参与此产出的任一作者实例不能独立检核")
    status = report.get("status")
    if status not in PASS | {"returned", "insufficient_evidence"}:
        raise WorkflowError("无效检核状态")
    simulation = report.get("simulation", False)
    if not isinstance(simulation, bool):
        raise WorkflowError("报告的模拟范围必须为布尔值")
    if simulation and not test_mode(root):
        raise WorkflowError("正式项目不能采用模拟检核")
    scope, findings, uncovered = report.get("scope"), report.get("findings"), report.get("uncovered", [])
    if (not isinstance(scope, list) or not scope or any(
            not isinstance(s, str) or not s.strip() for s in scope)
            or not isinstance(findings, list) or not isinstance(uncovered, list)):
        raise WorkflowError("报告缺检查范围、问题清单或未覆盖范围")
    if any(not isinstance(f, dict) or f.get("level") not in {"red", "yellow", "green"}
           for f in findings):
        raise WorkflowError("问题需要明确红/黄/绿等级")
    if status in PASS and (uncovered or any(f["level"] == "red" for f in findings)):
        raise WorkflowError("有未覆盖范围或红灯问题，不能标记通过")
    checked = report.get("checked_sources", [])
    if not isinstance(checked, list) or any(not evidence_valid(root, s) for s in checked):
        raise WorkflowError("报告中的已读资料无法核对")
    if not any(s["path"] == meta["snapshot_path"] and s["sha256"] == meta["sha256"] for s in checked):
        raise WorkflowError("检核报告未绑定实际成品快照")


def registered_review(root, meta):
    reports = [e for e in events(root, "reviews") if e.get("target") == ref(meta)]
    if not reports:
        raise WorkflowError(f"{meta['kind']} 当前版本缺独立检核，上一轮须先复核")
    event = reports[-1]
    if not evidence_valid(root, event.get("evidence")):
        raise WorkflowError("检核报告原件丢失或已改变")
    try:
        report = read_json(local(root, event["evidence"]["path"]))
    except (OSError, ValueError) as exc:
        raise WorkflowError("检核报告原件无法读取") from exc
    report_checks(root, meta, report)
    expected = {k: report[k] for k in ("target", "status", "scope", "findings", "checked_sources")}
    expected.update(actor=report["reviewer_instance"], simulation=report.get("simulation", False),
                    visual_assessment=report.get("visual_assessment"))
    if (any(event.get(k) != value for k, value in expected.items())
            or not isinstance(event.get("simulation", False), bool)
            or "reviewer_instance" in event and event["reviewer_instance"] != report["reviewer_instance"]
            or "uncovered" in event and event["uncovered"] != report.get("uncovered", [])):
        raise WorkflowError("检核登记与报告原文的版本、作者、状态、范围或依据不一致")
    return event
