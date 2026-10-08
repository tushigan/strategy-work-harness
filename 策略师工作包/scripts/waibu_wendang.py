#!/usr/bin/env python3
"""外部文档登记为交付物与外部能力实测探针（U09）。

register：飞书等外部文档登记 URL、回读快照（项目内文件）与回读时间，绑定任务；不写外部、不存凭据。
compare：改稿前把外部现值的新回读与登记快照比对，不同即提示可能有人手改。
probe：对外部能力只读实测（如 lark-cli 身份状态），失败或无法判断显示“未知”，不显示“未配置”。"""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from phase2_store import WorkflowError, local, now
from standalone_store import identifier, text
from workspace_lock import serialized

DOCS = "project/records/external-docs.jsonl"
PROBES = "project/records/capability-probes.jsonl"
PROBE_COMMANDS = {"feishu": ["lark-cli", "auth", "status"]}


def _rows(root, name):
    path = local(root, name)
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def _append(root, name, row):
    path = local(root, name); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _url(value):
    text(value, "外部文档链接"); parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username or parts.password or any(c.isspace() for c in value):
        raise WorkflowError("外部文档链接格式无效，不允许账号密码")
    return value


def register(root, task_id, url, snapshot, label, actor):
    from registration_guard import actor_check
    actor_check(actor); identifier(task_id); _url(url); text(label, "说明")
    if not snapshot.startswith("project/"):
        raise WorkflowError("回读快照须保存在 project/ 内（例如 project/inputs/外部回读/）")
    path = local(root, snapshot)
    if not path.is_file():
        raise WorkflowError("回读快照文件不存在：先只读回读外部文档并保存，再登记")
    try:
        from task_context import resolve_task
        resolve_task(Path(root).resolve(), task_id)
    except (ValueError, OSError, KeyError) as exc:
        raise WorkflowError(f"--task-id {task_id}：找不到这个任务（独立任务或正式计划任务），外部文档须绑定已登记的任务") from exc
    with serialized(root):
        row = {"schema_version": 1, "event": "registered", "task_id": task_id, "url": url, "label": label,
               "snapshot": {"path": snapshot, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
               "read_at": now(), "actor": actor, "role": "deliverable",
               "notice": "外部文档为交付物；工作包不写外部、不保存凭据"}
        return _append(root, DOCS, row)


def current(root, task_id=None):
    latest = {}
    for row in _rows(root, DOCS):
        if task_id is None or row["task_id"] == task_id:
            latest[(row["task_id"], row["url"])] = row
    return list(latest.values())


def compare(root, task_id, url, readback):
    rows = [r for r in current(root, task_id) if r["url"] == url]
    if not rows:
        raise WorkflowError("这个外部文档还没有登记为交付物")
    path = local(root, readback)
    if not path.is_file():
        raise WorkflowError("新回读文件不存在")
    old = local(root, rows[-1]["snapshot"]["path"])
    now_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    same = now_sha == rows[-1]["snapshot"]["sha256"]
    result = {"task_id": task_id, "url": url, "snapshot": rows[-1]["snapshot"], "read_at": rows[-1]["read_at"],
              "current": {"path": readback, "sha256": now_sha}, "changed": not same}
    if not same:
        before = old.read_text(encoding="utf-8", errors="replace").splitlines() if old.is_file() else []
        after = path.read_text(encoding="utf-8", errors="replace").splitlines()
        result.update(added_lines=len(set(after) - set(before)), removed_lines=len(set(before) - set(after)),
                      notice="外部现值与登记快照不同，可能有人在外部手改；先和策略师确认以哪版为准，再改稿并重新登记快照")
    return result


def probe(root, name, actor, command=None, timeout=10):
    from registration_guard import actor_check
    actor_check(actor)
    command = command or PROBE_COMMANDS.get(name)
    if not command:
        raise WorkflowError("没有该外部能力的只读探针命令")
    if shutil.which(command[0]) is None:
        status, detail = "unknown", f"本机找不到 {command[0]}，无法实测"
    else:
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            output = (run.stdout + run.stderr)[-2000:]
            lowered = output.lower()
            negative = any(w in lowered for w in ("not logged", "未登录", "expired", "unauthorized", "过期", "invalid", "error"))
            positive = any(w in lowered for w in ("logged in", "已登录", '"logged_in": true', '"status": "ok"', "authenticated", "valid until", "有效"))
            if run.returncode == 0 and positive and not negative:
                status, detail = "available", "只读探针返回成功且输出明确表示已登录"
            elif run.returncode == 0 and not negative:
                status, detail = "unknown", "探针返回成功但输出没有明确的已登录标识，无法确认可用（不等于未配置）"
            else:
                status, detail = "unknown", f"探针返回码 {run.returncode}，无法确认可用"
        except subprocess.TimeoutExpired:
            status, detail = "unknown", "探针超时"
        except OSError as exc:
            status, detail = "unknown", f"探针无法运行：{type(exc).__name__}"
    row = {"schema_version": 1, "name": name, "status": status, "detail": detail, "command": command[:3],
           "checked_at": now(), "actor": actor, "notice": "只读探测，不写外部、不保存凭据；未知不等于未配置"}
    with serialized(root):
        return _append(root, PROBES, row)


PROBE_FRESH_HOURS = 24


def latest_probes(root, now_time=None):
    """每项外部能力最近一次实测；超过 24 小时标为过期（resume 显示“未知（实测已过期）”）。"""
    from datetime import datetime, timezone
    try:
        rows = _rows(root, PROBES)
    except (OSError, ValueError):
        return {}
    result = {}
    current = now_time or datetime.now(timezone.utc)
    for r in rows:
        try:
            age = (current - datetime.fromisoformat(r["checked_at"])).total_seconds() / 3600
        except (KeyError, TypeError, ValueError):
            age = None
        result[r["name"]] = {**r, "age_hours": None if age is None else round(age, 1),
                             "stale": age is None or age > PROBE_FRESH_HOURS}
    return result


def main():
    parser = argparse.ArgumentParser(description="外部文档交付物登记/比对与外部能力只读探针")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("register")
    for k in ("--workspace",): r.add_argument(k, type=Path, required=True)
    r.add_argument("--task-id", required=True); r.add_argument("--url", required=True); r.add_argument("--snapshot", required=True)
    r.add_argument("--label", default="外部文档"); r.add_argument("--actor", required=True); r.add_argument("--json", action="store_true")
    c = sub.add_parser("compare"); c.add_argument("--workspace", type=Path, required=True); c.add_argument("--task-id", required=True)
    c.add_argument("--url", required=True); c.add_argument("--readback", required=True); c.add_argument("--json", action="store_true")
    p = sub.add_parser("probe"); p.add_argument("--workspace", type=Path, required=True); p.add_argument("--name", required=True)
    p.add_argument("--actor", required=True); p.add_argument("--json", action="store_true")
    args = parser.parse_args(); root = args.workspace.resolve()
    if args.command == "register":
        result = register(root, args.task_id, args.url, args.snapshot, args.label, args.actor)
    elif args.command == "compare":
        result = compare(root, args.task_id, args.url, args.readback)
    else:
        result = probe(root, args.name, args.actor)
    print(json.dumps(result, ensure_ascii=False, indent=2))
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
