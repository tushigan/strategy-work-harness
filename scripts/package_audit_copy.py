"""Stage a copy, re-audit it, read back its manifest, then publish without overwrite."""
import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from package_checks import MANIFEST, digest, issue, payload, read_local
from package_checks_content import json_value


def copy_payload(source, stage, items):
    for item in items:
        relative = item["path"]
        target = stage / relative
        if item["kind"] == "directory":
            target.mkdir(parents=True, exist_ok=True)
        elif item["kind"] == "symlink":
            if os.readlink(source / relative) != item["target"]:
                raise ValueError("Source link changed")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(item["target"])
        elif item["kind"] == "file":
            data = read_local(source, relative)
            if digest(data) != item["sha256"]:
                raise ValueError("Source file changed")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
            target.chmod(item["mode"])
        else:
            raise ValueError("Unsupported inventory entry")


def publish_directory(stage, target):
    """The OS no-replace primitive closes the destination existence-check race."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        status = rename(os.fsencode(stage), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        status = rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1)
    else:
        raise OSError(errno.ENOTSUP, "Atomic no-replace directory publication unavailable")
    if status != 0:
        raise OSError(ctypes.get_errno(), "Directory publication failed")


def prepare(root, destination, mode, audit):
    result = {"ok": False, "mode": mode, "errors": [], "warnings": [], "inventory": [],
              "excluded": [], "manifest_verified": False}
    stage, published = None, False
    try:
        original, target = Path(root).absolute(), Path(destination).absolute()
        source = original.resolve(strict=True)
        if original.is_symlink() or os.path.lexists(target):
            raise ValueError("Source link or existing destination")
        parent = target.parent.resolve(strict=True)
        target = parent / target.name
        if not parent.is_dir() or target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError("Overlapping source and destination")
    except (OSError, ValueError, TypeError, RuntimeError):
        issue(result["errors"], "invalid_destination")
        return result
    source_report = audit(source, mode)
    result.update({key: source_report[key] for key in (
        "errors", "warnings", "inventory", "excluded", "external_dependencies", "references")})
    if not source_report["ok"]:
        return result
    try:
        stage = Path(tempfile.mkdtemp(prefix=".package-copy-", dir=parent))
        expected = payload(source_report["inventory"])
        copy_payload(source, stage, expected)
        copied = audit(stage, mode)
        if not copied["ok"] or payload(copied["inventory"]) != expected:
            result["copy_audit"] = copied
            issue(result["errors"], "copy_audit_failed")
            return result
        fresh = audit(source, mode)
        if not fresh["ok"] or payload(fresh["inventory"]) != expected:
            issue(result["errors"], "source_changed")
            return result
        if MANIFEST in {x["path"] for x in source_report["inventory"]}:
            data = read_local(source, MANIFEST)
        else:
            data = (json.dumps({"schema_version": 1, "mode": mode, "inventory": expected,
                                "excluded_from_source": source_report["excluded"]},
                               ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with (stage / MANIFEST).open("xb") as handle:
            handle.write(data)
        if read_local(stage, MANIFEST) != data:
            raise ValueError("Manifest write readback failed")
        if json_value(data.decode("utf-8"))["inventory"] != expected:
            raise ValueError("Manifest content readback failed")
        final = audit(stage, mode)
        if not final["ok"]:
            result["copy_audit"] = final
            issue(result["errors"], "copy_audit_failed")
            return result
        publish_directory(stage, target)
        published = True
        readback = audit(target, mode)
        result.update(copy_audit=readback, manifest_verified=readback["ok"], ok=readback["ok"])
        if not readback["ok"]:
            issue(result["errors"], "published_copy_changed")
            result["target_retained"] = True
        return result
    except (OSError, ValueError, RuntimeError, KeyError):
        issue(result["errors"], "copy_failed")
        return result
    finally:
        if stage is not None and not published:
            try:
                shutil.rmtree(stage)
            except OSError:
                issue(result["errors"], "staging_cleanup_failed")
