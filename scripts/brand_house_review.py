"""Independent HTML reports bind exact bytes, history and complete Phase 3 sources."""
from pathlib import Path

from brand_house_guard import registered
from brand_house_store import live, source_meta, target
from phase2_store import WorkflowError, archive, local, read_json, test_mode
from phase3_decisions import valid_event
from phase3_document import SECTIONS
from phase3_events import append, events
from phase3_io import registry
from phase3_review import PASS, gate as body_gate, required_sources as body_sources
from phase3_sources import file_valid
from task_validation import read_check

ASSESSMENTS = {"content_correspondence", "display", "history", "saving", "boundaries"}
SCOPE = {"header", "notes", *SECTIONS["brand-house"]}


def required_sources(root, record, data, meta):
    values = [{"path": record["path"], "sha256": record["observed_sha"]},
              *body_sources(root, meta), *body_sources(root, source_meta(root, data))]
    return [{"path": p, "sha256": h} for p, h in sorted({(f["path"], f["sha256"]) for f in values})]


def review_sources(root, task_id):
    record, data, meta = live(root, task_id)
    return {"target": target(record), "document_id": data["document_id"], "body": record["synced_body"],
            "scope_required": sorted(SCOPE), "assessment_required": sorted(ASSESSMENTS),
            "checked_sources_required": required_sources(root, record, data, meta),
            "notice": "这是实际文件清单，不是检核通过；须由不同实际执行实例核对并出具报告"}


def report_checks(root, record, data, meta, report):
    if not isinstance(report, dict) or report.get("target") != target(record):
        raise WorkflowError("HTML 独立报告未绑定当前确切版本与字节指纹")
    authors = set(record["author_instances"])
    authors.update(m["author_instance"] for m in registry(root)["artifacts"][meta["key"]])
    reviewer = report.get("reviewer_instance")
    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer in authors:
        raise WorkflowError("HTML 作者或原正文作者不能独立检核自己的产出")
    status = report.get("status")
    if status not in PASS | {"returned", "insufficient_evidence"}:
        raise WorkflowError("无效 HTML 独立检核状态")
    if not isinstance(report.get("simulation"), bool) or report["simulation"] and not test_mode(root):
        raise WorkflowError("真实项目不能使用模拟 HTML 检核")
    scope = report.get("scope")
    if not isinstance(scope, list) or not scope or any(not isinstance(s, str) or not s.strip() for s in scope):
        raise WorkflowError("HTML 报告须说明实际检查范围")
    assessment = report.get("assessment")
    if not isinstance(assessment, dict) or any(not isinstance(assessment.get(k), str) or
            not assessment[k].strip() for k in ASSESSMENTS):
        raise WorkflowError("HTML 报告须判断内容对应、显示、历史、保存和边界五项")
    findings, uncovered = report.get("findings"), report.get("uncovered")
    if not isinstance(findings, list) or not isinstance(uncovered, list):
        raise WorkflowError("HTML 报告缺问题清单或未覆盖范围")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("level") not in {"red", "yellow", "green"}:
            raise WorkflowError("HTML 检核意见须区分红黄绿")
        if any(not isinstance(finding.get(k), str) or not finding[k].strip() for k in
               ("location", "requirement", "evidence", "impact", "action", "decision_owner")):
            raise WorkflowError("HTML 检核意见缺位置、要求、证据、影响、动作或判断人")
    checked = report.get("checked_sources")
    if not isinstance(checked, list) or any(not file_valid(root, f) for f in checked):
        raise WorkflowError("HTML 报告所查来源丢失或指纹不符")
    actual = {(f["path"], f["sha256"]) for f in checked}
    expected = {(f["path"], f["sha256"]) for f in required_sources(root, record, data, meta)}
    if not expected.issubset(actual):
        raise WorkflowError("HTML 报告须读取实际 HTML 与完整 Phase 3 来源，不能只看作者摘要")
    if status in PASS:
        if uncovered or any(f["level"] == "red" for f in findings):
            raise WorkflowError("存在红灯或未覆盖范围，HTML 不能通过")
        if any(f["level"] == "yellow" for f in findings) != (status == "passed_with_yellow"):
            raise WorkflowError("HTML 整体状态与黄灯意见不一致")
        if not SCOPE.issubset(set(scope)):
            raise WorkflowError("HTML 通过报告未覆盖所有章节、标题和缺口问题")
        body_gate(root, meta["key"])


def record_review(root, task_id, report_path):
    path = Path(report_path).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise WorkflowError("正式 HTML 报告须先存入当前工作包")
    record, data, meta = live(root, task_id)
    report = read_json(path)
    report_checks(root, record, data, meta, report)
    evidence = archive(root, path, "evidence")
    if read_json(local(root, evidence["path"])) != report:
        raise WorkflowError("报告在归档期间改变")
    live(root, task_id)
    return append(root, "phase4-reviews", {**report, "actor": report["reviewer_instance"], "evidence": evidence})


@read_check
def gate(root, task_id):
    record, data, meta = live(root, task_id)
    matches = [e for e in events(root, "phase4-reviews") if e.get("target") == target(record)]
    if not matches:
        raise WorkflowError("当前 HTML 版本尚未独立检核；不能复用旧批准")
    event = matches[-1]
    if not valid_event(root, event):
        raise WorkflowError("HTML 独立报告原件已失效")
    report = read_json(local(root, event["evidence"]["path"]))
    report_checks(root, record, data, meta, report)
    if report["status"] not in PASS:
        raise WorkflowError(f"HTML 独立检核未通过：{report['status']}")
    return {"target": target(record), "path": record["path"], "body": record["synced_body"],
            "status": report["status"], "review_id": event["record_id"]}


def task_gate(root, task_id, recorded=None):
    if not registered(root, task_id):
        return None
    result = gate(root, task_id)["target"]
    if recorded is not None and recorded.get("html") != result:
        raise WorkflowError("任务完成记录未绑定当前 HTML 版本，重新核对完成依据")
    return result
