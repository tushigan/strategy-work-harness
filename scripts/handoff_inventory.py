"""Inventory portable workspace files and locate unarchived or pending inputs."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path


BOOKKEEPING = {
    "project/records/handoff-manifest.json",
    "project/records/handoff-events.jsonl",
    "project/records/.handoff.lock",
    "project/records/.workspace-write.lock",
}


BOOKKEEPING_PREFIXES = ("project/records/handoffs/",)


PENDING_SUFFIXES = ("-pending.json", ".pending.json")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def is_ignored(root: Path, path: Path) -> bool:
    name = relative(root, path)
    return (
        name in BOOKKEEPING or any(name.startswith(prefix) for prefix in BOOKKEEPING_PREFIXES)
        or path.name == ".DS_Store"
        or "__pycache__" in path.parts
        or path.suffix == ".pyc"
    )


def file_inventory(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: relative(root, item)):
        if is_ignored(root, path) or (not path.is_file() and not path.is_symlink()):
            continue
        name = relative(root, path)
        if path.is_symlink():
            target = os.readlink(path)
            data = target.encode("utf-8")
            entries.append({
                "name": name,
                "kind": "symlink",
                "size": len(data),
                "sha256": sha256_bytes(data),
                "target": target,
            })
            continue
        data = path.read_bytes()
        entries.append({
            "name": name,
            "kind": "file",
            "size": len(data),
            "sha256": sha256_bytes(data),
        })
    return entries


def inventory_digest(entries: list[dict[str, object]]) -> str:
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


def root_extras(root: Path) -> list[str]:
    allowed_files = {
        "AGENTS.md", "README.md", "requirements.txt", ".gitignore",
        "品牌屋HTML运行说明.md", "外部连接说明.md", "工作包移交说明.md",
        "提案与设计检核运行说明.md", "策略与品牌屋运行说明.md",
        "运行环境与命令.md", "项目检核与结项运行说明.md",
        "package-inventory.json",
    }
    allowed_directories = {".agents", ".codex", "project", "scripts", "templates"}
    extras: list[str] = []
    for path in root.iterdir():
        if path.name in allowed_directories or path.name in allowed_files:
            continue
        if path.name in {".DS_Store", "__pycache__"}:
            continue
        extras.append(path.name)
    return sorted(extras)


def pending_files(root: Path) -> list[str]:
    records = root / "project/records"
    if not records.is_dir():
        return []
    found: list[str] = []
    for path in records.rglob("*"):
        if not path.is_file() or is_ignored(root, path):
            continue
        name = path.name.lower()
        if name.endswith(PENDING_SUFFIXES) and path.stat().st_size:
            found.append(relative(root, path))
    return sorted(found)
