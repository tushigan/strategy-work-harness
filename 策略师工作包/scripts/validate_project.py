#!/usr/bin/env python3
"""Validate the portable strategist workspace without external dependencies."""
from __future__ import annotations

from handoff_inventory import business_paths
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
# v1.7.2 F01：工作包自己读写的记录位置（按代码实际读写路径列出）。这里的 .json/.jsonl 读不了仍阻断；
# 其余位置（成果、资料、素材、学习目录等）读不了只告警该产物、每个文件 1 条，凭据仍按文本检查并阻断。
#   project/state.json                 项目状态（多处读写）
#   project/records/                   全部登记记录：standalone-tasks、agent-dispatch、archive、phase*-events/artifacts、delivery-gate、handoffs 等
#   project/tasks/                     计划与合同/排期指针（phase2_store、agent_dispatch）
#   project/reviews/                   检核报告、模板、分派请求、视觉计划（standalone_review、jianhe_zhunbei、shijue_zengliang、phase5）
#   project/briefs/、project/research/ Phase 3 简报、调研计划、证据、分析与资料快照（phase3_store、phase3_sources）
#   project/inputs/登记请求/           登记请求（guidang 受保护）
#   project/outputs/versions/、conflicts/、brand-house/、proposal/、control/  程序登记的版本快照、冲突副本、品牌屋、逐字稿与演示稿数据、总控产出
#   project/assets/index.json          素材库登记（sucai_ku 写入，对外导出时按它核对指纹）；素材文件本身不算记录
RECORD_LOCATIONS = (
    "project/state.json", "project/records", "project/tasks", "project/reviews", "project/briefs", "project/research",
    "project/inputs/登记请求", "project/outputs/versions", "project/outputs/conflicts", "project/outputs/brand-house",
    "project/outputs/proposal", "project/outputs/control", "project/assets/index.json",
)


def is_record_location(root: Path, path: Path) -> bool:
    import unicodedata
    fold = lambda value: unicodedata.normalize("NFC", value).casefold()
    rel = fold(path.relative_to(root).as_posix())
    return any(rel == fold(p) or rel.startswith(fold(p) + "/") for p in RECORD_LOCATIONS)


def artifact_unreadable(root: Path, path: Path, reason: str, errors: list[str], warnings: list[str],
                        capabilities: dict[str, Any]) -> None:
    """成果目录里读不成 JSON 的文件：每个文件只告警 1 条并登记为单个产物问题；按文本做疑似凭据检查，命中仍阻断。"""
    from package_checks_content import ASSIGNMENT, SECRET, placeholder
    rel = str(path.relative_to(root))
    warnings.append(f"仅影响该产物：无法按 JSON 读取 {rel}（{reason}）")
    issues_list = capabilities.setdefault("artifact_path_issues", [])
    if rel not in issues_list:
        issues_list.append(rel)
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:  # 逐行扫描，大文件不整份读进内存
            for line in handle:
                if SECRET.search(line) or any(not placeholder(next((x for x in m.groups()[1:] if x is not None), ""))
                                              for m in ASSIGNMENT.finditer(line)):
                    errors.append(f"{rel} 中疑似凭据（无法按 JSON 读取，按文本检查）")
                    return
    except OSError:
        return


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)
def _line_error(line: str) -> str | None:
    try:
        json.loads(line)
    except ValueError as exc:
        return str(exc)
    return None


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


