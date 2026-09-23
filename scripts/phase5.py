"""Phase 5 local entry point: proposal script, HTML deck and design review."""
import argparse
import json
from pathlib import Path

from phase2_store import WorkflowError, output_json
from phase5_common import all_status, local, registry, status_for


def object_file(path, label):
    if not path:
        raise WorkflowError(f"缺少{label}文件")
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkflowError(f"{label}必须是 JSON 对象")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=[
        "status", "impact-scan", "recover", "allow-more", "reject", "task-start", "task-complete",
        "script-publish", "script-sources", "script-review", "script-gate", "script-confirm",
        "deck-generate", "deck-sources", "deck-browser-check", "deck-review", "deck-gate", "deck-confirm",
        "design-submit", "design-sources", "design-review", "design-gate", "design-status",
    ])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--task")
    parser.add_argument("--key")
    parser.add_argument("--submission")
    parser.add_argument("--actor", default="")
    parser.add_argument("--author", default="")
    parser.add_argument("--brief-ref", type=Path)
    parser.add_argument("--simulation", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    root = args.workspace.resolve()
    request = object_file(args.request, "请求") if args.request else {}

    if args.action.startswith("task-"):
        from phase5_tasks import start, complete
        result = start(root, args.task, args.actor, args.as_of) if args.action == "task-start" else complete(root, args.task, args.actor)
    elif args.action == "recover":
        from phase5_io import recover
        result = recover(root)
    elif args.action == "allow-more":
        from phase5_decisions import allow_more
        result = allow_more(root, args.key, args.input, args.actor, args.simulation)
    elif args.action == "reject":
        from phase5_common import confirm
        result = confirm(root, args.key, args.input, args.actor, args.simulation, status="rejected")
    elif args.action == "status":
        if args.refresh:
            from phase5_tasks import refresh
            output_json(refresh(root))
            return
        result = {"artifacts": all_status(root), "submissions": {}}
        try:
            from design_expression import all_status as design_status
            result["submissions"] = design_status(root)
        except (ImportError, OSError, ValueError, KeyError, WorkflowError) as exc:
            result["submissions_error"] = str(exc)
    elif args.action == "impact-scan":
        from design_expression import all_status as design_status
        result = {"artifacts": all_status(root), "submissions": design_status(root)}
    elif args.action.startswith("script-"):
        from proposal_script import (confirm, gate, publish, record_review, review_sources, status)
        key = args.key or (f"{args.task}::proposal-script" if args.task else None)
        if args.action == "script-publish":
            result = publish(root, args.input, request, args.author or args.actor)
        elif args.action == "script-sources":
            result = review_sources(root, key)
        elif args.action == "script-review":
            result = record_review(root, key, args.input)
        elif args.action == "script-gate":
            result = gate(root, key, human=request.get("human", False))
        else:
            result = confirm(root, key, args.input, args.actor, args.simulation)
    elif args.action.startswith("deck-"):
        from html_deck import confirm, generate, gate, inspect, record_review, review_sources
        key = args.key or (f"{args.task}::html-deck" if args.task else None)
        if args.action == "deck-generate":
            result = generate(root, args.task, args.author or args.actor, request)
        elif args.action == "deck-sources":
            result = review_sources(root, key)
        elif args.action == "deck-browser-check":
            from deck_browser import run
            result = run(root, key)
        elif args.action == "deck-review":
            result = record_review(root, key, args.input)
        elif args.action == "deck-gate":
            result = gate(root, key, human=request.get("human", False))
        elif args.action == "deck-confirm":
            result = confirm(root, key, args.input, args.actor, args.simulation)
    else:
        from design_expression import (gate, record_review, review_sources, status, submit)
        if args.action == "design-submit":
            result = submit(root, args.task, args.input, args.actor or args.author,
                            request.get("brief"), request)
        elif args.action == "design-sources":
            result = review_sources(root, args.submission)
        elif args.action == "design-review":
            result = record_review(root, args.submission, args.input, args.author or args.actor)
        elif args.action == "design-gate":
            result = gate(root, args.submission)
        else:
            result = status(root, args.submission)
    output_json(result)


if __name__ == "__main__":
    try:
        main()
    except RecursionError:
        raise SystemExit("错误：JSON 嵌套过深，请减少嵌套层级后重试") from None
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"错误：{exc}")
