#!/usr/bin/env python3
"""检核豁免与发客户关口（U03）。

- 豁免：记录策略师原话、日期、绑定的版本（交付物指纹）与用途（self_view 自看 / external 对外交付）。
  豁免只说明“为什么没检核”，永远不算检核通过。
- 发客户登记：当前版本须有有效独立检核，或有针对“对外交付”的单独豁免原话，或（v1.7.5 G02 先交后检）已登记交给策略师且
  已派背后检核；否则拒绝并给出路。只管记录，不阻止人实际发送文件。
- 交给策略师（v1.7.5 G01）：handover 事件记任务、版本（交付物指纹）、时间、检核状态快照、“距上次检查通过隔 N 版”，并给一行
  给策略师的状态行；在线改稿（standalone_tasks iterate）登记成功后自动写一条。
- 先交后检的结果回报（v1.7.5 G02）：交出后登记的检核发现问题（红灯、黄灯、资料不够）时生成给策略师的白话提醒，续做状态置顶，
  策略师确认（ack，记原话）前一直显示；绿灯在下一次状态行顺带一句。
- 豁免分三种来源（v1.7.5 G07）：strategist 策略师明确不检核（须原话）/ not_run 检核没跑成（须原因：登记卡住、额度、超时、其他）/
  retroactive 事后补登记；旧记录没有来源字段按 strategist 读，不改原件。同一任务连续两次检核没跑成即提示停下报告，背后检核不再重派。
- 先交付后补登记（v1.7.5 G06）：deliver --retroactive --sent-at <策略师说的发出时间> --words <原话> [--versions 版本号…]，
  逐版一条，同时记实际发出时间（sent_at）与补登记时间（recorded_at）；一句原话覆盖多版须一次用 --versions 列全。
- 最终版补检（v1.7.5 G07；候选 r5 按用户 2026-10-11 拍板简化）：策略师标为最终版 / 客户确认（final）的版本——同一任务只按最新一条记录——
  本身或交付物与它完全相同的版本没有一次检查通过的记录时，续做状态只摆一行事实；策略师说不用查（recheck --decision dont，记原话）后不再出现。
  这是唯一的开关：不记“做”、不做承诺。出不出、说什么只由 recheck_state 一个判定定；没有成品的版本不能标最终版。
- 未登记的对外版本（v1.7.5 G09）：project/exports/对外/ 下没有登记过、又比该任务最近一次交出 / 发客户 / 对外导出登记更新的文件。
"""
import argparse
import json
from pathlib import Path

from phase2_store import WorkflowError, local, now
from standalone_store import digest, history, identifier, text
from workspace_lock import serialized

LOG = "project/records/delivery-gate.jsonl"
PURPOSES = {"self_view": "策略师自看", "external": "对外交付"}
WAIVER_SOURCES = {"strategist": "策略师明确不检核", "not_run": "检核没跑成", "retroactive": "事后补登记"}
NOT_RUN_REASONS = {"registration": "登记卡住", "quota": "额度用完", "timeout": "超时", "other": "其他"}


def waiver_source(event):
    """G07：豁免来源；旧记录没有该字段按“策略师明确不检核”读（不改原件）。"""
    return event.get("waiver_source") or "strategist"


def send_waivers(rows, task_id, fingerprint):
    """能当发客户依据的豁免（v1.7.5 批次二返修 #1）：只认“策略师明确不检核”的对外豁免（旧记录没有来源字段按策略师读）。
    “检核没跑成”“事后补登记”只说明为什么没检核，不能当发客户依据。普通发客户与补登记共用这一口径。"""
    return [e for e in rows if e["event"] == "waiver" and e["task_id"] == task_id and e["fingerprint"] == fingerprint
            and e["purpose"] == "external" and waiver_source(e) == "strategist"]


def waiver_label(event):
    """豁免在续做状态里的说法。策略师豁免沿用 v1.7.4 原文（旧记录标签不变）。"""
    source = waiver_source(event)
    if source == "not_run":
        return "检核没跑成（" + NOT_RUN_REASONS.get(event.get("reason_code"), "原因未写") + "）"
    if source == "retroactive":
        return "事后补登记（" + event.get("purpose_label", "") + "）"
    return "策略师豁免（" + event.get("purpose_label", "") + "）"


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
            item = catalog(root)[f"standalone/{task_id}"]
            target = item["target"]
            records = [r for r in review_events(root, "standalone_review") if r.get("target") == target]
            last = records[-1] if records else None
            if last and last.get("status") in {"passed", "passed_with_yellow"} and decision_valid(root, last):
                # F07（v1.7.3）：再按 gate 的当前规则做静态判断，不渲染、不读来源原件；过不了就不说“有效”。
                # v1.7.6 H11：与 gate 共用 standalone_review.static_gate_problem——报告原件指纹（上面 decision_valid）、
                # 检核员身份、模拟检核、派工用途（以登记时核对的为准）、报告格式与视觉证据字段。
                from phase2_store import read_json
                from standalone_review import static_gate_problem
                reason = static_gate_problem(root, target, item, read_json(local(root, last["evidence"]["path"])), last)
                if reason:
                    return {"reviewed": False, "record_id": last["record_id"], "reason": reason,
                            "review_revision": target.get("version"), "review_date": str(last.get("created_at", ""))[:10]}
                return {"reviewed": True, "record_id": last["record_id"],
                        "checked": "记录与报告原件、检核员身份、派工用途、视觉证据字段（resume 轻量核对，与 gate 共用身份、用途、格式与视觉字段判断）"}
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


def waive(root, task_id, purpose, words, actor, source="strategist", reason_code=None):
    from registration_guard import actor_check
    actor_check(actor)
    if source not in WAIVER_SOURCES:
        raise WorkflowError("豁免来源不认识，这次没有登记。技术附录（给 Agent）：--source 须为 " + "、".join(WAIVER_SOURCES))
    text(words, "策略师豁免原话" if source == "strategist" else "情况说明（策略师原话或检核没跑成的经过）")
    if purpose not in PURPOSES:
        raise WorkflowError("用途须为 self_view（策略师自看）或 external（对外交付）")
    if source == "not_run" and reason_code not in NOT_RUN_REASONS:
        raise WorkflowError("检核没跑成要写明原因（登记卡住、额度用完、超时、其他），这次没有登记。技术附录（给 Agent）：--source not_run 须带 "
                            "--reason-code " + " / ".join(NOT_RUN_REASONS))
    if source != "not_run" and reason_code is not None:
        raise WorkflowError("只有“检核没跑成”需要写原因代码。技术附录（给 Agent）：--reason-code 只用于 --source not_run")
    with serialized(root):
        current = version(root, task_id)
        rows = events(root)
        for e in rows:
            if (e["event"] == "waiver" and e["task_id"] == task_id and e["fingerprint"] == current["fingerprint"]
                    and e["purpose"] == purpose and _same_words(e["words"], words) and waiver_source(e) == source
                    and e.get("reason_code") == reason_code):
                return _with_stop(root, task_id, actor, {**e, "status": "existing"}) if source == "not_run" else {**e, "status": "existing"}
        if source == "strategist" and purpose == "external" and any(
                e["event"] == "waiver" and e["task_id"] == task_id and e["purpose"] == "self_view"
                and e["fingerprint"] == current["fingerprint"] and _same_words(e["words"], words) for e in rows):
            raise WorkflowError("这句原话已登记为“策略师自看”豁免，不能再当作对外交付豁免；"
                                "须策略师另外明确说出“不检核直接发客户”之类的对外原话后再登记")
        current.pop("files_changed", None)
        extra = {"waiver_source": source, "waiver_source_label": WAIVER_SOURCES[source],
                 **({"reason_code": reason_code, "reason_label": NOT_RUN_REASONS[reason_code]} if reason_code else {})}
        record = _append(root, {"event": "waiver", **current, "purpose": purpose, "purpose_label": PURPOSES[purpose],
                                "words": words, "actor": actor, **extra,
                                "notice": "豁免只记录未检核的原因，不是检核通过"})
        return _with_stop(root, task_id, actor, record) if source == "not_run" else record


def continue_command(task_id, actor):
    """连续没跑成停下后，策略师说继续时要跑的命令（不带 --behind，明确准备一次检核）。"""
    import shlex
    return f"python scripts/jianhe_zhunbei.py --workspace . --task-id {shlex.quote(task_id)} --actor {shlex.quote(actor)}"


def _with_stop(root, task_id, actor, record):
    """G02 ④：连续两次没跑成即停下（新登记与原样重试都给出）。判停下之前先收尾旧版本未结束的检核分派（宿主没派成），
    属收尾、不算重派；停下提示写明策略师说继续后要跑的命令（技术附录）。"""
    misses = not_run_streak(root, task_id)
    if misses < 2:
        return record
    closed = close_old_reviews(root, task_id, actor, "host_failed", f"连续 {misses} 次检查没跑成，先停下（收尾，不算重派）")
    return {**record, **({"closed_reviews": closed} if closed else {}), "stop": {
        "consecutive_not_run": misses,
        "message": f"这项工作已经连续 {misses} 次检查没跑成，先停下来告诉策略师，不再自动重派检查；"
                   "要不要再试、改天再查，还是这版不查，由策略师决定。",
        "continue_command": continue_command(task_id, actor),
        "technical_appendix": f"技术附录（给 Agent）：策略师说继续（再试一次）时运行 {continue_command(task_id, actor)}。"
                              "不带 --behind：明确准备一次检核；登记到检核结果之前背后检核不再自动重派。"}}


def close_old_reviews(root, task_id, actor, code, why):
    """收尾同一任务旧版本未结束的检核分派（v1.7.5 批次二返修 #4、#5；背后检核取代旧检核共用）：
    该版检核结果已登记的记 not_needed（分派收尾），未登记的记 code（交出新版 / 背后检核取代时 superseded，连续没跑成停下时 host_failed）。
    已登记的检核结果原样保留；只收尾，不派新检核；当前版本的检核分派不动。"""
    from agent_dispatch import cancel
    target = _target(root, task_id)
    reviews = _reviews(root, task_id)
    closed = []
    for item in _reviewer_dispatches(root, task_id):
        if item["review_target"] == target:
            continue
        old = item["review_target"].get("version")
        registered = [r for r in reviews if r.get("target") == item["review_target"]]
        reason_code = "not_needed" if registered else code
        reason = (f"第 {old} 版的检核结果已登记，分派收尾：{why}" if registered else f"第 {old} 版的检核没有做完就收尾：{why}")
        cancel(root, item["dispatch_id"], reason, actor, reason_code=reason_code)
        closed.append({"dispatch_id": item["dispatch_id"], "version": old, "reason_code": reason_code,
                       **({"registered_review_kept": registered[-1]["record_id"]} if registered else {})})
    return closed


