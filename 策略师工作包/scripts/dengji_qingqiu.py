"""登记请求目录（v1.7.5 G10）：位置常量与“用过后移到 已用/”的共用做法，standalone_tasks、validate_project、guidang、yunxing_rizhi 都从这里取。

project/inputs/登记请求/ 下的请求登记成功后移到 project/inputs/登记请求/已用/（保留子目录，同名加 -2、-3）；
原样重试时从“已用”里取同名的那份交给登记走原有幂等；同名多份且请求编号不同时不自动挑（r3 #5）。只用标准库，不引用其他工作包模块（运行日志也会引用本模块）。"""
import json
import os
import re
import unicodedata
from pathlib import Path

REQUESTS_DIR = "project/inputs/登记请求"
USED_DIR = REQUESTS_DIR + "/已用"


def is_used(root, path):
    """path（绝对或相对工作区）在“已用”目录里。"""
    root = Path(root).resolve()
    try:
        target = Path(path) if Path(path).is_absolute() else root / path
        return Path(os.path.abspath(target)).resolve().is_relative_to(root / USED_DIR)
    except (OSError, ValueError):
        return False


def location(root, request_path):
    """登记请求在 登记请求/ 下（不含“已用”）时返回 (工作区, 相对路径)，否则 None。相对路径按当前目录解析（与命令行一致）。"""
    root = Path(root).resolve()
    try:
        relative = Path(os.path.abspath(request_path)).resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None
    if not relative.startswith(REQUESTS_DIR + "/") or relative.startswith(USED_DIR + "/"):
        return None
    return root, relative


def _numbered(target, number):
    return target if number == 1 else target.with_name(f"{target.stem}-{number}{target.suffix}")


def _variants(base):
    """“已用”里同名的全部变体（同名、-2、-3……）：列目录取全部，不因编号空档停下；返回 [(编号, 路径)]，按编号排。"""
    pattern = re.compile(re.escape(unicodedata.normalize("NFC", base.stem)) + r"(?:-([2-9]|[1-9]\d+))?"
                         + re.escape(unicodedata.normalize("NFC", base.suffix)) + r"\Z")
    try:
        names = os.listdir(base.parent)
    except OSError:
        return []
    found = []
    for name in names:
        match = pattern.match(unicodedata.normalize("NFC", name))
        if match and (base.parent / name).is_file():
            found.append((int(match.group(1) or 1), base.parent / name))
    return sorted(found)


class SameNameCopies(ValueError):
    """“已用”里有多份同名登记单、请求编号不同（r3 #5）：不自动挑一份，交调用方报“请指明”。copies：[(相对路径, 请求编号)]。"""

    def __init__(self, copies):
        self.copies = copies
        super().__init__("“已用”里有多份同名登记单，请求编号不同：" + "、".join(f"{rel}（request_id {rid!r}）" for rel, rid in copies))


def used_copies(root, request_path):
    """原路径对应的“已用”里全部同名变体（相对路径，按编号排）。"""
    found = location(root, request_path)
    if not found:
        return []
    base = found[0] / USED_DIR / Path(found[1]).relative_to(REQUESTS_DIR)
    return [path.relative_to(found[0]).as_posix() for _, path in _variants(base)]


def used_copy(root, request_path):
    """原路径已移到“已用”的那份（相对路径）；没有返回 None。同名多份时：请求编号都相同取编号最大的一份，
    请求编号不同抛 SameNameCopies（不自动挑，免得把别的请求的结果当成这次的）。"""
    copies = used_copies(root, request_path)
    if not copies:
        return None
    ids = []
    for rel in copies:
        try:
            data = json.loads((Path(root).resolve() / rel).read_text(encoding="utf-8"))
            ids.append(data.get("request_id") if isinstance(data, dict) else None)
        except (OSError, ValueError):
            ids.append(None)
    if len({json.dumps(x) for x in ids}) > 1:
        raise SameNameCopies(list(zip(copies, ids)))
    return copies[-1]


def move_used(root, request_path):
    """登记成功后把 登记请求/ 下用过的请求移到 已用/（同名加 -2、-3，接在已有最大编号之后）；别处的请求不动；
    移动失败返回 None，不影响已成功的登记。"""
    found = location(root, request_path)
    if not found:
        return None
    root, relative = found
    base = root / USED_DIR / Path(relative).relative_to(REQUESTS_DIR)
    number = max((n for n, _ in _variants(base)), default=0) + 1
    while _numbered(base, number).exists():
        number += 1
    target = _numbered(base, number)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(root / relative, target)
    except OSError:
        return None
    return target.relative_to(root).as_posix()
