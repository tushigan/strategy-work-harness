"""Natural-language controller entry point for small, unplanned project tasks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from phase2_store import WorkflowError, now
from workspace_lock import serialized
from standalone_store import (JOURNAL, digest, history, identifier, reference_path,
                              storage, task_value, text)


def save(root, request, actor):
    root = Path(root).resolve()
    storage(root)  # Check before acquiring a lock that creates directories.
    required_fields = {"task_id", "request_id", "expected_revision", "reason", "task"}
    if (not isinstance(request, dict)
            or set(request) not in (required_fields,
                                    required_fields | {"accept_changed_references"})):
        raise WorkflowError("请求字段不完整或含未支持字段")
    accept_changed_references = request.get("accept_changed_references", False)
    if type(accept_changed_references) is not bool:
        raise WorkflowError("accept_changed_references 须为布尔值")
    identifier(request["task_id"])
    identifier(request["request_id"])
    text(actor, "记录者")
    text(request["reason"], "更新原因")
    if type(request["expected_revision"]) is not int or request["expected_revision"] < 0:
        raise WorkflowError("expected_revision 须为非负整数")
    hash_request = dict(request)
    if hash_request.get("accept_changed_references") is False:
        hash_request.pop("accept_changed_references")
    request_hash = digest({"request": hash_request, "actor": actor})
    with serialized(root):
        events, raw = history(root)
        prior = next((e for e in events if e["request_id"] == request["request_id"]), None)
        if prior:
            if prior["request_hash"] != request_hash:
                raise WorkflowError("请求编号已用于不同内容，保留原记录")
            return prior
        current = next((e for e in reversed(events) if e["task_id"] == request["task_id"]), None)
        previous_source_key = current["task"].get("source_request_key") if current else None
        requested_source_key = request["task"].get("source_request_key")
        if previous_source_key and requested_source_key not in (None, previous_source_key):
            raise WorkflowError("来源请求键已绑定，不能在后续版本中更换")
        source_key = previous_source_key or requested_source_key
        if source_key:
            duplicate = next((e for e in reversed(events)
                              if e["task"].get("source_request_key") == source_key
                              and e["task_id"] != request["task_id"]), None)
            if duplicate:
                raise WorkflowError(f"同一来源请求已登记为任务 {duplicate['task_id']}，请续做原任务")
        revision = current["revision"] if current else 0
        if request["expected_revision"] != revision:
            raise WorkflowError("任务版本已变化，请重新读取后更新，不能覆盖其它对话的进度")
        task_request = dict(request["task"])
        if previous_source_key and requested_source_key is None:
            task_request["source_request_key"] = previous_source_key
        normalized_task = task_value(
            root, task_request,
            previous=current["task"] if current else None,
            accept_changed_references=accept_changed_references,
        )
        references_changed = bool(current and any(
            normalized_task[name] != current["task"][name]
            for name in ("sources", "deliverables")
        ))
        if current and current["task"]["status"] == "completed" and references_changed:
            if not accept_changed_references:
                raise WorkflowError("来源或交付物已换版，须显式接受新版")
            if (normalized_task["status"] == "completed"
                    or normalized_task["completion_evidence"].strip()):
                raise WorkflowError("接受新版后须先进入待核对状态并清空旧完成依据")
        if current and current["task"]["status"] != "completed" \
                and normalized_task["status"] == "completed":
            previous_completed = next((event for event in reversed(events)
                                       if event["task_id"] == request["task_id"]
                                       and event["task"]["status"] == "completed"), None)
            if (previous_completed
                    and any(current["task"][name] != previous_completed["task"][name]
                            for name in ("sources", "deliverables"))
                    and normalized_task["completion_evidence"]
                    == previous_completed["task"]["completion_evidence"]):
                raise WorkflowError("新版交付物须提交新的完成依据，不能沿用旧版文案")
        event = {"schema_version": 1, "task_id": request["task_id"],
                 "request_id": request["request_id"], "request_hash": request_hash,
                 "revision": revision + 1, "created_at": now(), "actor": actor,
                 "reason": request["reason"], "task": normalized_task}
        path = storage(root)
        fd, temporary = tempfile.mkstemp(prefix=".writing-standalone-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw + (b"\n" if raw and not raw.endswith(b"\n") else b""))
                stream.write((json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            if (path.read_bytes() if path.exists() else b"") != raw:
                raise WorkflowError("保存期间记录被修改，请重新读取；未覆盖原件")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return event


def inspect(root, include_archived=False):
    root = Path(root).resolve()
    result = {"status": "empty", "count": 0, "active_count": 0, "tasks": [],
              "errors": [], "record_path": JOURNAL,
              "scope": "独立任务进度；不代表合同交付、独立检核或客户批准"}
    try:
        events, _ = history(root)
    except (OSError, ValueError, RecursionError) as exc:
        result.update(status="blocked", errors=[str(exc)])
        return result
    latest = {e["task_id"]: e for e in events}
    for event in latest.values():
        task = event["task"]
        if task["status"] == "archived" and not include_archived:
            continue
        issues, links = [], []
        for ref in task["sources"] + task["deliverables"]:
            if "url" in ref:
                links.append({**ref, "availability": "外部链接未随包复制；本次未联网核验"})
                continue
            try:
                path = reference_path(root, ref["path"])
                if not path.is_file():
                    issues.append({"path": ref["path"], "issue": "文件缺失"})
                elif hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
                    issues.append({"path": ref["path"], "issue": "文件已变化，需回读并登记新版本"})
            except (OSError, ValueError):
                issues.append({"path": ref["path"], "issue": "文件不可读取或引用不安全"})
        item = {**task, "task_id": event["task_id"], "revision": event["revision"],
                "updated_at": event["created_at"], "actor": event["actor"],
                "reason": event["reason"], "file_issues": issues, "external_links": links,
                "effective_status": "needs_attention" if issues else task["status"]}
        result["tasks"].append(item)
    result["count"] = len(result["tasks"])
    result["active_count"] = sum(t["status"] in {"planned", "in_progress", "waiting"}
                                  for t in result["tasks"])
    if latest:
        result["status"] = "attention" if any(t["file_issues"] for t in result["tasks"]) else "recorded"
    return result


def main():
    parser = argparse.ArgumentParser(description="独立任务记录与恢复，不改变合同计划")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("save", "inspect"):
        sub = commands.add_parser(command)
        sub.add_argument("--workspace", type=Path, required=True)
        sub.add_argument("--json", action="store_true", help="输出始终为 JSON")
        if command == "save":
            sub.add_argument("--request", type=Path, required=True)
            sub.add_argument("--actor", required=True)
        else:
            sub.add_argument("--include-archived", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "save":
            result = save(args.workspace, json.loads(args.request.read_text(encoding="utf-8")), args.actor)
        else:
            result = inspect(args.workspace, args.include_archived)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if result.get("errors") else 0
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
