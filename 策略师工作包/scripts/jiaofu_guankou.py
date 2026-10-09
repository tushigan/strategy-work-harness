#!/usr/bin/env python3
"""检核豁免与发客户关口（U03）。

- 豁免：记录策略师原话、日期、绑定的版本（交付物指纹）与用途（self_view 自看 / external 对外交付）。
  豁免只说明“为什么没检核”，永远不算检核通过。
- 发客户登记：当前版本须有有效独立检核，或有针对“对外交付”的单独豁免原话；否则拒绝并给两条出路。
  只管记录，不阻止人实际发送文件。
"""
import argparse
import json
from pathlib import Path

from phase2_store import WorkflowError, local, now
from standalone_store import digest, history, identifier, text
from workspace_lock import serialized

LOG = "project/records/delivery-gate.jsonl"
PURPOSES = {"self_view": "策略师自看", "external": "对外交付"}


def events(root):
    path = local(root, LOG)
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkflowError(f"交付关口记录第 {number} 行损坏，未修改原件") from exc
            if not isinstance(item, dict) or item.get("schema_version") != 1 or "event" not in item:
                raise WorkflowError(f"交付关口记录第 {number} 行格式无效")
            rows.append(item)
    return rows


def _append(root, event):
    path = local(root, LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": 1, "created_at": now(), **event}
    record["record_id"] = digest({k: v for k, v in record.items() if k != "created_at"})[:32]
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def live_changes(root, deliverables):
    """按实际文件字节重算：返回与登记指纹不符或缺失的交付物路径（外部链接不核）。"""
    import hashlib
    changed = []
    for ref in deliverables:
        if "path" not in ref:
            continue
        try:
            path = local(root, ref["path"])
            ok = path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == ref["sha256"]
        except (OSError, ValueError):
            ok = False
        if not ok:
            changed.append(ref["path"])
    return changed


def version(root, task_id, live=True):
    """当前版本 = 任务最新修订号 + 当前交付物指纹（外部链接按 URL）。
    live=True 时按实际文件字节核对：登记后原地改过的文件不再是“这个版本”，豁免、导出与发客户都拒绝。"""
    identifier(task_id)
    own = [e for e in history(root)[0] if e["task_id"] == task_id]
    if not own:
        raise WorkflowError("找不到任务：" + task_id)
    task = own[-1]["task"]
    files = sorted((r.get("path") or r.get("url"), r.get("sha256")) for r in task["deliverables"])
    if not files:
        raise WorkflowError("当前没有登记交付物，先用 iterate/save 登记要发的版本")
    result = {"task_id": task_id, "revision": own[-1]["revision"], "deliverables": [list(x) for x in files],
              "fingerprint": digest(files)}
    changed = live_changes(root, task["deliverables"])
    if live and changed:
        raise WorkflowError("当前交付物已变化（实际文件与登记指纹不符）：" + "、".join(changed[:5])
                            + "。登记后原地改过的文件不是已登记的版本：先用 standalone_tasks.py iterate/save 登记新版，再豁免/导出/发客户")
    return {**result, "files_changed": changed}


def review_state(root, task_id, light=False):
    """light=True（resume 用）：只核对当前版本的检核记录及报告原件，不重读来源与成品，避免日常恢复多读原件。"""
    if light:
        try:
            from phase6_events import events as review_events, decision_valid
            from phase6_targets import catalog
            target = catalog(root)[f"standalone/{task_id}"]["target"]
            records = [r for r in review_events(root, "standalone_review") if r.get("target") == target]
            last = records[-1] if records else None
            if last and last.get("status") in {"passed", "passed_with_yellow"} and decision_valid(root, last):
                # F07（v1.7.3）：再按 gate 的当前规则做静态判断（报告格式与视觉证据字段），不渲染、不读来源原件；
                # 过不了就不说“有效”，与发客户 / 导出时 gate 的口径一致。
                from phase2_store import read_json
                from standalone_visual import static_problem
                reason = static_problem(root, catalog(root)[f"standalone/{task_id}"]["meta"], read_json(local(root, last["evidence"]["path"])))
                if reason:
                    return {"reviewed": False, "record_id": last["record_id"], "reason": reason,
                            "review_revision": target.get("version"), "review_date": str(last.get("created_at", ""))[:10]}
                return {"reviewed": True, "record_id": last["record_id"], "checked": "记录与报告原件、视觉证据字段（resume 轻量核对）"}
            return {"reviewed": False}
        except (ValueError, OSError, KeyError, IndexError):
            return {"reviewed": False}
    from standalone_review import gate
    try:
        gate(root, task_id)
        from phase6_events import events as review_events
        from phase6_targets import catalog
        target = catalog(root)[f"standalone/{task_id}"]["target"]
        record = [r for r in review_events(root, "standalone_review") if r.get("target") == target][-1]
        return {"reviewed": True, "record_id": record["record_id"]}
    except (ValueError, OSError, KeyError, IndexError):
        return {"reviewed": False}


def _same_words(a, b):
    """同一句话：忽略标点、空白、全半角与大小写（加个句号不算另一句）。"""
    import re, unicodedata
    fold = lambda t: re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", t)).casefold()
    return fold(a) == fold(b)


def waive(root, task_id, purpose, words, actor):
    from registration_guard import actor_check
    actor_check(actor); text(words, "策略师豁免原话")
    if purpose not in PURPOSES:
        raise WorkflowError("用途须为 self_view（策略师自看）或 external（对外交付）")
    with serialized(root):
        current = version(root, task_id)
        rows = events(root)
        for e in rows:
            if (e["event"] == "waiver" and e["task_id"] == task_id and e["fingerprint"] == current["fingerprint"]
                    and e["purpose"] == purpose and _same_words(e["words"], words)):
                return {**e, "status": "existing"}
        if purpose == "external" and any(e["event"] == "waiver" and e["task_id"] == task_id and e["purpose"] == "self_view"
                                         and e["fingerprint"] == current["fingerprint"] and _same_words(e["words"], words) for e in rows):
            raise WorkflowError("这句原话已登记为“策略师自看”豁免，不能再当作对外交付豁免；"
                                "须策略师另外明确说出“不检核直接发客户”之类的对外原话后再登记")
        current.pop("files_changed", None)
        return _append(root, {"event": "waiver", **current, "purpose": purpose, "purpose_label": PURPOSES[purpose],
                              "words": words, "actor": actor,
                              "notice": "豁免只记录未检核的原因，不是检核通过"})


def status(root, task_id, light=False, file_issues=None):
    """file_issues：resume 已核对过的当前来源/成品问题（复用，避免重复读原件）；有问题时不显示“已有有效检核”。"""
    current = version(root, task_id, live=False)
    review = review_state(root, task_id, light)
    mine = [e for e in events(root) if e["task_id"] == task_id and e.get("fingerprint") == current["fingerprint"]]
    waivers = [e for e in mine if e["event"] == "waiver"]
    delivered = [e for e in mine if e["event"] == "delivered"]
    exports = [e for e in mine if e["event"] == "external_version"]
    stale = list(current["files_changed"]) + [i["path"] for i in (file_issues or []) if i.get("path")]
    if review["reviewed"] and stale:
        review = {**review, "reviewed": False, "stale_files": sorted(set(stale))}
        label = "检核登记过，但当前来源或成品已变化，需重新核对后才算有效（发客户以 deliver 核对为准）"
    elif stale:
        label = "该版本未检核；且当前文件与登记不符：" + "、".join(sorted(set(stale))[:3])
    elif review["reviewed"]:
        label = "当前版本已有有效独立检核"
    elif review.get("reason"):
        label = (f"检核记录在（r{review.get('review_revision') or current['revision']}，{review.get('review_date')}），"
                 f"但按当前规则不完整：{review['reason']}；发客户、导出前须重检或补视觉证据")
    elif waivers:
        label = "该版本未检核，原因：策略师豁免（" + "、".join(sorted({w["purpose_label"] for w in waivers})) + "）"
    else:
        label = "该版本未检核"
    pending = _tradeoffs(root, task_id)
    return {"task_id": task_id, "revision": current["revision"], "fingerprint": current["fingerprint"],
            "review": review, "review_label": label, "waivers": [{k: w[k] for k in ("purpose", "words", "created_at")} for w in waivers],
            "files_changed": current["files_changed"],
            "external_versions": [{"record_id": x["record_id"], **{k: x[k] for k in ("files", "scan", "created_at")},
                                   "intact": not live_changes(root, x["files"])} for x in exports],
            "delivered": [{k: d.get(k) for k in ("to", "basis", "created_at", "external_files")} for d in delivered],
            "strategist_pending": pending}


def _tradeoffs(root, task_id):
    """作者交回时写明的“可讨论取舍”（U07），交付时首先列出。"""
    try:
        from agent_dispatch import read_log, replay
    except ImportError:
        return []
    items = []
    for d in replay(read_log(root)).values():
        if d.get("task_id") == task_id and d.get("tradeoffs"):
            items.extend({"dispatch_id": d["dispatch_id"], "tradeoff": t} for t in d["tradeoffs"])
    return items


def deliver(root, task_id, to, actor, words=None, export_record=None):
    from registration_guard import actor_check
    actor_check(actor); text(to, "发送对象")
    with serialized(root):
        current = version(root, task_id)
        current.pop("files_changed", None)
        review = review_state(root, task_id)
        external = [e for e in events(root) if e["event"] == "waiver" and e["task_id"] == task_id
                    and e["fingerprint"] == current["fingerprint"] and e["purpose"] == "external"]
        if review["reviewed"]:
            basis = {"review_record_id": review["record_id"]}
        elif external:
            basis = {"external_waiver": external[-1]["record_id"], "words": external[-1]["words"]}
        else:
            raise WorkflowError(
                "拒绝登记“已对外交付”：当前版本没有有效独立检核，也没有针对对外交付的豁免原话。两条出路："
                f"① 运行 jianhe_zhunbei.py --task-id {task_id} 准备独立检核，通过后再登记；"
                "② 请策略师明确说出“不检核直接发客户”的原话，用 jiaofu_guankou.py waive --purpose external 记录后再登记。"
                "（自看豁免不能用于对外交付）")
        rows = events(root)
        exports = [e for e in rows if e["event"] == "external_version" and e["task_id"] == task_id and e["fingerprint"] == current["fingerprint"]]
        if export_record:
            chosen = next((e for e in exports if e["record_id"] == export_record), None)
            if chosen is None:
                raise WorkflowError("--export-record：不是当前版本已登记的对外版本")
        else:
            chosen = next((e for e in reversed(exports) if not live_changes(root, e["files"])), None)
            if chosen is None and exports:
                raise WorkflowError("当前版本已登记的对外导出文件都已被改动或缺失，不能登记发送；重新导出（deck_export.py external），"
                                    "或用 --export-record 指定未改动的那一份")
        if chosen is not None and live_changes(root, chosen["files"]):
            raise WorkflowError("所选对外导出文件已被改动或缺失，不能作为发出的文件登记；重新导出")
        sent = {"record_id": chosen["record_id"], "files": chosen["files"]} if chosen else None
        for e in rows:
            if e["event"] == "delivered" and e["task_id"] == task_id and e["fingerprint"] == current["fingerprint"] and e["to"] == to \
                    and e.get("external_files", sent) == sent:
                return {**e, "status": "existing"}
        urls = [d for d, _ in current["deliverables"] if isinstance(d, str) and d.startswith(("http:", "https:"))]
        record = _append(root, {"event": "delivered", **current, "to": to, "basis": basis, "words": words, "actor": actor,
                                "external_files": sent,
                                "notice": "只记录发送事实，不代表客户批准" + ("" if sent else "；未指明发出的对外导出文件（当前版本未导出）")
                                + ("；外部文档内容未核对（工作包只认链接），建议先 waibu_wendang.py compare 比对当前内容" if urls else "")})
        return {**record, "strategist_pending_first": _tradeoffs(root, task_id)}


def register_external(root, task_id, files, scan, actor, source=None):
    """对外导出产物登记为“对外版本”（U08），与当前版本指纹绑定；仍须经 deliver 关口登记发送。"""
    with serialized(root):
        current = version(root, task_id)
        current.pop("files_changed", None)
        if source and [source.get("path"), source.get("sha256")] not in current["deliverables"]:
            raise WorkflowError("导出期间当前版本已变化，未登记对外版本")
        return _append(root, {"event": "external_version", **current, "files": files, "scan": scan, "actor": actor})


def delivered(root, task_id):
    try:
        current = version(root, task_id, live=False)
    except (ValueError, OSError):
        return False
    return any(e["event"] == "delivered" and e["task_id"] == task_id and e["fingerprint"] == current["fingerprint"]
               for e in events(root))


def main():
    parser = argparse.ArgumentParser(description="检核豁免登记与发客户关口（只管记录，不阻止实际发送）")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("waive", "deliver", "status"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", type=Path, required=True)
        p.add_argument("--task-id", required=True)
        p.add_argument("--json", action="store_true")
        if name != "status":
            p.add_argument("--actor", required=True)
        if name == "waive":
            p.add_argument("--purpose", choices=sorted(PURPOSES), required=True)
            p.add_argument("--words", required=True)
        if name == "deliver":
            p.add_argument("--to", required=True)
            p.add_argument("--words")
            p.add_argument("--export-record", help="发出的对外版本 record_id；默认当前版本最新且文件未变的对外导出")
    args = parser.parse_args()
    root = args.workspace.resolve()
    if args.command == "waive":
        result = waive(root, args.task_id, args.purpose, args.words, args.actor)
    elif args.command == "deliver":
        result = deliver(root, args.task_id, args.to, args.actor, args.words, args.export_record)
    else:
        result = status(root, args.task_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main

    def main():
        return cli(_business_main, __file__)

    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