def not_run_streak(root, task_id):
    """G02 ④：同一任务从最近一次登记检核结果之后，连续登记了几次“检核没跑成”。"""
    marks = [(e["created_at"], "miss") for e in events(root)
             if e["event"] == "waiver" and e["task_id"] == task_id and waiver_source(e) == "not_run"]
    marks += [(r.get("created_at", ""), "done") for r in _reviews(root, task_id)]
    streak = 0
    for _, kind in sorted(marks):
        streak = streak + 1 if kind == "miss" else 0
    return streak


def status(root, task_id, light=False, file_issues=None):
    """file_issues：resume 已核对过的当前来源/成品问题（复用，避免重复读原件）；有问题时不显示“已有有效检核”。"""
    current = version(root, task_id, live=False)
    review = review_state(root, task_id, light)
    mine = [e for e in events(root) if e["task_id"] == task_id and e.get("fingerprint") == current["fingerprint"]]
    waivers = [e for e in mine if e["event"] == "waiver"]
    delivered = [e for e in mine if e["event"] == "delivered"]
    exports = [e for e in mine if e["event"] == "external_version"]
    stale = list(current["files_changed"]) + [i["path"] for i in (file_issues or []) if i.get("path")]
    try:
        target = _target(root, task_id)
    except (KeyError, OSError, ValueError, TypeError):
        target = None
    behind = behind_label(root, task_id, current, target) if target else None
    if review["reviewed"] and stale:
        review = {**review, "reviewed": False, "stale_files": sorted(set(stale))}
        label = "检核登记过，但当前来源或成品已变化，需重新核对后才算有效（发客户以 deliver 核对为准）"
    elif stale:
        label = "该版本未检核；且当前文件与登记不符：" + "、".join(sorted(set(stale))[:3])
    elif review["reviewed"]:
        label = "当前版本已有有效独立检核"
    elif behind:
        label = behind
    elif review.get("reason"):
        label = (f"检核记录在（r{review.get('review_revision') or current['revision']}，{review.get('review_date')}），"
                 f"但按当前规则不完整：{review['reason']}；发客户、导出前须重检或补视觉证据")
    elif waivers:
        label = "该版本未检核，原因：" + "、".join(sorted({waiver_label(w) for w in waivers}))
    elif memory_only_since_pass(root, task_id):
        label = "该版本未检核（只接受了新的项目记忆、成品没变；发客户前仍要再检查一次，会接着上次的结果准备）"
    else:
        label = "该版本未检核"
    pending = _tradeoffs(root, task_id)
    index = _fingerprint_index(root, task_id) if delivered else {}
    return {"task_id": task_id, "revision": current["revision"], "fingerprint": current["fingerprint"],
            "review": review, "review_label": label,
            "waivers": [{**{k: w[k] for k in ("purpose", "words", "created_at")}, "source": waiver_source(w), "label": waiver_label(w)} for w in waivers],
            "files_changed": current["files_changed"],
            "external_versions": [{"record_id": x["record_id"], **{k: x[k] for k in ("files", "scan", "created_at")},
                                   "intact": not live_changes(root, x["files"])} for x in exports],
            "delivered": [{**{k: d.get(k) for k in ("to", "basis", "created_at", "external_files")}, "when": delivery_when(d),
                           "version": record_version(d, index)} for d in delivered],
            "deliveries": deliveries(root, task_id),
            "strategist_pending": pending, **(progress(root, task_id, target) if target else {})}


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


def pptx_checks(root, current, sent):
    """G13（v1.7.5）：当前版本交付物里的 PPTX 与对外 HTML 对照（页数、每页文字）。有选定的对外导出时对它的 HTML，否则对交付物里
    同名（或唯一一份）HTML；配不上的、打不开的记“未核对”（unchecked），不冒充一致。没有 PPTX 返回 []。"""
    from pptx_hedui import compare, pair
    paths = [d for d, _ in current["deliverables"] if isinstance(d, str) and not d.startswith(("http:", "https:"))]
    pptx = [p for p in paths if p.lower().endswith(".pptx")]
    if not pptx:
        return []
    exported = [f["path"] for f in (sent or {}).get("files", []) if f["path"].lower().endswith((".html", ".htm"))]
    pairs, lone = ([(pptx[0], exported[0])], []) if exported and len(pptx) == 1 else pair(paths)
    out = []
    for pptx_path, html_path in pairs:
        try:
            result = compare(local(root, pptx_path), local(root, html_path))
        except Exception as exc:  # noqa: BLE001 —— 批次三返修 r2 #2：PPTX 解析出任何错都记“未核对”，发客户登记不因此抛出
            result = {"status": "unchecked", "reason": f"读不出来（{type(exc).__name__}）"}
        out.append({**result, "pptx": pptx_path, "html": html_path})
    out += [{"status": "unchecked", "pptx": p, "html": None, "reason": "这项工作里没有能对应的网页版"} for p in lone]
    return out


def _has_platform_page(root, current, sent):
    """交付物（或选定的对外导出文件）里有没有登记过的平台提报页（按文件指纹对）。读不出来就当没有，不拦发客户。"""
    try:
        from pingtai_kuaizhao import snapshots
        pages = {s["html"]["sha256"] for s in snapshots(root)}
        if not pages:
            return False
        shas = {sha for _, sha in current["deliverables"]} | {f.get("sha256") for f in (sent or {}).get("files", []) if isinstance(f, dict)}
        return bool(pages & shas)
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return False


def platform_recheck_words():
    """“需复核”标签 → 发客户被拦时给策略师的短说法。全盘细查返修 P2-4：标签从平台快照模块导入，不在这里抄字面量（标签改了不会悄悄失配）。"""
    from pingtai_kuaizhao import STALE_LABEL, UNVERIFIED_LABEL, CHANGED_LABEL, COMPETITORS_LABEL
    return {STALE_LABEL: "平台上的数据在你核对之后又更新了", UNVERIFIED_LABEL: "已绑定，但核对没通过",
            CHANGED_LABEL: "核对之后这份文件又被改过", COMPETITORS_LABEL: "核对之后竞品对照表又被改过"}


def platform_recheck_names(rows):
    words = platform_recheck_words()
    return "、".join(f"{r['path'].rsplit('/', 1)[-1]}（{words.get(r['label'], r['label'])}）" for r in rows)


def _platform_unbound_note(root, task_id, current):
    """U2（全盘细查返修）：同一任务以前的版本有交付物绑定过平台快照、这一版的交付物一个都没绑定 → 提醒一次（不拦；从没绑定过不提醒）。
    记录读不出来时不提醒（拦不拦由 _platform_recheck 管）。"""
    try:
        from pingtai_kuaizhao import _events as platform_events, LOG as PLATFORM_LOG
        if not local(root, PLATFORM_LOG).exists():
            return None
        bound = {e["path"] for e in platform_events(root) if e["event"] == "bind" and e.get("kind") in {"deck", "matrix"}}
        now_paths = {d for d, _ in current["deliverables"] if isinstance(d, str)}
        before = {d for event in _versions(root, task_id).values() for d, _ in _files(event) if isinstance(d, str)} - now_paths
    except (ImportError, WorkflowError, OSError, ValueError, KeyError, TypeError):
        return None
    if not (before & bound) or (now_paths & bound):
        return None
    return ("这项工作以前的版本里有和平台测算数据绑定核对过的成果，这一版的交付物都没有和平台快照核对；"
            "如果里面有平台的价格、规格、利润，发之前先核对一下")


def _platform_recheck(root, current):
    """交付物里有绑定了平台快照的成果（演示稿、矩阵表），但它现在“需复核”（平台数据更新了、上次核对没通过、核对后文件又被改过）→ 返回这些行。
    没绑定过的不拦（本包不知道它含不含平台数字）；从没登记过平台快照（没有记录文件）也不拦。
    v1.7.6 W8：记录文件在、却读不出来（损坏、格式不对）时不放行——拒绝记发客户，先修好记录或重新核对（批次四曾吞掉异常放行）。"""
    try:
        from pingtai_kuaizhao import needs_recheck, LOG as PLATFORM_LOG
    except ImportError:
        return []
    if not local(root, PLATFORM_LOG).exists():
        return []
    paths = {d for d, _ in current["deliverables"] if isinstance(d, str)}
    try:
        return [r for r in needs_recheck(root) if r["path"] in paths]
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise WorkflowError(
            "这一版还不能记为已发给客户：平台快照的记录读不出来，没法确认里面的价格、规格、利润和平台对得上。"
            "先把这份记录修好（或从备份恢复），再重新核对、绑定，之后再记发客户。"
            f"技术附录（给 Agent）：{PLATFORM_LOG} 存在但读取失败（{type(exc).__name__}: {str(exc)[:200]}）；"
            "v1.7.6 W8 起不再当作没有绑定放行。不改原件：核对损坏行后修复，或重新 register / bind（bind 当场核对）。") from exc


