"""Check package-local references; external capabilities are not bundled files."""
from pathlib import PurePosixPath
import posixpath
import re

from package_checks import issue, normalized, path_problem

PATH_KEY = re.compile(r"(?:^|_)(?:path|paths|file|filename|directory|dir)$", re.I)
COLLECTION_KEY = re.compile(r"(?:^|_)(?:files|snapshots?)$", re.I)
FILES_KEY = re.compile(r"(?:^|_)files$", re.I)
PREDICATE_KEY = re.compile(r"(?:^|_)(?:is|has|only|all|valid|exists|within|unchanged)(?:_|$)", re.I)
FILE_SHAPE = re.compile(r"[/\\]|^~|\.[A-Za-z0-9]{1,12}(?:[#?].*)?$")
ROOT_PATH = re.compile(r"^(?:project|scripts|templates|\.codex|\.agents)/")
URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
COMMAND_PATH = re.compile(r"""(?:\$\{?PWD\}?/)?((?:scripts|\.codex)/[^ "';|<>]+)""")


def reference(name, value, result, entries, *, required=True, base="", line=None,
              source_relative=False):
    if not isinstance(value, str) or not value:
        issue(result["errors"], "invalid_reference", name, line)
        return
    value = normalized(value)
    if value.startswith(("https://", "http://")):
        # URL content is scanned separately; never call the remote service here.
        issue(result["warnings"], "external_reference_not_bundled", name, line)
        result["references"].append({"source": name, "kind": "external", "required": required})
        return
    candidate = posixpath.normpath(posixpath.join(base, value)) if base else value
    problem = path_problem(candidate)
    if value.startswith(("/", "~", "file:", "\\")) or re.match(r"^[A-Za-z]:", value):
        problem = "absolute_path"
    if problem or URL.match(value):
        issue(result["errors"], problem or "escaping_reference", name, line)
        return
    path_value = value.split("#", 1)[0]
    if source_relative and not base and not ROOT_PATH.match(value) and path_value not in entries:
        sibling = posixpath.normpath(posixpath.join(str(PurePosixPath(name).parent), path_value))
        if sibling in entries:
            base = str(PurePosixPath(name).parent)
    target = posixpath.normpath(posixpath.join(base, value.split("#", 1)[0]))
    if path_problem(target):
        issue(result["errors"], "escaping_reference", name, line)
        return
    item = entries.get(target)
    result["references"].append({"source": name, "path": target, "required": required,
                                 "kind": "local"})
    if item is None or item["excluded"]:
        issue(result["errors"] if required else result["warnings"], "missing_reference", name, line)
    return target


def inspect_values(name, data, result, entries, line=None):
    def walk(value, key="", required=True):
        if isinstance(value, dict):
            required = value.get("required", required) is not False
            archive_record = (isinstance(value.get("path"), str)
                              and PurePosixPath(value["path"]).suffix.lower() in {".xlsx", ".pptx"}
                              and "package_parts" in value)
            if archive_record:
                archive = reference(name, value["path"], result, entries, required=required,
                                    line=line, source_relative=True)
                parts = value["package_parts"]
                if not isinstance(parts, list):
                    issue(result["errors"], "invalid_reference", name, line)
                    parts = []
                for part in parts:
                    member = part.get("path") if isinstance(part, dict) else part
                    part_required = part.get("required", required) is not False if isinstance(
                        part, dict) else required
                    if not isinstance(member, str) or path_problem(member):
                        issue(result["errors"], path_problem(member) if isinstance(member, str)
                              else "invalid_reference", name, line)
                    elif archive:
                        result["references"].append({"source": name, "kind": "archive_member",
                                                     "archive": archive, "path": normalized(member),
                                                     "required": part_required})
                    if isinstance(part, dict):
                        for field, child in part.items():
                            if field != "path":
                                walk(child, field, part_required)
            fingerprint_map = FILES_KEY.search(key) and bool(value) and all(
                isinstance(child, str) and re.fullmatch(r"[0-9a-f]{64}", child)
                for child in value.values())
            for child_key, child in value.items():
                if archive_record and child_key in {"path", "package_parts"}:
                    continue
                if ROOT_PATH.match(child_key) or (
                        COLLECTION_KEY.search(key) and (FILE_SHAPE.search(child_key) or
                                                        fingerprint_map)):
                    reference(name, child_key, result, entries, required=required, line=line,
                              source_relative=True)
                walk(child, child_key, required)
        elif isinstance(value, list):
            # Brand-house field paths are JSON components, not lists of files.
            if key == "path" and value and all(
                    type(part) is int and part >= 0 or isinstance(part, str)
                    and re.fullmatch(r"[\w-]+", part) for part in value):
                return
            for child in value:
                if FILES_KEY.search(key) and not isinstance(child, (str, dict)):
                    issue(result["errors"], "invalid_reference", name, line)
                walk(child, key, required)
        elif isinstance(value, str):
            if name == ".agents/skills/\u65b9\u6cd5\u6765\u6e90.json" and key == "file" and value == "SKILL.md":
                return
            if key == "location" and " / " in value and ROOT_PATH.match(value):
                value = value.split(" / ", 1)[0]
            if (PATH_KEY.search(key) or FILES_KEY.search(key)
                    or COLLECTION_KEY.search(key) and FILE_SHAPE.search(value)
                    or ROOT_PATH.match(value) or value.startswith(("../", "file://"))):
                base = ".codex" if name == ".codex/config.toml" and key == "config_file" else ""
                reference(name, value, result, entries, required=required, base=base, line=line,
                          source_relative=True)
            elif key in {"command", "shell_command"}:
                for match in COMMAND_PATH.finditer(value):
                    reference(name, match.group(1), result, entries, line=line)
        elif value is not None and PATH_KEY.search(key) and not (
                isinstance(value, bool) and PREDICATE_KEY.search(key)):
            issue(result["errors"], "invalid_reference", name, line)
    walk(data)


def markdown_links(name, text, result, entries):
    for match in re.finditer(r"!?\[[^\]]*\]\(([^)\n]+)\)", text):
        value = match.group(1).strip().strip("<>")
        if value.startswith("#"):
            continue
        base = str(PurePosixPath(name).parent)
        reference(name, value, result, entries, base=base)
