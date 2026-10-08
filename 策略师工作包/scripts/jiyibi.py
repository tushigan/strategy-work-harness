#!/usr/bin/env python3
"""记一笔（U12）：把工作包外的关键动作（导出、外部写入、发送、交付、上传）记进项目记录。

只记事实与证据位置，不执行动作、不代表成功以外的含义；结果未知如实写 unknown，先核对、不重发。"""
import argparse
import json
from pathlib import Path

from phase2_store import WorkflowError, local, now
from standalone_store import text
from workspace_lock import serialized

LOG = "project/records/off-package-actions.jsonl"
KINDS = {"export", "external_write", "send", "delivery", "upload", "other"}
RESULTS = {"success", "failed", "unknown"}


def record(root, kind, summary, result, actor, task_id=None, refs=(), url=None):
    from registration_guard import actor_check
    from yunxing_rizhi import detect_host
    actor_check(actor); text(summary, "动作说明")
    if kind not in KINDS or result not in RESULTS:
        raise WorkflowError("kind 须为 " + "/".join(sorted(KINDS)) + "；result 须为 success/failed/unknown")
    evidence = []
    for value in refs:
        path = local(root, value)
        if not path.is_file():
            raise WorkflowError(f"证据文件不存在：{value}")
        import hashlib
        evidence.append({"path": value, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    row = {"schema_version": 1, "kind": kind, "summary": summary, "result": result, "task_id": task_id, "url": url,
           "evidence": evidence, "host": detect_host(), "actor": actor, "created_at": now(),
           "notice": "记一笔只是记录；结果未知先核对，不重发" if result == "unknown" else "记一笔只是记录"}
    with serialized(root):
        path = local(root, LOG); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def recent(root, limit=12):
    path = Path(root) / LOG
    if not path.is_file():
        return []
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return rows[-limit:]


def main():
    parser = argparse.ArgumentParser(description="记一笔：工作包外关键动作")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--ref", action="append", default=[])
    parser.add_argument("--url")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    print(json.dumps(record(args.workspace.resolve(), args.kind, args.summary, args.result, args.actor, args.task_id, args.ref, args.url),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main

    def main():
        return cli(_business_main, __file__)

    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
