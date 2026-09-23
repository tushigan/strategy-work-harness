"""Local brand-house HTML generation, inspection, synchronization and review gates."""
import argparse
from pathlib import Path

from brand_house_generate import generate
from brand_house_review import gate, record_review, review_sources
from brand_house_store import inspect, scan
from brand_house_sync import sync_body
from phase2_store import WorkflowError, local, output_json


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("generate", "inspect", "scan", "sync-body", "review", "gate"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", type=Path, default=Path("."))
        if name != "scan":
            command.add_argument("--task", "--task-id", dest="task_id", required=True)
        if name in {"generate", "sync-body"}:
            command.add_argument("--author", required=True)
        if name == "sync-body":
            command.add_argument("--decision", required=True, help="工作包内真实决定 JSON 的相对路径")
        if name == "review":
            command.add_argument("--report", help="工作包内独立实例报告相对路径；省略则仅列实际检查来源")
    return result


def main():
    cli = parser()
    args = cli.parse_args()
    root = args.workspace.resolve()
    try:
        if args.command == "generate":
            result = generate(root, args.task_id, args.author)
        elif args.command == "inspect":
            result = inspect(root, args.task_id)
        elif args.command == "scan":
            result = scan(root)
        elif args.command == "sync-body":
            result = sync_body(root, args.task_id, local(root, args.decision), args.author)
        elif args.command == "review":
            result = record_review(root, args.task_id, local(root, args.report)) if args.report else review_sources(root, args.task_id)
        else:
            result = gate(root, args.task_id)
        output_json(result)
    except (OSError, WorkflowError, ValueError, KeyError, TypeError) as exc:
        cli.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
