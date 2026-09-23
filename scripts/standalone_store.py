"""Validate and preserve independent task history without changing formal workflows."""
from datetime import date
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

from phase2_store import WorkflowError, local

JOURNAL = "project/records/standalone-tasks.jsonl"
STATUSES = {"planned", "in_progress", "waiting", "completed", "paused", "cancelled", "archived"}
FIELDS = {"title", "project_label", "request_text", "goal", "status", "summary",
          "next_action", "owner", "due_date", "sources", "deliverables",
          "completion_evidence"}
OPTIONAL_FIELDS = {"source_request_key"}
EVENT_FIELDS = {"schema_version", "request_id", "request_hash", "task_id", "revision",
                "created_at", "actor", "reason", "task"}


def text(value, name, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise WorkflowError(f"{name} 必须为{'可空' if empty else '非空'}文字")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise WorkflowError(f"{name} 含非法控制字符")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise WorkflowError("任务及请求编号须使用字母、数字、短横线或下划线")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def storage(root):
    root = Path(root).resolve()
    for name in ("project", "project/records"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise WorkflowError("独立任务存储目录必须为普通目录")
    for name in (JOURNAL, "project/records/.workspace-write.lock"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise WorkflowError("独立任务记录与锁必须为普通文件")
    return root / JOURNAL


def reference_path(root, value, stored=False):
    if (not isinstance(value, str) or not value.startswith("project/")
            or PurePosixPath(value).as_posix() != value or ".." in PurePosixPath(value).parts
            or "\\" in value or any(ord(c) < 32 for c in value) or value == JOURNAL):
        raise WorkflowError("本地资料须归档在 project/ 内，不能引用任务日志自身")
    if stored:
        # Historical references remain readable even if their live target changed.
        return Path(root) / value
    original = Path(root)
    for part in PurePosixPath(value).parts:
        original = original / part
        if original.is_symlink():
            raise WorkflowError("独立任务资料不能通过符号链接引用")
    return local(root, value)


def references(root, items, stored=False, previous=None, accept_changed_references=False):
    if not isinstance(items, list):
        raise WorkflowError("来源与交付物必须为列表")
    previous_hashes = {
        item["path"]: item["sha256"]
        for item in (previous or [])
        if isinstance(item, dict) and set(item) == {"label", "path", "sha256"}
    }
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise WorkflowError("每份来源或交付物必须为对象")
        text(item.get("label"), "资料说明")
        expected = {"label", "path", "sha256"} if stored else {"label", "path"}
        if set(item) == expected:
            reference_path(root, item["path"], stored=stored)
            value = dict(item)
            if stored:
                if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                    raise WorkflowError("资料指纹无效")
            elif item["path"] in previous_hashes and not accept_changed_references:
                # Ordinary progress updates must keep evidence bound to the version
                # that was previously checked, even when the live file has changed.
                value["sha256"] = previous_hashes[item["path"]]
            else:
                path = reference_path(root, item["path"])
                if not path.is_file():
                    raise WorkflowError("本地资料文件不存在，请先归档原件")
                value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif set(item) == {"label", "url"}:
            text(item["url"], "外部链接")
            url = urlsplit(item["url"])
            if (url.scheme not in {"https", "http", "thread"} or not url.netloc
                    or url.username or url.password or any(c.isspace() for c in item["url"])):
                raise WorkflowError("外部链接格式无效，不允许账号密码")
            value = dict(item)
        else:
            raise WorkflowError("资料只能包含 label 与 path 或 url，不接受其它字段")
        result.append(value)
    return result


def task_value(root, value, stored=False, previous=None, accept_changed_references=False):
    if (not isinstance(value, dict) or not FIELDS.issubset(value)
            or set(value) - FIELDS - OPTIONAL_FIELDS):
        raise WorkflowError("独立任务字段不完整或含未支持字段")
    result = dict(value)
    for name in ("title", "project_label", "request_text", "goal", "summary", "next_action"):
        text(value[name], name)
    if not isinstance(value["status"], str) or value["status"] not in STATUSES:
        raise WorkflowError("独立任务状态无效")
    if value["owner"] is not None:
        text(value["owner"], "负责人")
    if value["due_date"] is not None:
        text(value["due_date"], "日期")
        try:
            if date.fromisoformat(value["due_date"]).isoformat() != value["due_date"]:
                raise ValueError()
        except ValueError as exc:
            raise WorkflowError("日期须为 YYYY-MM-DD，不确定时留空") from exc
    text(value["completion_evidence"], "完成依据", empty=True)
    if value.get("source_request_key") is not None:
        text(value["source_request_key"], "来源请求键")
    for name in ("sources", "deliverables"):
        result[name] = references(
            root, value[name], stored,
            previous=(previous or {}).get(name),
            accept_changed_references=accept_changed_references,
        )
    if value["status"] == "completed" and (not value["completion_evidence"].strip()
                                              or not value["deliverables"]):
        raise WorkflowError("完成任务须提供完成依据及交付物，不代表客户批准")
    return result


def history(root):
    path = storage(root)
    if not path.exists():
        return [], b""
    raw = path.read_bytes()
    if not raw.strip():
        raise WorkflowError("独立任务历史文件为空，可能被截断；请恢复原件，不能当作新任务包")
    events, latest, request_ids = [], {}, set()
    try:
        for line in raw.decode("utf-8").split("\n"):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict) or set(item) != EVENT_FIELDS:
                raise ValueError("事件字段不完整")
            identifier(item["task_id"])
            identifier(item["request_id"])
            if (type(item["schema_version"]) is not int or item["schema_version"] != 1
                    or type(item["revision"]) is not int
                    or item["revision"] != latest.get(item["task_id"], 0) + 1
                    or item["request_id"] in request_ids
                    or not isinstance(item["request_hash"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["request_hash"])):
                raise ValueError("版本序列或请求编号无效")
            for name in ("actor", "reason", "created_at"):
                text(item[name], name)
            task_value(root, item["task"], stored=True)
            events.append(item)
            latest[item["task_id"]] = item["revision"]
            request_ids.add(item["request_id"])
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise WorkflowError("独立任务历史损坏，请核对原件；未修改旧记录") from exc
    return events, raw