def deliver(root, task_id, to, actor, words=None, export_record=None, pptx_words=None):
    from registration_guard import actor_check
    actor_check(actor); text(to, "发送对象")
    with serialized(root):
        current = version(root, task_id)
        current.pop("files_changed", None)
        review = review_state(root, task_id)
        rows = events(root)
        external = send_waivers(rows, task_id, current["fingerprint"])
        behind = None if review["reviewed"] or external else behind_basis(root, task_id, current)
        if review["reviewed"]:
            basis = {"review_record_id": review["record_id"]}
        elif external:
            basis = {"external_waiver": external[-1]["record_id"], "words": external[-1]["words"]}
        elif behind:
            basis = behind  # G02（v1.7.5）先交后检：已交给策略师且已派背后检核；标签照实写，不算检核通过
        else:
            others = sorted({waiver_source(e) for e in rows if e["event"] == "waiver" and e["task_id"] == task_id
                             and e["fingerprint"] == current["fingerprint"] and waiver_source(e) != "strategist"})
            memory_only = memory_only_since_pass(root, task_id)
            raise WorkflowError(
                "这一版还不能记为已发给客户：这版没检查过，也没有你说的不用检查。"
                + (memory_only_text(root, task_id, memory_only) if memory_only else "")
                + ("已记下的“检查没跑成”或“事后补登记”只说明为什么没检查，不能当作发给客户的依据。" if others else "")
                + "三条出路：① 先做一次检查，通过后再记；② 你明确说一句“这版不用检查，直接发客户”之类的话，记下后再记"
                "（只说过自己先看、不用检查的，不能用来发客户）；③ 这一版交给你之后派了背后检查，也可以记，会照实写“已交出、检查中”。"
                "技术附录（给 Agent）：拒绝登记“已对外交付”——当前版本没有有效独立检核，也没有策略师来源的对外豁免原话。"
                f"三条出路（v1.7.4 原有两条出路①②，v1.7.5 加先交后检③）：① 运行 jianhe_zhunbei.py --task-id {task_id} 准备独立检核，通过后再 deliver；"
                "② 策略师明确说出对外原话后，用 jiaofu_guankou.py waive --purpose external --words \"<原话>\" 记录再 deliver"
                "（自看豁免 self_view 不能用于对外交付；--source not_run / retroactive 的豁免不算发客户依据"
                + ("，本版现有：" + "、".join(others) if others else "") + "）；"
                f"③ 先交后检（v1.7.5）：这一版已交给策略师（jiaofu_guankou.py handover，在线改稿自动登记）并已派背后检核"
                f"（jianhe_zhunbei.py --task-id {task_id} --behind）后也可 deliver，标签照实写“已交出、检核中”。"
                + ("（v1.7.6 H10：与最近一次检核通过的版本相比只是显式接受了 project/records 下的追加式记录；任务版本签名含来源指纹，"
                   "接受新记忆即新版本，发客户前须再检核，jianhe_zhunbei 按增量准备）" if memory_only else ""))
        recheck = _platform_recheck(root, current)
        if recheck:
            names = platform_recheck_names(recheck)
            raise WorkflowError(
                "这一版还不能记为已发给客户：里面绑定了产品线测算平台数字的成果还没和平台数据对上——" + names + "。"
                "发出去的价格、规格、利润要和平台上最新保存的一致：先重新核对，核对通过后再记发客户。"
                "技术附录（给 Agent）：交付物含已绑定平台快照的成果但绑定状态需复核（pingtai_kuaizhao.needs_recheck）："
                + json.dumps([{k: r[k] for k in ("path", "kind", "bound_to", "latest", "label")} for r in recheck], ensure_ascii=False)
                + "；演示稿运行 juzhen_hedui.py deck、矩阵表运行 juzhen_hedui.py roles，对得上后用 pingtai_kuaizhao.py bind 重新绑定到最新一版快照（bind 当场核对，通过才记已核对）")
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
        number = _target(root, task_id)["version"]  # P2-3：普通发客户记录也记检核所认的版本号，按版本去重（改回旧文件是新的一版）
        index = _fingerprint_index(root, task_id)
        for e in rows:
            if e["event"] == "delivered" and e["task_id"] == task_id and not e.get("retroactive") and e["to"] == to \
                    and record_version(e, index) == number and e["fingerprint"] == current["fingerprint"] \
                    and e.get("external_files", sent) == sent:
                return {**e, "status": "existing", **_shown_echo(root, task_id, number, to)}
        checks = pptx_checks(root, current, sent)
        different = [c for c in checks if c["status"] == "different"]
        if different and not (pptx_words and pptx_words.strip()):
            from pptx_hedui import plain as pptx_plain
            raise WorkflowError("这一版还不能记为已发给客户。\n" + "\n".join(pptx_plain(c) for c in different)  # 候选 r2 #7：三段式各占一行
                                + "\n技术附录（给 Agent）：PPTX 与对外 HTML 页数或文字不一致（pptx_hedui.compare）："
                                + json.dumps([{k: c.get(k) for k in ("pptx", "html", "pptx_pages", "html_pages")} for c in different], ensure_ascii=False)
                                + "；改好 PPTX 登记新版后再 deliver，或策略师确认差异本来就是她要的，用 --pptx-words \"<原话>\" 记下再 deliver")
        urls = [d for d, _ in current["deliverables"] if isinstance(d, str) and d.startswith(("http:", "https:"))]
        unchecked = [c for c in checks if c["status"] == "unchecked"]
        platform_page = _has_platform_page(root, current, sent)
        unbound_note = _platform_unbound_note(root, task_id, current)
        # 候选 r2 #7：检查发现的问题（红灯）策略师还没确认时照样可以记（G02 用户选 A），提示里把那条提醒原样再说一遍
        reds = [x for x in reminders(root, task_id) if x["level"] == "red"]
        # 候选 r3 #8：多条红灯时每条的技术附录拆出来，合并放在提示末尾（给策略师的部分连在一起读）
        parts = [_split_appendix(x["text"]) for x in reds]
        red_note = ("\n\n还有 " + str(len(reds)) + " 条检查发现的问题你还没确认（这次照样记下了发出）：\n"
                    + "\n\n".join(body for body, _ in parts)) if reds else ""
        appendix = list(dict.fromkeys(a for _, a in parts if a))
        if urls:
            appendix.append("外部文档只认链接，用 waibu_wendang.py compare 比对当前内容")
        record = _append(root, {"event": "delivered", **current, "version": number, "to": to, "basis": basis, "words": words, "actor": actor,
                                **({"pptx_checks": [{k: c.get(k) for k in ("status", "pptx", "html", "pptx_pages", "html_pages", "reason", "differences",
                                                                                "chart_unchecked_pages") if c.get(k) is not None} for c in checks]} if checks else {}),
                                **({"pptx_words": pptx_words.strip()} if different else {}),
                                "external_files": sent,
                                "notice": "只记录发送事实，不代表客户批准" + ("" if sent else "；未指明发出的对外导出文件（当前版本未导出）")
                                + ("；外部文档（链接）里的内容没有核对，发之前建议对一下它现在的内容" if urls else "")
                                + ("；可编辑的 PPT 没核对（打不开或配不上网页版），不算和网页版一致" if unchecked else "")
                                + ("；可编辑的 PPT 和网页版有不一样的地方，你已确认本来就要这样（原话已记下）" if different else "")
                                # 批次四开工补修 3：图表页对照一致时也说图表里的字没核对
                                + "".join(f"；可编辑的 PPT 第 {n} 页有图表，图表里的字没有核对" for c in checks for n in c.get("chart_unchecked_pages") or [])
                                # v1.7.6 批次三返修：交付里有平台导出的提报页时提醒看负责人、状态、审核意见（只提醒，不拦）
                                + ("；这份交付里有平台导出的提报页：给客户前请看一眼页上的负责人、状态（草稿、待审核、审核通过等）和审核意见有没有保留、对不对" if platform_page else "")
                                + (f"；{unbound_note}" if unbound_note else "")
                                + red_note
                                # 批次四开工补修 6：命令名放技术附录；候选 r3 #8：红灯提醒的技术附录也合并到这里，放在最后
                                + ("\n\n## 技术附录（给 Agent）\n" + "\n".join(appendix) if appendix else "")})
        return {**record, **_shown_echo(root, task_id, number, to), "strategist_pending_first": _tradeoffs(root, task_id)}


def _split_appendix(text):
    """把一段给策略师的文字拆成（正文, 技术附录内容）；没有技术附录时附录为空串。"""
    body, mark, rest = str(text).partition("\n技术附录（给 Agent）：")
    return body, (rest.strip() if mark else "")


def _shown_echo(root, task_id, number, to=None):
    """候选 r3 #4：发客户、最终版等命令输出回显给策略师看的序号（记录里的版本号不变），内部号不同时技术附录写对应。"""
    shown = shown_version(root, task_id, number)
    return {"shown_version": shown, "version_line": (f"第 {shown} 版已记为发给{to}。" if to else f"第 {shown} 版") + version_note(root, task_id, [number])}


# ---------------- v1.7.5 G01 交给策略师即登记、G02 先交后检的结果回报、G09 未登记的对外版本 ----------------
PASS = {"passed", "passed_with_yellow"}
REVIEW_PURPOSE = "先交后检"


def _target(root, task_id):
    from phase6_targets import catalog
    return catalog(root)[f"standalone/{task_id}"]["target"]


def _reviews(root, task_id):
    """本任务已登记的独立检核记录（按登记先后）。"""
    from phase6_events import events as review_events
    return [r for r in review_events(root, "standalone_review")
            if isinstance(r.get("target"), dict) and r["target"].get("space") == "standalone" and r["target"].get("key") == task_id]


def last_pass(root, task_id):
    """最后一次检核通过（通过或通过带黄灯、报告原件仍在）的记录；没有返回 None。"""
    from phase6_events import decision_valid
    passed = [r for r in _reviews(root, task_id) if r.get("status") in PASS and decision_valid(root, r)]
    return passed[-1] if passed else None


def memory_only_since_pass(root, task_id):
    """v1.7.6 H10（评估后保留“接受新记忆即新版本、发客户前须再检核”，只补指引）：当前版本与最近一次检核通过的版本相比，
    只是接受了新的项目记忆这类只追加的记录（成品、原话、目标、其他来源与依据都没变）→ 返回那次通过的版本号，否则 None。"""
    try:
        record = last_pass(root, task_id)
        if not record:
            return None
        from phase6_targets import standalone_versions
        from standalone_budget import substantive
        from task_validation import is_records_path
        current, passed = _target(root, task_id)["version"], record["target"]["version"]
        if passed == current:
            return None
        tasks = {number: e["task"] for e, number, _ in standalone_versions([e for e in history(root)[0] if e["task_id"] == task_id])}
        before, now_task = tasks[passed], tasks[current]
        if substantive(before) != substantive(now_task) or before["completion_evidence"] != now_task["completion_evidence"]:
            return None
        old = {r.get("path"): r.get("sha256") for r in before["sources"]}
        changed = [r for r in now_task["sources"] if old.get(r.get("path")) != r.get("sha256")]
        return passed if changed and all(is_records_path(r.get("path")) for r in changed) else None
    except (KeyError, OSError, ValueError, TypeError, WorkflowError):
        return None


