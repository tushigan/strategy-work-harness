"""Independent business review and human confirmation, always version-bound."""
from pathlib import Path

from phase2_review import gate as phase2_gate, required_sources as phase2_sources
from phase2_store import WorkflowError, archive, local, read_json, test_mode
from phase3_events import append, events
from phase3_decisions import valid_event
from phase3_io import registry
from phase3_sources import file_valid, source_files
from phase3_store import current, ref, resolve
from task_validation import read_check

PASS = {"passed", "passed_with_yellow"}
ASSESSMENTS = {"task_fulfilment", "evidence_reasoning", "counterevidence",
               "role_boundary", "uncertainties"}
RESERVED_FIELDS = {"record_id", "created_at", "record_type", "actor", "evidence"}


def required_sources(root, meta):
    values = [{"path": meta["snapshot_path"], "sha256": meta["sha256"]},
              {"path": meta["markdown_snapshot"], "sha256": meta["markdown_sha256"]},
              *meta.get("source_files", [])]
    for dep in meta["dependencies"]:
        upstream = resolve(root, dep)
        if dep["space"] == "phase2":
            values.extend(phase2_sources(root, upstream))
        elif dep["space"] == "source":
            values.extend(source_files(root, dep))
        else:
            values.extend(required_sources(root, upstream))
    return [{"path": p, "sha256": h} for p, h in sorted({(v["path"], v["sha256"]) for v in values})]


def dependencies_pass(root, meta):
    for dep in meta["dependencies"]:
        if dep["space"] == "phase2":
            phase2_gate(root, dep["key"])
        elif dep["space"] == "phase3":
            gate(root, dep["key"])
        else:
            resolve(root, dep)


def report_checks(root, meta, report):
    if not isinstance(report, dict):
        raise WorkflowError("独立报告必须是 JSON 对象")
    supplied = RESERVED_FIELDS.intersection(report)
    if supplied:
        raise WorkflowError(f"独立报告包含系统保留字段：{', '.join(sorted(supplied))}")
    if report.get("target") != ref(meta):
        raise WorkflowError("独立报告不是当前产出版本")
    actor = report.get("reviewer_instance")
    authors = {m["author_instance"] for m in registry(root)["artifacts"][meta["key"]]}
    if not isinstance(actor, str) or not actor.strip() or actor in authors:
        raise WorkflowError("作者实例不能独立检核自己参与撰写的产出")
    status = report.get("status")
    if status not in PASS | {"returned", "insufficient_evidence"}:
        raise WorkflowError("无效独立检核状态")
    if not isinstance(report.get("simulation"), bool):
        raise WorkflowError("报告须明确是否为自动测试模拟")
    if report["simulation"] and not test_mode(root):
        raise WorkflowError("真实项目不能使用模拟检核")
    if not isinstance(report.get("scope"), list) or not report["scope"] or any(
            not isinstance(s, str) or not s.strip() for s in report["scope"]):
        raise WorkflowError("报告须说明实际检查范围")
    if not isinstance(report.get("assessment"), dict) or any(
           not isinstance(report["assessment"].get(k), str) or
           not report["assessment"][k].strip() for k in ASSESSMENTS):
        raise WorkflowError("检核须说明任务、推导、反证、职责和不确定性的实质判断")
    findings, uncovered = report.get("findings"), report.get("uncovered")
    if not isinstance(findings, list) or not isinstance(uncovered, list):
        raise WorkflowError("报告缺问题清单或未覆盖范围")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("level") not in {"red", "yellow", "green"}:
            raise WorkflowError("检核意见须区分红黄绿")
        if any(not isinstance(finding.get(k), str) or not finding[k].strip()
               for k in ("location", "requirement", "evidence", "impact", "action", "decision_owner")):
            raise WorkflowError("检核意见缺位置、要求、依据、影响、动作或判断人")
    checked = report.get("checked_sources")
    if not isinstance(checked, list) or any(not file_valid(root, f) for f in checked):
        raise WorkflowError("报告的已读来源丢失或指纹不符")
    actual = {(f["path"], f["sha256"]) for f in checked}
    expected = {(f["path"], f["sha256"]) for f in required_sources(root, meta)}
    if (meta["snapshot_path"], meta["sha256"]) not in actual:
        raise WorkflowError("报告未核对实际成品")
    if status in PASS:
        if uncovered or any(f["level"] == "red" for f in findings):
            raise WorkflowError("存在红灯或未覆盖范围，不能通过")
        yellow = any(f["level"] == "yellow" for f in findings)
        if yellow != (status == "passed_with_yellow"):
            raise WorkflowError("整体状态与黄灯意见不一致")
        if not expected.issubset(actual):
            raise WorkflowError("通过报告未覆盖成品、原文或上游依赖")
        sections = read_json(local(root, meta["snapshot_path"]))["sections"]
        if not {s["id"] for s in sections}.issubset(set(report["scope"])):
            raise WorkflowError("通过报告没有覆盖所有业务章节")
        dependencies_pass(root, meta)


