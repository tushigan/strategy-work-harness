"""Explicit, version-bound alignment using Phase 3 decisions and revision budgets."""
from pathlib import Path

from brand_house_data import content, mapped_document
from brand_house_guard import aligning
from brand_house_store import inspect, require_record, save_record, target, verified
from phase2_store import WorkflowError, fingerprint, local, read_json, sha, test_mode
from phase3_decisions import decision, valid_event
from phase3_events import append, events
from phase3_io import PENDING, check_pointers, recover
from phase3_sources import file_valid
from phase3_store import current, latest, publish, ref
from workspace_lock import serialized


def normalized(document):
    return {k: v for k, v in document.items() if k != "revision"}


def authorization(root, record, path):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise WorkflowError("同步决定须先存入当前工作包，不直接使用项目外文件")
    permit = read_json(path)
    if not isinstance(permit, dict) or permit.get("action") != "sync-body":
        raise WorkflowError("需提供明确的 sync-body 决定原文")
    if permit.get("target") != target(record) or permit.get("document_id") != record["document_id"]:
        raise WorkflowError("决定未绑定确切当前 HTML 指纹、版本和文档身份")
    if permit.get("body") != record["synced_body"]:
        raise WorkflowError("决定未绑定待同步的确切正文版本")
    if any(not isinstance(permit.get(k), str) or not permit[k].strip()
           for k in ("actor", "decision_text", "reason")):
        raise WorkflowError("同步必须保留真实决定原文、身份及变更原因")
    if not isinstance(permit.get("simulation"), bool) or permit["simulation"] and not test_mode(root):
        raise WorkflowError("只有 test_mode 隔离项目可以使用模拟确认")
    return permit, sha(path)


def pending_result(root, record, desired, pending):
    """Recover only the exact already-authorized Phase 3 transaction."""
    path = local(root, PENDING)
    if path.exists():
        transaction = read_json(path)
        candidate = transaction["meta"]
        if candidate["key"] != pending["body"]["key"] or transaction["previous"] is None or (
                ref(transaction["previous"]) != pending["body"]):
            raise WorkflowError("存在另一份正文待恢复交易，不能借本次授权放行")
        if candidate.get("content_hash") != fingerprint(normalized(desired)) or (
                candidate.get("revision", {}).get("event_id") != pending["decision_id"]):
            raise WorkflowError("待恢复正文不是这次授权的 HTML 内容")
        recover(root)
    meta = latest(root, pending["body"]["key"])
    if ref(meta) == pending["body"]:
        return None
    if meta.get("content_hash") != fingerprint(normalized(desired)) or (
            not meta.get("revision") or meta["revision"].get("event_id") != pending["decision_id"]):
        raise WorkflowError("正文和 HTML 都发生了另一笔修改，保存两边并手工对齐，不能猜测合并")
    check_pointers(root, meta)
    return meta


def sync_body(root, task_id, decision_path, author):
    with serialized(root):
        return _sync_body_unlocked(root, task_id, decision_path, author)