def memory_only_text(root, task_id, passed):
    shown = shown_version(root, task_id, passed)
    return ((f"上次检查通过的是第 {shown} 版；" if shown else "") +
            "这一版只接受了新的项目记忆、成品没变，但新加的记忆可能和稿子里的说法对不上，按规则发客户前还要再检查一次（会接着上次的结果准备，不用从头查）。")


def _reviewer_dispatches(root, task_id, active_only=True):
    from agent_dispatch import ACTIVE, read_log, replay
    return [d for d in replay(read_log(root)).values()
            if d.get("task_id") == task_id and d.get("role") == "independent-reviewer" and d.get("review_target")
            and (d.get("status") in ACTIVE or not active_only)]


def progress(root, task_id, target=None):
    """“自上次检查通过已隔 N 版”（G02）：版 = 检核所认的版本号（交付物、来源等实质内容每变一次加一版）。"""
    try:
        target = target or _target(root, task_id)
    except (KeyError, OSError, ValueError, TypeError):
        return {}
    passed = last_pass(root, task_id)
    since = target["version"] - passed["target"]["version"] if passed else None
    plain = (f"自上次检查通过已隔 {since} 版" if since else "当前这一版检查通过") if passed else "之前还没有哪一版通过检查"
    return {"version_number": target["version"], "last_pass_version": passed["target"]["version"] if passed else None,
            "versions_since_pass": since, "since_pass": plain}


def _state(root, task_id, target):
    """当前版本的背后检核状态：passed / passed_with_notes / found_issues / insufficient / checking / not_checked。
    候选 r5 #6：这一版最后一条结果没通过、之后又准备了还没结束的检核分派（重新检查）时也算 checking（标签与“正在检查”同一个判断）。
    v1.7.6 H05：最后一条结果是“资料不够、没看完”（insufficient_evidence）单列 insufficient，标签照实说，不并进“发现问题”；
    发客户关口对它的判定与 found_issues 相同（都算“已有结果”，behind_basis 只拒 not_checked），只改说法。"""
    mine = [r for r in _reviews(root, task_id) if r.get("target") == target]
    running = [d for d in _reviewer_dispatches(root, task_id) if d["review_target"] == target]
    if mine:
        last = mine[-1]
        if last.get("status") == "passed":
            return "passed", last
        if last.get("status") == "passed_with_yellow":
            return "passed_with_notes", last
        if any(_after(d.get("prepared_at", ""), last.get("created_at", "")) for d in running):
            return "checking", None
        return ("insufficient" if last.get("status") == "insufficient_evidence" else "found_issues"), last
    if running:
        return "checking", None
    return "not_checked", None


def _handovers(root, task_id):
    return [e for e in events(root) if e["event"] == "handover" and e["task_id"] == task_id]


def _handed_for(root, task_id, target):
    """这一版的交出记录（批次二返修 #2）：按检核所认的版本号（target.version）认，不按交付物指纹——
    改回旧文件生成的新版本有自己的交出记录，背后检核的提醒挂在新版本上。"""
    return [e for e in _handovers(root, task_id) if e.get("version") == target["version"]]


def behind_basis(root, task_id, current):
    """发客户登记的先交后检依据：当前版本已登记交给策略师，且当前版本已派背后检核（未结束）或已有登记的检核结果。"""
    target = _target(root, task_id)
    handed = _handed_for(root, task_id, target)
    if not handed:
        return None
    state, record = _state(root, task_id, target)
    if state == "not_checked":
        return None
    checking = [d["dispatch_id"] for d in _reviewer_dispatches(root, task_id) if d["review_target"] == target]
    return {"handover": handed[-1]["record_id"], "mode": REVIEW_PURPOSE,
            "behind_review": {"state": state, **({"review_record_id": record["record_id"]} if record else {}),
                              **({"dispatch_id": checking[-1]} if checking and not record else {})},
            "label": _STATE_LABEL[state], "notice": "先交后检：已交出，检核结果见标签；不算发出前已检核"}


CHECKING_LABEL = "检核中（这次检核还没有结果，不算已检核）"
_STATE_LABEL = {"checking": "已交出、" + CHECKING_LABEL,
                "found_issues": "已交出，检核结果：发现问题（见给策略师的提醒），这一版未通过检核",
                "insufficient": "已交出，检核结果：资料不够，没看完（见给策略师的提醒），这一版不算检查过，不代表一定有错",
                "passed_with_notes": "已交出，检核结果：通过（有可改进处，见提醒）",
                "passed": "已交出，检核结果：通过"}


def behind_label(root, task_id, current, target=None):
    """续做状态的标签（G02）：当前版本已交给策略师、且已派背后检核或已有结果但还不算“有效检核”时给出；
    候选 r5 #6：当前版本有进行中的检核时，不论交没交出都说“检核中”（只改文字，发客户关口仍按 behind_basis，要求已交出）。"""
    try:
        target = target or _target(root, task_id)
        state, _ = _state(root, task_id, target)
        handed = _handed_for(root, task_id, target)
    except (KeyError, OSError, ValueError, TypeError):
        return None
    if not handed:
        return CHECKING_LABEL if state == "checking" else None
    return None if state in ("not_checked", "passed") else _STATE_LABEL[state]


def shown_version(root, task_id, number):
    """给策略师看的版本序号（候选 r2 #6）：只数有交付物的版本——任务先建时没有成品的那一版不算，首份交出稿是“第 1 版”。
    检核、交出、发客户等记录里的版本号（检核所认的版本号）不变；没有成品的版本返回 None（候选 r4 #4）；算不出来时原样返回。"""
    try:
        from standalone_store import history
        from phase6_targets import standalone_versions
        own = [e for e in history(root)[0] if e["task_id"] == task_id]
        with_files = {n for e, n, _ in standalone_versions(own) if e["task"].get("deliverables")}
        shown = len({n for n in with_files if n <= number})
        if number not in with_files:  # 候选 r4 #4：没有成品的版本没有给她看的序号（不回显像序号的内部号）
            return None
        return shown
    except (OSError, ValueError, KeyError, TypeError, WorkflowError):
        return number


def _pair_text(root, task_id, numbers):
    """内部版本号与给策略师看的序号的对应（不同的才写；没有成品的版本写“没有成品”）；都相同时返回空串。"""
    pairs = [(n, shown_version(root, task_id, n)) for n in dict.fromkeys(x for x in numbers if x is not None)]
    differ = [(n, k) for n, k in pairs if n != k]
    if not differ:
        return ""
    return ("给策略师说的版本序号只数有成品的版本；记录里的内部版本号（检核所认的版本号）："
            + "、".join(f"说的第 {k} 版 = 内部第 {n} 版" if k is not None else f"内部第 {n} 版没有成品（不给策略师编号）" for n, k in differ))


def version_note(root, task_id, numbers):
    """内部版本号与给策略师看的序号不同时，技术附录写明（候选 r2 #6）；相同时返回空串。"""
    body = _pair_text(root, task_id, numbers)
    return "\n技术附录（给 Agent）：" + body + "。" if body else ""


def shown_map(root, task_id):
    """{内部版本号: 给策略师看的序号}（只含有交付物的版本，候选 r3 #4）；算不出来时返回空。"""
    try:
        from standalone_store import history
        from phase6_targets import standalone_versions
        own = [e for e in history(root)[0] if e["task_id"] == task_id]
        with_files = sorted({n for e, n, _ in standalone_versions(own) if e["task"].get("deliverables")})
        return {n: k for k, n in enumerate(with_files, 1)}
    except (OSError, ValueError, KeyError, TypeError, WorkflowError):
        return {}


def map_note(root, task_id):
    """技术附录用：本任务全部“说的第 K 版 = 内部第 N 版”（策略师说的第 K 版先换成内部号再传参，候选 r3 #4）。"""
    pairs = shown_map(root, task_id)
    return ("给策略师说的第 K 版换成内部版本号再传参：" + "、".join(f"说的第 {k} 版 = 内部第 {n} 版" for n, k in pairs.items())
            if pairs else "本任务还没有有成品的版本")


def status_line(root, task_id, handed=None):
    """给策略师的一行状态（白话，G01）：第 N 版已交给你；检查状态；距上次检查通过隔几版；另附上次交出后新出的检查结果。"""
    target = _target(root, task_id)
    state, _ = _state(root, task_id, target)
    info = progress(root, task_id)
    since = info.get("versions_since_pass")
    gap = f"距上次检查通过隔 {since} 版。" if since else ("之前还没有哪一版通过检查。" if info.get("last_pass_version") is None else "")
    parts = [f"{_said(root, task_id, target['version'])}已交给你；"]
    parts.append({"passed": "这一版已经检查过，没问题。", "passed_with_notes": "这一版已经检查过，有几处可以改进的地方，见下面的提醒。",
                  "found_issues": "这一版检查发现了问题，见下面的提醒。",
                  "insufficient": "这一版检查时资料不够，没看完，还不算检查过，见下面的提醒。", "checking": "还在检查中，",
                  "not_checked": "还没检查，"}[state])
    if state in ("checking", "not_checked"):
        parts.append(gap or "这一版之前的版本已检查通过。")
    previous = [e for e in _handovers(root, task_id) if e["version"] < target["version"]]
    after = previous[-1]["created_at"] if previous else ""
    greens = sorted({r["target"]["version"] for r in _reviews(root, task_id)
                     if r.get("status") == "passed" and r.get("created_at", "") > after and r["target"]["version"] < target["version"]})
    if greens:
        parts.append("另外，" + "、".join(f"第 {shown_version(root, task_id, v)} 版" for v in greens) + "已检查，没问题。")
    open_items = [x for x in reminders(root, task_id) if x["version"] != target["version"]]
    if open_items:
        parts.append(f"另外有 {len(open_items)} 条检查发现的问题要你看，见下面的提醒。")
    return "".join(parts) + version_note(root, task_id, [target["version"], *greens])


