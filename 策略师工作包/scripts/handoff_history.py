"""Validate local handoff storage and the complete immutable history chain."""
import hashlib
import json
from pathlib import Path
import re

DIRECTORIES = ("project", "project/records", "project/records/handoffs")
RECORD_FILES = ("handoff-manifest.json", "handoff-events.jsonl",
                ".workspace-write.lock", ".handoff.lock")


def storage_errors(root: Path) -> list[str]:
    for name in DIRECTORIES:
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            return [f"交接存储目录必须是工作区内的普通目录，不允许符号链接: {name}"]
    for name in RECORD_FILES:
        path = root / "project/records" / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return [f"交接存储文件必须是普通文件，不允许符号链接: project/records/{name}"]
    directory = root / "project/records/handoffs"
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file():
                return [f"交接清单历史版本必须是普通文件: {path.relative_to(root).as_posix()}"]
    return []


def history_errors(root: Path, manifest: dict | None) -> list[str]:
    """Bind every prepared version, including old ones excluded from business inventory."""
    errors = []
    directory = root / "project/records/handoffs"
    path = root / "project/records/handoff-events.jsonl"
    try:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip()] if path.is_file() else []
        if any(not isinstance(event, dict) for event in events):
            raise ValueError("每条事件必须是对象")
    except (OSError, UnicodeError, ValueError, RecursionError):
        return ["交接事件记录无法读取，先人工核对，不能生成或接收新清单"]
    prepared = [event for event in events if event.get("event") == "handoff_prepared"]
    expected = set()
    previous = None
    for event in prepared:
        identifier, digest = event.get("manifest_id"), event.get("manifest_sha256")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier)
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or event.get("manifest_history_path") != f"project/records/handoffs/{identifier}.json"):
            errors.append("交接准备事件的编号、指纹或历史路径无效")
            continue
        name = f"{identifier}.json"
        if name in expected or event.get("previous_manifest_sha256") != previous:
            errors.append(f"交接历史准备事件重复或版本链断裂: {name}")
        expected.add(name)
        previous = digest
        historical = directory / name
        if not historical.is_file():
            # Only the latest copy is reconstructible from the untouched latest manifest.
            if manifest and identifier == manifest["manifest_id"]:
                continue
            errors.append(f"交接清单历史版本缺失: project/records/handoffs/{name}")
        elif hashlib.sha256(historical.read_bytes()).hexdigest() != digest:
            errors.append(f"交接清单历史版本冲突: project/records/handoffs/{name}")
    actual = {p.name for p in directory.iterdir() if p.name != ".DS_Store"} if directory.is_dir() else set()
    for name in sorted(actual - expected):
        errors.append(f"交接清单历史版本缺少匹配准备事件: project/records/handoffs/{name}")
    if manifest:
        latest = root / "project/records/handoff-manifest.json"
        if (not prepared or prepared[-1].get("manifest_id") != manifest["manifest_id"]
                or prepared[-1].get("manifest_sha256") != hashlib.sha256(latest.read_bytes()).hexdigest()):
            errors.append("最近交接清单与最后一次准备事件不一致，不能覆盖或接收")
    elif prepared or actual:
        errors.append("已有交接历史但缺少最近清单，先人工核对恢复")
    return errors
