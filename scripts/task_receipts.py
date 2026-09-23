#!/usr/bin/env python3
"""Append action receipts and version-bound relations for project tasks."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from phase2_store import WorkflowError, now
from standalone_store import digest, identifier, references, storage, text
from task_context import resolve_task
from workspace_lock import serialized


JOURNAL = "project/records/task-receipts.jsonl"
KINDS = {"receipt", "relation", "formal_link"}
OUTCOMES = {"success", "failed", "unknown"}
EVENT_FIELDS = {"schema_version", "kind", "record_id", "request_id", "request_hash",
                "revision", "created_at", "actor", "reason", "task_id", "task_ref", "data"}


def journal(root: Path) -> Path:
    storage(root)
    path = Path(root).resolve() / JOURNAL
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise WorkflowError("任务回执记录必须为普通文件")
    return path


def _refs(root: Path, values: object, stored: bool = False) -> list[dict]:
    result = references(root, values, stored=stored)
    if any(item.get("path") == JOURNAL for item in result):
        raise WorkflowError("任务回执不能引用自身日志")
    return result


def _reference_only(value: dict) -> dict:
    keys = ("label", "path", "sha256") if "path" in value else ("label", "url")
    return {name: value[name] for name in keys}


def _task_ref(root: Path, task_id: str) -> dict:
    return resolve_task(root, task_id)["reference"]


def _deliverable(root: Path, task_id: str, label: str) -> dict:
    context = resolve_task(root, task_id)
    if context["kind"] != "standalone":
        raise WorkflowError("当前版本关系只允许绑定独立任务交付物；正式成果沿用既有版本引用")
    matches = [item for item in context["task"]["deliverables"] if item.get("label") == label]
    if len(matches) != 1:
        raise WorkflowError("来源任务中须有且只有一份同名当前交付物")
    return {"task_id": task_id, "task_revision": context["reference"]["version"], **matches[0]}


def _data(root: Path, kind: str, task_id: str, value: object, stored: bool = False) -> dict:
    if not isinstance(value, dict):
        raise WorkflowError("回执或关系内容必须为对象")
    if kind == "receipt":
        expected = {"source_id", "source_checkpoint", "checked_at", "outcome", "summary",
                    "source_request_key", "external_operation_id", "evidence_refs"}
        if set(value) != expected:
            raise WorkflowError("行动回执字段不完整或含未知字段")
        for name in ("source_id", "source_checkpoint", "checked_at", "summary"):
            text(value[name], name)
        for name in ("source_request_key", "external_operation_id"):
            if value[name] is not None:
                text(value[name], name)
        if value["outcome"] not in OUTCOMES:
            raise WorkflowError("行动结果须为 success、failed 或 unknown")
        result = dict(value)
        result["evidence_refs"] = _refs(root, value["evidence_refs"], stored=stored)
        if value["outcome"] == "success" and not result["evidence_refs"]:
            raise WorkflowError("成功回执须附实际返回或回读证据")
        return result
    if kind == "relation":
        request_fields = {"upstream_task_id", "upstream_deliverable_label",
                          "downstream_task_id", "relation_type", "status", "note"}
        stored_fields = request_fields | {"upstream_deliverable", "downstream_task_ref"}
        if set(value) != (stored_fields if stored else request_fields):
            raise WorkflowError("任务关系字段不完整或含未知字段")
        for name in ("upstream_task_id", "upstream_deliverable_label",
                     "downstream_task_id", "relation_type", "status", "note"):
            text(value[name], name)
        if value["status"] not in {"active", "retired"}:
            raise WorkflowError("任务关系状态无效")
        if task_id != value["downstream_task_id"]:
            raise WorkflowError("关系记录必须归属于下游任务")
        if value["upstream_task_id"] == value["downstream_task_id"]:
            raise WorkflowError("任务不能依赖自身")
        result = dict(value)
        if stored:
            if not isinstance(value["upstream_deliverable"], dict):
                raise WorkflowError("关系缺少上游交付版本")
            _refs(root, [_reference_only(value["upstream_deliverable"])], stored=True)
            if not isinstance(value["downstream_task_ref"], dict):
                raise WorkflowError("关系缺少下游任务版本")
        else:
            result["upstream_deliverable"] = _deliverable(
                root, value["upstream_task_id"], value["upstream_deliverable_label"])
            result["downstream_task_ref"] = _task_ref(root, value["downstream_task_id"])
        return result
    expected = {"formal_task_id", "decision", "evidence_refs", "note"}
    if set(value) != expected:
        raise WorkflowError("正式计划关联字段不完整或含未知字段")
    text(value["formal_task_id"], "正式任务编号")
    text(value["decision"], "转入决定")
    text(value["note"], "说明")
    if not stored:
        formal = resolve_task(root, value["formal_task_id"])
        if formal["kind"] != "formal":
            raise WorkflowError("关联目标须为当前正式计划任务")
    result = dict(value)
    result["evidence_refs"] = _refs(root, value["evidence_refs"], stored=stored)
    if not result["evidence_refs"]:
        raise WorkflowError("转入正式计划须附明确决定证据")
    return result


def history(root: Path) -> tuple[list[dict], bytes]:
    path = journal(root)
    if not path.exists():
        return [], b""
    raw = path.read_bytes()
    if not raw.strip():
        raise WorkflowError("任务回执历史为空，可能被截断；请恢复原件")
    records, latest, requests = [], {}, set()
    try:
        for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict) or set(item) != EVENT_FIELDS:
                raise ValueError(f"第 {number} 行字段无效")
            identifier(item["record_id"]); identifier(item["request_id"]); identifier(item["task_id"])
            if (item["schema_version"] != 1 or item["kind"] not in KINDS
                    or type(item["revision"]) is not int
                    or item["revision"] != latest.get(item["record_id"], 0) + 1
                    or item["request_id"] in requests):
                raise ValueError(f"第 {number} 行版本或请求编号无效")
            for name in ("created_at", "actor", "reason"):
                text(item[name], name)
            _data(root, item["kind"], item["task_id"], item["data"], stored=True)
            records.append(item); latest[item["record_id"]] = item["revision"]
            requests.add(item["request_id"])
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise WorkflowError("任务回执历史损坏，请核对原件；未修改旧记录") from exc
    return records, raw


def save(root: Path, request: dict, actor: str) -> dict:
    expected = {"kind", "record_id", "request_id", "expected_revision", "reason", "task_id", "data"}
    if not isinstance(request, dict) or set(request) != expected:
        raise WorkflowError("任务回执请求字段不完整或含未知字段")
    if request["kind"] not in KINDS:
        raise WorkflowError("未知回执类别")
    for name in ("record_id", "request_id", "task_id"):
        identifier(request[name])
    text(actor, "记录者"); text(request["reason"], "更新原因")
    if type(request["expected_revision"]) is not int or request["expected_revision"] < 0:
        raise WorkflowError("expected_revision 须为非负整数")
    request_hash = digest({"request": request, "actor": actor})
    root = Path(root).resolve(); journal(root)
    with serialized(root):
        records, raw = history(root)
        prior = next((x for x in records if x["request_id"] == request["request_id"]), None)
        if prior:
            if prior["request_hash"] != request_hash:
                raise WorkflowError("请求编号已用于不同内容，保留原记录")
            return prior
        current = next((x for x in reversed(records) if x["record_id"] == request["record_id"]), None)
        revision = current["revision"] if current else 0
        if revision != request["expected_revision"]:
            raise WorkflowError("回执版本已变化，请重新读取后更新")
        if request["kind"] == "receipt" and request["data"].get("source_request_key"):
            duplicates = [x for x in records if x["kind"] == "receipt"
                          and x["data"].get("source_request_key") == request["data"]["source_request_key"]
                          and x["task_id"] != request["task_id"]]
            if duplicates:
                raise WorkflowError(f"同一来源请求已关联任务 {duplicates[-1]['task_id']}，请续做原任务")
        data = _data(root, request["kind"], request["task_id"], request["data"])
        event = {"schema_version": 1, "kind": request["kind"],
                 "record_id": request["record_id"], "request_id": request["request_id"],
                 "request_hash": request_hash, "revision": revision + 1, "created_at": now(),
                 "actor": actor, "reason": request["reason"], "task_id": request["task_id"],
                 "task_ref": _task_ref(root, request["task_id"]), "data": data}
        path = journal(root)
        fd, temporary = tempfile.mkstemp(prefix=".writing-receipt-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw + (b"\n" if raw and not raw.endswith(b"\n") else b""))
                stream.write((json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode())
                stream.flush(); os.fsync(stream.fileno())
            if (path.read_bytes() if path.exists() else b"") != raw:
                raise WorkflowError("保存期间回执记录被修改，请重新读取")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return event


def _relation_issue(root: Path, event: dict) -> str | None:
    if event["data"]["status"] == "retired":
        return None
    value = event["data"]["upstream_deliverable"]
    try:
        current = _deliverable(root, event["data"]["upstream_task_id"],
                               event["data"]["upstream_deliverable_label"])
        expected = {k: value.get(k) for k in ("label", "path", "url", "sha256") if k in value}
        actual = {k: current.get(k) for k in expected}
        if actual != expected:
            return "上游交付物已换版，下游仍绑定旧版"
        _refs(root, [_reference_only(value)], stored=True)
        if "path" in value:
            checked = _refs(root, [{"label": value["label"], "path": value["path"]}])[0]
            if checked["sha256"] != value["sha256"]:
                return "上游交付文件内容已变化，需先登记新版"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return str(exc)
    return None


def inspect(root: Path) -> dict:
    result = {"status": "empty", "record_path": JOURNAL, "receipts": [], "relations": [],
              "formal_links": [], "attention": [], "errors": []}
    try:
        records, _ = history(Path(root).resolve())
        latest = {item["record_id"]: item for item in records}
        for item in latest.values():
            row = dict(item)
            if item["kind"] == "relation":
                issue = _relation_issue(Path(root).resolve(), item)
                row["effective_status"] = "needs_attention" if issue else item["data"]["status"]
                if issue:
                    row["issue"] = issue; result["attention"].append(row)
                result["relations"].append(row)
            else:
                result["receipts" if item["kind"] == "receipt" else "formal_links"].append(row)
        result["status"] = "attention" if result["attention"] else ("recorded" if records else "empty")
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        result.update(status="blocked", errors=[str(exc)])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("save", "inspect"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--actor", default="")
    args = parser.parse_args()
    try:
        if args.action == "save":
            if not args.request:
                raise WorkflowError("save 缺少 --request")
            result = save(args.workspace, json.loads(args.request.read_text(encoding="utf-8")), args.actor)
        else:
            result = inspect(args.workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if result.get("errors") else 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
