"""Local design-file submission and strategist-side expression review."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import phase5_common as common
from phase2_store import WorkflowError
from phase3_review import gate as phase3_gate, required_sources as phase3_sources
from phase3_sources import identifier
from phase3_store import current as phase3_current, ref as phase3_ref
from design_input import pages as _pages, strategy_checks as _strategy_checks, validate_report_coverage
from phase5_review import validate_report_fields
from workspace_lock import serialized

PASS = common.PASS
ASSESSMENTS = {"task_fulfilment", "evidence_reasoning", "counterevidence",
               "role_boundary", "uncertainties"}
REG = common.SUBMISSIONS


def _registry(root):
    path = common.local(root, REG)
    return common.read_json(path) if path.exists() else {"schema_version": "0.2", "submissions": {}}


def _save(root, value):
    common.atomic_json(common.local(root, REG), value)


def _brief(root, brief_ref):
    if not isinstance(brief_ref, dict) or brief_ref.get("space") != "phase3":
        raise WorkflowError("设计任务必须绑定 phase3 简报版本")
    meta = phase3_current(root, brief_ref.get("key"))
    if meta.get("kind") != "brief" or phase3_ref(meta) != brief_ref:
        raise WorkflowError("设计简报不是当前版本")
    phase3_gate(root, meta["key"])
    data = common.read_json(common.local(root, meta["path"]))
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise WorkflowError("策略简报结构不可读")
    return meta, data


def _target(item):
    return {"space": "design-submission", "key": item["submission_id"], "sha256": item["sha256"]}


def _current(root, submission_id):
    item = _registry(root).get("submissions", {}).get(submission_id)
    if not isinstance(item, dict):
        raise WorkflowError(f"缺少设计稿提交 {submission_id}")
    path = common.local(root, item["path"])
    if not path.is_file() or common.sha(path) != item["sha256"]:
        raise WorkflowError("设计稿原件缺失或指纹改变")
    brief, data = _brief(root, item["brief_ref"])
    if item["brief_ref"] != phase3_ref(brief):
        raise WorkflowError("设计简报已更新，当前设计稿检核失效")
    return item, path, data


def submit(root, task_id, input_path, submitter, brief_ref, request=None):
    with serialized(root):
        return _submit_unlocked(root, task_id, input_path, submitter, brief_ref, request)


def _submit_unlocked(root, task_id, input_path, submitter, brief_ref, request=None):
    root, input_path = Path(root).resolve(), Path(input_path)
    identifier(task_id)
    if not isinstance(submitter, str) or not submitter.strip():
        raise WorkflowError("设计稿提交必须记录策略师执行实例")
    if request is not None and not isinstance(request, dict):
        raise WorkflowError("提交请求须为对象")
    author = (request or {}).get("author_instance", submitter)
    if not isinstance(author, str) or not author.strip():
        raise WorkflowError("设计稿作者实例须明确")
    brief, brief_data = _brief(root, brief_ref)
    if brief["task_id"] != task_id:
        raise WorkflowError("设计稿必须使用本任务简报")
    try:
        pages, format_name = _pages(input_path)
    except (WorkflowError, ValueError, OSError, ImportError, KeyError) as exc:
        suffix = input_path.suffix.lower()
        if suffix == ".ppt":
            pages, format_name = 0, "unsupported"
        else:
            pages, format_name = 0, "unreadable"
        parse_error = str(exc)
    else:
        parse_error = None
    submission_id = f"DESIGN-{task_id}-{uuid.uuid4().hex[:12]}"
    suffix = input_path.suffix.lower() or ".bin"
    relative = f"project/design-submissions/{task_id}/{submission_id}{suffix}"
    target = common.local(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not input_path.is_file():
        raise WorkflowError("设计稿文件不存在")
    shutil.copyfile(input_path, target)
    digest = common.sha(target)
    checks, missing = _strategy_checks(brief_data)
    item = {"submission_id": submission_id, "task_id": task_id, "path": relative,
            "sha256": digest, "format": format_name, "pages": pages,
            "submitter_instance": submitter, "brief_ref": phase3_ref(brief),
            "author_instance": author,
            "strategy_checks": checks, "missing_strategy_inputs": missing,
            "parse_error": parse_error, "created_at": common.now()}
    reg = _registry(root)
    reg["submissions"][submission_id] = item
    _save(root, reg)
    common.append_event(root, "design_submission", {"submission_id": submission_id,
        "target": _target(item), "actor": submitter, "status": "submitted"})
    return {**item, "target": _target(item)}


def review_sources(root, submission_id):
    item, path, brief_data = _current(root, submission_id)
    expected = [{"path": item["path"], "sha256": item["sha256"]}]
    brief = phase3_current(root, item["brief_ref"]["key"])
    expected.extend(phase3_sources(root, brief))
    pages = list(range(1, item["pages"] + 1))
    return {"target": _target(item), "submission_id": submission_id,
            "brief_ref": item["brief_ref"], "submission_file": expected[0],
            "format": item["format"], "pages": pages,
            "strategy_checks": item["strategy_checks"],
            "missing_strategy_inputs": item["missing_strategy_inputs"],
            "scope_required": [f"page {p}" for p in pages] if pages else ["file readability"],
            "checked_sources_required": expected,
            "notice": "仅提供实际来源和检查范围，不代表通过。",
            "brief_title": brief_data.get("title", "")}


def _valid_file(root, ref):
    return isinstance(ref, dict) and isinstance(ref.get("path"), str) and (
        common.local(root, ref["path"]).is_file() and common.sha(common.local(root, ref["path"])) == ref.get("sha256"))


def _review_checks(root, item, report):
    validate_report_fields(report)
    sources = review_sources(root, item["submission_id"])
    if report.get("target") != sources["target"]:
        raise WorkflowError("设计检核报告不是当前提交版本")
    if report.get("submission_id") != item["submission_id"] or report.get("brief_ref") != item["brief_ref"]:
        raise WorkflowError("报告没有绑定当前设计稿和简报")
    reviewer = report.get("reviewer_instance")
    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer in {
            item["submitter_instance"], item["author_instance"]}:
        raise WorkflowError("提交者不能独立检核自己的设计稿")
    if report.get("status") not in PASS | {"returned", "insufficient_evidence"}:
        raise WorkflowError("无效设计检核状态")
    if not isinstance(report.get("simulation"), bool) or (report["simulation"] and not common.test_mode(root)):
        raise WorkflowError("报告必须明确 simulation；模拟仅限隔离测试")
    if not isinstance(report.get("scope"), list) or not report["scope"] or any(
            not isinstance(x, str) or not x.strip() for x in report["scope"]):
        raise WorkflowError("报告必须说明实际检查范围")
    if not isinstance(report.get("assessment"), dict) or any(
            not isinstance(report["assessment"].get(k), str) or not report["assessment"][k].strip()
            for k in ASSESSMENTS):
        raise WorkflowError("报告缺少策略检核的完整判断")
    findings, uncovered = report.get("findings"), report.get("uncovered")
    if not isinstance(findings, list) or not isinstance(uncovered, list):
        raise WorkflowError("报告缺少 findings 或 uncovered")
    if any(not isinstance(x, str) or not x.strip() for x in uncovered):
        raise WorkflowError("未覆盖范围须说明具体限制")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("level") not in {"red", "yellow", "green"}:
            raise WorkflowError("设计意见必须区分红黄绿")
        for field in ("location", "requirement", "evidence", "impact", "action", "decision_owner"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                raise WorkflowError(f"设计意见缺少 {field}")
    checked = report.get("checked_sources")
    if not isinstance(checked, list) or any(not _valid_file(root, ref) for ref in checked):
        raise WorkflowError("报告来源丢失或指纹不符")
    if not {(x["path"], x["sha256"]) for x in sources["checked_sources_required"]}.issubset(
            {(x.get("path"), x.get("sha256")) for x in checked}):
        raise WorkflowError("报告没有覆盖设计稿和策略简报全部来源")
    validate_report_coverage(root, item, report)
    if item["missing_strategy_inputs"] and report["status"] in PASS:
        raise WorkflowError("策略简报缺少关键检查项，不能宣称通过")
    if report["status"] in PASS and (uncovered or any(x["level"] == "red" for x in findings)):
        raise WorkflowError("有红灯或未覆盖范围，不能通过")
    if report["status"] in PASS and any(x["level"] == "yellow" for x in findings) != (
            report["status"] == "passed_with_yellow"):
        raise WorkflowError("整体状态与黄灯意见不一致")


def _last(root, submission_id):
    values = [x for x in common.events(root, "design_review") if x.get("submission_id") == submission_id]
    if not values:
        raise WorkflowError("当前设计稿尚未独立检核")
    return values[-1]


def record_review(root, submission_id, report_path, reviewer=""):
    with serialized(root):
        return _record_review_unlocked(root, submission_id, report_path, reviewer)


def _record_review_unlocked(root, submission_id, report_path, reviewer=""):
    item, _, _ = _current(root, submission_id)
    report = common.read_json(report_path)
    if not isinstance(report, dict):
        raise WorkflowError("独立检核报告必须是 JSON 对象")
    if reviewer and reviewer != report.get("reviewer_instance"):
        raise WorkflowError("调用检核者与报告作者不一致")
    _review_checks(root, item, report)
    archived = f"project/reviews/design-expression/{submission_id}-{uuid.uuid4().hex}.json"
    target = common.local(root, archived)
    common.atomic_bytes(target, Path(report_path).read_bytes())
    if common.read_json(target) != report:
        raise WorkflowError("报告归档期间内容改变")
    _review_checks(root, _current(root, submission_id)[0], report)
    return common.append_event(root, "design_review", {**report, "submission_id": submission_id,
        "target": _target(item), "evidence": {"path": archived, "sha256": common.sha(target)}})


def gate(root, submission_id):
    item, _, _ = _current(root, submission_id)
    event = _last(root, submission_id)
    evidence = event.get("evidence", {})
    if not _valid_file(root, evidence):
        raise WorkflowError("设计检核原件已失效")
    report = common.read_json(common.local(root, evidence["path"]))
    _review_checks(root, item, report)
    if report["status"] not in PASS:
        raise WorkflowError(f"设计检核未通过：{report['status']}")
    return item


def status(root, submission_id=None):
    if not submission_id:
        return all_status(root)
    item = _registry(root).get("submissions", {}).get(submission_id)
    if not item:
        raise WorkflowError(f"缺少设计稿提交 {submission_id}")
    try:
        gate(root, submission_id)
        return {"target": _target(item), "status": "passed", "review": _last(root, submission_id)["status"]}
    except (WorkflowError, OSError, ValueError, KeyError) as exc:
        return {"target": _target(item), "status": "blocked", "reason": str(exc),
                "format": item.get("format"), "pages": item.get("pages")}


def all_status(root):
    return {key: status(root, key) for key in _registry(root).get("submissions", {})}
