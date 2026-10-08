"""Read OOXML parts, including VML, and check archive-local relationships."""
import io
from pathlib import PurePosixPath
import posixpath
import struct
import zipfile

from package_checks import MAX_BYTES, issue, normalized, path_problem
from package_checks_content import scan_literal, scan_xml


def inspect_office(data, filename, name, result, inspect_child, budget):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        scan_comment(archive.comment, name, result)
        items = archive.infolist()
        names = {item.filename for item in items}
        if len(names) != len(items):
            raise ValueError("Duplicate archive member")
        size = sum(item.file_size for item in items)
        budget[0] -= size
        if size > MAX_BYTES or budget[0] < 0:
            raise ValueError("Archive exceeds audit limit")
        main = "xl/workbook.xml" if PurePosixPath(filename).suffix.lower() == ".xlsx" else (
            "ppt/presentation.xml")
        if not {"[Content_Types].xml", "_rels/.rels", main}.issubset(names):
            raise ValueError("Missing Office package structure")
        for item in items:
            if path_problem(item.filename) or item.flag_bits & 1:
                raise ValueError("Unsafe archive member")
            scan_literal(item.filename, name, result["errors"])
            scan_comment(item.comment, name, result)
            scan_extra(item.extra, name, result)
            if item.is_dir():
                continue
            child = archive.read(item)
            if item.filename.lower().endswith((".xml", ".rels", ".vml")):
                root = scan_xml(child, name, result["errors"])
                expected = {"[Content_Types].xml": "Types", main: (
                    "workbook" if main.startswith("xl/") else "presentation")}
                if item.filename in expected and root.tag.rsplit("}", 1)[-1] != expected[item.filename]:
                    raise ValueError("Invalid Office package structure")
                if item.filename.endswith(".rels"):
                    relationships(item.filename, root, names, name, result)
            else:
                inspect_child(item.filename, child)


def scan_comment(data, name, result):
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        text = data.decode("cp437")
    scan_literal(text, name, result["errors"])


def scan_extra(data, name, result):
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            raise ValueError("Invalid ZIP extra field")
        kind, size = struct.unpack_from("<HH", data, offset)
        value = data[offset + 4:offset + 4 + size]
        if len(value) != size:
            raise ValueError("Truncated ZIP extra field")
        if kind in {0x7075, 0x6375}:
            if size < 5 or value[0] != 1:
                raise ValueError("Unsupported ZIP Unicode metadata")
            scan_literal(value[5:].decode("utf-8"), name, result["errors"])
        elif kind not in {0x0001, 0x000A, 0x5455, 0x7875}:
            # Other opaque extensions cannot be treated as inspected text.
            raise ValueError("Unsupported ZIP extra field")
        offset += 4 + size


def check_archive_references(contents, result):
    members = {}
    for item in result["references"]:
        if item["kind"] != "archive_member":
            continue
        source, archive = item["source"], item["archive"]
        if archive not in contents:
            issue(result["errors"] if item["required"] else result["warnings"],
                  "missing_reference", source)
            continue
        if archive not in members:
            try:
                with zipfile.ZipFile(io.BytesIO(contents[archive])) as package:
                    members[archive] = set(package.namelist())
            except (OSError, ValueError, zipfile.BadZipFile):
                members[archive] = set()
                issue(result["errors"], "unreadable_binary", archive)
        if item["path"] not in members[archive]:
            issue(result["errors"] if item["required"] else result["warnings"],
                  "missing_reference", source)


def relationships(member, root, members, name, result):
    if root.tag != "{http://schemas.openxmlformats.org/package/2006/relationships}Relationships":
        raise ValueError("Invalid Office relationships")
    folder = str(PurePosixPath(member).parent.parent)
    for relation in root:
        value = relation.get("Target")
        if not isinstance(value, str) or not value:
            raise ValueError("Invalid Office relationship")
        value = normalized(value)
        if relation.get("TargetMode") == "External":
            if value.startswith(("https://", "http://")):
                issue(result["warnings"], "external_reference_not_bundled", name)
            else:
                issue(result["errors"], path_problem(value) or "escaping_reference", name)
            continue
        if value.startswith("#"):
            continue
        value = value.split("#", 1)[0]
        target = posixpath.normpath(value.lstrip("/") if value.startswith("/") else
                                   posixpath.join(folder, value))
        problem = path_problem(target)
        if problem:
            issue(result["errors"], problem, name)
        elif target not in members:
            issue(result["errors"], "missing_reference", name)
