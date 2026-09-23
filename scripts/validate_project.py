#!/usr/bin/env python3
"""Validate the portable strategist workspace without external dependencies."""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any
from workspace_status import inspect_workflows
from runtime_structure import (PROJECT_MEMORY_RUNTIME_FILES, STANDALONE_RUNTIME_FILES,
                               runtime_requirements_for_root, startup_structure_errors)
REQUIRED_FILES = (
    "AGENTS.md",
    ".codex/config.toml",
    ".codex/agents/project-controller.toml",
    ".codex/agents/independent-reviewer.toml",
    ".codex/hooks.json",
    "project/state.json",
    "project/README.md",
    "project/records/README.md",
    ".agents/skills/contract-schedule/SKILL.md",
    ".agents/skills/project-import-template/SKILL.md",
    "scripts/phase2_review.py",
    "scripts/workbook_io.mjs",
    ".agents/skills/task-brief/SKILL.md",
    ".agents/skills/research-evidence/SKILL.md",
    ".agents/skills/brand-house/SKILL.md",
    "scripts/phase3.py",
    "scripts/phase5.py",
    "scripts/proposal_script.py",
    "scripts/html_deck.py",
    "scripts/design_expression.py",
    ".agents/skills/proposal-script/SKILL.md",
    ".agents/skills/html-deck/SKILL.md",
    ".agents/skills/design-expression-review/SKILL.md",
    "templates/proposal-deck.html",
    "scripts/review_gate.py",
    "scripts/impact_scan.py",
    "scripts/control_artifacts.py",
    "scripts/project_closure.py",
    "scripts/package_audit.py",
    "scripts/project_handoff.py",
    "scripts/workspace_lock.py",
    "scripts/agent_dispatch.py",
    "scripts/standalone_store.py",
    "scripts/standalone_tasks.py",
    "scripts/task_context.py",
    "scripts/project_memory_store.py",
    "scripts/project_memory.py",
    "scripts/task_receipts.py",
    "scripts/standalone_review.py",
    "scripts/knowledge_connectors.py",
    ".agents/skills/version-impact/SKILL.md",
    ".agents/skills/knowledge-connectors/SKILL.md",
    "project/records/closure-checklist.md",
)
ALLOWED_PROJECT_STATUS = {"not_initialized", "active", "paused", "closed"}
ALLOWED_STAGE = {
    "project_setup",
    "contract_scope",
    "research",
    "brand_house_draft",
    "internal_proposal",
    "client_confirmation",
    "closure",
}
ABSOLUTE_PATH = re.compile(r"^(?:/|~[/\\]|[A-Za-z]:[/\\]|\\\\)")
SENSITIVE_NAME = re.compile(
    r"^(?:\.env(?:\..*)?|id_[^/]+|.*(?:credential|secret|token|password|private[-_]?key).*|.*\.(?:pem|key|p12|pfx|crt))$",
    re.IGNORECASE,
)
SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|private[_-]?key|authorization)",
    re.IGNORECASE,
)
ALLOWED_FIELD_STATUS = {
    "task_status": {"not_started", "in_progress", "blocked", "completed", "cancelled"},
    "artifact_status": {"not_started", "draft", "ready_for_review", "approved", "stale", "blocked", "archived"},
    "machine_review": {"not_started", "in_progress", "passed", "passed_with_yellow", "returned", "insufficient_evidence"},
    "human_confirmation": {"not_requested", "pending", "confirmed", "rejected", "superseded"},
    "client_confirmation": {"not_requested", "pending", "confirmed", "rejected", "superseded"},
    "system_import": {"not_applicable", "not_uploaded", "pending_manual_upload", "succeeded", "failed", "unknown"},
}
def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)
def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def walk_paths(value: Any, key: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            found.extend(walk_paths(child_value, child_key))
    elif isinstance(value, list):
        for child in value:
            found.extend(walk_paths(child, key))
    elif isinstance(value, str) and (
        key in {"path", "relative_path", "file"} or key.endswith("_path")
    ):
        found.append((key, value))
    return found


def walk_sensitive_keys(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else key
            if SENSITIVE_KEY.search(key) and child not in (None, "", [], {}):
                found.append(child_path)
            found.extend(walk_sensitive_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(walk_sensitive_keys(child, f"{prefix}[{index}]"))
    return found


def path_is_inside(root: Path, value: str) -> bool:
    # 工作包必须可复制；业务资料引用统一使用相对路径，不能依赖当前电脑的绝对路径。
    if ABSOLUTE_PATH.match(value):
        return False
    candidate = root / value
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        return False
    return True


def inspect_structured_data(
    root: Path,
    source: str,
    data: Any,
    errors: list[str],
    warnings: list[str],
    required_paths: set[str] | None = None,
) -> None:
    required_paths = required_paths or set()
    for key, value in walk_paths(data):
        if ABSOLUTE_PATH.match(value):
            errors.append(f"{source} 中路径必须是工作包内相对路径 ({key}): {value}")
            continue
        if not path_is_inside(root, value):
            errors.append(f"{source} 中路径越界 ({key}): {value}")
            continue
        target = (root / value).resolve(strict=False)
        if not target.exists():
            message = f"{source} 引用目标不存在: {value}"
            (errors if value in required_paths else warnings).append(message)
    for key_path in walk_sensitive_keys(data):
        errors.append(f"{source} 中疑似凭据字段: {key_path}")


def check(root: Path) -> tuple[list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    capabilities: dict[str, Any] = {}

    runtime_required = set(runtime_requirements_for_root(root))
    optional_generation_files = set(STANDALONE_RUNTIME_FILES + PROJECT_MEMORY_RUNTIME_FILES)
    required_files = [relative for relative in REQUIRED_FILES
                      if relative not in optional_generation_files or relative in runtime_required]
    for relative in required_files:
        if not (root / relative).is_file():
            errors.append(f"缺少必需文件: {relative}")

    errors.extend(startup_structure_errors(root))

    state_path = root / "project/state.json"
    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            raw_state = load_json(state_path)
            if not isinstance(raw_state, dict):
                errors.append("project/state.json 顶层必须是对象")
            else:
                state = raw_state
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"project/state.json 无法读取: {exc}")

    if state:
        if state.get("status") not in ALLOWED_PROJECT_STATUS:
            errors.append(f"project/state.json 的 status 无效: {state.get('status')!r}")
        if state.get("current_stage") not in ALLOWED_STAGE:
            errors.append(
                f"project/state.json 的 current_stage 无效: {state.get('current_stage')!r}"
            )
        for field, allowed_statuses in ALLOWED_FIELD_STATUS.items():
            value = state.get(field)
            if not isinstance(value, dict) or "status" not in value:
                errors.append(f"state 缺少独立记录字段: {field}")
            elif value["status"] not in allowed_statuses:
                errors.append(f"{field} 的 status 无效: {value['status']!r}")

        required_paths = {
            item.get("path")
            for item in state.get("references", [])
            if isinstance(item, dict) and item.get("required") is True and item.get("path")
        }
        inspect_structured_data(root, "project/state.json", state, errors, warnings, required_paths)

    project_dir = root / "project"
    if project_dir.is_dir():
        for path in project_dir.rglob("*.json"):
            if path == state_path or not path.is_file():
                continue
            try:
                inspect_structured_data(
                    root,
                    str(path.relative_to(root)),
                    load_json(path),
                    errors,
                    warnings,
                )
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"记录文件无法读取 {path.relative_to(root)}: {exc}")
        for path in project_dir.rglob("*.jsonl"):
            if not path.is_file():
                continue
            try:
                with path.open(encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            errors.append(f"记录文件 JSONL 无法读取 {path.relative_to(root)}:{line_number}: {exc}")
                            continue
                        inspect_structured_data(
                            root,
                            f"{path.relative_to(root)}:{line_number}",
                            record,
                            errors,
                            warnings,
                        )
            except OSError as exc:
                errors.append(f"记录文件无法读取 {path.relative_to(root)}: {exc}")

    integrations = state.get("integrations", {}) if state else {}
    for name in ("feishu", "cloud_brain"):
        item = integrations.get(name, {}) if isinstance(integrations, dict) else {}
        status = item.get("status") if isinstance(item, dict) else None
        if status not in {"not_configured", "available", "unavailable", "needs_auth"}:
            errors.append(f"外部能力 {name} 缺少有效 status")
        capabilities[name] = {
            "status": status or "missing",
            "local_work_allowed": True,
        }
        if status in {"not_configured", "needs_auth", "unavailable"}:
            warnings.append(f"外部能力未就绪: {name}={status}")

    for relative in (".codex/config.toml", "project/state.json", ".codex/hooks.json"):
        path = root / relative
        if not path.is_file():
            continue
        try:
            if path.suffix == ".toml":
                config = load_toml(path)
                for key_path in walk_sensitive_keys(config):
                    errors.append(f"{relative} 中疑似凭据字段: {key_path}")
            else:
                load_json(path)
        except (OSError, ValueError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"配置无法解析 {relative}: {exc}")

    for path in (root / ".codex/agents").glob("*.toml"):
        relative = path.relative_to(root).as_posix()
        if not path.is_file():
            continue
        try:
            config = load_toml(path)
            if not config.get("name") or not config.get("developer_instructions"):
                errors.append(f"Agent 配置缺少 name 或 developer_instructions: {relative}")
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"Agent 配置无法解析 {relative}: {exc}")

    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if SENSITIVE_NAME.match(path.name):
            errors.append(f"疑似敏感文件不得进入工作包: {path.relative_to(root)}")

    return errors, warnings, capabilities


def main() -> int:
    parser = argparse.ArgumentParser(description="策略师 Harness 启动检查")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--hook", action="store_true", help="以宿主 Hook 可消费的 JSON 输出")
    args = parser.parse_args()
    root = args.root.resolve()
    errors, warnings, capabilities = check(root)
    capabilities.update(inspect_workflows(root, warnings))
    status = "fail" if errors else ("warn" if warnings else "pass")
    result = {
        "status": status,
        "root_name": root.name,
        "errors": errors,
        "warnings": warnings,
        "external_capabilities": capabilities,
        "local_work_allowed": not errors,
    }
    if args.json or args.hook:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"策略师 Harness 启动检查: {status}")
        for item in errors:
            print(f"错误: {item}")
        for item in warnings:
            print(f"提示: {item}")
        print("本地工作允许继续: " + ("是" if not errors else "否"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
