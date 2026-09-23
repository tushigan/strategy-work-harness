#!/usr/bin/env python3
"""Record explicit fresh-Agent handoffs without pretending to spawn one."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dispatch_files import file_ref, local, text_ref, verify_refs
from phase2_store import WorkflowError, atomic_json, local as phase2_local
from workspace_lock import serialized

LOG = "project/records/agent-dispatch.jsonl"
INDEX = "project/records/agent-dispatch-index.json"
ROLES = {
    "project-controller", "strategy-author", "proposal-author", "deck-builder",
    "design-expression-reviewer", "independent-reviewer",
}
ACTIVE = {"prepared", "assigned", "returned_for_readback"}


def read_log(root: Path) -> list[dict]:
    path = root / LOG
    if not path.exists():
        return []
    records: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"分派记录损坏，第 {number} 行不能解析") from exc
        if not isinstance(value, dict):
            raise WorkflowError(f"分派记录损坏，第 {number} 行不是对象")
        records.append(value)
    return records


def append(root: Path, event: dict) -> dict:
    path = root / LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"record_id": uuid.uuid4().hex,
              "created_at": datetime.now(timezone.utc).isoformat(), **event}
    atomic_json(phase2_local(root, INDEX), {"schema_version": 1, "last_record_id": record["record_id"]})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


def latest(records: list[dict], dispatch_id: str) -> dict | None:
    found = [item for item in records if item.get("dispatch_id") == dispatch_id]
    return found[-1] if found else None


def replay(records: list[dict]) -> dict[str, dict]:
    states: dict[str, dict] = {}
    for item in records:
        dispatch_id = item.get("dispatch_id")
        if not dispatch_id:
            raise WorkflowError("分派记录缺少 dispatch_id")
        states.setdefault(dispatch_id, {}).update(item)
    return states


def project_context(root: Path, task_id: str) -> tuple[dict, dict]:
    from project_memory_store import inspect as inspect_memory
    memory = inspect_memory(root)
    if memory["errors"]:
        raise WorkflowError("项目记忆无法安全加载：" + "；".join(memory["errors"]))
    memory_snapshot = {key: memory[key] for key in ("memory_version", "digest", "current_facts")}
    from task_context import resolve_task
    has_task_records = ((root / "project/records/standalone-tasks.jsonl").exists()
                        or (root / "project/tasks/task-plan.json").exists())
    try:
        task = resolve_task(root, task_id)
        task_snapshot = {"status": "verified", "kind": task["kind"],
                         "reference": task["reference"]}
    except WorkflowError:
        if has_task_records:
            raise
        task_snapshot = {"status": "unverified_legacy", "kind": None, "reference": None}
    return memory_snapshot, task_snapshot


def verify_context(root: Path, item: dict) -> None:
    verify_refs(root, item)
    expected_memory = item.get("project_memory")
    expected_task = item.get("task_context")
    if expected_memory is None and expected_task is None:
        return
    current_memory, current_task = project_context(root, item["task_id"])
    if expected_memory != current_memory:
        raise WorkflowError("项目记忆版本已变化或来源失效，须重新准备分派")
    if expected_task != current_task:
        raise WorkflowError("任务上下文已变化，须重新准备分派")


def validate_request(root: Path, request: dict) -> dict:
    role = request.get("role")
    if role not in ROLES:
        raise WorkflowError("未知分派角色，不能绕过六个工作角色")
    task_id = request.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise WorkflowError("分派必须绑定 task_id")
    text = request.get("task_text")
    if not isinstance(text, str) or not text.strip():
        raise WorkflowError("分派必须保留完整任务原文")
    for name in ("permissions", "completion_criteria"):
        value = request.get(name)
        if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value):
            raise WorkflowError(f"{name}必须是非空文字列表")
    inputs = request.get("input_files")
    if not isinstance(inputs, list) or not inputs:
        raise WorkflowError("分派必须提供至少一个原文或输入文件")
    input_refs = [file_ref(root, value, "输入文件") for value in inputs]
    upstream = request.get("upstream_refs", [])
    if not isinstance(upstream, list) or any(not isinstance(item, dict) for item in upstream):
        raise WorkflowError("上游版本必须是对象列表")
    if any(not all(isinstance(item.get(key), (str, int)) for key in ("space", "key", "version", "sha256"))
           for item in upstream):
        raise WorkflowError("上游版本必须包含 space/key/version/sha256")
    author = request.get("author_instance")
    if author is not None and (not isinstance(author, str) or not author.strip()):
        raise WorkflowError("author_instance 不能为空")
    diagnostic = request.get("diagnostic_instance")
    if diagnostic is not None and (not isinstance(diagnostic, str) or not diagnostic.strip()):
        raise WorkflowError("diagnostic_instance 不能为空")
    excluded = request.get("excluded_instances", [])
    if not isinstance(excluded, list) or any(not isinstance(x, str) or not x.strip() for x in excluded):
        raise WorkflowError("excluded_instances必须是文字列表")
    excluded = list(dict.fromkeys(excluded + [x for x in (author, diagnostic) if x]))
    if role == "independent-reviewer" and not excluded:
        raise WorkflowError("独立检核必须提供作者/诊断者实例排除集")
    memory_snapshot, task_snapshot = project_context(root, task_id)
    return {
        "role": role, "task_id": task_id, "task_text": text,
        "input_files": input_refs, "upstream_refs": upstream,
        "permissions": list(request["permissions"]),
        "completion_criteria": list(request["completion_criteria"]),
        "author_instance": author, "diagnostic_instance": diagnostic,
        "excluded_instances": excluded,
        "independent_required": role == "independent-reviewer",
        "project_memory": memory_snapshot,
        "task_context": task_snapshot,
    }


def prepare(root: Path, request: dict, actor: str) -> dict:
    with serialized(root):
        payload = validate_request(root, request)
        if any(item.get("status") in ACTIVE for item in replay(read_log(root)).values()
               if item.get("task_id") == payload.get("task_id")):
            raise WorkflowError("同一任务已有未结束分派；回传后由当前主控回读并执行 complete，"
                                "新主控先执行 takeover；确需放弃时明确 cancel，不重复占用")
        dispatch_id = "dispatch-" + uuid.uuid4().hex
        return append(root, {"event": "prepared", "dispatch_id": dispatch_id,
                             "status": "prepared", "actor": actor,
                             "controller_instance": actor, **payload})


def current(root: Path, dispatch_id: str) -> dict:
    item = replay(read_log(root)).get(dispatch_id)
    if not item:
        raise WorkflowError("找不到分派记录")
    return item


def assign(root: Path, dispatch_id: str, instance: str, evidence: object, actor: str,
           host_instance: str) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") != "prepared":
            raise WorkflowError("只有 prepared 分派可以绑定实际实例")
        if (not isinstance(instance, str) or not instance.strip()
                or not isinstance(host_instance, str) or not host_instance.strip()):
            raise WorkflowError("必须提供真实host实例和执行实例标识")
        if item.get("independent_required") and instance in set(item.get("excluded_instances", [])):
            raise WorkflowError("独立检核实例不能与作者或设计诊断实例相同")
        evidence_ref = file_ref(root, evidence, "调用证据")
        verify_context(root, item)
        return append(root, {"event": "assigned", "dispatch_id": dispatch_id,
                             "status": "assigned", "actor": actor,
                             "instance": instance, "host_instance": host_instance,
                             "invocation_evidence": evidence_ref})


def return_candidate(root: Path, dispatch_id: str, candidate: object, actor: str) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") != "assigned":
            raise WorkflowError("只有已绑定实际实例的分派可以回传")
        verify_context(root, item)
        values = candidate if isinstance(candidate, list) else [candidate]
        candidate_refs = [file_ref(root, value, "回传候选") for value in values]
        return append(root, {"event": "returned", "dispatch_id": dispatch_id,
                             "status": "returned_for_readback", "actor": actor,
                             "instance": item["instance"], "host_instance": item["host_instance"],
                             "candidates": candidate_refs, "readback_verified": False,
                             "review_status": "not_registered", "passed": False})


def controller_of(root: Path, item: dict) -> str | None:
    return item.get("controller_instance") or next((record.get("actor")
        for record in read_log(root) if record.get("dispatch_id") == item["dispatch_id"]
        and record.get("event") == "prepared"), None)


def takeover(root: Path, dispatch_id: str, evidence: object, reason: str, actor: str) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        controller = controller_of(root, item)
        if item.get("status") not in ACTIVE:
            raise WorkflowError("只有未结束分派可以登记主控接手")
        if (not isinstance(actor, str) or not actor.strip() or actor == "未登记实例"
                or actor in (controller, item.get("instance"))):
            raise WorkflowError("须由不同的新主控接手，不能由候选执行者接手")
        if not isinstance(reason, str) or not reason.strip():
            raise WorkflowError("主控接手须保留原因")
        verify_context(root, item)
        proof = text_ref(root, evidence)
        return append(root, {"event": "controller_taken_over", "dispatch_id": dispatch_id,
            "status": item["status"], "actor": actor, "reason": reason,
            "original_controller_instance": item.get("original_controller_instance", controller),
            "previous_controller_instance": controller, "controller_instance": actor,
            "takeover_refs": [*item.get("takeover_refs", []), proof]})


def complete(root: Path, dispatch_id: str, evidence: object, actor: str) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") != "returned_for_readback":
            raise WorkflowError("只有已回传候选的分派可以登记主控回读")
        controller = controller_of(root, item)
        if not actor or actor == "未登记实例" or actor != controller or actor == item.get("instance"):
            raise WorkflowError("必须由当前登记主控回读；新主控先登记接手，候选作者不能代替")
        verify_context(root, item)
        proof = text_ref(root, evidence)
        return append(root, {"event": "completed", "dispatch_id": dispatch_id,
                             "status": "completed", "actor": actor,
                             "readback_evidence": proof, "readback_verified": True,
                             "review_status": "not_registered", "passed": False})


def cancel(root: Path, dispatch_id: str, reason: str, actor: str) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") not in ACTIVE:
            raise WorkflowError("当前分派不能取消")
        if not isinstance(reason, str) or not reason.strip():
            raise WorkflowError("取消必须保留原因")
        return append(root, {"event": "cancelled", "dispatch_id": dispatch_id,
                             "status": "cancelled", "actor": actor, "reason": reason})


def inspect(root: Path) -> dict:
    with serialized(root):
        records = read_log(root)
        latest_records = replay(records)
        return {"schema_version": 1, "records": len(records),
                "dispatches": list(latest_records.values()),
                "active": [item for item in latest_records.values() if item.get("status") in ACTIVE]}


def main() -> int:
    parser = argparse.ArgumentParser(description="策略工作 SubAgent 显式分派记录")
    parser.add_argument("action", choices=("prepare", "assign", "return", "takeover", "complete", "cancel", "inspect"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--dispatch-id")
    parser.add_argument("--instance")
    parser.add_argument("--host-instance")
    parser.add_argument("--evidence")
    parser.add_argument("--candidate", action="append")
    parser.add_argument("--reason")
    parser.add_argument("--actor", default="未登记实例")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    if args.action == "prepare":
        if not args.request:
            raise WorkflowError("prepare 缺少 --request")
        result = prepare(root, json.loads(args.request.read_text(encoding="utf-8")), args.actor)
    elif args.action == "assign":
        result = assign(root, args.dispatch_id, args.instance, args.evidence, args.actor,
                        args.host_instance)
    elif args.action == "return":
        result = return_candidate(root, args.dispatch_id, args.candidate, args.actor)
    elif args.action == "complete":
        result = complete(root, args.dispatch_id, args.evidence, args.actor)
    elif args.action == "takeover":
        result = takeover(root, args.dispatch_id, args.evidence, args.reason, args.actor)
    elif args.action == "cancel":
        result = cancel(root, args.dispatch_id, args.reason, args.actor)
    else:
        result = inspect(root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
