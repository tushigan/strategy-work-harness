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
        failed = bool(result.get("errors"))
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        from yunxing_rizhi import command_error
        command_error(exc)  # G04：命令级运行日志记这条报错首行
        print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False))
        failed = True
    if failed and args.command == "inspect":
        from yunxing_rizhi import StateReport
        raise StateReport(1)  # F08（r4 #1）：查询如实报出 blocked，退出码仍 1，重试守卫不计数；真崩（未捕获异常）照常计数
    return 1 if failed else 0


if __name__ == "__main__":
    from yunxing_rizhi import cli  # G04 r3：与其他入口一样套上运行日志，入口捕获的报错由 command_error 记首行
    raise SystemExit(cli(main, __file__))