def _sync_body_unlocked(root, task_id, decision_path, author):
    if not isinstance(author, str) or not author.strip():
        raise WorkflowError("同步需提供真实执行作者实例")
    inspect(root, task_id)
    record = require_record(root, task_id)
    if not record["pending_body_alignment"]:
        return {"status": "no_body_change", "target": target(record), "published": False,
                "reason": record.get("impact_reason", "没有待同步正文内容，样式改动不发布正文")}
    permit, permit_sha = authorization(root, record, decision_path)
    raw, data = verified(root, record)
    if sha(local(root, record["path"])) != record["observed_sha"]:
        raise WorkflowError("决定后 HTML 再次改变，不能沿用旧授权")
    desired = mapped_document(data)
    pending = record.get("sync_pending")
    key = record["synced_body"]["key"]
    if not pending and ref(latest(root, key)) != record["synced_body"]:
        raise WorkflowError("正文已另行更新，保存 HTML 与正文两边并手工对齐，不能猜测合并")
    with aligning(root, record["synced_body"], record["observed_sha"], record["observed_content_sha"]):
        result = None
        if pending:
            if pending["target"] != target(record) or pending["authorization_sha"] != permit_sha or pending["author"] != author:
                raise WorkflowError("尚有未完成同步，必须用原决定及作者恢复，不覆盖新改动")
            event = next((e for e in events(root, "phase3-decisions") if e["record_id"] == pending["decision_id"]), {})
            if not valid_event(root, event) or event.get("target") != pending["body"]:
                raise WorkflowError("原同步决定已失效")
            result = pending_result(root, record, desired, pending)
        if result is None:
            try:
                previous = current(root, key)
            except WorkflowError as exc:
                raise WorkflowError(f"{exc}；保留 HTML 与正文两边，先核对上游或人工修改后手工对齐") from exc
            if ref(previous) != record["synced_body"]:
                raise WorkflowError("正文已另行更新或来源改变，保存两边并手工对齐；不覆盖新正文")
            before = read_json(local(root, previous["snapshot_path"]))
            changed = normalized(before) != normalized(desired)
            if not pending:
                event = decision(root, key, "align", decision_path, permit["actor"], permit["simulation"])
                if event["evidence"]["sha256"] != permit_sha:
                    raise WorkflowError("决定原文在归档期间改变，停止同步")
                pending = {"target": target(record), "body": ref(previous), "author": author,
                           "authorization_sha": permit_sha, "decision_id": event["record_id"],
                           "desired_content_hash": fingerprint(normalized(desired))}
                record = {**record, "sync_pending": pending}
                save_record(root, task_id, record)
            if changed:
                reason = permit["reason"]
                revision = {"base": ref(previous), "trigger": "alignment", "event_id": pending["decision_id"],
                            "reason": reason, "section_reasons": {s["id"]: reason for s in desired["sections"]}}
                try:
                    result = publish(root, desired, author,
                                     [d for d in previous["dependencies"] if d["space"] != "phase2"], revision)
                except Exception:
                    if not local(root, PENDING).exists() and ref(latest(root, key)) == ref(previous):
                        record.pop("sync_pending", None)
                        save_record(root, task_id, record)
                    raise
            else:
                result = previous
        if sha(local(root, record["path"])) != record["observed_sha"] or sha(decision_path) != permit_sha:
            raise WorkflowError("同步期间 HTML 或决定再次变化；已留待恢复记录，不把两边当成一致")
        if not file_valid(root, {"path": result["snapshot_path"], "sha256": result["sha256"]}):
            raise WorkflowError("同步正文快照校验失败，保留待恢复记录")
        if normalized(read_json(local(root, result["snapshot_path"]))) != normalized(desired):
            raise WorkflowError("同步正文回读不一致")
        published = ref(result) != record["synced_body"]
        impact = ("按真实字段路径同步正文；原有下游版本须重新对齐和检核" if published else
                  "仅模型栏目标签或恢复后的相同文字已明确确认；不将标签当无影响，HTML 本版仍须独立检核")
        alignment = {"target": target(record), "old_body": record["synced_body"], "body": ref(result),
                     "actor": permit["actor"], "author_instance": author,
                     "decision_id": pending["decision_id"], "simulation": permit["simulation"],
                     "content_sha": fingerprint(content(data["history"][-1]["snapshot"])),
                     "published": published, "impact_reason": impact}
        if not any(e.get("decision_id") == pending["decision_id"] for e in events(root, "phase4-alignments")):
            append(root, "phase4-alignments", alignment)
        ready = {**record, "synced_body": ref(result), "synced_content_sha": alignment["content_sha"],
                 "pending_body_alignment": False, "impact_reason": impact,
                 "author_instances": sorted(set(record["author_instances"] + [author])),
                 "alignment_decision_id": pending["decision_id"]}
        ready.pop("sync_pending", None)
        save_record(root, task_id, ready)
    current(root, key)
    return {"status": "aligned_pending_independent_review", **alignment}
