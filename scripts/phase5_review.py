"""Version-bound independent review, human decisions and honest status."""
import uuid
from pathlib import Path

from phase5_common import (PASS, WorkflowError, append_event, atomic_bytes, current,
                           events, latest, local, read_json, ref, registry,
                           required_sources, sha, test_mode)
from task_validation import read_check

ASSESSMENTS = {"task_fulfilment", "evidence_reasoning", "counterevidence",
               "role_boundary", "uncertainties"}
RESERVED_FIELDS = {"record_id", "created_at", "event", "evidence", "required_scope"}


def validate_report_fields(report):
    if not isinstance(report, dict):
        raise WorkflowError("独立检核报告必须是 JSON 对象")
    supplied = RESERVED_FIELDS.intersection(report)
    if supplied:
        raise WorkflowError(f"独立检核报告包含系统保留字段：{', '.join(sorted(supplied))}")


def last_review(root, target):
    matches = [item for item in events(root, "review") if item.get("target") == target]
    if not matches:
        raise WorkflowError("当前版本尚未独立检核")
    return matches[-1]


def valid_event_file(root, item):
    if not isinstance(item, dict) or not item.get("path"):
        return False
    path = local(root, item["path"])
    return path.is_file() and sha(path) == item.get("sha256")


def review_checks(root, meta, report, *, required_scope=()):
    validate_report_fields(report)
    if report.get("target") != ref(meta):
        raise WorkflowError("独立报告不是当前产出版本")
    reviewer = report.get("reviewer_instance")
    authors = {x["author_instance"] for x in registry(root)["artifacts"][meta["key"]]}
    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer in authors:
        raise WorkflowError("作者实例不能独立检核自己参与撰写的产出")
    if report.get("status") not in PASS | {"returned", "insufficient_evidence"}:
        raise WorkflowError("无效独立检核状态")
    if not isinstance(report.get("simulation"), bool) or (
            report["simulation"] and not test_mode(root)):
        raise WorkflowError("报告必须明确 simulation；模拟检核仅限隔离测试项目")
    scope = report.get("scope")
    if not isinstance(scope, list) or not scope or any(not isinstance(x, str) or not x.strip() for x in scope):
        raise WorkflowError("报告必须说明实际检查范围")
    assessment = report.get("assessment")
    if not isinstance(assessment, dict) or any(not isinstance(assessment.get(k), str) or
            not assessment[k].strip() for k in ASSESSMENTS):
        raise WorkflowError("检核须说明任务、推导、反证、职责及不确定性的实质判断")
    findings, uncovered, checked = (report.get(k) for k in ("findings", "uncovered", "checked_sources"))
    if not isinstance(findings, list) or not isinstance(uncovered, list) or not isinstance(checked, list):
        raise WorkflowError("报告缺少 findings、uncovered 或 checked_sources")
    if any(not isinstance(x, str) or not x.strip() for x in uncovered):
        raise WorkflowError("未覆盖范围须为明确文字列表")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("level") not in {"red", "yellow", "green"}:
            raise WorkflowError("检核意见必须区分红黄绿")
        for field in ("location", "requirement", "evidence", "impact", "action", "decision_owner"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                raise WorkflowError(f"检核意见缺少 {field}")
    if any(not valid_event_file(root, item) for item in checked):
        raise WorkflowError("报告已读来源丢失或指纹不符")
    expected = {(x["path"], x["sha256"]) for x in required_sources(root, meta)}
    actual = {(x["path"], x["sha256"]) for x in checked}
    if not any((x["path"], x["sha256"]) in actual for x in meta["snapshot_files"]):
        raise WorkflowError("报告未核对实际产物")
    from phase5_visual import required_scope as actual_scope, validate_deck
    required_scope = set(required_scope) | set(actual_scope(root, meta))
    missing = {f"source:{path}" for path, _ in expected - actual}
    missing.update(f"scope:{section}" for section in required_scope - set(scope))
    if missing and (not missing.issubset(uncovered) or not any(x not in missing for x in uncovered)):
        raise WorkflowError("未覆盖范围须逐项列出 source:路径、scope:编号，并说明未检查原因")
    if report["status"] in PASS:
        if missing:
            raise WorkflowError("通过报告必须覆盖实际成品、全部上游来源和业务章节")
        if uncovered or any(x["level"] == "red" for x in findings):
            raise WorkflowError("有红灯或未覆盖范围，不能记录为通过")
        if any(x["level"] == "yellow" for x in findings) != (report["status"] == "passed_with_yellow"):
            raise WorkflowError("整体状态与黄灯意见不一致")
        if meta["kind"] == "html-deck":
            validate_deck(root, meta, report)


def record_review(root, key, report_path, required_scope=()):
    meta = current(root, key)
    report = read_json(report_path)
    review_checks(root, meta, report, required_scope=required_scope)
    archived = f"project/reviews/phase5/{key.replace(':', '-')}-v{meta['version']:04d}-{uuid.uuid4().hex}.json"
    target = local(root, archived)
    atomic_bytes(target, Path(report_path).read_bytes())
    if read_json(target) != report or ref(current(root, key)) != ref(meta):
        raise WorkflowError("报告归档期间文件或目标改变，不能登记旧判断")
    review_checks(root, meta, report, required_scope=required_scope)
    return append_event(root, "review", {**report, "target": ref(meta),
        "required_scope": list(required_scope), "evidence": {"path": archived, "sha256": sha(target)}})


@read_check
def gate(root, key, *, human=False):
    meta = current(root, key)
    review = last_review(root, ref(meta))
    if not valid_event_file(root, review.get("evidence")):
        raise WorkflowError("独立报告原件已失效")
    report = read_json(local(root, review["evidence"]["path"]))
    review_checks(root, meta, report, required_scope=review.get("required_scope", ()))
    if report["status"] not in PASS:
        raise WorkflowError(f"独立检核未通过：{report['status']}")
    for dep in meta["dependencies"]:
        if dep["space"] == "phase3":
            from phase3_review import gate as upstream_gate
            upstream_gate(root, dep["key"], human=meta["kind"] in {"proposal-script", "html-deck"} and
                          dep["key"].endswith(("::brand-house", "::derivation")))
        elif dep["space"] == "phase5":
            gate(root, dep["key"], human=meta["kind"] == "html-deck" and
                 dep["key"].endswith("::proposal-script"))
    if human:
        decisions = [x for x in events(root, "confirmation") if x.get("target") == ref(meta)]
        decision = decisions[-1] if decisions else {}
        if decision.get("status") != "confirmed" or decision.get("review_id") != review["record_id"] or not (
                decision.get("review_sha256") == review["evidence"]["sha256"]) or not (
                valid_event_file(root, decision.get("evidence"))) or (
                decision.get("simulation") and not test_mode(root)):
            raise WorkflowError("当前版本没有绑定最新检核的有效策略师确认")
    return meta


def confirm(root, key, evidence_path, actor, simulation=False, status="confirmed"):
    if not isinstance(actor, str) or not actor.strip() or type(simulation) is not bool or simulation and not test_mode(root):
        raise WorkflowError("确认需要实际确认者；模拟确认仅限隔离测试项目")
    if status not in {"confirmed", "rejected"}:
        raise WorkflowError("无效人工决定")
    evidence = Path(evidence_path)
    if not evidence.is_file() or not evidence.read_text(encoding="utf-8").strip():
        raise WorkflowError("必须保留策略师提供的确认原文")
    meta = gate(root, key) if status == "confirmed" else current(root, key)
    archived = f"project/reviews/phase5/confirmations/{uuid.uuid4().hex}.txt"
    target = local(root, archived)
    atomic_bytes(target, evidence.read_bytes())
    try:
        review = last_review(root, ref(meta))
    except WorkflowError:
        if status != "rejected":
            raise
        review = {}
    return append_event(root, "confirmation", {"target": ref(meta), "status": status,
        "actor": actor, "simulation": simulation, "review_id": review.get("record_id"),
        "review_sha256": review.get("evidence", {}).get("sha256"),
        "evidence": {"path": archived, "sha256": sha(target)}})


def status_for(root, key):
    try:
        meta = current(root, key)
    except (WorkflowError, OSError, ValueError, KeyError) as exc:
        return {"target": ref(latest(root, key)), "status": "stale", "reason": str(exc)}
    result = {"target": ref(meta), "status": "current", "review": "not_reviewed", "human_confirmed": False}
    try:
        gate(root, key)
        result["review"] = last_review(root, ref(meta))["status"]
    except (WorkflowError, OSError, ValueError, KeyError) as exc:
        result.update(review="blocked", reason=str(exc))
        return result
    try:
        gate(root, key, human=True)
        result["human_confirmed"] = True
    except (WorkflowError, OSError, ValueError, KeyError):
        pass
    return result
