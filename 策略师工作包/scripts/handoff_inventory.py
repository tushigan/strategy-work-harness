"""Inventory portable workspace files and locate unarchived or pending inputs."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

from runtime_structure import LOCAL_ONLY_ROOTS


BOOKKEEPING = {
    "project/records/handoff-manifest.json",
    "project/records/handoff-events.jsonl",
    "project/records/.handoff.lock",
    "project/records/.workspace-write.lock",
}


BOOKKEEPING_PREFIXES = ("project/records/handoffs/", "project/运行日志/")


PENDING_SUFFIXES = ("-pending.json", ".pending.json")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    """Hash in fixed-size chunks and return (digest, size).

    Reading whole files with read_bytes() puts the entire file in memory; a workspace
    holding large acceptance artifacts can exhaust RAM here. The digest is identical
    either way.
    """
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def is_ignored(root: Path, path: Path) -> bool:
    name = relative(root, path)
    return (
        name in BOOKKEEPING or any(name.startswith(prefix) for prefix in BOOKKEEPING_PREFIXES)
        or path.name == ".DS_Store"
        or "__pycache__" in path.parts
        or path.suffix == ".pyc"
        # 版本控制与 gitignored 的本地开发目录不是工作包内容。.git 每次提交都变，
        # 哈希进清单会让 accept 必然拒绝；验收产物目录则可能大到耗尽内存。
        or bool(set(Path(name).parts[:1]) & LOCAL_ONLY_ROOTS)
    )


def business_paths(root):
    root = Path(root)
    for parent, dirs, files in os.walk(root, followlinks=False):
        here = Path(parent)
        dirs[:] = [d for d in dirs if d != "__pycache__"
                   and not (here == root / "project" and d == "运行日志")
                   and not (here == root and d in LOCAL_ONLY_ROOTS)]
        for name in dirs + files:
            yield here / name


def file_inventory(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(business_paths(root), key=lambda item: relative(root, item)):
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
        digest, size = sha256_file(path)
        entries.append({
            "name": name,
            "kind": "file",
            "size": size,
            "sha256": digest,
        })
    return entries


def inventory_digest(entries: list[dict[str, object]]) -> str:
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


def root_extras(root: Path) -> list[str]:
    allowed_files = {
        "AGENTS.md", "CLAUDE.md", "统一包说明.md", "README.md", "requirements.txt", ".gitignore",
        "品牌屋HTML运行说明.md", "外部连接说明.md", "工作包移交说明.md",
        "提案与设计检核运行说明.md", "策略与品牌屋运行说明.md",
        "运行环境与命令.md", "项目检核与结项运行说明.md",
        "工作能力与流程总览.md",
        "package-inventory.json",
    }
    allowed_directories = {".agents", ".codex", ".claude", "project", "scripts", "templates"} | LOCAL_ONLY_ROOTS
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
