"""Preserve revision reasons and explicit human allowances without resetting counts."""
import uuid
from pathlib import Path

import phase5_common as c
from phase2_store import WorkflowError


def allow_more(root, key, evidence_path, actor, simulation=False):
    if not isinstance(actor, str) or not actor.strip() or type(simulation) is not bool or (
            simulation and not c.test_mode(root)):
        raise WorkflowError("增加修订轮次须有实际确认者；模拟仅限隔离案例")
    meta = c.latest(root, key)
    c.check_files(root, meta)
    evidence = Path(evidence_path)
    if not evidence.is_file() or not evidence.read_text(encoding="utf-8").strip():
        raise WorkflowError("须保留人工增加轮次的授权原文")
    relative = f"project/reviews/phase5/decisions/{uuid.uuid4().hex}.txt"
    c.atomic_bytes(c.local(root, relative), evidence.read_bytes())
    return c.append_event(root, "allow_more", {"target": c.ref(meta), "actor": actor,
        "simulation": simulation, "evidence": {"path": relative, "sha256": c.sha(c.local(root, relative))}})


def revision_details(root, previous, history, revision):
    if previous is None:
        if revision:
            raise WorkflowError("首次生成不能伪装成修订")
        return None, []
    if not isinstance(revision, dict) or revision.get("base") != c.ref(previous):
        raise WorkflowError("修订须精确绑定上一版本")
    for field in ("reason", "trigger"):
        if not isinstance(revision.get(field), str) or not revision[field].strip():
            raise WorkflowError(f"修订缺少 {field}")
    if not isinstance(revision.get("changes"), list) or not revision["changes"] or any(
            not isinstance(x, str) or not x.strip() for x in revision["changes"]):
        raise WorkflowError("修订须记录具体改动")
    sources = list(previous.get("source_files", []))
    rounds = sum(bool(x.get("revision")) for x in history)
    if previous.get("revision"):
        review = c.last_review(root, c.ref(previous))
        if not c.valid_event_file(root, review.get("evidence")) or (
                review.get("simulation") and not c.test_mode(root)):
            raise WorkflowError("上一轮修订尚无有效独立检核，不能连续修改")
    if rounds >= 2:
        decisions = [x for x in c.events(root, "allow_more") if x.get("target") == c.ref(previous)]
        decision = decisions[-1] if decisions else {}
        if decision.get("record_id") != revision.get("allowance_id") or not c.valid_event_file(
                root, decision.get("evidence")) or decision.get("simulation") and not c.test_mode(root):
            raise WorkflowError("自动修订已达两轮；保留历史，须人工明确增加一轮")
        sources.append(decision["evidence"])
    detail = {**revision, "automatic_round": rounds + 1}
    return detail, sources
