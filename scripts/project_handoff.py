#!/usr/bin/env python3
"""Create a portable, read-only-first project recovery and handoff report."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from phase2_store import local
from workspace_lock import serialized
from handoff_package import inspect_package
from handoff_history import history_errors, storage_errors

# Keep the original import surface while implementations live with their responsibilities.
from handoff_inventory import (
    BOOKKEEPING, BOOKKEEPING_PREFIXES, PENDING_SUFFIXES, file_inventory,
    inventory_digest, is_ignored, pending_files, relative, root_extras, sha256_bytes,
)
from handoff_records import (
    append_event, now, pending_markers, read_json, read_manifest, recent_records,
    write_json_atomic,
)
from handoff_inspection import (
    ATTENTION_STATUSES, STATE_FIELDS, external_dependencies, inspect_workspace,
    role_definitions, state_attention, unsafe_storage_report, workflow_snapshot,
)
from handoff_transfer import (
    _accept_unlocked, _prepare_unlocked, accept, build_manifest, prepare,
)


def print_result(result: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"项目交接检查: {result.get('status', 'unknown')}")
    report = result.get("report", result)
    project = report.get("project", {})
    print(f"项目: {project.get('project_name') or '未初始化'}")
    print(f"阶段: {project.get('current_stage') or '未知'}")
    print(f"下一步: {project.get('next_action') or '未知'}")
    for item in report.get("workflow_snapshot", {}).get("standalone", {}).get("tasks", []):
        if item["status"] != "archived":
            print(f"独立任务 {item['task_id']}：{item['title']} / {item['effective_status']} / {item['next_action']}")
    for item in dict.fromkeys([*report.get("errors", []), *result.get("errors", [])]):
        print(f"阻断: {item}")
    for item in dict.fromkeys([*report.get("warnings", []), *result.get("warnings", [])]):
        print(f"提示: {item}")
    blocked = result.get("status") == "blocked" or report.get("errors")
    print("可生成交接清单: " + ("是" if report.get("handoff_ready") and not blocked else "否"))
    print("本地工作可继续: " + ("否" if blocked else "是"))


def main() -> int:
    parser = argparse.ArgumentParser(description="策略师项目恢复与交接检查")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "prepare", "accept"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--workspace", type=Path, required=True)
        sub.add_argument("--actor", default="未登记实例")
        sub.add_argument("--json", action="store_true")
        if name == "accept":
            sub.add_argument("--manifest-id", required=True)
    args = parser.parse_args()
    root = args.workspace.resolve()
    if args.command == "inspect":
        result = inspect_workspace(root)
        print_result(result, args.json)
        return 1 if result["errors"] else 0
    if args.command == "prepare":
        result = prepare(root, args.actor)
        print_result(result, args.json)
        return 1 if result["status"] == "blocked" else 0
    result = accept(root, args.actor, args.manifest_id)
    print_result(result, args.json)
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
