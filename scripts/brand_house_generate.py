"""Create one primary HTML, exclusively, with resumable generation metadata."""
import os
from pathlib import Path
import tempfile

from brand_house_data import content, make_data
from brand_house_guard import registered
from brand_house_html import embed
from brand_house_store import identity, marks, save_record, source_meta, target, verified
from phase2_store import WorkflowError, fingerprint, local, now, read_json, sha
from phase3_events import append, events
from phase3_io import atomic_bytes, digest
from phase3_review import gate
from phase3_store import key_for, ref


def create_once(path, raw):
    """Publish complete bytes with a no-replace link; never expose a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".generating-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError as exc:
            raise WorkflowError("HTML 已存在，不能覆盖人工稿；请 inspect 或人工核对") from exc
    finally:
        Path(name).unlink(missing_ok=True)


def finish_generation(root, record):
    raw = local(root, record["auxiliary_path"]).read_bytes()
    if digest(raw) != record["observed_sha"]:
        raise WorkflowError("待生成的辅助副本指纹不符，不能恢复")
    _, data = verified(root, record, raw)
    original = source_meta(root, data)
    actual = gate(root, original["key"])
    if ref(actual) != record["synced_body"]:
        raise WorkflowError("生成中断后来源已变化，保留两边并人工对齐")
    path = local(root, record["path"])
    if path.exists():
        if not path.is_file() or sha(path) != record["observed_sha"]:
            raise WorkflowError("待恢复位置有不同文件，不能覆盖人工稿；完整待生成版仍在 auxiliary_path")
    else:
        create_once(path, raw)
    if path.read_bytes() != raw:
        raise WorkflowError("生成后的 HTML 回读不同，保留 pending")
    ready = {**record, "status": "ready"}
    if not any(e.get("action") == "generate" and e.get("document_id") == record["document_id"]
               for e in events(root, "html-save-log")):
        append(root, "html-save-log", {"action": "generate", "status": "readback_verified",
               "actor": record["author_instances"][0], "document_id": record["document_id"],
               "target": target(record), "proof": "python_generated_and_read_back_not_browser_writeback"})
    save_record(root, record["task_id"], ready)
    return {"path": record["path"], "target": target(ready), "document_id": record["document_id"],
            "status": "generated_pending_html_review", "synced_body": record["synced_body"]}


def generate(root, task_id, author):
    if not isinstance(author, str) or not author.strip():
        raise WorkflowError("生成必须提供实际作者执行实例")
    record = registered(root, task_id)
    if record:
        if record["status"] == "generation_pending":
            if record["author_instances"][0] != author:
                raise WorkflowError("恢复生成须使用原作者实例，不能重新声明作者")
            return finish_generation(root, record)
        raise WorkflowError("已登记唯一 HTML，不重新生成或覆盖；请 inspect")
    body = gate(root, key_for(task_id, "brand-house"))
    project_id = read_json(local(root, "project/state.json"))["project_id"]
    if not isinstance(project_id, str) or not project_id.strip() or project_id == "UNINITIALIZED" or task_id == "UNINITIALIZED":
        raise WorkflowError("项目尚未初始化，不能生成正式品牌屋")
    document = read_json(local(root, body["snapshot_path"]))
    data = make_data(project_id, body, document)
    source_meta(root, data)
    template = local(root, "templates/brand-house-model.html").read_text(encoding="utf-8")
    if "<!--BH_STYLES-->" in template or "<!--BH_SCRIPTS-->" in template:
        raise WorkflowError("通用模板尚未构建，先运行 build_brand_house_template.py")
    raw = embed(template, data)
    path = f"project/outputs/brand-house/{task_id}/brand-house.html"
    auxiliary = f"project/records/brand-house/{task_id}/last-valid.html"
    if local(root, path).exists() or local(root, auxiliary).exists():
        raise WorkflowError("生成位置已有未登记文件或辅助副本，不能覆盖；先人工核对")
    record = {"task_id": task_id, "path": path, "document_id": data["document_id"],
              "identity_sha": identity(data), "source_body": ref(body), "synced_body": ref(body),
              "author_instances": [author], "observed_sha": digest(raw), "observed_current": data["current"],
              "observed_content_sha": fingerprint(content(data["history"][-1]["snapshot"])),
              "synced_content_sha": fingerprint(content(data["history"][-1]["snapshot"])),
              "observed_at": now(), "pending_body_alignment": False, "status": "generation_pending",
              "auxiliary_path": auxiliary,
              **{f"{name}_marks": marks(data[name]) for name in ("history", "events", "branches")}}
    atomic_bytes(local(root, auxiliary), raw)
    save_record(root, task_id, record)
    return finish_generation(root, record)
