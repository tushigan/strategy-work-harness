"""Validate release metadata without freezing mutable project business records."""
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import re

from package_checks import (MANIFEST, RUNTIME_LOCK_FILES, digest, path_problem, read_local,
                            required_package_files_for)
from package_checks_content import json_value


def canonical_name(value):
    return (isinstance(value, str) and value != "." and not path_problem(value)
            and PurePosixPath(value).as_posix() == value
            and not any(ord(character) < 32 for character in value))


def release_entries(value):
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "mode", "inventory", "excluded_from_source"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["mode"] not in ("template", "synthetic")
            or not isinstance(value["inventory"], list) or not value["inventory"]
            or not isinstance(value["excluded_from_source"], list)):
        raise ValueError("发行清单结构、版本或发行类别无效")
    entries = {}
    fields = {"directory": set(), "file": {"size", "mode", "sha256"}, "symlink": {"target", "sha256"}}
    for item in value["inventory"]:
        if not isinstance(item, dict):
            raise ValueError("发行清单条目必须是对象")
        name, kind = item.get("path"), item.get("kind")
        if (not canonical_name(name) or name == MANIFEST or name in entries
                or not isinstance(kind, str) or kind not in fields
                or set(item) != {"path", "kind", "excluded"} | fields[kind]
                or item["excluded"] is not False):
            raise ValueError("发行清单条目重复、路径或字段无效")
        if kind != "directory" and (not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ValueError("发行文件指纹无效")
        if kind == "file" and (type(item["size"]) is not int or item["size"] < 0
                or type(item["mode"]) is not int or not 0 <= item["mode"] <= 0o7777):
            raise ValueError("发行文件大小或权限记录无效")
        if kind == "symlink":
            target = item["target"]
            if (not isinstance(target, str) or not target or "\\" in target
                    or PurePosixPath(target).is_absolute() or PureWindowsPath(target).drive
                    or any(ord(character) < 32 for character in target)
                    or not canonical_name(posixpath.normpath(posixpath.join(
                        PurePosixPath(name).parent.as_posix(), target)))
                    or digest(os.fsencode(target)) != item["sha256"]):
                raise ValueError("发行符号链接目标或指纹无效")
        entries[name] = item
    for name in entries:
        parent = PurePosixPath(name).parent.as_posix()
        if parent != "." and entries.get(parent, {}).get("kind") != "directory":
            raise ValueError("发行清单缺少上级目录记录")
    required_files = required_package_files_for(entries)
    if any(entries.get(name, {}).get("kind") != "file" for name in required_files):
        raise ValueError("发行清单缺少必需文件记录")
    excluded = set()
    for item in value["excluded_from_source"]:
        if not isinstance(item, dict) or set(item) != {"path", "reason"}:
            raise ValueError("发行排除记录无效")
        name = item["path"]
        if not canonical_name(name) or name in excluded or name in entries:
            raise ValueError("发行排除记录路径无效或重复")
        path = PurePosixPath(name)
        valid = (item["reason"] == "runtime_lock" and name in RUNTIME_LOCK_FILES) or (
            item["reason"] == "runtime_cache" and (path.name in {".DS_Store", "__pycache__"}
                or (path.parent.name == "__pycache__" and path.suffix == ".pyc")))
        if not valid:
            raise ValueError("发行排除记录不是允许的运行缓存或锁文件")
        excluded.add(name)
    return entries


def project_data(name):
    return name == "project" or name.startswith("project/")


def same_content(left, right):
    # Copy/extraction tools may change permission bits without changing payloads.
    return all(left.get(key) == right.get(key) for key in ("kind", "size", "sha256", "target"))


def current_release_entries(root):
    entries = {}
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if project_data(name) or name == MANIFEST:
            continue
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError(f"发行符号链接越界: {name}")
            target = os.readlink(path)
            item = {"kind": "symlink", "target": target, "sha256": digest(os.fsencode(target))}
        elif path.is_dir():
            if path.name == "__pycache__":
                continue
            item = {"kind": "directory"}
        elif path.is_file():
            if path.name == ".DS_Store" or (path.parent.name == "__pycache__" and path.suffix == ".pyc"):
                continue
            data = read_local(root, name)
            item = {"kind": "file", "size": len(data), "sha256": digest(data)}
        else:
            raise ValueError(f"发行文件类型不支持: {name}")
        entries[name] = item
    return entries


def inspect_package(root: Path):
    result = {"status": "absent", "errors": []}
    path = root / MANIFEST
    if not path.exists() and not path.is_symlink():
        return result
    try:
        data = read_local(root, MANIFEST)
        value = json_value(data.decode("utf-8"))
        expected = release_entries(value)
        actual = current_release_entries(root)
        release_names = {name for name in expected if not project_data(name)}
        release_changes = [name for name in sorted(release_names | set(actual))
                   if not same_content(expected.get(name, {}), actual.get(name, {}))]
        if release_changes:
            raise ValueError("发行文件缺失、新增或指纹已变化: " + ", ".join(release_changes))
        result.update(status="valid", schema_version=value["schema_version"], mode=value["mode"],
                      release_entry_count=len(expected), mutable_root="project")
    except (OSError, ValueError, UnicodeError, RuntimeError) as exc:
        result.update(status="invalid", errors=[f"{MANIFEST} 核验失败: {exc}"])
    return result