def handover(root, task_id, actor, source="manual"):
    """G01：把当前可对外的版本登记为“已交给策略师”。同一版本（交付物指纹）重复登记原样返回。"""
    from registration_guard import actor_check
    actor_check(actor)
    with serialized(root):
        current = version(root, task_id)
        current.pop("files_changed", None)
        target = _target(root, task_id)
        for e in _handed_for(root, task_id, target):
            return {**e, "status": "existing", "status_line": status_line(root, task_id)}
        state, record = _state(root, task_id, target)
        line = status_line(root, task_id)
        info = progress(root, task_id)
        return _append(root, {"event": "handover", **current, "version": target["version"], "source": source, "actor": actor,
                              "review_snapshot": {"state": state, **({"review_record_id": record["record_id"]} if record else {})},
                              "versions_since_pass": info.get("versions_since_pass"), "last_pass_version": info.get("last_pass_version"),
                              "status_line": line, "notice": "只记录交给策略师的事实，不代表已检核或客户批准"})


_LEVEL_WORDS = {"red": "要更正的问题", "yellow": "可以改进的地方"}


def reminders(root, task_id=None):
    """G02：交出后才登记的检核结果里，需要策略师知道的（发现问题、通过带黄灯、资料不够没看完），策略师确认前一直给出。"""
    rows = events(root)
    acked = {e["review_record_id"] for e in rows if e["event"] == "reminder_ack"}
    handed = {}
    for e in rows:
        if e["event"] == "handover" and (task_id is None or e["task_id"] == task_id):
            handed.setdefault((e["task_id"], e.get("version")), e["created_at"])
    out, cache = [], {}
    for (tid, number), at in sorted(handed.items(), key=lambda x: (x[0][0], x[0][1] or 0)):
        if tid not in cache:
            cache[tid] = _reviews(root, tid)  # 每个任务只读一次检核记录
        for record in cache[tid]:
            target = record.get("target", {})
            if (target.get("version") != number or record.get("created_at", "") < at or record["record_id"] in acked
                    or record.get("status") == "passed"):
                continue
            findings = [f for f in record.get("findings", []) if isinstance(f, dict) and f.get("level") in _LEVEL_WORDS]
            serious = record.get("status") in ("returned", "insufficient_evidence") or any(f["level"] == "red" for f in findings)
            out.append({"task_id": tid, "version": number, "review_record_id": record["record_id"], "level": "red" if serious else "yellow",
                        "review_status": record.get("status"), "findings": findings,
                        "text": _reminder_text(shown_version(root, tid, number), record, findings, serious) + version_note(root, tid, [number])})
    return out


def _reminder_text(number, record, findings, serious):
    reds = [f for f in findings if f["level"] == "red"]
    yellows = [f for f in findings if f["level"] == "yellow"]
    if record.get("status") == "insufficient_evidence" and not findings:
        what = f"第 {number} 版交给你之后在背后做了检查，但资料不够，没能看完，这一版还不算检查过。"
    elif record.get("status") == "insufficient_evidence":  # v1.7.6 H05：资料不够又看到了问题，两件事都照实说
        counts = "、".join(x for x in (f"{len(reds)} 处要更正的问题" if reds else "", f"{len(yellows)} 处可以改进的地方" if yellows else "") if x)
        what = f"第 {number} 版交给你之后在背后做了检查，资料不够，没能看完，这一版还不算检查过；已经看到的部分里有 {counts}。"
    else:
        counts = "、".join(x for x in (f"{len(reds)} 处要更正的问题" if reds else "", f"{len(yellows)} 处可以改进的地方" if yellows else "") if x)
        what = f"第 {number} 版交给你之后在背后做了检查，发现{counts or '问题'}。"
    effect = ("如果这一版已经发给客户，客户看到的稿里有这些问题。" if serious
              else "不算错误，这一版可以照常用；改不改由你决定。")
    todo = ("看下面每一处的问题和建议给客户的更正说法，决定要不要跟客户更正；看过后告诉我一声，这条提醒就不再显示。" if serious
            else "看一下下面这几处，决定改不改；看过后告诉我一声，这条提醒就不再显示。")
    lines = [f"发生了什么：{what}", f"对你有什么影响：{effect}", f"要你做什么：{todo}"]
    for f in reds + yellows:
        lines.append(f"- {f['location']}：{f['evidence']}；建议{'给客户的更正说法' if f['level'] == 'red' else '的改法'}：{f['action']}")
    return "\n".join(lines)


def acknowledge(root, task_id, review_record_id, words, actor):
    """策略师看过提醒（G02）：记她的原话，之后续做状态不再置顶这条提醒。"""
    from registration_guard import actor_check
    actor_check(actor); text(words, "策略师原话")
    with serialized(root):
        pending = {x["review_record_id"]: x for x in reminders(root, task_id)}
        for e in events(root):
            if e["event"] == "reminder_ack" and e["review_record_id"] == review_record_id:
                return {**e, "status": "existing"}
        if review_record_id not in pending:
            raise WorkflowError("这条检查提醒找不到或不需要确认。技术附录（给 Agent）：--review-record 须是 jiaofu_guankou status / 续做状态 "
                                "strategist_reminders 里列出的 review_record_id")
        item = pending[review_record_id]
        return _append(root, {"event": "reminder_ack", "task_id": task_id, "version": item["version"], "review_record_id": review_record_id,
                              "level": item["level"], "words": words, "actor": actor,
                              "record": "交出后检核：" + ("资料不够，没看完" if item.get("review_status") == "insufficient_evidence"
                                                         else "发现问题" if item["level"] == "red" else "有可改进处") + "，已提醒，策略师已确认"})


# ---------------- v1.7.5 G06 先交付后补登记、G07 最终版补检 ----------------
def _local(value, date_only=False):
    """给策略师看的时间：本机时区的“10 月 9 日 15:30”（只有日期时“10 月 9 日”）。"""
    from datetime import datetime
    try:
        t = datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return "时间未知"
    return f"{t.month} 月 {t.day} 日" + ("" if date_only else f" {t:%H:%M}")


def delivery_when(event):
    """发客户记录的时间说法：补登记的写“实际发出，补登记”两个时间，不让补登记看起来像当时发生。
    发出时间容许比本机时钟晚几分钟（见 _sent_at）；显示时不晚于补登记时间（批次三开工 A）。"""
    if event.get("retroactive"):
        sent = event["sent_at"]
        try:
            if _moment(sent) > _moment(event["recorded_at"]):
                sent = event["recorded_at"]
        except (TypeError, ValueError, KeyError):
            pass
        return (f"{_local(sent, event.get('sent_at_precision') == 'date')} 发出，"
                f"{_local(event['recorded_at'])} 补登记")
    return f"{_local(event.get('created_at'))} 登记发出"


def _versions(root, task_id):
    """{版本号: 该版最后一条任务记录}（版本号即检核所认的版本号）。"""
    from standalone_store import history as task_history
    from phase6_targets import standalone_versions
    own = [e for e in task_history(root)[0] if e["task_id"] == task_id]
    if not own:
        raise WorkflowError("找不到任务：" + task_id)
    return {number: event for event, number, _ in standalone_versions(own)}


def _version_born(root, task_id):
    """{版本号: 该版第一次登记的时间}（补登记核对“发出时间不早于这一版做出来”，批次二返修 #9）。"""
    from standalone_store import history as task_history
    from phase6_targets import standalone_versions
    born = {}
    for event, number, _ in standalone_versions([e for e in task_history(root)[0] if e["task_id"] == task_id]):
        born.setdefault(number, event["created_at"])
    return born


def _task_born(root, task_id):
    """该任务第一条记录的时间：补登记的发出时间不能早于它（那时这项工作还没开始；批次三开工 A，更正返修 #9）。"""
    from standalone_store import history as task_history
    own = [e for e in task_history(root)[0] if e["task_id"] == task_id]
    if not own:
        raise WorkflowError("找不到任务：" + task_id)
    return own[0]["created_at"]


def _moment(value):
    from datetime import datetime
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.astimezone()


def _fingerprint_index(root, task_id):
    """{交付物指纹: [(版本号, 该版第一次登记时间)]}：旧发客户记录没有版本号时按指纹找版本（P2-3）。"""
    index = {}
    try:
        born = _version_born(root, task_id)
        for number, event in _versions(root, task_id).items():
            files = _files(event)
            if files:
                index.setdefault(digest(files), []).append((number, born.get(number)))
    except (OSError, ValueError, KeyError, TypeError, WorkflowError):
        return {}
    return index


def record_version(event, index):
    """发客户记录对应的版本号：有 version 字段用它；旧记录按指纹找，取记录登记之前最早的那一版（改回旧文件时同指纹有几版，
    记录登记之后才出现的版本不可能是当时发的那一版；P2-3）。找不到时为 None。"""
    if event.get("version") is not None:
        return event["version"]
    found = index.get(event.get("fingerprint"), [])
    try:
        before = [n for n, born in found if born and _moment(born) <= _moment(event["created_at"])]
    except (TypeError, ValueError, KeyError):
        before = []
    return min(before) if before else (min(n for n, _ in found) if found else None)


def after_send_note(number, what, created_at, sent, precision):
    """检核通过 / 策略师原话登记得不早于发出时间时的说法（依据看先后）。只写日期的发出时间与它同一天时分不清先后，
    如实写出来并加标记，按发出之后算（P2-1）。"""
    created = _moment(created_at)
    sent_moment = _moment(sent)
    done = {"检查": "检查通过", "不用检查的话": "说不用检查"}[what]
    if precision == "date" and created.astimezone(sent_moment.tzinfo).date() == sent_moment.date():
        return {"same_day_unclear": True,
                "note": f"第 {number} 版{done}和发出都在 {_local(created_at, True)}：同一天登记的，分不清先后，按发出之后算，不算发出时的依据。"}
    return {"same_day_unclear": False, "note": f"第 {number} 版是发出后才{done}的（{_local(created_at)}），不算发出时的依据。"}


def _files(event):
    return sorted((r.get("path") or r.get("url"), r.get("sha256")) for r in event["task"]["deliverables"])


