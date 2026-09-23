#!/usr/bin/env python3
"""Resolve formal and independent tasks, and record minimal project identity."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from phase2_store import WorkflowError, atomic_json, json_bytes, local, now, read_json
from standalone_store import digest, history, identifier, text
from workspace_lock import serialized


IDENTITY_FIELDS = ("project_id", "project_name", "client_name", "brand_name")
IDENTITY_LOG = "project/records/project-identity.jsonl"


def state_digest(state: dict) -> str:
    return hashlib.sha256(json_bytes(state)).hexdigest()


def identity_ready(state: dict) -> bool:
    return all(isinstance(state.get(name), str) and state[name].strip()
               for name in IDENTITY_FIELDS)


def identity_status(root: Path) -> dict:
    state = read_json(local(root, "project/state.json"))
    return {
        "ready": identity_ready(state),
        "contract_initialized": state.get("status") != "not_initialized",
        "state_sha256": state_digest(state),
        "identity": {name: state.get(name) for name in IDENTITY_FIELDS},
        "notice": "项目身份与合同计划分别判断；最小身份不代表合同已识别或项目已立项。",
    }


def _identity_events(root: Path) -> list[dict]:
    path = local(root, IDENTITY_LOG)
    if not path.exists():
        return []
    result = []
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if (not isinstance(item, dict) or item.get("schema_version") != 1
                    or not isinstance(item.get("request_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", item["request_id"])
                    or not isinstance(item.get("request_hash"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["request_hash"])):
                raise ValueError(f"第 {number} 行结构无效")
            result.append(item)
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"项目身份历史损坏，请恢复原件：{exc}") from exc
    return result


def save_identity(root: Path, request: dict, actor: str) -> dict:
    root = Path(root).resolve()
    allowed = {"request_id", "expected_state_sha256", "reason", "identity"}
    if not isinstance(request, dict) or set(request) != allowed:
        raise WorkflowError("项目身份请求字段不完整或含未知字段")
    identifier(request["request_id"])
    text(actor, "记录者")
    text(request["reason"], "登记原因")
    identity = request["identity"]
    if not isinstance(identity, dict) or set(identity) != set(IDENTITY_FIELDS):
        raise WorkflowError("项目身份须包含项目编号、项目名、客户名和品牌名")
    for name in IDENTITY_FIELDS:
        text(identity[name], name)
    request_hash = digest({"request": request, "actor": actor})
    with serialized(root):
        prior = next((item for item in _identity_events(root)
                      if item["request_id"] == request["request_id"]), None)
        if prior:
            if prior["request_hash"] != request_hash:
                raise WorkflowError("请求编号已用于不同的项目身份内容")
            return prior
        state_path = local(root, "project/state.json")
        state = read_json(state_path)
        current_sha = state_digest(state)
        already_applied = all(state.get(name) == identity[name] for name in IDENTITY_FIELDS)
        if request["expected_state_sha256"] != current_sha and not already_applied:
            raise WorkflowError("项目状态已变化，请重新读取；不能覆盖其他对话的修改")
        previous = {name: state.get(name) for name in IDENTITY_FIELDS}
        state.update(identity)
        state["last_updated"] = now()
        atomic_json(state_path, state)
        event = {
            "schema_version": 1,
            "request_id": request["request_id"],
            "request_hash": request_hash,
            "created_at": now(),
            "actor": actor,
            "reason": request["reason"],
            "previous_identity": previous,
            "identity": dict(identity),
            "state_sha256": state_digest(state),
            "contract_initialized": state.get("status") != "not_initialized",
        }
        path = local(root, IDENTITY_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event


def _formal_task(root: Path, task_id: str) -> dict | None:
    try:
        from phase3_store import plan_ref, task
        value = task(root, task_id)
        return {"kind": "formal", "task_id": task_id, "task": value,
                "reference": plan_ref(root), "status": value.get("status")}
    except (WorkflowError, OSError, ValueError, KeyError, TypeError):
        return None


def _standalone_task(root: Path, task_id: str) -> dict | None:
    events, _ = history(root)
    event = next((item for item in reversed(events) if item["task_id"] == task_id), None)
    if not event:
        return None
    value = event["task"]
    reference = {"space": "standalone", "key": task_id,
                 "version": event["revision"], "sha256": digest(value)}
    return {"kind": "standalone", "task_id": task_id, "task": value,
            "reference": reference, "status": value["status"],
            "updated_at": event["created_at"]}


def resolve_task(root: Path, task_id: str) -> dict:
    identifier(task_id)
    formal = _formal_task(root, task_id)
    standalone = _standalone_task(root, task_id)
    if formal and standalone:
        raise WorkflowError("正式任务与独立任务编号冲突，请先明确保留哪一项")
    if formal:
        return formal
    if standalone:
        return standalone
    raise WorkflowError("当前项目找不到该任务")


def inspect(root: Path, task_id: str | None = None) -> dict:
    result = {"identity": identity_status(root)}
    if task_id:
        result["task"] = resolve_task(root, task_id)
    else:
        events, _ = history(root)
        ids = list(dict.fromkeys(item["task_id"] for item in events))
        result["standalone_tasks"] = [resolve_task(root, value) for value in ids]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    show = sub.add_parser("inspect")
    show.add_argument("--workspace", type=Path, required=True)
    show.add_argument("--task-id")
    save = sub.add_parser("save-identity")
    save.add_argument("--workspace", type=Path, required=True)
    save.add_argument("--request", type=Path, required=True)
    save.add_argument("--actor", required=True)
    args = parser.parse_args()
    try:
        result = (inspect(args.workspace, args.task_id) if args.action == "inspect" else
                  save_identity(args.workspace,
                                json.loads(args.request.read_text(encoding="utf-8")), args.actor))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
