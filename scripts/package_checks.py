"""Filesystem inventory and fixed package policy. Never execute candidate code."""
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from urllib.parse import unquote
from runtime_structure import (PROJECT_MEMORY_RUNTIME_FILES, REQUIRED_RUNTIME_FILES,
                               STANDALONE_RUNTIME_FILES, required_runtime_files_for)

MANIFEST = "package-inventory.json"
MATERIALS = "project/records/package-materials.json"
GUIDE = "\u5de5\u4f5c\u5305\u79fb\u4ea4\u8bf4\u660e.md"
REQUIRED_FILES = (
    "AGENTS.md", "README.md", GUIDE, "requirements.txt", ".codex/config.toml",
    ".codex/hooks.json", ".codex/hooks/validate-project.sh",
    ".codex/agents/project-controller.toml", ".codex/agents/independent-reviewer.toml",
    ".agents/skills/independent-review/SKILL.md",
    ".agents/skills/version-impact/SKILL.md",
    ".agents/skills/knowledge-connectors/SKILL.md",
    "scripts/validate_project.py", "scripts/check_environment.py",
    "scripts/review_gate.py", "scripts/impact_scan.py", "scripts/package_audit.py",
    "scripts/package_checks.py", "scripts/workbook_io.mjs", "scripts/phase3.py",
    "scripts/phase5.py", "scripts/brand_house_history.mjs",
    "templates/proposal-deck.html", "templates/brand-house-model.html",
    "templates/brand-house-shell.html", "templates/brand-house.css",
    "project/state.json", "project/README.md", "project/records/README.md",
    "project/records/revision-counts.json", "project/records/dependencies.json",
    "project/records/closure-checklist.md",
    ".agents/skills/project-import-template/assets/openclaw-gantt-import-template.xlsx",
    *REQUIRED_RUNTIME_FILES,
)


def required_package_files_for(names):
    runtime = set(required_runtime_files_for(names))
    optional_generation_files = set(STANDALONE_RUNTIME_FILES + PROJECT_MEMORY_RUNTIME_FILES)
    return tuple(name for name in REQUIRED_FILES
                 if name not in optional_generation_files or name in runtime)
RUNTIME_LOCK_FILES = {
    "project/records/.workspace-write.lock",
    "project/records/.handoff.lock",
    "project/records/.connector.lock",
}
EXCLUSION_RULES = [
    ".DS_Store",
    "__pycache__/",
    "__pycache__/*.pyc",
    *sorted(RUNTIME_LOCK_FILES),
]
FORBIDDEN = {".venv", "venv", "node_modules", ".git", ".ssh", ".aws", ".azure",
             ".config", ".npmrc", ".pypirc", ".netrc", ".bash_history", ".zsh_history",
             "login data", "cookies", "auth.json"}
SENSITIVE_NAME = re.compile(
    r"^(?:\.env(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|"
    r".*(?:credentials?|secrets?|tokens?|passwords?|private[-_]?key).*|"
    r".*\.(?:pem|key|p12|pfx|keystore))$", re.I)
MAX_BYTES = 64 * 1024 * 1024


def issue(items, code, path="", line=None):
    value = {"code": code, "path": path}
    if line is not None:
        value["line"] = line
    if value not in items:
        items.append(value)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def normalized(value):
    for _ in range(3):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def path_problem(value):
    value = normalized(value)
    if (not value or "\x00" in value or "\\" in value
            or ".." in PurePosixPath(value).parts):
        return "escaping_reference"
    if (value.startswith(("/", "~", "file:", "\\"))
            or PureWindowsPath(value).drive):
        return "absolute_path"
    return None


def read_fd(fd):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
        raise ValueError("Unsupported file or audit size limit exceeded")
    chunks, size = [], 0
    while chunk := os.read(fd, 1024 * 1024):
        size += len(chunk)
        if size > MAX_BYTES:
            raise ValueError("Audit size limit exceeded")
        chunks.append(chunk)
    after = os.fstat(fd)
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("File changed during read")
    return b"".join(chunks), after


def read_local(root, relative):
    """Open each path component without following symlinks, including parents."""
    if path_problem(relative):
        raise ValueError("Non-canonical local name")
    parts = PurePosixPath(relative).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return read_fd(fd)[0]
    finally:
        os.close(fd)


def inventory(root, result):
    contents = {}

    def walk(fd, parent=""):
        try:
            with os.scandir(fd) as entries:
                names = sorted(item.name for item in entries)
        except OSError:
            issue(result["errors"], "unreadable_directory", parent)
            return
        for name in names:
            relative = f"{parent}/{name}" if parent else name
            item = {"path": relative, "kind": "unknown", "excluded": False}
            result["inventory"].append(item)
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if name.lower() in FORBIDDEN or SENSITIVE_NAME.fullmatch(name):
                    issue(result["errors"], "forbidden_entry", relative)
                if "\n" in name or "\r" in name or path_problem(relative):
                    issue(result["errors"], "unsafe_filename", relative)
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(name, dir_fd=fd)
                    item.update(kind="symlink", target=target, sha256=digest(os.fsencode(target)))
                    resolved = (root / relative).resolve(strict=True)
                    if (Path(target).is_absolute() or PureWindowsPath(target).drive
                            or "\\" in target or not resolved.is_relative_to(root)
                            or name in {".DS_Store", "__pycache__"}
                            or "__pycache__" in resolved.parts):
                        issue(result["errors"], "unsafe_symlink", relative)
                elif stat.S_ISDIR(info.st_mode):
                    item.update(kind="directory", excluded=name == "__pycache__")
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        walk(child, relative)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    cache = relative in RUNTIME_LOCK_FILES or name == ".DS_Store" or (
                        PurePosixPath(parent).name == "__pycache__" and name.endswith(".pyc"))
                    reason = "runtime_lock" if relative in RUNTIME_LOCK_FILES else "runtime_cache"
                    item.update(kind="file", size=info.st_size, mode=stat.S_IMODE(info.st_mode),
                                excluded=cache)
                    if "__pycache__" in PurePosixPath(relative).parts and not cache:
                        issue(result["errors"], "cache_payload", relative)
                    if not cache:
                        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                        try:
                            data, checked = read_fd(child)
                        finally:
                            os.close(child)
                        item.update(size=checked.st_size, sha256=digest(data))
                        contents[relative] = data
                else:
                    issue(result["errors"], "unsupported_entry", relative)
            except (OSError, ValueError, RuntimeError):
                issue(result["errors"], "unsafe_symlink" if item["kind"] == "symlink"
                      else "unreadable_entry", relative)
            if item["excluded"]:
                result["excluded"].append({"path": relative, "reason": reason if item["kind"] == "file" else "runtime_cache"})

    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            walk(fd)
        finally:
            os.close(fd)
    except OSError:
        issue(result["errors"], "unreadable_root")
    result["inventory"].sort(key=lambda item: item["path"])
    if result["excluded"]:
        issue(result["warnings"], "runtime_caches_excluded")
    return contents


def payload(items):
    return [item for item in items if not item["excluded"] and item["path"] != MANIFEST]
