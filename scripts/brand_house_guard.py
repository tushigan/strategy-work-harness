"""A lightweight Phase 3 guard; never calls current/resolve recursively."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from phase2_store import WorkflowError, local, read_json, sha

INDEX = "project/records/phase4-html.json"
_SYNC = ContextVar("brand_house_sync", default=None)


def registry(root):
    path = local(root, INDEX)
    data = read_json(path) if path.exists() else {"schema_version": "1.0", "documents": {}}
    if not isinstance(data, dict) or data.get("schema_version") != "1.0" or not isinstance(data.get("documents"), dict):
        raise WorkflowError("HTML 登记表损坏，保留文件并人工核对")
    return data


def registered(root, task_id):
    return registry(root)["documents"].get(task_id)


def guard(root, meta):
    if meta.get("kind") != "brand-house":
        return
    record = registered(root, meta["task_id"])
    # Generation does not revise the body; task_gate separately rejects incomplete HTML.
    if not record or record.get("status") == "generation_pending":
        return
    if record.get("alignment_decision_id"):
        from phase3_decisions import valid_event
        from phase3_events import events
        decision = next((e for e in events(root, "phase3-decisions")
                         if e["record_id"] == record["alignment_decision_id"]), {})
        if not valid_event(root, decision) or decision.get("action") != "align":
            raise WorkflowError("HTML 正文对齐决定原文失效，不能继续使用旧批准")
    path = local(root, record["path"])
    if not path.is_file() or sha(path) != record["observed_sha"]:
        raise WorkflowError("关联 HTML 已改变或丢失，先运行 brand_house.py inspect；旧正文批准不能放行")
    if record.get("pending_body_alignment") or record.get("sync_pending"):
        scope = _SYNC.get()
        body = {"space": "phase3", "key": meta["key"], "version": meta["version"], "sha256": meta["sha256"]}
        expected = (str(Path(root).resolve()), body, record["observed_sha"], record["observed_content_sha"])
        if scope != expected:
            raise WorkflowError("HTML 内容已改变，pending_body_alignment：正文及递归下游待明确授权对齐")


@contextmanager
def aligning(root, body, html_sha, content_sha):
    """Only the synchronizer may temporarily read this exact old body."""
    scope = (str(Path(root).resolve()), body, html_sha, content_sha)
    if _SYNC.get() is not None:
        raise WorkflowError("不能嵌套正文同步")
    token = _SYNC.set(scope)
    try:
        yield
    finally:
        _SYNC.reset(token)