def deliveries(root, task_id):
    """本任务全部发客户记录（各版），附版本号、时间说法与原话；续做状态与复盘统计共用。
    补登记的版本在补登记之后才检查通过的，也照实写“发出后才检查通过”（批次三开工 A）。"""
    from phase6_events import decision_valid
    index = _fingerprint_index(root, task_id)
    try:
        reviews = [r for r in _reviews(root, task_id) if r.get("status") in PASS and decision_valid(root, r)]
    except (OSError, ValueError, KeyError, TypeError, WorkflowError):
        reviews = []
    out = []
    for e in events(root):
        if e["event"] != "delivered" or e["task_id"] != task_id:
            continue
        number = record_version(e, index)
        item = {"version": number, **({"shown_version": shown_version(root, task_id, number)} if number is not None else {}),
                "to": e.get("to"), "when": delivery_when(e), "retroactive": bool(e.get("retroactive")),
                "words": e.get("words"), **({"covers_versions": e["covers_versions"]} if e.get("covers_versions") else {})}
        basis = e.get("basis") or {}
        if e.get("retroactive") and "review_record_id" not in basis:
            after = basis.get("review_after_send")
            if not after:
                later = [r for r in reviews if r.get("target", {}).get("version") == number]
                if later:
                    try:
                        after = after_send_note(shown_version(root, task_id, number), "检查", later[0]["created_at"], e["sent_at"],
                                                e.get("sent_at_precision"))  # 候选 r3 #4：说明用给策略师看的序号
                    except (TypeError, ValueError, KeyError):
                        after = None
            if after:
                item["review_after_send"] = after["note"]
                if after.get("same_day_unclear"):
                    item["same_day_unclear"] = True
        out.append(item)
    return out


SENT_AT_SLACK_MINUTES = 5


def _sent_at(value):
    """策略师说的发出时间：2026-10-09、2026-10-09 15:30、2026-10-09T15:30（可带时区）；没写时区按本机时区。"""
    from datetime import datetime, timezone
    text_value = str(value or "").strip()
    try:
        moment = datetime.fromisoformat(text_value)
    except ValueError:
        raise WorkflowError("发出时间看不懂，这次没有补登记。请按“2026-10-09”或“2026-10-09 15:30”写策略师说的时间。"
                            "技术附录（给 Agent）：--sent-at 须为 ISO 日期或日期时间") from None
    precision = "date" if len(text_value) == 10 else "time"
    if moment.tzinfo is None:
        moment = moment.astimezone()
    # 容许比本机时钟晚 5 分钟：策略师说的时间只到分钟（“15:31”可能是本机 15:30:40），她的设备与本机时钟也可能差一两分钟；
    # 超过 5 分钟才当作将来的时间拒绝。显示与统计里发出时间不晚于补登记时间（delivery_when）。
    if moment > datetime.now(timezone.utc) + __import__("datetime").timedelta(minutes=SENT_AT_SLACK_MINUTES):
        raise WorkflowError("发出时间晚于现在，这次没有补登记。技术附录（给 Agent）：--sent-at 不能是将来的时间"
                            f"（容许比本机时钟晚 {SENT_AT_SLACK_MINUTES} 分钟）")
    return moment.isoformat(), precision


def _minute_only(value):
    """策略师说的发出时间只写到分钟（如“2026-10-09 15:30”，可带时区），没写秒。"""
    import re
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?", str(value or "").strip()))


def _floor_minute(moment, like):
    """把记录时间换到发出时间的时区后去掉秒（只到分钟的发出时间与它同一分钟时不算更早，候选 r4 #9）。"""
    return moment.astimezone(like.tzinfo).replace(second=0, microsecond=0)


def deliver_retroactive(root, task_id, to, actor, words, sent_at, versions=None):
    """G06：合法的“先交付后补登记”。逐版一条发客户记录，每条都记实际发出时间（策略师说的）与补登记时间、同一句原话的引用；
    该版有通过的检核就以检核为依据，有对外豁免原话就以豁免为依据，都没有时同时登记一条“事后补登记”的豁免（G07 第三类），
    不算检核通过。一句原话覆盖多版须一次用 --versions 列全（同一句原话不能分几次补登记不同的版本）。"""
    from registration_guard import actor_check
    from phase6_events import decision_valid
    actor_check(actor); text(to, "发送对象"); text(words, "策略师原话")
    sent, precision = _sent_at(sent_at)
    with serialized(root):
        history_versions = _versions(root, task_id)
        wanted = sorted(set(versions or [max(history_versions)]))
        missing = [n for n in wanted if n not in history_versions]
        if missing:  # 候选 r3 #4：正文不写内部号（策略师没见过它），技术附录写对应关系
            raise WorkflowError("要补登记的版本找不到，这次没有补登记；请跟策略师核对是第几版。"
                                "技术附录（给 Agent）：--versions 是内部版本号（检核所认的版本号），没有" + "、".join(f"内部第 {n} 版" for n in missing)
                                + f"，本任务现有内部 1—{max(history_versions)}；" + map_note(root, task_id))
        born = _version_born(root, task_id)
        sent_moment = _moment(sent)
        # 批次三开工 A（更正返修 #9）：补登记的发出时间早于该版登记属正常（当时发了、没登记），照记并标“补登记”；
        # 只拒绝早于该任务第一条记录的时间（那时这项工作还没开始；只写日期时按日期比）。将来的时间由 _sent_at 拒绝。
        first = _task_born(root, task_id)
        started = _moment(first)
        # 候选 r4 #9：发出时间只写到分钟时按分钟比较（与任务第一条记录同一分钟不拒）；写到秒的照秒比
        minute = precision != "date" and _minute_only(sent_at)
        early = ((sent_moment.date() < started.astimezone(sent_moment.tzinfo).date()) if precision == "date"
                 else sent_moment < _floor_minute(started, sent_moment) if minute else sent_moment < started)
        if early:
            raise WorkflowError(f"这项工作是 {_local(first)} 才开始记录的，这个时间这项工作还没开始，这次没有补登记。"
                                "请再确认一下发出的时间，或者是不是另一项工作。技术附录（给 Agent）：--sent-at 早于该任务第一条记录时间，向策略师核对发出时间或任务"
                                f"（独立任务记录 created_at {first}）")
        rows = events(root)

        def same_version(e, number, fingerprint):
            return (e["event"] == "delivered" and e["task_id"] == task_id and e.get("retroactive") and e["to"] == to
                    and (e.get("version") == number if e.get("version") is not None else e["fingerprint"] == fingerprint))

        # 批次二返修 #3：某版已用同一句原话补登记过（不论发出时间），或同一发出时间补登记过，原样返回，不另造发出记录与豁免
        existing = {}
        for number in wanted:
            files = _files(history_versions[number])
            fingerprint = digest(files) if files else None
            done = next((e for e in rows if same_version(e, number, fingerprint)
                         and (e.get("sent_at") == sent or _same_words(e.get("words") or "", words))), None)
            if done:
                existing[number] = done
        fresh = [n for n in wanted if n not in existing]
        # “一句原话覆盖几版须一次列全”只在同一发出时间、同一发送对象内要求
        earlier = sorted({e.get("version") for e in rows if e["event"] == "delivered" and e["task_id"] == task_id
                          and e.get("retroactive") and e["to"] == to and e.get("sent_at") == sent
                          and _same_words(e.get("words") or "", words)} - set(wanted) - {None}) if fresh else []
        if earlier:
            raise WorkflowError("这句原话已经用来补登记过" + "、".join(f"第 {shown_version(root, task_id, n)} 版" for n in earlier)
                                + "（同一个发出时间、同一个对象），这次没有补登记。一句原话覆盖几版时，要一次把这几版都列出来。"
                                "技术附录（给 Agent）：同一 --to、同一 --sent-at 下，--versions 须包含这句原话以前补登记过的全部版本（内部第 "
                                + "、".join(str(n) for n in earlier) + " 版）；" + map_note(root, task_id))
        reviews = _reviews(root, task_id)
        out = []
        for number in wanted:
            if number in existing:
                out.append({**existing[number], "status": "existing"}); continue
            event = history_versions[number]
            files = _files(event)
            if not files:
                raise WorkflowError("这一版还没有成品（没有登记交付物），没法补登记发出的是什么，这次没有补登记。"
                                    f"技术附录（给 Agent）：内部第 {number} 版没有交付物；" + map_note(root, task_id))
            fingerprint = digest(files)
            # 批次二返修 #9：依据看先后——检核、策略师原话登记得比发出晚的，不算发出时的依据（照实写“发出后才检查通过”）
            passed = [r for r in reviews if r.get("target", {}).get("version") == number and r.get("status") in PASS and decision_valid(root, r)]
            passed_before = [r for r in passed if _moment(r["created_at"]) < sent_moment]
            words_all = send_waivers(rows, task_id, fingerprint)
            waived = [e for e in words_all if _moment(e["created_at"]) < sent_moment]
            base = {"task_id": task_id, "revision": event["revision"], "deliverables": [list(x) for x in files], "fingerprint": fingerprint}
            if passed_before:
                basis = {"review_record_id": passed_before[-1]["record_id"]}
            elif waived:
                basis = {"external_waiver": waived[-1]["record_id"], "words": waived[-1]["words"]}
            else:
                waiver = _append(root, {"event": "waiver", **base, "purpose": "external", "purpose_label": PURPOSES["external"],
                                        "words": words, "actor": actor, "waiver_source": "retroactive",
                                        "waiver_source_label": WAIVER_SOURCES["retroactive"], "version": number,
                                        "notice": "事后补登记：发出时没有检核也没有登记豁免，只记录原因，不是检核通过"})
                basis = {"retroactive_waiver": waiver["record_id"]}
                if passed:  # 依据看先后：发出后才检查通过（只写日期、同一天的如实写分不清先后，P2-1）
                    later = passed[0]
                    basis["review_after_send"] = {"review_record_id": later["record_id"],
                                                  **after_send_note(shown_version(root, task_id, number), "检查", later["created_at"], sent, precision)}
                if words_all:
                    later = words_all[0]
                    basis["waiver_after_send"] = {"external_waiver": later["record_id"],
                                                  **after_send_note(shown_version(root, task_id, number), "不用检查的话", later["created_at"], sent, precision)}
                if any(basis.get(k, {}).get("same_day_unclear") for k in ("review_after_send", "waiver_after_send")):
                    basis["same_day_unclear"] = True
            before_version = (sent_moment.date() < _moment(born[number]).astimezone(sent_moment.tzinfo).date() if precision == "date"
                              else sent_moment < _floor_minute(_moment(born[number]), sent_moment) if minute
                              else sent_moment < _moment(born[number]))
            record = _append(root, {"event": "delivered", **base, "version": number, "to": to, "basis": basis, "words": words,
                                    "actor": actor, "external_files": None, "retroactive": True, "sent_at": sent,
                                    "sent_at_precision": precision, "recorded_at": now(), "covers_versions": wanted,
                                    "version_registered_at": born[number], "sent_before_version_registered": before_version,
                                    "words_digest": digest(words),
                                    "notice": "事后补登记：实际发出时间是策略师说的 sent_at，登记时间是 recorded_at；只记录发送事实，不代表客户批准"})
            out.append(record)
        listed = [{"version": r.get("version"), "shown_version": shown_version(root, task_id, r.get("version")), "when": delivery_when(r),
                   "basis": r["basis"], "status": r.get("status", "recorded"), "record_id": r["record_id"]} for r in out]
        # 候选 r3 #4：回显给策略师看的序号（记录里的 version 仍是内部号），内部号不同时技术附录写对应
        said = "、".join(f"第 {x['shown_version']} 版（{x['when']}）" for x in listed)
        reminder = _retroactive_platform_note(root, [history_versions[n] for n in wanted])
        return {"status": "recorded", "task_id": task_id, "versions": wanted, "deliveries": listed,
                **({"platform_reminder": reminder} if reminder else {}),
                "text": f"已补登记发给{to}：{said}。" + (reminder or "") + version_note(root, task_id, wanted)}


