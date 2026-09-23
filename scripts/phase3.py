"""Local Phase 3 entry point; content is authored by Codex, not by a canned generator."""
import argparse
from pathlib import Path

from phase2_store import WorkflowError, output_json, read_json
from phase3_decisions import decision, restore_registered, resume_pending
from phase3_document import scaffold
from phase3_feedback import bind_feedback, capabilities, ingest_feedback
from phase3_io import recover
from phase3_review import confirm, record_review, required_sources
from phase3_sources import register_source
from source_verification import inspect_source, verify_source
from phase3_store import current, publish
from phase3_tasks import complete, inspect, refresh, start


def read_object(path, label):
    if path is None:
        raise WorkflowError(f"缺少{label}文件，请提供对应路径")
    value = read_json(path)
    if not isinstance(value, dict):
        raise WorkflowError(f"{label}必须是 JSON 对象，不能是数组或单个值")
    return value


def check_request(request):
    dependencies = request.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(d, dict) for d in dependencies):
        raise WorkflowError("上游 dependencies 必须是版本引用对象列表")
    for name in ("revision", "target"):
        if name in request and not isinstance(request[name], dict):
            raise WorkflowError(f"请求中的 {name} 必须是对象")
    revision = request.get("revision", {})
    if "section_reasons" in revision and not isinstance(revision["section_reasons"], dict):
        raise WorkflowError("section_reasons 必须按章节填写修改原因")
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["source", "source-status", "source-verify", "publish", "review", "confirm", "reject",
                        "feedback", "bind-feedback", "status", "start", "complete", "recover",
                        "decision", "restore-registered", "review-sources", "capabilities", "scaffold"])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--task")
    parser.add_argument("--kind")
    parser.add_argument("--actor", default="")
    parser.add_argument("--version", type=int, help="核验所依据的来源版本，由 source-status 回读")
    parser.add_argument("--sha256", help="当前来源数据指纹")
    parser.add_argument("--original-sha256", help="归档原件指纹")
    parser.add_argument("--location", help="第1页，或 'Sheet'!A2:B2 这样的明确范围")
    parser.add_argument("--verified-by", help="实际核验人，不是自动推定的资料提供者")
    parser.add_argument("--reason", help="本次补录核验的理由")
    parser.add_argument("--verification-action", help="实际执行过的原件核验动作")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--simulation", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    request = check_request(read_object(args.request, "请求")) if args.request else {}
    action = args.action
    if action == "source":
        result = register_source(root, args.input, read_object(args.metadata, "来源元数据"), args.actor)
    elif action == "source-status":
        result = inspect_source(root, args.key)
    elif action == "source-verify":
        reference = {"space": "source", "key": args.key, "version": args.version, "sha256": args.sha256}
        result = verify_source(root, reference, args.original_sha256, args.location, args.input,
                               args.verified_by, args.reason, args.verification_action, args.actor)
    elif action == "publish":
        result = publish(root, read_object(args.input, "产出"), args.actor, request.get("dependencies", []),
                         request.get("revision"))
    elif action == "review":
        result = record_review(root, args.key, args.input)
    elif action in {"confirm", "reject"}:
        result = confirm(root, args.key, args.input, args.actor,
                         "confirmed" if action == "confirm" else "rejected", args.simulation)
    elif action == "feedback":
        transcript = Path(request["transcript"]) if request.get("transcript") else None
        result = ingest_feedback(root, args.input, args.actor, request.get("feedback_role", "strategist"),
                                 request.get("target"), transcript, args.simulation)
    elif action == "bind-feedback":
        result = bind_feedback(root, request["feedback_id"], request["target"], args.input,
                               args.actor, args.simulation)
    elif action == "start":
        result = start(root, args.task, args.actor)
    elif action == "complete":
        result = complete(root, args.task, args.actor)
    elif action == "status":
        result = refresh(root) if args.refresh else inspect(root)
    elif action == "recover":
        result = resume_pending(root, request["decision_id"]) if request.get("decision_id") else recover(root)
    elif action == "decision":
        result = decision(root, args.key, request["action"], args.input, args.actor, args.simulation)
    elif action == "restore-registered":
        result = restore_registered(root, args.key, request["decision_id"])
    elif action == "review-sources":
        result = required_sources(root, current(root, args.key))
    elif action == "scaffold":
        result = scaffold(args.task, args.kind, request.get("brief_type", "strategy"))
    else:
        result = capabilities()
    if action not in {"status", "capabilities", "review-sources", "scaffold", "source-status"}:
        refresh(root)
    output_json(result)


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(str(exc))
