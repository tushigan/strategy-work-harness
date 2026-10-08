from task_validation import read_check
"""Validate and preserve independent task history without changing formal workflows."""
from datetime import date
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

from phase2_store import WorkflowError, local

JOURNAL = "project/records/standalone-tasks.jsonl"
HEAD = "project/records/standalone-tasks.head.json"  # 截尾核对锚点：记录条数与末条摘要
STATUSES = {"planned", "in_progress", "waiting", "completed", "paused", "cancelled", "archived"}
FIELDS = {"title", "project_label", "request_text", "goal", "status", "summary",
          "next_action", "owner", "due_date", "sources", "deliverables",
          "completion_evidence"}
OPTIONAL_FIELDS = {"source_request_key", "process_refs", "historical_refs", "decision_refs", "artifact_versions"}
EVENT_FIELDS = {"schema_version", "request_id", "request_hash", "task_id", "revision",
                "created_at", "actor", "reason", "task"}
DELTA_FIELDS = EVENT_FIELDS - {"task"} | {"task_changes", "base_task_sha256", "task_sha256"}
OPTIONAL_EVENT_FIELDS = {"task_removed", "revision_budget", "observed_adoption", "iteration", "prev_line_sha256"}
ITERATION_FIELDS = {"round", "mode", "words_ref", "changes", "previous", "reviewed"}


