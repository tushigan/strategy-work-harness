"""Command-line entry point for local project memory."""
import argparse
import json
from pathlib import Path

from project_memory_store import inspect, save


def _request(value):
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    if value.lstrip().startswith("{"):
        return json.loads(value)
    path = Path(value)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value)


def main():
    parser = argparse.ArgumentParser(description="保存和读取工作包内的长期项目事实")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("inspect")
    read.add_argument("--workspace", type=Path, required=True)
    read.add_argument("--include-retired", action="store_true")
    write = commands.add_parser("save")
    write.add_argument("--workspace", type=Path, required=True)
    write.add_argument("--request-json", required=True,
                       help="JSON 原文、JSON 文件路径，或 @JSON文件路径")
    write.add_argument("--actor", required=True)
    args = parser.parse_args()
    try:
        result = (save(args.workspace, _request(args.request_json), args.actor)
                  if args.command == "save"
                  else inspect(args.workspace, args.include_retired))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if result.get("errors") else 0
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
