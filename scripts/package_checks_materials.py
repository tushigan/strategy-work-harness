"""Fail-closed material declarations and template/synthetic state checks."""
from pathlib import PurePosixPath
import re
import struct
import zipfile
import zlib

from package_checks import GUIDE, MANIFEST, MATERIALS, MAX_BYTES, digest, issue, payload, path_problem
from package_checks_content import json_value, scan_literal
from package_checks_images import inspect_image
from package_checks_office import check_archive_references, inspect_office
from runtime_structure import MANUALS

ROOT_DOCS = {"README.md", "AGENTS.md", GUIDE, "requirements.txt", ".gitignore",
             "\u5916\u90e8\u8fde\u63a5\u8bf4\u660e.md",
             "\u8fd0\u884c\u73af\u5883\u4e0e\u547d\u4ee4.md",
             *MANUALS}
PROJECT_DOCS = {f"project/{x}README.md" for x in (
    "", "inputs/", "tasks/", "briefs/", "research/", "outputs/", "exports/", "records/", "reviews/")}
PROJECT_DOCS.add("project/records/closure-checklist.md")
EMPTY_STATUS = {"not_initialized", "not_started", "not_applicable", "not_requested",
                "not_configured", "project_setup"}
BASELINE = ".agents/skills/project-import-template/assets/openclaw-gantt-import-template.xlsx"
BASELINE_SHA = "ae3c8d041ff8aa9a2fcc48a7ceb32d2c0b7e54950b4a307a132b7e19f5a0d59d"
VENDORED_SKILL_PREFIX = ".agents/skills/huashu-design/"
SKILL_REFERENCE_DOCS = {
    ".agents/skills/html-deck/references/page-plan.md",
    ".agents/skills/research-evidence/references/调研情境验收.md",
    ".agents/skills/research-evidence/references/资料核验.md",
}


def empty_record(value, key=""):
    if value in (None, [], {}):
        return True
    if key == "schema_version":
        return isinstance(value, (str, int))
    if isinstance(value, dict):
        return all(empty_record(child, field) for field, child in value.items())
    return isinstance(value, str) and value in EMPTY_STATUS


def infrastructure(name):
    if name.startswith(VENDORED_SKILL_PREFIX):
        return True
    path = PurePosixPath(name)
    if name in ROOT_DOCS or name in PROJECT_DOCS or name in SKILL_REFERENCE_DOCS:
        return True
    if path.suffix in {".py", ".js", ".mjs", ".cjs"} and path.parts[0] == "scripts":
        return True
    if path.parts[0] == "templates" and path.suffix in {".html", ".css", ".js"}:
        return True
    if name.startswith(".codex/") and path.suffix in {".toml", ".json", ".sh", ".md"}:
        return True
    return name.startswith(".agents/skills/") and (
        path.name in {"SKILL.md", "README.md", "\u65b9\u6cd5\u6765\u6e90.json"})


def check_state(state, mode, result):
    if not isinstance(state, dict):
        issue(result["errors"], "invalid_state", "project/state.json")
        return
    if mode == "synthetic":
        if state.get("test_mode") is not True:
            issue(result["errors"], "synthetic_test_mode", "project/state.json")
        if state.get("status") not in {"not_initialized", "active", "paused", "closed"}:
            issue(result["errors"], "invalid_state", "project/state.json")
        return
    permitted = {"schema_version", "next_action", "last_updated", "integrations", "test_mode"}
    invalid = state.get("status") != "not_initialized" or state.get("test_mode", False) is not False
    for key, value in state.items():
        if key not in permitted and not empty_record(value, key):
            invalid = True
    integrations = state.get("integrations", {})
    if not isinstance(integrations, dict):
        invalid = True
    else:
        for value in integrations.values():
            if not isinstance(value, dict) or value.get("status") != "not_configured":
                invalid = True
            elif any(v not in (None, "", [], {}) for k, v in value.items() if k not in {"status", "scope"}):
                invalid = True
    if invalid:
        issue(result["errors"], "template_business_data", "project/state.json")


def declarations(contents, mode, result):
    if mode != "synthetic":
        return {}
    try:
        data = json_value(contents.get(MATERIALS, b"").decode("utf-8"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or data.get("classification") != "synthetic"
                or data.get("contains_real_client_data") is not False
                or not isinstance(data.get("statement"), str) or not data["statement"].strip()
                or not isinstance(data.get("materials"), list)):
            raise ValueError("Explicit synthetic declaration required")
        found = {}
        for entry in data["materials"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                    or path_problem(entry["path"]) or entry["path"] in found
                    or entry["path"] == MATERIALS or entry.get("classification") != "synthetic"
                    or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))):
                raise ValueError("Invalid material entry")
            found[entry["path"]] = entry
        return found
    except (ValueError, UnicodeError, RecursionError):
        issue(result["errors"], "material_declaration", MATERIALS)
        return {}


