"""Explicit human decisions, cumulative automatic revisions, and feedback provenance."""
from pathlib import Path

from phase2_store import (WorkflowError, archive, local, read_json,
                         sha, test_mode)
from phase3_events import append, events
from phase3_io import PENDING, atomic_bytes, preserve, recover, registry
from phase3_sources import file_valid


def valid_event(root, event):
    return bool(event) and all(file_valid(root, event.get(k)) for k in
        ("evidence", "transcript", "clarification") if k == "evidence" or k in event) and (
            not event.get("simulation") or test_mode(root))


def decision(root, target, action, evidence_path, actor, simulation=False):
    from phase3_store import latest, ref
    if action not in {"align", "allow_more", "restore_registered", "resume_pending"}:
        raise WorkflowError("未知决定")
    if not actor.strip() or not Path(evidence_path).read_text(encoding="utf-8").strip():
        raise WorkflowError("必须保留策略师的实际决定原文与身份")
    if simulation and not test_mode(root):
        raise WorkflowError("模拟决定只允许隔离测试项目")
    pending_ref = None
    if action == "resume_pending":
        pending = local(root, PENDING)
        meta = read_json(pending)["meta"]
        if meta["key"] != target:
            raise WorkflowError("待恢复交易不是所选产出")
        pending_ref = {"path": PENDING, "sha256": sha(pending)}
    else:
        meta = latest(root, target)
    count, allowance = budget(root, target)
    observed = []
    if action in {"restore_registered", "resume_pending"}:
        for field in ("path", "markdown_path"):
            path = local(root, meta[field])
            observed.append({"path": meta[field], "sha256": sha(path) if path.is_file() else None,
                             "preserved_path": preserve(root, meta[field])})
    return append(root, "phase3-decisions", {
        "target": ref(meta), "action": action, "actor": actor, "simulation": simulation,
        "previous_automatic_count": count, "allowed_total": max(count, allowance) + 2,
        "observed_files": observed,
        "pending_ref": pending_ref,
        "evidence": archive(root, evidence_path, "evidence")})


def resume_pending(root, decision_id):
    from phase3_store import ref
    event = next((e for e in events(root, "phase3-decisions") if e["record_id"] == decision_id), {})
    if not valid_event(root, event) or event.get("action") != "resume_pending" or not file_valid(
            root, event.get("pending_ref")):
        raise WorkflowError("恢复交易需绑定未改变的交易和实际决定")
    meta = read_json(local(root, PENDING))["meta"]
    if ref(meta) != event["target"]:
        raise WorkflowError("待恢复版本已改变")
    pairs = (("path", "snapshot_path", "sha256"),
             ("markdown_path", "markdown_snapshot", "markdown_sha256"))
    observed = {f["path"]: f["sha256"] for f in event["observed_files"]}
    for pointer, snapshot, hash_field in pairs:
        if not file_valid(root, {"path": meta[snapshot], "sha256": meta[hash_field]}):
            raise WorkflowError("待恢复快照已改变")
        path = local(root, meta[pointer])
        if (sha(path) if path.is_file() else None) not in {observed[meta[pointer]], meta[hash_field]}:
            preserve(root, meta[pointer])
            raise WorkflowError("决定之后文件再次改变；不覆盖，请重新核对")
    for pointer, snapshot, _ in pairs:
        atomic_bytes(local(root, meta[pointer]), local(root, meta[snapshot]).read_bytes())
    return recover(root)


def restore_registered(root, key, decision_id):
    from phase3_store import latest, ref
    meta = latest(root, key)
    event = next((e for e in events(root, "phase3-decisions") if e["record_id"] == decision_id), {})
    if not valid_event(root, event) or event.get("target") != ref(meta) or event.get("action") != "restore_registered":
        raise WorkflowError("恢复登记稿需要绑定当前版本的实际决定")
    for item in event["observed_files"]:
        path = local(root, item["path"])
        if (sha(path) if path.is_file() else None) != item["sha256"]:
            raise WorkflowError("决定之后文件再次改变，请重新核对；不覆盖")
    for dest, snapshot, hash_field in (("path", "snapshot_path", "sha256"),
                                      ("markdown_path", "markdown_snapshot", "markdown_sha256")):
        if sha(local(root, meta[snapshot])) != meta[hash_field]:
            raise WorkflowError("历史快照损坏，不能恢复")
    for dest, snapshot in (("path", "snapshot_path"), ("markdown_path", "markdown_snapshot")):
        atomic_bytes(local(root, meta[dest]), local(root, meta[snapshot]).read_bytes())
    return append(root, "phase3-restorations", {"target": ref(meta), "decision_id": decision_id,
                  "preserved_files": event["observed_files"], "status": "registered_restored",
                  "note": "仅恢复登记指针；人工稿仍保留，可据其内容另建新版本"})


