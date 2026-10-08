"""Independent review of controller output, never an automatic approval."""
from control_artifacts import latest, review_sources
from phase2_store import WorkflowError, local, read_json, test_mode
from phase5_review import ASSESSMENTS, validate_report_fields
from phase6_events import append, archive, decision_valid, valid_file
from phase6_targets import catalog, control_ref, identity, last_review, resolve

PASS = {"passed", "passed_with_yellow"}


def revision_review(root, meta):
    target = control_ref(meta)
    try:
        record = last_review(root, target)
    except WorkflowError:
        raise WorkflowError("上一轮修订尚未独立复核，不能继续自动修订") from None
    if not decision_valid(root, record) or any(not valid_file(root, f) for f in meta["files"]):
        raise WorkflowError("上一轮独立复核或产出原件失效，不能跳过复核")
    if record.get("reviewer_instance") in catalog(root)[identity(target)]["authors"]:
        raise WorkflowError("上一轮复核者参与过此产出，须独立实例重新复核")
    raw = read_json(local(root, record["evidence"]["path"]))
    if raw.get("target") != target or any(record.get(k) != v for k, v in raw.items()):
        raise WorkflowError("上一轮复核与归档原文不符")
    # A returned review permits correction; passing the old version is not required.
    return record


def checks(root, meta, report):
    validate_report_fields(report)
    expected = review_sources(root, meta["key"])
    if report.get("target") != expected["target"]:
        raise WorkflowError("报告不是当前总控版本")
    reviewer = report.get("reviewer_instance")
    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer in expected["author_instances"]:
        raise WorkflowError("总控作者不能自审，须实际调用不同执行实例")
    if type(report.get("simulation")) is not bool or report["simulation"] and not test_mode(root):
        raise WorkflowError("模拟检核仅限 test_mode 合成案例")
    if report.get("status") not in PASS | {"returned", "insufficient_evidence"}:
        raise WorkflowError("无效检核状态")
    for name in ("scope", "uncovered", "checked_sources", "findings"):
        if not isinstance(report.get(name), list):
            raise WorkflowError(f"缺少 {name} 列表")
    if not report["scope"] or any(not isinstance(x, str) or not x.strip()
            for x in report["scope"] + report["uncovered"]):
        raise WorkflowError("检查范围和未覆盖项须为明确文字")
    if not isinstance(report.get("assessment"), dict) or any(
            not isinstance(report["assessment"].get(k), str) or not report["assessment"][k].strip()
            for k in ASSESSMENTS):
        raise WorkflowError("须逐项判断任务、推导、反证、职责和不确定性")
    for item in report["findings"]:
        if not isinstance(item, dict) or item.get("level") not in {"red", "yellow", "green"} or any(
                not isinstance(item.get(k), str) or not item[k].strip()
                for k in ("location", "requirement", "evidence", "impact", "action", "decision_owner")):
            raise WorkflowError("检核意见缺等级、定位、依据、影响或动作")
    if any(not valid_file(root, f) for f in report["checked_sources"]):
        raise WorkflowError("已检查文件丢失或指纹不符")
    actual = {(f["path"], f["sha256"]) for f in report["checked_sources"]}
    missing = {f"source:{f['path']}" for f in expected["checked_sources_required"]
               if (f["path"], f["sha256"]) not in actual}
    missing.update(f"scope:{s}" for s in expected["scope_required"] if s not in report["scope"])
    if missing and not missing.issubset(report["uncovered"]):
        raise WorkflowError("缺少原文/章节必须逐项写入 uncovered")
    if report["status"] in PASS:
        if missing or report["uncovered"] or any(f["level"] == "red" for f in report["findings"]):
            raise WorkflowError("有红灯或未覆盖范围不能通过")
        if any(f["level"] == "yellow" for f in report["findings"]) != (
                report["status"] == "passed_with_yellow"):
            raise WorkflowError("黄灯与整体结论不一致")
        from review_gate import gate
        for dep in meta["dependencies"]:
            gate(root, dep)


def record_review(root, key, path):
    meta = resolve(root, control_ref(latest(root, key)))
    report = read_json(path)
    checks(root, meta, report)
    evidence = archive(root, path)
    if read_json(local(root, evidence["path"])) != report:
        raise WorkflowError("报告在归档过程中改变")
    checks(root, resolve(root, control_ref(meta)), report)
    return append(root, "control_review", {**report, "evidence": evidence})


def gate(root, key):
    meta = resolve(root, control_ref(latest(root, key)))
    record = last_review(root, control_ref(meta))
    if not valid_file(root, record.get("evidence")):
        raise WorkflowError("总控检核报告原件已失效")
    report = read_json(local(root, record["evidence"]["path"]))
    checks(root, meta, report)
    if report["status"] not in PASS:
        raise WorkflowError("总控产出未获独立检核通过")
    return meta