def binary_kind(name, data):
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff"):
        return "image"
    if suffix == ".pdf" and data.startswith(b"%PDF-"):
        return "pdf"
    if suffix in {".xlsx", ".pptx"} and data.startswith(b"PK\x03\x04"):
        return "office"
    if suffix in {".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".flac", ".webm"}:
        if data.startswith((b"RIFF", b"ID3", b"OggS", b"fLaC", b"\x1a\x45\xdf\xa3")) or data[4:8] == b"ftyp":
            return "media"
    return None


def inspect_binary(name, data, entry, result):
    kind = binary_kind(name, data)
    if not kind:
        issue(result["errors"], "unknown_binary", name)
        return
    if not (entry and entry.get("reviewed") is True and isinstance(entry.get("review_note"), str)
            and entry["review_note"].strip()):
        issue(result["errors"], "binary_review_required", name)
    try:
        inspect_payload(name, data, name, result, [MAX_BYTES])
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, struct.error, zlib.error):
        issue(result["errors"], "unreadable_binary", name)
    issue(result["warnings"], "binary_manual_review_not_verified", name)


def inspect_payload(filename, data, name, result, budget, depth=0):
    if depth > 8:
        raise ValueError("Nested archive exceeds audit limit")
    kind = binary_kind(filename, data)
    if kind == "image":
        inspect_image(data, name, result["errors"])
    elif kind == "office":
        inspect_office(data, filename, name, result, lambda member, child: inspect_payload(
            member, child, name, result, budget, depth + 1), budget)
    elif kind in {"pdf", "media"}:
        # Keep the existing conservative fallback for formats not changed here.
        scan_literal(data.decode("latin-1"), name, result["errors"], assignments=False)
    else:
        issue(result["errors"], "unknown_binary", name)


def check_materials(contents, texts, parsed, result, declaration):
    mode = result["mode"]
    state_values = parsed.get("project/state.json") or [(None, None)]
    state = state_values[0][1]
    check_state(state, mode, result)
    materials = declarations(contents, mode, result)
    assets = {}
    for entry in declaration["bundled_assets"]:
        if (not isinstance(entry, dict) or entry.get("path") != BASELINE
                or entry.get("sha256") != BASELINE_SHA
                or entry.get("classification") != "reference_template"
                or entry.get("reviewed") is not True or not entry.get("review_note")):
            issue(result["errors"], "asset_declaration", GUIDE)
        else:
            assets[entry["path"]] = entry
    for name, entry in {**assets, **materials}.items():
        if name not in contents or digest(contents[name]) != entry["sha256"]:
            issue(result["errors"], "material_fingerprint", name)
    for item in payload(result["inventory"]):
        name = item["path"]
        if item["kind"] == "directory":
            continue
        if name == MATERIALS and mode == "synthetic":
            continue
        values = parsed.get(name, [])
        empty = name.startswith("project/records/") and name.endswith((".json", ".jsonl")) and (
            name in parsed and all(empty_record(value) for _, value in values))
        business = not infrastructure(name) and not empty and name not in assets
        if name == "project/state.json":
            business = mode == "synthetic"
        if business:
            if mode == "template":
                issue(result["errors"], "template_business_data", name)
            elif name not in materials:
                issue(result["errors"], "undeclared_material", name)
        data = contents.get(name)
        # 随包的设计 Skill 含有完整演示素材；它们是工具资产，不是客户业务资料。
        if data is not None and name not in texts and not name.startswith(VENDORED_SKILL_PREFIX):
            if name in assets and digest(data) == BASELINE_SHA:
                continue
            inspect_binary(name, data, materials.get(name), result)
        for _, value in values:
            if isinstance(value, dict) and value.get("status") in {
                    "returned", "failed", "insufficient_evidence", "passed_with_yellow"}:
                issue(result["warnings"], "business_history_not_cleared", name)
    if mode == "synthetic":
        issue(result["warnings"], "synthetic_declaration_not_independently_verified", MATERIALS)
    check_archive_references(contents, result)


def check_manifest(contents, result):
    if MANIFEST not in contents:
        return
    try:
        value = json_value(contents[MANIFEST].decode("utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("mode") != result["mode"]
                or value.get("inventory") != payload(result["inventory"])
                or not isinstance(value.get("excluded_from_source"), list)):
            raise ValueError("Manifest mismatch")
    except (ValueError, UnicodeError, RecursionError):
        issue(result["errors"], "inventory_mismatch", MANIFEST)