def record_review(root, key, report_path):
    meta = current(root, key)
    report = read_json(report_path)
    report_checks(root, meta, report)
    evidence = archive(root, report_path, "evidence")
    if read_json(local(root, evidence["path"])) != report or ref(current(root, key)) != ref(meta):
        raise WorkflowError("报告归档期间文件或目标改变，不能登记旧判断")
    return append(root, "phase3-reviews", {
        **report, "actor": report["reviewer_instance"],
        "evidence": evidence})


def last_review(root, meta):
    matches = [r for r in events(root, "phase3-reviews") if r.get("target") == ref(meta)]
    if not matches:
        raise WorkflowError("当前版本尚未独立检核")
    return matches[-1]


@read_check
def gate(root, key, human=False):
    meta = current(root, key)
    review = last_review(root, meta)
    if not valid_event(root, review):
        raise WorkflowError("独立报告原件已失效")
    report = read_json(local(root, review["evidence"]["path"]))
    report_checks(root, meta, report)
    if any(review.get(k) != v for k, v in report.items()) or (
            review.get("actor") != report["reviewer_instance"]):
        raise WorkflowError("独立检核登记与报告原文不一致")
    if report["status"] not in PASS:
        raise WorkflowError(f"独立检核未通过：{report['status']}")
    if human:
        confirmations = [e for e in events(root, "phase3-confirmations") if e.get("target") == ref(meta)]
        confirmation = confirmations[-1] if confirmations else {}
        if not valid_event(root, confirmation) or confirmation.get("status") != "confirmed" or (
                confirmation.get("review_id") != review["record_id"]) or (
                confirmation.get("review_sha256") != review["evidence"]["sha256"]):
            raise WorkflowError("当前版本没有有效的策略师内部确认")
    return meta


def confirm(root, key, evidence_path, actor, status="confirmed", simulation=False):
    if status not in {"confirmed", "rejected"} or not actor.strip():
        raise WorkflowError("确认需要实际确认者和有效决定")
    if not Path(evidence_path).read_text(encoding="utf-8").strip():
        raise WorkflowError("必须保留策略师提供的确认原文")
    if simulation and not test_mode(root):
        raise WorkflowError("模拟审批仅限隔离合成测试")
    meta = gate(root, key) if status == "confirmed" else current(root, key)
    if status == "confirmed":
        payload = read_json(local(root, meta["path"]))
        if any(g["blocks_completion"] for g in payload["gaps"]):
            raise WorkflowError("仍有阻断性缺口，不能确认完成")
    review = last_review(root, meta)
    return append(root, "phase3-confirmations", {
        "target": ref(meta), "status": status, "actor": actor, "scope": "internal_only",
        "review_id": review["record_id"], "simulation": simulation,
        "review_sha256": review["evidence"]["sha256"],
        "evidence": archive(root, evidence_path, "evidence")})
