"""Version-bound business review; not an identity or access-control service."""
import argparse
from pathlib import Path

from phase2_store import (WorkflowError, append, archive, current, events, local,
                         output_json, read_json, ref, revision_budget, sha, test_mode)
from phase2_review_guard import PASS, evidence_valid, registered_review, report_checks
from task_validation import read_check


def exact(event, meta):
    return event.get("target") == ref(meta)


def deterministic_gate(root, kind, meta):
    from validate_gantt import ready_contract, validate_current
    if kind == "contract" and not ready_contract(read_json(local(root, meta["path"]))):
        raise WorkflowError("合同仍有未处理问题，不能确认")
    if kind == "plan":
        check = validate_current(root)
        if not check["valid"]:
            raise WorkflowError("排期仍是草稿:\n" + "\n".join(check["errors"]))
    if kind == "gantt":
        from check_export import file_gate
        file_gate(root, meta)


def required_sources(root, meta):
    sources = [{"path": meta["snapshot_path"], "sha256": meta["sha256"]}, *meta["source_files"]]
    for dep in meta["dependencies"]:
        sources.extend(required_sources(root, current(root, dep["kind"])))
    if meta["kind"] == "gantt":
        from compare_import_template import binding
        sources.append(binding(root)["source"])
    if meta["kind"] == "gantt":
        from check_export import file_gate
        sources.extend(file_gate(root, meta)["previews"])
    return [{"path": p, "sha256": h} for p, h in
            sorted({(s["path"], s["sha256"]) for s in sources})]


def verify_pass_sources(root, meta, checked, report):
    expected = {(s["path"], s["sha256"]) for s in required_sources(root, meta)}
    actual = {(s["path"], s["sha256"]) for s in checked}
    if not expected.issubset(actual):
        raise WorkflowError("检核未覆盖原件、版本依赖、模板或全部显示效果素材")
    if meta["kind"] == "gantt" and (report.get("visual_assessment") or {}).get("status") != "passed":
        raise WorkflowError("实际 Excel 显示效果尚未由独立检核者评估")


@read_check
def gate(root, kind, human=True):
    meta = current(root, kind)
    for dep in meta["dependencies"]:
        gate(root, dep["kind"])
    deterministic_gate(root, kind, meta)
    report = registered_review(root, meta)
    if report["status"] not in PASS:
        raise WorkflowError(f"{kind} 独立检核未通过")
    verify_pass_sources(root, meta, report.get("checked_sources", []), report)
    if human:
        confirmations = [e for e in events(root, "confirmations") if exact(e, meta)]
        confirmation = confirmations[-1] if confirmations else {}
        if confirmation.get("status") != "confirmed" or confirmation.get("review_id") != report["record_id"]:
            raise WorkflowError(f"{kind} 当前版本未获策略师确认")
        if not evidence_valid(root, confirmation.get("evidence")):
            raise WorkflowError("人工确认依据丢失或改变")
        if confirmation.get("simulation") and not test_mode(root):
            raise WorkflowError("正式项目不能采用模拟确认")
    return meta


def record_review(root, kind, report_path):
    meta = current(root, kind)
    report = read_json(report_path)
    report_checks(root, meta, report)
    actor, status = report["reviewer_instance"], report["status"]
    checked = report.get("checked_sources", [])
    if status in PASS:
        for dep in meta["dependencies"]:
            gate(root, dep["kind"])
        deterministic_gate(root, kind, meta)
        verify_pass_sources(root, meta, checked, report)
    return append(root, "reviews", {"record_type": "machine_review", "target": ref(meta),
                  "actor": actor, "status": status, "scope": report["scope"],
                  "findings": report["findings"], "checked_sources": checked,
                  "uncovered": report.get("uncovered", []),
                  "visual_assessment": report.get("visual_assessment"),
                  "simulation": bool(report.get("simulation")),
                  "evidence": archive(root, report_path, "evidence")})


def confirm(root, kind, evidence_path, actor, status="confirmed", simulation=False):
    if not actor.strip() or not Path(evidence_path).read_text(encoding="utf-8").strip():
        raise WorkflowError("必须提供实际确认者及非空确认原文")
    if simulation and not test_mode(root):
        raise WorkflowError("模拟确认仅允许隔离测试项目")
    meta = gate(root, kind, human=False) if status == "confirmed" else current(root, kind)
    reports = [e for e in events(root, "reviews") if exact(e, meta)]
    return append(root, "confirmations", {"record_type": "human_confirmation", "target": ref(meta),
                  "actor": actor, "status": status, "scope": kind, "simulation": simulation,
                  "review_id": reports[-1]["record_id"] if reports else None,
                  "evidence": archive(root, evidence_path, "evidence")})


def allow_more(root, kind, evidence_path, actor, simulation=False):
    if simulation and not test_mode(root):
        raise WorkflowError("模拟决定仅允许隔离测试项目")
    if not actor.strip() or not Path(evidence_path).read_text(encoding="utf-8").strip():
        raise WorkflowError("缺少策略师的真实决定原文")
    count, allowance = revision_budget(root, kind)
    return append(root, "phase2-decisions", {"record_type": "revision_decision", "kind": kind,
                  "actor": actor, "status": "allow_more_revisions", "simulation": simulation,
                  "previous_count": count, "allowed_total": max(count, allowance) + 2,
                  "evidence": archive(root, evidence_path, "evidence")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["review", "confirm", "reject", "status", "allow-more"])
    parser.add_argument("kind", choices=["contract", "plan", "gantt"])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--actor", default="")
    parser.add_argument("--simulation", action="store_true")
    args = parser.parse_args()
    if args.action == "status":
        from workflow_status import import_state
        result = {"status": ("test_ready_not_for_upload" if test_mode(args.workspace)
                             else "ready_for_manual_upload") if args.kind == "gantt" else "confirmed",
                  "artifact": gate(args.workspace, args.kind), "system_import": import_state(args.workspace)["status"]}
    elif args.evidence is None:
        raise WorkflowError("缺 --evidence")
    elif args.action == "review":
        result = record_review(args.workspace, args.kind, args.evidence)
    elif args.action == "allow-more":
        result = allow_more(args.workspace, args.kind, args.evidence, args.actor, args.simulation)
    else:
        result = confirm(args.workspace, args.kind, args.evidence, args.actor,
                         "confirmed" if args.action == "confirm" else "rejected", args.simulation)
    output_json(result)


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
