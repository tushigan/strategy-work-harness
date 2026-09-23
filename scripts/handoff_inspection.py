"""Assemble the read-only recovery report and preserve all existing checks."""
from __future__ import annotations
import json
from pathlib import Path

from phase2_store import local
from handoff_history import history_errors, storage_errors
from handoff_package import inspect_package
from handoff_inventory import file_inventory, inventory_digest, pending_files, relative, root_extras
from handoff_records import now, pending_markers, read_json, read_manifest, recent_records


ATTENTION_STATUSES = {
    "pending", "in_progress", "blocked", "draft", "ready_for_review",
    "returned", "insufficient_evidence", "unknown", "needs_auth",
}


STATE_FIELDS = (
    "task_status", "artifact_status", "machine_review", "human_confirmation",
    "client_confirmation", "system_import",
)


def state_attention(state: dict[str, object]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for field in STATE_FIELDS:
        value = state.get(field)
        if not isinstance(value, dict):
            continue
        status = value.get("status")
        if status in ATTENTION_STATUSES:
            result.append({"field": field, "status": str(status)})
    integrations = state.get("integrations")
    if isinstance(integrations, dict):
        for name, value in integrations.items():
            if isinstance(value, dict) and value.get("status") in ATTENTION_STATUSES:
                result.append({"field": f"integrations.{name}", "status": str(value["status"])})
    return result


def role_definitions(root: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    directory = root / ".codex/agents"
    if not directory.is_dir():
        return result
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.11 is required by the package.
        return result
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            result.append({
                "name": data.get("name", path.stem),
                "file": relative(root, path),
                "explicit_fresh_agent_required": True,
                "automatic_spawn": False,
                "valid": bool(data.get("developer_instructions")),
            })
        except (OSError, ValueError, TypeError):
            result.append({
                "name": path.stem,
                "file": relative(root, path),
                "explicit_fresh_agent_required": True,
                "automatic_spawn": False,
                "valid": False,
            })
    return result


def external_dependencies(state: dict[str, object]) -> list[dict[str, object]]:
    integrations = state.get("integrations", {})
    result: list[dict[str, object]] = []
    if isinstance(integrations, dict):
        for name, value in sorted(integrations.items()):
            if isinstance(value, dict):
                result.append({
                    "name": name,
                    "status": value.get("status", "missing"),
                    "scope": value.get("scope"),
                    "last_checked": value.get("last_checked"),
                    "requires_receiver_check": True,
                })
    return result


def workflow_snapshot(root: Path, warnings: list[str]) -> dict[str, object]:
    try:
        from workspace_status import inspect_workflows
        return inspect_workflows(root, warnings)
    except (ImportError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        warnings.append(f"统一恢复检查无法读取业务状态: {exc}")
        return {}


def unsafe_storage_report(root: Path) -> dict[str, object] | None:
    errors = storage_errors(root)
    if not errors:
        return None
    return {"schema_version": 1, "checked_at": now(), "workspace": root.name,
            "status": "blocked", "handoff_ready": False, "storage_blocked": True, "errors": errors,
            "warnings": [], "project": {"next_action": "先恢复普通交接目录和文件，保留原记录后重新检查"}}


def inspect_workspace(root: Path) -> dict[str, object]:
    root = root.resolve()
    unsafe = unsafe_storage_report(root)
    if unsafe:
        return unsafe
    errors: list[str] = []
    warnings: list[str] = []
    state_path = root / "project/state.json"
    state: dict[str, object] = {}
    if state_path.is_file():
        try:
            raw = read_json(state_path)
            if isinstance(raw, dict):
                state = raw
            else:
                errors.append("project/state.json 顶层必须是对象")
        except (OSError, json.JSONDecodeError, RecursionError) as exc:
            errors.append(f"project/state.json 无法读取: {exc}")
    else:
        errors.append("缺少 project/state.json")

    try:
        from validate_project import check
        validation_errors, validation_warnings, _ = check(root)
        errors.extend(validation_errors)
        warnings.extend(validation_warnings)
    except (ImportError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        errors.append(f"工作包基础检查失败: {exc}")

    extras = root_extras(root)
    if extras:
        warnings.append("工作区根目录有未归档文件: " + ", ".join(extras))
        errors.append("根目录存在未归档业务文件，先移入 project/ 对应目录: " + ", ".join(extras))

    package = inspect_package(root)
    errors.extend(package["errors"])

    manifest_path = root / "project/records/handoff-manifest.json"
    manifest = None
    if manifest_path.exists() or manifest_path.is_symlink():
        try:
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise ValueError("交接清单必须是普通文件")
            manifest = read_manifest(manifest_path)
            local(root, manifest["manifest_history_path"])
            historical = root / manifest["manifest_history_path"]
            if historical.exists() or historical.is_symlink():
                if historical.is_symlink() or not historical.is_file():
                    raise ValueError("交接清单历史版本必须是普通文件")
                if historical.read_bytes() != manifest_path.read_bytes():
                    raise ValueError(f"交接清单历史版本冲突: {relative(root, historical)}")
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            errors.append(f"已有交接清单损坏，先人工恢复或归档: {exc}")
    try:
        errors.extend(history_errors(root, manifest))
    except (OSError, UnicodeError, RecursionError) as exc:
        errors.append(f"交接历史无法读取，先人工核对: {exc}")

    pending = pending_files(root)
    attention = state_attention(state)
    embedded_pending = pending_markers(root)
    if pending:
        warnings.append("存在中断后待恢复的写入记录: " + ", ".join(pending))
        errors.append("先恢复待处理写入，再生成或接收交接清单: " + ", ".join(pending))
    if embedded_pending:
        warnings.append("记录中存在待处理状态，需回读后决定: " + ", ".join(
            f"{item['path']}:{item['field']}" for item in embedded_pending[:8]))

    workflows = workflow_snapshot(root, warnings)
    errors.extend(workflows.get("standalone", {}).get("errors", []))
    errors.extend(workflows.get("project_memory", {}).get("errors", []))
    errors.extend(workflows.get("task_receipts", {}).get("errors", []))
    from workspace_status import project_progress
    project = project_progress(state, workflows)
    inventory = file_inventory(root)
    records = recent_records(root)
    errors.extend(f"记录摘要无法读取，先人工核对: {name}: {value['error']}"
                  for name, value in records.items() if value.get("status") == "unreadable")
    status = "blocked" if errors else ("attention" if warnings or attention else "ready")
    return {
        "schema_version": 1,
        "checked_at": now(),
        "workspace": root.name,
        "status": status,
        "handoff_ready": not errors and not pending,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "project": project,
        "state_attention": attention,
        "pending_transactions": pending,
        "embedded_pending": embedded_pending,
        "root_unarchived_entries": extras,
        "package_metadata": package,
        "external_dependencies": external_dependencies(state),
        "roles": role_definitions(root),
        "subagent_policy": {
            "automatic_spawn": False,
            "requires_explicit_fresh_agent": True,
            "independent_review_required_for_formal_outputs": True,
            "independent_review_required_for_standalone_deliverables": True,
        },
        "workflow_snapshot": workflows,
        "recent_records": records,
        "inventory": {
            "file_count": len(inventory),
            "sha256": inventory_digest(inventory),
        },
    }
