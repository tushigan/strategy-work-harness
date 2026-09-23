#!/usr/bin/env python3
"""Read-only package audit and verified, repeatable release-directory copying."""
import sys

sys.dont_write_bytecode = True

import argparse
import json
from pathlib import Path

from package_checks import (EXCLUSION_RULES, GUIDE, MANIFEST, inventory, issue)
from package_checks_content import redacted, scan_text, scan_values, structured
from package_checks_dependencies import check_dependencies, policy
from package_checks_materials import check_manifest, check_materials
from package_checks_references import inspect_values, markdown_links
from runtime_structure import structure_issues

TEXT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".jsx", ".json", ".jsonl", ".toml", ".md",
                 ".txt", ".html", ".css", ".sh", ".ps1", ".csv", ".tsv", ".yaml",
                 ".yml", ".xml", ".svg", ".srt", ".vtt", ".ini", ".cfg"}


def audit(root, mode="template"):
    """Return a deterministic report; never import or run the audited workspace."""
    result = {"ok": False, "mode": mode, "errors": [], "warnings": [], "inventory": [],
              "excluded": [], "exclusion_rules": list(EXCLUSION_RULES),
              "external_dependencies": [], "references": []}
    if mode not in {"template", "synthetic"}:
        issue(result["errors"], "invalid_mode")
        return redacted(result)
    try:
        root = Path(root).absolute()
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Root must be a real directory")
        root = root.resolve(strict=True)
    except (OSError, ValueError, TypeError, RuntimeError):
        issue(result["errors"], "invalid_root")
        return redacted(result)
    contents = inventory(root, result)
    texts, parsed = {}, {}
    for name, data in contents.items():
        try:
            text = data.decode("utf-8-sig")
            if "\x00" in text or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
                raise ValueError("Binary control character")
        except (UnicodeError, ValueError):
            continue
        scan_text(name, text, result["errors"])
        if Path(name).suffix.lower() in TEXT_SUFFIXES or name == ".gitignore":
            texts[name] = text
        if Path(name).suffix.lower() in {".json", ".jsonl", ".toml"}:
            parsed[name] = structured(name, text, result["errors"])
    entries = {x["path"]: x for x in result["inventory"]}
    # Aliases are inspected using already inventoried bytes, never by following a link.
    for name, item in entries.items():
        if item["kind"] != "symlink":
            continue
        try:
            target = (root / name).resolve(strict=True).relative_to(root).as_posix()
            if target in contents and (Path(name).suffix in TEXT_SUFFIXES
                                       or Path(name).name in {".gitignore", "LICENSE"}):
                text = contents[target].decode("utf-8-sig")
                texts[name] = text
                scan_text(name, text, result["errors"])
                if Path(name).suffix in {".json", ".jsonl", ".toml"}:
                    parsed[name] = structured(name, text, result["errors"])
        except (OSError, ValueError, RuntimeError):
            issue(result["errors"], "unsafe_symlink", name)
    for name, values in parsed.items():
        for line, data in values:
            scan_values(data, name, result["errors"], line)
            if name != MANIFEST:
                inspect_values(name, data, result, entries, line)
    for name, text in texts.items():
        if name.endswith(".md") and name != GUIDE:
            markdown_links(name, text, result, entries)
    result["errors"].extend(structure_issues(entries, texts))
    declaration = policy(texts, result)
    # The fenced declaration is structured data too, including its asset paths.
    scan_values(declaration, GUIDE, result["errors"])
    inspect_values(GUIDE, declaration, result, entries)
    check_dependencies(texts, entries, result, declaration)
    check_materials(contents, texts, parsed, result, declaration)
    check_manifest(contents, result)
    result["ok"] = not result["errors"]
    return redacted(result)


def prepare_copy(root, target, mode="template"):
    from package_audit_copy import prepare
    return prepare(root, target, mode, audit)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only package audit and verified copy")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("audit", "prepare-copy"):
        sub = commands.add_parser(command)
        sub.add_argument("--root", type=Path, required=True)
        sub.add_argument("--mode", choices=("template", "synthetic"), default="template")
        sub.add_argument("--json", action="store_true", help="Emit JSON (also the default)")
        if command == "prepare-copy":
            sub.add_argument("--target", "--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    result = audit(args.root, args.mode) if args.command == "audit" else prepare_copy(
        args.root, args.target, args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