def _retroactive_platform_note(root, version_events):
    """U1（全盘细查返修）：补登记记的是已经发生的发送，不拦；但交付物里有绑定了平台快照、现在“需复核”的成果时提醒一句。记录读不出来也提醒核实不了。"""
    try:
        from pingtai_kuaizhao import needs_recheck, LOG as PLATFORM_LOG
        if not local(root, PLATFORM_LOG).exists():
            return None
        paths = {d for event in version_events for d, _ in _files(event) if isinstance(d, str)}
        rows = [r for r in needs_recheck(root) if r["path"] in paths]
    except ImportError:
        return None
    except (WorkflowError, OSError, ValueError, KeyError, TypeError):
        return "提醒：平台快照的记录读不出来，没法确认发出去的价格、规格、利润和平台对得上，建议跟客户那边再核实一下。"
    if not rows:
        return None
    return ("提醒：发出去的成果里有还没和平台数据对上的——" + platform_recheck_names(rows)
            + "。这次只是照实补记，不拦；建议尽快核对，必要时跟客户更正。")


def mark_final(root, task_id, words, actor, number=None, client_confirmed=False):
    """G07：策略师标“最终版”或转达“客户已确认”的版本（记原话）。同一任务只按最新一条（候选 r5）：之后这一版没有一次检查通过的记录时，
    续做状态摆一行事实（见 recheck_state）。v1.7.6 H04：每次调用都追加新的一条（带序号），不按内容去重。"""
    from registration_guard import actor_check
    actor_check(actor); text(words, "策略师原话")
    with serialized(root):
        history_versions = _versions(root, task_id)
        number = number or max(history_versions)
        if number not in history_versions:  # 候选 r3 #4：正文不写内部号，技术附录写对应关系
            raise WorkflowError("找不到这一版，这次没有登记；请跟策略师核对是第几版。"
                                f"技术附录（给 Agent）：--version 是内部版本号（检核所认的版本号），没有内部第 {number} 版，"
                                f"本任务现有内部 1—{max(history_versions)}；" + map_note(root, task_id))
        files = _files(history_versions[number])
        if not files:  # 候选 r4 #4：照 deliver_retroactive 的写法拒——没有成品的版本说不清定的是什么
            raise WorkflowError("这一版还没有成品（没有登记交付物），不能记为最终版，这次没有登记；请跟策略师核对是第几版。"
                                f"技术附录（给 Agent）：内部第 {number} 版没有交付物；" + map_note(root, task_id))
        shown = shown_version(root, task_id, number)
        echo = {"shown_version": shown,
                "text": f"记下了：第 {shown} 版是{'客户已确认的版本' if client_confirmed else '你说的最终版'}。这只是记下你的话，不算检查通过。"
                        + version_note(root, task_id, [number])}
        # 批次二返修 #8：客户确认是新事实——同一版先标最终版、后转达客户确认时追加一条，原来那句不丢。
        # v1.7.6 H04：每次标最终版都是新的一条（带本任务第几条最终版记录的序号 sequence，记录编号随之不同），旧记录保留；
        # 同一版、同一句原话再标一次也是新记录——之前那条的“不用查”不沿用到新记录（同一任务只按最新一条，见 latest_final）。
        sequence = 1 + sum(1 for e in events(root) if e["event"] == "final_version" and e["task_id"] == task_id)
        if sequence > 1:
            echo["text"] += "之前记过的最终版记录都保留，以这一条为准。"
        return {**_append(root, {"event": "final_version", "task_id": task_id, "version": number, "revision": history_versions[number]["revision"],
                                 "fingerprint": digest(files), "client_confirmed": bool(client_confirmed), "words": words, "actor": actor,
                                 "sequence": sequence, "notice": "只记录策略师说这是最终版 / 客户已确认，不是检核通过"}), **echo}


def _after(a, b):
    """a 晚于 b（两个都是记录里的时间）；读不出来时按不晚于算。"""
    try:
        return _moment(a) > _moment(b)
    except (TypeError, ValueError):
        return False


def _said(root, task_id, number):
    """给策略师说的“第 K 版”（只数有成品的版本）；没有成品的版本没有给她看的序号，说“还没有成品的那一版”（候选 r4 #4）。"""
    shown = shown_version(root, task_id, number) if number is not None else None
    return f"第 {shown} 版" if shown is not None else "还没有成品的那一版"


def latest_final(rows, task_id):
    """同一任务只按最新一条最终版记录（更早的视为被取代，候选 r5）；没有返回 None。"""
    found = [e for e in rows if e["event"] == "final_version" and e["task_id"] == task_id]
    return found[-1] if found else None


def recheck_state(root, task_id, final, *, rows=None):
    """最终版补检的唯一判定（候选 r5，用户 2026-10-11 拍板简化：只摆事实 + 一个“不用查”开关；不记“做”、不做承诺）。只看事实：
    该任务最新一条最终版记录（final）定的第 K 版；第 K 版本身或交付物与它完全相同的版本有没有检查通过（之后版本的通过不算，
    资料不够没看完、退回都不算通过）；当前第 M 版有没有进行中的检核（与标签同一个判断 _state）；这条最终版记录有没有“不用查”。
    另看同一任务有没有未结束的分派、检核准备会不会被拦——只决定技术附录给不给命令。
    返回 show（这一行出不出）、给策略师的一行 text（白话三段式里的“发生了什么”）、给 Agent 的 appendix。"""
    import shlex
    from agent_dispatch import ACTIVE, read_log, replay, unregistered_candidates
    from phase6_events import decision_valid
    rows = events(root) if rows is None else rows
    number = final["version"]
    versions = _versions(root, task_id)

    def content(n):
        event = versions.get(n)
        files = _files(event) if event else []
        return digest(files) if files else None

    mine = content(number)

    def is_k(n):  # 第 K 版本身，或交付物与它完全相同的版本
        return n == number or (mine is not None and content(n) == mine)

    passed = [r for r in _reviews(root, task_id) if isinstance(r.get("target"), dict) and is_k(r["target"].get("version"))
              and r.get("status") in PASS and decision_valid(root, r)]
    dont = [e for e in rows if e["event"] == "recheck_dont" and e.get("final_record_id") == final["record_id"]]
    target = _target(root, task_id)
    current = target["version"]
    same = is_k(current)
    checking = _state(root, task_id, target)[0] == "checking"
    active = [d["dispatch_id"] for d in replay(read_log(root)).values() if d.get("task_id") == task_id and d.get("status") in ACTIVE]
    base = {"task_id": task_id, "version": number, "final_record_id": final["record_id"], "current_version": current,
            "same_as_current": same, "checking": checking, "client_confirmed": bool(final.get("client_confirmed")),
            "active_dispatches": active, "passed_review": passed[-1]["record_id"] if passed else None,
            "dont_record": dont[-1]["record_id"] if dont else None, "files_changed_since_registered": False}
    if passed or dont:
        return {**base, "show": False, "text": None, "appendix": ""}
    K, M = _said(root, task_id, number), _said(root, task_id, current)
    stale = _stale(root, target) if same else None
    base["files_changed_since_registered"] = bool(stale)
    text = f"你定为最终版的{K}（{'客户手上这一版' if final.get('client_confirmed') else '你定的这一版'}）还没有一次检查通过的记录。"
    if same and stale and checking:  # r2 #13：已有检核在跑——先说在查哪一版，再说改动不在这次检查里
        text += (f"{K}正在检查（查的是登记时的那份）；但这一版的成品或用到的资料登记之后又被直接改过或删了，这些改动不在这次检查里："
                 "要先把改动登记成新的一版（或把文件恢复原样）。不用查就告诉我，这行就不再出现。")
    elif same and stale:  # v1.7.6 H03：成品或来源登记后被直接改过或删了，派检查会被拒，先登记新版
        text += "这一版的成品或用到的资料登记之后被直接改过或删了，现在派不了检查：要先把改动登记成新的一版（或把文件恢复原样）。不用查就告诉我，这行就不再出现。"
    elif same:
        text += "这一版正在检查，结果出来会照常告诉你。" if checking else "要查就照平常派背后检查；不用查就告诉我，这行就不再出现。"
    else:
        text += f"现在是{M}，{'正在检查' if checking else '没在检查'}；背后检查只能看现在的版本，看不到{K}本身。不用再提醒就告诉我。"
    misses = not_run_streak(root, task_id)
    if not same:
        why = "背后检核只看当前版本，补查不了这一版本身，不给命令"
    elif stale:
        why = (f"当前版本的成品或来源文件与登记不符（{stale}），检核准备会被拒：先回读并登记新版（standalone_tasks.py iterate / save）"
               "或恢复文件，再准备检核；现在不给命令" + ("；进行中的检核查的是登记时的那份，改动不在其中" if checking else ""))
    elif active:
        why = "已有分派在跑，等它结束"
    elif [c for c in unregistered_candidates(root) if c["task_id"] == task_id]:
        why = "有已回读、还没登记的稿子，先登记为当前版本或放弃，再准备检核；现在不给命令"
    elif misses >= 2:
        why = f"已连续 {misses} 次检查没跑成、先停下，按停下提示问策略师；不给命令"
    else:
        why = (f"照平常准备背后检核：python scripts/jianhe_zhunbei.py --workspace . --task-id {shlex.quote(task_id)} --behind "
               f"--actor {shlex.quote(final.get('actor') or '<主控>')}")
    pairs = _pair_text(root, task_id, [number, current])
    return {**base, "show": True, "text": text, "appendix": "技术附录（给 Agent）：" + "；".join(p for p in (why, pairs) if p) + "。"}