def budget(root, key):
    history = registry(root)["artifacts"].get(key, [])
    count = sum(bool(m.get("revision")) for m in history)
    allowance = 2
    for event in events(root, "phase3-decisions"):
        if event.get("target", {}).get("key") == key and event.get("action") == "allow_more" and valid_event(root, event):
            allowance = max(allowance, event["allowed_total"])
    return count, allowance


def revision_details(root, key, previous, data, revision):
    from phase3_store import ref
    if previous is None:
        if revision:
            raise WorkflowError("首次生成不能伪装成历史修订")
        return None
    if previous.get("revision"):
        reviews = [e for e in events(root, "phase3-reviews") if e.get("target") == ref(previous)]
        if not reviews or not valid_event(root, reviews[-1]):
            raise WorkflowError("上一轮修订尚未完成有效独立复核，不能跳过复核连续改稿")
    count, allowance = budget(root, key)
    if count >= allowance:
        raise WorkflowError(f"自动修订已达 {allowance} 轮，请策略师决定；不清零")
    if not isinstance(revision, dict) or revision.get("base") != ref(previous):
        raise WorkflowError("修订必须绑定当前稿的版本")
    trigger = revision.get("trigger")
    event_id = revision.get("event_id")
    records = events(root, {"feedback": "feedback", "review": "phase3-reviews",
                            "alignment": "phase3-decisions"}.get(trigger, "missing"))
    event = next((e for e in records if e["record_id"] == event_id), {})
    if not valid_event(root, event) or event.get("target") != ref(previous):
        raise WorkflowError("修订缺匹配版本的反馈、检核或对齐授权")
    if trigger == "review" and event.get("status") not in {"returned", "passed_with_yellow", "insufficient_evidence"}:
        raise WorkflowError("此检核没有待处理意见")
    if trigger == "alignment" and event.get("action") not in {"align", "allow_more"}:
        raise WorkflowError("不是下游更新授权")
    if trigger == "feedback" and event.get("status") != "bound":
        raise WorkflowError("反馈尚未唯一绑定")
    old = read_json(local(root, previous["snapshot_path"]))
    old_sections = {s["id"]: s["text"] for s in old["sections"]}
    reasons = revision.get("section_reasons", {})
    changes = []
    for section in data["sections"]:
        before = old_sections.get(section["id"], "")
        if before != section["text"]:
            if not isinstance(reasons.get(section["id"]), str) or not reasons[section["id"]].strip():
                raise WorkflowError("每个正文变更必须说明原因")
            changes.append({"section": section["id"], "before": before, "after": section["text"],
                            "reason": reasons[section["id"]]})
    reason = revision.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise WorkflowError("修订须说明整体原因及非正文变更")
    sources = previous.get("source_files", []) + [
        event[k] for k in ("evidence", "transcript", "clarification") if k in event]
    if count >= 2:
        permits = [e for e in events(root, "phase3-decisions") if
                   e.get("target", {}).get("key") == key and e.get("action") == "allow_more"
                   and e.get("allowed_total", 0) > count and valid_event(root, e)]
        sources.append(permits[-1]["evidence"])
    if any(not file_valid(root, f) for f in sources):
        raise WorkflowError("继承的反馈或授权来源失效，先核对，不丢掉历史依据")
    sources = list({(f["path"], f["sha256"]): f for f in sources}.values())
    return {"base": ref(previous), "trigger": trigger, "event_id": event_id,
            "automatic_round": count + 1, "director_rework": bool(
                trigger == "feedback" and event.get("feedback_role") == "director"),
            "reason": reason, "changes": changes,
            "source_files": sources}
