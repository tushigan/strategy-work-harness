"""Parse structured records and inspect literals without echoing matched values."""
import ast
import io
import json
from pathlib import PurePosixPath
import re
import tokenize
import tomllib
import xml.etree.ElementTree as ET

from package_checks import issue

SECRET_KEY = re.compile(
    r"^(?:api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|"
    r"client[-_]?secret|password|passwd|secret|token|private[-_]?key|authorization)$", re.I)
SECRET = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,}|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bxox[baprs]-[A-Za-z0-9-]{12,}|"
    r"\bBearer[ \t]+[A-Za-z0-9._~+/-]{12,}|"
    r"https?://[^/\s:@]+:[^/\s@]+@)", re.I)
ASSIGNMENT = re.compile(
    r"""(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"""
    r"""password|passwd|secret|token|authorization)\b["']?\s*(?::|=(?!=|>))\s*"""
    r"""(?:"([^"\r\n]*)"|'([^'\r\n]*)'|([^\s,;#]+))""")
DEV_PATH = re.compile(
    r"""(?<![\w:])(?:/(?:Users|home|Volumes|private|tmp|opt|var|mnt)/[^"'`\s<>]+|"""
    r"""[A-Za-z]:[/\\][^"'`\s<>]+|~[/\\][^"'`\s<>]+|\\\\[\w.-]+\\[^\s]+)""")


def placeholder(value):
    if not isinstance(value, str):
        return value in (None, False, [], {})
    return value.strip().lower() in {"", "null", "none", "false", "redacted", "<redacted>",
                                    "${api_key}", "${token}", "your_api_key"}


def scan_literal(text, name, errors, line=None, assignments=True):
    if SECRET.search(text):
        issue(errors, "credential", name, line)
    if DEV_PATH.search(text):
        issue(errors, "absolute_path", name, line)
    if assignments:
        for match in ASSIGNMENT.finditer(text):
            value = next((part for part in match.groups()[1:] if part is not None), "")
            if not placeholder(value):
                issue(errors, "credential", name, line)


def python_literals(text, name, errors):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        issue(errors, "invalid_source", name)
        scan_literal(text, name, errors)
        return
    regex_modules, regex_functions = {"re"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            regex_modules.update(x.asname or x.name for x in node.names if x.name == "re")
        if isinstance(node, ast.ImportFrom) and node.module == "re":
            regex_functions.update(x.asname or x.name for x in node.names)
    ignored = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        is_regex = (isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
                    and function.value.id in regex_modules) or (
                        isinstance(function, ast.Name) and function.id in regex_functions)
        pattern = node.args[0]
        if is_regex and isinstance(pattern, ast.Constant) and isinstance(pattern.value, str):
            if re.search(r"[\[\]{}\\^$]|[.*+?](?:[+?])?|\(\?", pattern.value):
                ignored.add(id(pattern))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in ignored:
            scan_literal(node.value, name, errors, node.lineno)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Constant) and not placeholder(value.value):
                for target in targets:
                    key = target.id if isinstance(target, ast.Name) else ""
                    if SECRET_KEY.fullmatch(key):
                        issue(errors, "credential", name, node.lineno)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                        and SECRET_KEY.fullmatch(key.value) and isinstance(value, ast.Constant)
                        and not placeholder(value.value)):
                    issue(errors, "credential", name, node.lineno)
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                scan_literal(token.string, name, errors, token.start[0])
    except tokenize.TokenError:
        issue(errors, "invalid_source", name)


def reject_duplicates(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("Duplicate key")
        value[key] = child
    return value


def json_value(text):
    return json.loads(text, object_pairs_hook=reject_duplicates,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite")))


def structured(name, text, errors):
    suffix = PurePosixPath(name).suffix.lower()
    try:
        if suffix == ".toml":
            return [(None, tomllib.loads(text))]
        if suffix == ".json":
            return [(None, json_value(text))]
        if suffix == ".jsonl":
            values = []
            for number, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    try:
                        values.append((number, json_value(line)))
                    except (ValueError, RecursionError):
                        issue(errors, "invalid_structure", name, number)
            return values
    except (ValueError, RecursionError):
        issue(errors, "invalid_structure", name)
    return []


def scan_text(name, text, errors):
    if name.endswith(".py"):
        python_literals(text, name, errors)
    elif PurePosixPath(name).suffix.lower() in {".js", ".mjs", ".cjs", ".html"}:
        # Only actual quoted assignments are secret assignments in JavaScript.
        scan_literal(text, name, errors, assignments=False)
        for match in ASSIGNMENT.finditer(text):
            if match.group(2) is not None or match.group(3) is not None:
                if not placeholder(match.group(2) or match.group(3)):
                    issue(errors, "credential", name, text.count("\n", 0, match.start()) + 1)
    elif PurePosixPath(name).suffix.lower() not in {".json", ".jsonl", ".toml"}:
        scan_literal(text, name, errors)


def scan_values(value, name, errors, line=None):
    if isinstance(value, dict):
        for key, child in value.items():
            scan_literal(key, name, errors, line)
            if SECRET_KEY.fullmatch(key) and not placeholder(child):
                issue(errors, "credential", name, line)
            scan_values(child, name, errors, line)
    elif isinstance(value, list):
        for child in value:
            scan_values(child, name, errors, line)
    elif isinstance(value, str):
        scan_literal(value, name, errors, line)


class MetadataTree(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("Metadata DTD is not supported")


def scan_xml(data, name, errors):
    """Scan decoded XML values, including entity escapes, comments and attributes."""
    parser = ET.XMLParser(target=MetadataTree(insert_comments=True, insert_pis=True))
    try:
        root = ET.fromstring(data, parser=parser)
    except ET.ParseError as exc:
        raise ValueError("Invalid XML metadata") from exc
    for node in root.iter():
        scan_values(node.attrib, name, errors)
        for text in (node.text, node.tail):
            if text:
                scan_literal(text, name, errors)
        tag = node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""
        if SECRET_KEY.fullmatch(tag) and not placeholder(node.text):
            issue(errors, "credential", name)
    return root


def redacted(value):
    if isinstance(value, dict):
        return {redacted(key): "[redacted]" if SECRET_KEY.fullmatch(key) else redacted(child)
                for key, child in value.items()}
    if isinstance(value, list):
        return [redacted(child) for child in value]
    if isinstance(value, str):
        value = SECRET.sub("[redacted]", value)
        value = ASSIGNMENT.sub(lambda match: match.group(1) + "=[redacted]", value)
        return DEV_PATH.sub("[absolute-path]", value)
    return value
