"""Local file fingerprints and readable evidence for Agent dispatches."""
from pathlib import Path
import re
import unicodedata

from phase2_store import WorkflowError, sha

# Default_Ignorable_Code_Point letters in Unicode DerivedCoreProperties.
IGNORABLE_LETTERS = frozenset("\u115f\u1160\u3164\uffa0")


def local(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or relative.startswith(("/", "~"))
            or "\\" in relative or re.match(r"^[A-Za-z]:", relative)):
        raise WorkflowError("分派引用必须是工作包内相对路径")
    raw = root / relative
    resolved = raw.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve()):
        raise WorkflowError("分派引用越界")
    current = raw
    while current != root:
        if current.is_symlink():
            raise WorkflowError("分派引用不能使用符号链接")
        current = current.parent
    return raw


def file_ref(root: Path, value: object, label: str) -> dict[str, str]:
    if isinstance(value, str):
        path_value, expected = value, None
    elif isinstance(value, dict):
        path_value, expected = value.get("path"), value.get("sha256")
    else:
        raise WorkflowError(f"{label}必须是路径或路径指纹对象")
    path = local(root, path_value)
    if path.is_symlink() or not path.is_file():
        raise WorkflowError(f"{label}必须是工作包内真实文件，不能是符号链接或目录")
    actual = sha(path)
    if expected is not None and expected != actual:
        raise WorkflowError(f"{label}指纹与当前文件不符")
    # macOS may expose one temporary directory through two equivalent aliases.
    # Compare and store the canonical path so a valid local file is not rejected
    # merely because the workspace spelling differs.
    return {"path": path.resolve().relative_to(root.resolve()).as_posix(), "sha256": actual}


def text_ref(root: Path, evidence: object) -> dict[str, str]:
    proof = file_ref(root, evidence, "主控文字证据")
    try:
        text = local(root, proof["path"]).read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise WorkflowError("主控证据须为可读、非空 UTF-8 文字记录") from exc
    controls = any(unicodedata.category(char) == "Cc" and char not in "\t\r\n" for char in text)
    meaningful = any(char not in IGNORABLE_LETTERS and unicodedata.category(char)[0] in "LN"
                     for char in text)
    if controls or not meaningful:
        raise WorkflowError("主控证据须为可读、非空 UTF-8 文字记录")
    return proof


def verify_refs(root: Path, item: dict) -> None:
    for value in [*item["input_files"], *item.get("candidates", []), *item.get("takeover_refs", [])]:
        file_ref(root, value, "回读文件")