def stored_event(event, previous_task):
    """写盘形态：第2版只存相对上一版的变化字段；首版存全部字段。
    链式摘要：base_task_sha256 = 上一版补全后任务的摘要，task_sha256 = 本版补全后任务的摘要；
    删掉、改动或重排中间一条，读取时补全结果对不上即判损坏（校验强度不低于存完整任务的第1版）。
    另有逐条链 prev_line_sha256（上一条事件的内容摘要，含无变化事件与元数据，不含存储格式字段）和截尾锚点 HEAD（条数 + 末条摘要）。
    边界：防误删、误改、重排与截尾（都能发现）；不防有意按规则重算整条链和锚点的恶意改写。"""
    task = event["task"]
    base = previous_task or {}
    changes = {k: v for k, v in task.items() if base.get(k) != v or k not in base}
    removed = sorted(k for k in base if k not in task)
    stored = {k: v for k, v in event.items() if k != "task"}
    stored.update(schema_version=2, task_changes=changes, base_task_sha256=digest(previous_task) if previous_task else None,
                  task_sha256=digest(task))
    if removed:
        stored["task_removed"] = removed
    return stored


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
    for name in (JOURNAL, HEAD, "project/records/.workspace-write.lock"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise WorkflowError("独立任务记录与锁必须为普通文件")
    return root / JOURNAL


# 只关乎存储格式的字段。改稿预算快照 revision_budget 计入内容摘要（M02）：删掉或改小它，链与锚点即报损坏，
# 不会让检核目标退回改动前的版本；读取时仍按历史重新推导核对（真正的旧记录没有快照时按历史推导，少计即阻断）。
FORMAT_FIELDS = {"schema_version", "task_changes", "task_removed", "base_task_sha256", "task_sha256", "prev_line_sha256"}


def event_digest(event):
    """事件的内容摘要：补全后的任务 + 元数据（请求、版本、时间、记录者、原因、接纳、改稿、改稿预算快照），不含存储格式字段。
    同一事件从第2版变化格式换成第1版全量格式，摘要不变；内容或这些元数据任何改动，摘要都变。"""
    return digest({k: v for k, v in event.items() if k not in FORMAT_FIELDS})


def _r3_digest(event):
    """未发布的 1.6.0 r3 候选的摘要算法（不含预算快照）：只用来在报损坏时指明原因，不据此放行。"""
    return digest({k: v for k, v in event.items() if k not in FORMAT_FIELDS | {"revision_budget"}})


def write_head(root, count, last_event):
    """记录写入后更新截尾锚点（先写临时文件再替换）。记录先写、锚点后写：中途中断时记录比锚点多一条，读取时可接受。"""
    import os, tempfile
    path = Path(root).resolve() / HEAD
    fd, temporary = tempfile.mkstemp(prefix=".writing-head-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "journal": JOURNAL, "events": count, "last_event_sha256": event_digest(last_event)}, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _check_head(root, events, chained):
    path = Path(root).resolve() / HEAD
    if not path.exists():
        if chained:
            raise ValueError("截尾核对锚点 " + HEAD + " 缺失（逐条链记录必须有锚点）")
        return
    head = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(head, dict) or type(head.get("events")) is not int or head["events"] < 1:
        raise ValueError("截尾核对锚点格式损坏")
    if head["events"] > len(events):
        raise ValueError(f"记录被截尾：锚点记 {head['events']} 条，现只有 {len(events)} 条")
    if event_digest(events[head["events"] - 1]) != head.get("last_event_sha256"):
        if _r3_digest(events[head["events"] - 1]) == head.get("last_event_sha256"):
            raise ValueError(f"第 {head['events']} 条记录的锚点按未发布的 1.6.0 r3 候选格式计算（预算快照未计入摘要），需人工迁移")
        raise ValueError(f"第 {head['events']} 条记录与锚点摘要不符（被改动或截尾后重写）")


def reference_path(root, value, stored=False):
    if (not isinstance(value, str) or not value.startswith("project/")
            or PurePosixPath(value).as_posix() != value or ".." in PurePosixPath(value).parts
            or "\\" in value or any(ord(c) < 32 for c in value) or value == JOURNAL or value.startswith("project/运行日志/")):
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


def known_fingerprint(root, path, sha):
    """带指纹的历史来源须有出处：现场文件就是这个版本，或已有任务记录、归档记录登记过这个 路径+指纹。"""
    live = Path(root) / path
    try:
        if live.is_file() and not live.is_symlink() and hashlib.sha256(live.read_bytes()).hexdigest() == sha:
            return True
    except OSError:
        pass
    try:
        events = history(root)[0]
    except (WorkflowError, OSError):
        events = []
    for event in events:
        for name in ("sources", "deliverables", "process_refs", "historical_refs", "decision_refs"):
            if any(isinstance(r, dict) and r.get("path") == path and r.get("sha256") == sha for r in event["task"].get(name, [])):
                return True
    try:
        from guidang import archived_index
        return (path, sha) in archived_index(root)
    except (ImportError, WorkflowError, OSError):
        return False


def references(root, items, stored=False, previous=None, accept_changed_references=False, pinned=False):
    """pinned=True（历史来源）：允许直接给出登记时的指纹，按原指纹保存、不读现场文件（文件已改或已移走也照样可引用）。"""
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
        keep = "path" in item and (stored or (pinned and set(item) == {"label", "path", "sha256"}))  # 外部链接（label+url）走下面的分支
        if set(item) == expected or keep:
            reference_path(root, item["path"], stored=keep)
            value = dict(item)
            if keep:
                if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                    raise WorkflowError("资料指纹无效")
                if not stored and previous_hashes.get(item["path"]) != item["sha256"] and not known_fingerprint(root, item["path"], item["sha256"]):
                    raise WorkflowError(f"历史来源 {item['path']} 的指纹 {item['sha256'][:12]}… 在现场文件、已有任务记录和归档记录里都查不到；"
                                        "只能钉住确实登记过或现存的版本，不能登记来历不明的指纹")
            elif item["path"] in previous_hashes and (not accept_changed_references or isinstance(accept_changed_references, dict) and item["path"] not in accept_changed_references):
                # Ordinary progress updates must keep evidence bound to the version
                # that was previously checked, even when the live file has changed.
                value["sha256"] = previous_hashes[item["path"]]
            else:
                path = reference_path(root, item["path"])
                if not path.is_file():
                    raise WorkflowError("本地资料文件不存在，请先归档原件")
                value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                if isinstance(accept_changed_references,dict) and item["path"] in accept_changed_references and value["sha256"] != accept_changed_references[item["path"]]:
                    raise WorkflowError("观察版在登记前改变，未写业务记录")
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


def request_task(task):
    """把已存任务转回请求形态：引用去掉程序算的指纹（未变的文件仍绑定原指纹）。"""
    value = dict(task)
    for name in ("sources", "deliverables", "process_refs", "decision_refs"):  # 历史来源保留登记指纹（pinned）
        if name in value:
            value[name] = [{k: v for k, v in ref.items() if k != "sha256"} for ref in value[name]]
    return value


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
    for name in ("process_refs", "historical_refs", "decision_refs"):
        if name in value:
            result[name] = references(root,value[name],stored,
                previous=(previous or {}).get(name),accept_changed_references=accept_changed_references,
                pinned=name == "historical_refs")
    if "artifact_versions" in value:
        from chengguo_shenfen import versions
        result["artifact_versions"]=versions(root,value["artifact_versions"],result,previous,stored)
    if value["status"] == "completed" and (not value["completion_evidence"].strip()
                                              or not value["deliverables"]):
        raise WorkflowError("完成任务须提供完成依据及交付物，不代表客户批准")
    return result


@read_check
def history(root):
    path = storage(root)
    if not path.exists():
        return [], b""
    raw = path.read_bytes()
    if not raw.strip():
        raise WorkflowError("独立任务历史文件为空，可能被截断；请恢复原件，不能当作新任务包")
    events, latest, request_ids, previous_task = [], {}, set(), {}
    lines = [x for x in raw.split(b"\n") if x.strip()]
    previous_event, chained = None, False
    try:
        for number, raw_line in enumerate(lines, 1):
            item = json.loads(raw_line.decode("utf-8"))
            if not isinstance(item, dict):
                raise ValueError(f"第 {number} 行不是事件")
            if item.get("schema_version") == 2:
                # 第2版只存变化字段（U17）；读取时按前一版补全，校验强度不变。
                keys = set(item)
                if {"task_changes"} <= keys and not {"base_task_sha256", "task_sha256"} <= keys:
                    raise ValueError(f"第 {number} 行是第2版事件但缺少链式摘要字段（未发布的 1.6.0 早期候选格式，需人工迁移）")
                if not (DELTA_FIELDS <= keys <= DELTA_FIELDS | OPTIONAL_EVENT_FIELDS | {"task"}):
                    raise ValueError(f"第 {number} 行事件字段不完整")
                if not isinstance(item["task_changes"], dict) or not isinstance(item.get("task_removed", []), list):
                    raise ValueError("变化字段格式损坏")
                base = previous_task.get(item["task_id"], {})
                if not base and item["revision"] != 1:
                    raise ValueError("缺少前一版本")
                if item["base_task_sha256"] != (digest(base) if base else None):
                    raise ValueError("变化记录与前一版本不衔接（中间记录缺失、改动或重排）")
                rebuilt = {k: v for k, v in {**base, **item["task_changes"]}.items() if k not in item.get("task_removed", [])}
                if "task" in item and item["task"] != rebuilt:
                    raise ValueError("完整任务与变化记录不一致")
                if item["task_sha256"] != digest(rebuilt):
                    raise ValueError("补全后的任务与记录摘要不符")
                item["task"] = rebuilt
            elif set(item) - {"prev_line_sha256"} not in (EVENT_FIELDS, EVENT_FIELDS | {"revision_budget"}, EVENT_FIELDS | {"revision_budget","observed_adoption"}):
                raise ValueError(f"第 {number} 行事件字段不完整")
            identifier(item["task_id"])
            identifier(item["request_id"])
            if (type(item["schema_version"]) is not int or item["schema_version"] not in (1, 2)
                    or type(item["revision"]) is not int
                    or item["revision"] != latest.get(item["task_id"], 0) + 1
                    or item["request_id"] in request_ids
                    or not isinstance(item["request_hash"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["request_hash"])):
                raise ValueError("版本序列或请求编号无效")
            for name in ("actor", "reason", "created_at"):
                text(item[name], name)
            if "observed_adoption" in item:
                adoption=item["observed_adoption"]
                if not isinstance(adoption,dict) or set(adoption)!={"basis","accepted_paths","user_original"}:raise ValueError("接纳凭据格式损坏")
                if not isinstance(adoption["accepted_paths"],list) or not isinstance(adoption["basis"],dict):raise ValueError("接纳凭据不完整")
                b=adoption["basis"]
                if b.get("basis_sha256")!=digest({k:v for k,v in b.items() if k!="basis_sha256"}) or b.get("task_id")!=item["task_id"] or b.get("expected_revision")!=item["revision"]-1:raise ValueError("接纳未绑定前一观察版")
                if adoption["accepted_paths"]:text(adoption["user_original"],"采用手改的用户原文")
            if "iteration" in item:
                it = item["iteration"]
                if not isinstance(it, dict) or set(it) != ITERATION_FIELDS or type(it["round"]) is not int:
                    raise ValueError("在线改稿记录损坏")
            task_value(root, item["task"], stored=True)
            # 逐条链：prev_line_sha256 = 上一条事件的内容摘要（含无变化事件与元数据）；链开始后每条都必须有
            if "prev_line_sha256" in item:
                if item["prev_line_sha256"] != previous_event:
                    if events and item["prev_line_sha256"] == _r3_digest(events[-1]):
                        raise ValueError(f"第 {number} 行的逐条链按未发布的 1.6.0 r3 候选格式计算（预算快照未计入摘要），需人工迁移")
                    raise ValueError(f"第 {number} 行与上一条记录不衔接（中间记录被删除、改动或重排）")
                chained = True
            elif chained:
                raise ValueError(f"第 {number} 行缺少逐条链字段（链式记录之后不应出现）")
            previous_event = event_digest(item)
            previous_task[item["task_id"]] = item["task"]
            events.append(item)
            latest[item["task_id"]] = item["revision"]
            request_ids.add(item["request_id"])
        _check_head(root, events, chained)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        cause = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
        raise WorkflowError(f"独立任务历史损坏（{cause}），请核对原件；未修改旧记录") from exc
    return events, raw