@__import__('yunxing_rizhi').observed('validate_project')
def check(root: Path, current_only=False) -> tuple[list[str], list[str], dict[str, Any]]:
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
        for path in (p for p in business_paths(root) if p.is_relative_to(project_dir) and p.suffix == ".json"):
            # Dedicated generated fact summaries are process outputs, not a second
            # business registry. Explicit registered file references still verify bytes.
            if path.is_relative_to(root/"project/records/handoff-facts"):
                continue
            if path == state_path or not path.is_file():
                continue
            try:
                registry = path.is_relative_to(root / "project/records")
                try:
                    data = load_json(path)
                except (OSError, ValueError) as exc:
                    if is_record_location(root, path):
                        raise
                    artifact_unreadable(root, path, str(exc), errors, warnings, capabilities)
                    continue
                local_errors = []
                inspect_structured_data(
                    root,
                    str(path.relative_to(root)),
                    data,
                    errors if registry else local_errors,
                    warnings,
                )
                # U10：成果/资料里的单个产物路径问题只标记该产物，不让整个工作区 blocked；凭据与登记记录仍阻断。
                for message in local_errors:
                    if "疑似凭据" in message:
                        errors.append(message)
                    else:
                        warnings.append("仅影响该产物：" + message + "（用 lujing.py relativize 相对化后再登记）")
                        capabilities.setdefault("artifact_path_issues", []).append(str(path.relative_to(root)))
            except (OSError, ValueError) as exc:
                errors.append(f"记录文件无法读取 {path.relative_to(root)}: {exc}")
        from package_checks_content import DEV_PATH
        for path in (p for p in business_paths(root) if p.is_relative_to(project_dir) and p.suffix in {".py", ".cjs", ".mjs", ".js", ".sh"}):
            try:
                if path.is_file() and path.stat().st_size < 2 * 1024 * 1024 and DEV_PATH.search(path.read_text(encoding="utf-8", errors="replace")):
                    warnings.append(f"脚本含本机绝对路径（只告警，构建脚本与中间数据应放成果目录、用相对路径）: {path.relative_to(root)}")
            except OSError:
                pass
        for path in (p for p in business_paths(root) if p.is_relative_to(project_dir) and p.suffix == ".jsonl"):
            if not path.is_file():
                continue
            if not is_record_location(root, path):
                # F01：成果目录里的 .jsonl 先整份试读；有任何一行读不了即整份按“无法按 JSON 读取”告警 1 条（不按行报）。
                try:
                    with path.open(encoding="utf-8") as handle:
                        bad = next((f"第 {n} 行：{exc}" for n, line in enumerate(handle, 1) if line.strip()
                                    for exc in [_line_error(line)] if exc), None)
                except (OSError, ValueError) as exc:
                    bad = str(exc)
                if bad:
                    artifact_unreadable(root, path, bad, errors, warnings, capabilities)
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
                        # Dispatch missing files are classified by current replay below.
                        dispatch_record = path.name == "agent-dispatch.jsonl"
                        path_warnings = [] if dispatch_record else warnings
                        registry = path.is_relative_to(root / "project/records")
                        local_errors = []
                        inspect_structured_data(root, f"{path.relative_to(root)}:{line_number}",
                                                record, errors if registry else local_errors, path_warnings)
                        # U10/K18：成果目录里的 .jsonl 与 .json 同样只标记该产物，凭据仍阻断。
                        for message in local_errors:
                            if "疑似凭据" in message:
                                errors.append(message)
                            else:
                                warnings.append("仅影响该产物：" + message + "（用 lujing.py relativize 相对化后再登记）")
                                issues_list = capabilities.setdefault("artifact_path_issues", [])
                                if str(path.relative_to(root)) not in issues_list:
                                    issues_list.append(str(path.relative_to(root)))

            except (OSError, ValueError) as exc:
                errors.append(f"记录文件无法读取 {path.relative_to(root)}: {exc}")

    try:
        from current_dependencies import dispatch_issues
        dispatch = dispatch_issues(root,current_only=current_only)
        errors.extend(dispatch['current_errors'])
        if dispatch['historical_issues'] and not current_only:
            warnings.extend('纯历史且无当前依赖：'+x for x in dispatch['historical_issues'])
        if dispatch['historical_issues'] or dispatch['historical_unchecked_refs']:
            capabilities['historical_dispatch_issues'] = {'count':len(dispatch['historical_issues']),'unchecked_references':dispatch['historical_unchecked_refs'],
                'details':'agent_dispatch.py inspect / project_handoff.py inspect（历史保留）'}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append('分派历史或当前依赖损坏：'+str(exc))
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
        try:
            from waibu_wendang import latest_probes
            probed = latest_probes(root).get(name)
        except (ImportError, OSError, ValueError):
            probed = None
        if probed:
            capabilities[name].update(status=probed["status"], probed_at=probed["checked_at"], source="实测探针")
            if probed["status"] != "available":
                warnings.append(f"外部能力 {name}：未知（最近一次只读实测：{probed['detail']}）")
        elif status in {"not_configured", "needs_auth", "unavailable"}:
            # 记录值只是初始/旧状态，未经实测不能说“未配置”。
            capabilities[name].update(status="unknown", recorded_status=status, source="未实测")
            warnings.append(f"外部能力 {name}：未知（未实测；用 waibu_wendang.py probe 只读探测）")

    for relative in (".codex/config.toml", "project/state.json", ".codex/hooks.json", ".claude/settings.json"):
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

    for path in business_paths(root):
        if not path.is_file() or ".git" in path.parts:
            continue
        if SENSITIVE_NAME.match(path.name):
            errors.append(f"疑似敏感文件不得进入工作包: {path.relative_to(root)}")

    from check_package_integrity import check as check_integrity
    integrity = check_integrity(root)
    errors.extend(integrity["errors"])
    return errors, warnings, capabilities


def main() -> int:
    parser = argparse.ArgumentParser(description="策略师 Harness 启动检查")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--hook", action="store_true", help="以宿主 Hook 可消费的 JSON 输出")
    parser.add_argument("--structure-only", action="store_true", help="Hook 环境提示；不声称业务已检查")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.structure_only:
        from check_package_integrity import check as integrity
        checked = integrity(root)
        errors, warnings, capabilities = checked["errors"], ["仅检查宿主入口；主控须运行 project_handoff.py resume"], {}
    else:
        errors, warnings, capabilities = check(root)
    if not args.structure_only:
        capabilities.update(inspect_workflows(root, warnings))
    status = "fail" if errors else ("warn" if warnings else "pass")
    result = {
        "status": status,
        "mode": "structure_hint" if args.structure_only else "full",
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
    from yunxing_rizhi import cli
    _business_main = main
    def main():
        return cli(_business_main, __file__)

if __name__ == "__main__":
    sys.exit(main())