def _stale(root, target):
    """v1.7.6 H03：当前版本的成品或来源文件登记后被改过或删了（与检核准备同一个核对 phase6_targets.resolve）时返回原因，否则 None。"""
    from phase6_targets import resolve
    try:
        resolve(root, target)
    except WorkflowError as exc:
        return str(exc)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return "文件读不出来：" + str(exc)[:120]
    return None


def final_checks(root, task_id=None):
    """续做状态、交付关口 status 里的最终版一行（G07，候选 r5）：每个任务只按最新一条最终版记录，出不出、说什么只由 recheck_state 定。"""
    rows = events(root)
    tasks = dict.fromkeys(e["task_id"] for e in rows if e["event"] == "final_version" and (not task_id or e["task_id"] == task_id))
    out = []
    for tid in tasks:
        s = recheck_state(root, tid, latest_final(rows, tid), rows=rows)
        if s["show"]:
            out.append({k: s[k] for k in ("task_id", "version", "final_record_id", "current_version", "checking", "client_confirmed",
                                          "active_dispatches", "files_changed_since_registered", "text", "appendix")})
    return out


def recheck_dont(root, task_id, final_record_id, words, actor):
    """策略师说最终版不用查（记原话）：这条最终版记录的那一行不再出现。这是唯一的开关（候选 r5）；同一条记录重复登记原样返回。
    只认该任务最新一条最终版记录：之后又记了新的（如转达客户确认），旧的那条已被取代，拒绝并说明——“不用查”不沿用到新记录。
    v1.7.5 候选 r4 及以前的答复记录（recheck_answer，做 / 不做）一律按“没有答复”处理。"""
    from registration_guard import actor_check
    actor_check(actor); text(words, "策略师原话")
    with serialized(root):
        rows = events(root)
        final = latest_final(rows, task_id)
        if final is None or final["record_id"] != final_record_id:
            known = any(e["event"] == "final_version" and e["task_id"] == task_id and e["record_id"] == final_record_id for e in rows)
            raise WorkflowError(("这条最终版记录之后又记了新的一条（例如转达了客户确认），要按新的那条再跟策略师确认一次；这次没有登记。"
                                 if known else "找不到这条最终版记录，这次没有登记。")
                                + "技术附录（给 Agent）：--final-record 须是续做状态 final_unchecked 里的 final_record_id（该任务最新一条最终版记录"
                                + (f"，现在是 {final['record_id']}" if final else "，本任务还没有最终版记录") + "）")
        said = _said(root, task_id, final["version"])
        echo = {"text": f"记下了：你说{said}不用查，这一行不再出现。这不算检查通过。" + version_note(root, task_id, [final["version"]])}
        for e in rows:
            if e["event"] == "recheck_dont" and e.get("final_record_id") == final_record_id:
                return {**e, "status": "existing", **echo}
        return {**_append(root, {"event": "recheck_dont", "task_id": task_id, "final_record_id": final_record_id, "version": final["version"],
                                 "words": words, "actor": actor, "notice": "只记录策略师说这一版不用查，不算检查通过"}), **echo}


EXPORT_DIR = "project/exports/对外"


def unregistered_exports(root, limit=20):
    """G09：对外目录下没有登记过（不在任何对外导出 / 发客户 / 交出登记的文件所在目录里，也不是任务交付物）、
    又比该任务（目录名是任务编号时）或全部任务最近一次登记更新的文件。只读文件元数据，不算指纹。"""
    import os
    from datetime import datetime
    base = local(root, EXPORT_DIR)
    if not base.is_dir():
        return []
    rows = events(root)
    from standalone_store import history as task_history
    task_events = task_history(root)[0]
    tasks = {e["task_id"] for e in task_events}
    registered_files = {r["path"] for e in task_events for r in e["task"].get("deliverables", []) if "path" in r}
    latest = {}
    for e in rows:
        if e["event"] not in ("handover", "delivered", "external_version"):
            continue
        latest[e["task_id"]] = max(latest.get(e["task_id"], ""), e["created_at"])
        registered_files.update(d for d, _ in e.get("deliverables", []) if isinstance(d, str))
        for item in (e.get("files") or []) + ((e.get("external_files") or {}).get("files") or []):
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                registered_files.add(item["path"])
    if not latest:
        return []
    registered_dirs = {f.rsplit("/", 1)[0] for f in registered_files if f.startswith(EXPORT_DIR + "/")}
    overall = max(latest.values())
    found = []
    for current_dir, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not os.path.islink(os.path.join(current_dir, d)))
        rel_dir = Path(current_dir).relative_to(Path(root)).as_posix()
        for name in sorted(names):
            path = Path(current_dir) / name
            rel = f"{rel_dir}/{name}"
            if name.startswith(".") or path.is_symlink() or rel in registered_files or rel_dir in registered_dirs:
                continue
            first = rel[len(EXPORT_DIR) + 1:].split("/", 1)[0]
            since = latest.get(first, overall) if first in tasks else overall
            mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
            if _later(mtime, since):
                found.append({"path": rel, "task_id": first if first in tasks else None, "modified_at": mtime[:19],
                              "registered_until": datetime.fromisoformat(since).astimezone().isoformat()[:19]})
    return found[:limit] + ([{"path": f"（另有 {len(found) - limit} 个）", "task_id": None}] if len(found) > limit else [])


def _later(a, b):
    from datetime import datetime
    try:
        return datetime.fromisoformat(a) > datetime.fromisoformat(b)
    except ValueError:
        return False


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
    for name in ("waive", "deliver", "status", "handover", "ack", "final", "recheck"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", type=Path, required=True)
        p.add_argument("--task-id", required=True)
        p.add_argument("--json", action="store_true")
        if name != "status":
            p.add_argument("--actor", required=True)
        if name == "ack":
            p.add_argument("--review-record", required=True, help="要确认的检查提醒（review_record_id）")
            p.add_argument("--words", required=True, help="策略师看过提醒后的原话")
        if name == "waive":
            p.add_argument("--purpose", choices=sorted(PURPOSES), required=True)
            p.add_argument("--words", required=True)
            p.add_argument("--source", choices=sorted(WAIVER_SOURCES), default="strategist",
                           help="豁免来源（v1.7.5）：strategist 策略师明确不检核 / not_run 检核没跑成 / retroactive 事后补登记")
            p.add_argument("--reason-code", choices=sorted(NOT_RUN_REASONS), help="--source not_run 时必填：没跑成的原因")
        if name == "deliver":
            p.add_argument("--to", required=True)
            p.add_argument("--words")
            p.add_argument("--export-record", help="发出的对外版本 record_id；默认当前版本最新且文件未变的对外导出")
            p.add_argument("--pptx-words", help="PPTX 与网页版有差异、策略师确认本来如此时她的原话（v1.7.5 G13）")
            p.add_argument("--retroactive", action="store_true", help="先交付后补登记（v1.7.5）：须带 --sent-at 与 --words")
            p.add_argument("--sent-at", help="策略师说的实际发出时间，如 2026-10-09 或 2026-10-09 15:30")
            p.add_argument("--versions", type=int, nargs="+", help="补登记的版本号（检核所认的版本号）；一句原话覆盖几版就一次列全")
        if name == "final":
            p.add_argument("--words", required=True, help="策略师说这是最终版 / 客户已确认的原话")
            p.add_argument("--version", type=int, help="版本号；默认当前版本")
            p.add_argument("--client-confirmed", action="store_true", help="客户已确认（策略师转达）")
        if name == "recheck":
            p.add_argument("--final-record", required=True)
            p.add_argument("--decision", choices=("dont",), required=True, help="只有 dont：策略师说最终版不用查（候选 r5）")
            p.add_argument("--words", required=True, help="策略师说不用查的原话")
    args = parser.parse_args()
    root = args.workspace.resolve()
    if args.command == "waive":
        result = waive(root, args.task_id, args.purpose, args.words, args.actor, args.source, args.reason_code)
    elif args.command == "deliver" and args.retroactive:
        if not args.sent_at or not args.words:
            raise WorkflowError("补登记要写清策略师说的发出时间和她的原话，这次没有补登记。技术附录（给 Agent）：--retroactive 须带 --sent-at 与 --words")
        if args.export_record:
            raise WorkflowError("补登记不指定对外导出记录。技术附录（给 Agent）：--retroactive 不与 --export-record 同用")
        result = deliver_retroactive(root, args.task_id, args.to, args.actor, args.words, args.sent_at, args.versions)
    elif args.command == "deliver":
        if args.sent_at or args.versions:
            raise WorkflowError("写了发出时间或版本号却没说是补登记。技术附录（给 Agent）：--sent-at、--versions 只与 --retroactive 同用")
        result = deliver(root, args.task_id, args.to, args.actor, args.words, args.export_record, args.pptx_words)
    elif args.command == "final":
        result = mark_final(root, args.task_id, args.words, args.actor, args.version, args.client_confirmed)
    elif args.command == "recheck":
        result = recheck_dont(root, args.task_id, args.final_record, args.words, args.actor)
    elif args.command == "handover":
        result = handover(root, args.task_id, args.actor)
    elif args.command == "ack":
        result = acknowledge(root, args.task_id, args.review_record, args.words, args.actor)
    else:
        try:  # 批次三返修 r2 #3（F08 同类）：交付关口记录损坏时如实报 blocked，不计数；之后的报错（如任务不存在）照常计数
            events(root)
            # 批次四开工补修 7：独立任务记录、分派记录读不出来同样算状态（只在读取、解析这一步）
            from standalone_store import history
            from agent_dispatch import read_log, replay
            history(root); replay(read_log(root))
        except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
            from yunxing_rizhi import StateReport, command_error
            command_error(exc)
            print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False, indent=2))
            raise StateReport(1) from None
        result = {**status(root, args.task_id), "strategist_reminders": reminders(root, args.task_id),
                  "final_unchecked": final_checks(root, args.task_id)}
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
