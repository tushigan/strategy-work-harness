"""HTML observations store references and hashes, never a second current body."""
import json

from brand_house_data import compare, content, make_fields
from brand_house_guard import INDEX, registered, registry
from brand_house_html import extract
from phase2_store import WorkflowError, atomic_json, fingerprint, local, now, read_json, sha
from phase3_events import append, events
from phase3_io import atomic_bytes, digest, registry as body_registry
from workspace_lock import serialized


def save_record(root, task_id, record):
    with serialized(root):
        return _save_record_unlocked(root, task_id, record)


def _save_record_unlocked(root, task_id, record):
    data = registry(root)
    data["documents"][task_id] = record
    atomic_json(local(root, INDEX), data)


def identity(data):
    return value_sha({k: data[k] for k in ("schema_version", "document_id", "project_id", "task_id", "source", "fields")})


def value_sha(value):
    return digest(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8"))


def target(record):
    return {"space": "html", "key": record["task_id"], "revision": record["observed_current"],
            "sha256": record["observed_sha"]}


def marks(items):
    return [{"id": item["id"], "sha256": value_sha(item)} for item in items]


def source_meta(root, data):
    from phase3_store import ref
    project_id = read_json(local(root, "project/state.json"))["project_id"]
    if project_id == "UNINITIALIZED" or data["project_id"] != project_id or data["task_id"] == "UNINITIALIZED":
        raise WorkflowError("HTML 项目身份不符或仍是 UNINITIALIZED 空白模板")
    reference = data["source"]["body"]
    history = body_registry(root)["artifacts"].get(f"{data['task_id']}::brand-house", [])
    matches = [m for m in history if ref(m) == reference]
    if len(matches) != 1:
        raise WorkflowError("HTML 原始来源引用不存在于此任务的真实 Phase 3 历史")
    meta = matches[0]
    path = local(root, meta["snapshot_path"])
    if not path.is_file() or sha(path) != meta["sha256"]:
        raise WorkflowError("HTML 原始正文来源缺失或已被修改")
    if data["source"]["document"] != read_json(path) or data["source"]["dependencies"] != meta["dependencies"]:
        raise WorkflowError("HTML 原始来源文档或依赖被改写，不能继续")
    fields, initial = make_fields(data["source"]["document"])
    if data["fields"] != fields or data["history"][0]["snapshot"] != initial:
        raise WorkflowError("HTML 字段映射或初始生成版不对应真实完整正文")
    return meta


def verified(root, record, raw=None):
    raw = local(root, record["path"]).read_bytes() if raw is None else raw
    data = extract(raw)
    source_meta(root, data)
    if data["document_id"] != record["document_id"] or identity(data) != record["identity_sha"]:
        raise WorkflowError("HTML 文档身份、来源或字段映射已被替换")
    for name in ("history", "events", "branches"):
        old = record[f"{name}_marks"]
        new = marks(data[name])
        if new[:len(old)] != old:
            raise WorkflowError(f"HTML {name} 历史前缀被删除、修改或复用旧ID；已保留最后有效副本")
    return raw, data


def require_record(root, task_id):
    record = registered(root, task_id)
    if not record:
        raise WorkflowError("此任务尚未登记 HTML；先 generate")
    if record.get("status") == "generation_pending":
        raise WorkflowError("HTML 生成尚未结束；再次 generate 恢复，不覆盖人工稿")
    return record


def effective(root, record):
    from phase3_store import current, ref
    meta = current(root, record["synced_body"]["key"])
    if ref(meta) != record["synced_body"]:
        raise WorkflowError("正文已另行更新，HTML 仍对应旧来源；保存两边并人工对齐")
    if record["synced_content_sha"] != record["observed_content_sha"]:
        raise WorkflowError("HTML 当前文字未与有效正文引用对齐")
    return meta


def live(root, task_id):
    record = require_record(root, task_id)
    path = local(root, record["path"])
    if not path.is_file() or sha(path) != record["observed_sha"]:
        raise WorkflowError("HTML 当前字节未登记，先 inspect；不能使用旧批准")
    raw, data = verified(root, record)
    meta = effective(root, record)
    if digest(raw) != record["observed_sha"]:
        raise WorkflowError("HTML 读取期间改变，重新 inspect")
    return record, data, meta


def inspect(root, task_id):
    record = require_record(root, task_id)
    raw, data = verified(root, record)
    snapshot = data["history"][-1]["snapshot"]
    prior_index = len(record["history_marks"]) - 1
    changes = compare(data["history"][prior_index]["snapshot"], snapshot)
    transitions = [compare(data["history"][i - 1]["snapshot"], data["history"][i]["snapshot"])
                   for i in range(prior_index + 1, len(data["history"]))]
    content_changed = any(d["content"] for d in transitions)
    observed_sha = digest(raw)
    content_sha = fingerprint(content(snapshot))
    pending = record["pending_body_alignment"] or content_changed or content_sha != record["synced_content_sha"]
    reason = ("HTML 文字含栏目标签改变；正文与下游须核对并明确授权对齐" if pending else
              "仅字体、字号、字重或版式改变，文字及来源未变，不改写策略正文" if
              changes["style"] or changes["layout"] else
              "当前文字和样式未变；显式保存或文件外壳改变仍需针对当前 HTML 重新检核")
    new_record = {**record, "observed_sha": observed_sha, "observed_current": data["current"],
                  "observed_content_sha": content_sha, "pending_body_alignment": bool(pending),
                  "observed_at": now(), "impact_reason": reason,
                  **{f"{name}_marks": marks(data[name]) for name in ("history", "events", "branches")}}
    if sha(local(root, record["path"])) != observed_sha:
        raise WorkflowError("读取时 HTML 再次变化，请重试；未登记为有效保存")
    atomic_bytes(local(root, record["auxiliary_path"]), raw)
    log = events(root, "html-save-log")
    for event in data["events"]:
        if not any(e.get("document_id") == data["document_id"] and e.get("file_event_id") == event["id"] for e in log):
            append(root, "html-save-log", {"task_id": task_id, "document_id": data["document_id"],
                   "file_event_id": event["id"], "file_event": event, "status": event["status"],
                   "proof": "embedded_event_only_not_browser_writeback_verification"})
    if observed_sha != record["observed_sha"]:
        append(root, "html-save-log", {"task_id": task_id, "document_id": data["document_id"],
               "action": "inspect", "status": "readback_verified", "target": target(new_record),
               "changes": changes, "impact_reason": reason,
               "proof": "file_currently_contains_revision_only_not_browser_writeback_success"})
    save_record(root, task_id, new_record)
    result = {"target": target(new_record), "path": record["path"], "versions": len(data["history"]),
              "changes": changes, "pending_body_alignment": bool(pending), "impact_reason": reason,
              "synced_body": new_record["synced_body"], "auxiliary_path": record["auxiliary_path"],
              "file_events": data["events"], "browser_writeback_verified": False}
    try:
        effective(root, new_record)
        result["status"] = "aligned_pending_html_review"
    except (WorkflowError, OSError, KeyError, ValueError) as exc:
        result.update(status="stale", reason=str(exc))
    return result


def scan(root):
    from phase3_tasks import inspect as phase3_inspect
    result = {"html": {}, "phase3": None}
    for task_id in registry(root)["documents"]:
        try:
            result["html"][task_id] = inspect(root, task_id)
        except (WorkflowError, OSError, KeyError, ValueError) as exc:
            result["html"][task_id] = {"status": "blocked", "reason": str(exc)}
    result["phase3"] = phase3_inspect(root)
    return result
