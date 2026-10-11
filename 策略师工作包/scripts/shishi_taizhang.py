#!/usr/bin/env python3
"""事实台账（v1.7.5 G03）：每份稿用到的关键说法一行——说法、所在任务与页、出处（文件与位置）、状态。

- 状态：已确认 / 待核 / 客户口述 / 不得对外。“已确认”须写出处。
- 记录 project/records/fact-ledger.jsonl，只追加（享受 F06 前缀规则：作为分派输入时之后的追加不算改动）；改状态或口径写一条
  update，旧值与原因都留着，不改原件。
- 命令：add 记一条说法；update 改状态或口径（自动列出受影响的页）；cite 补记引用（任务与页）；affected 列出引用某条的任务与页；
  checklist 某任务起草前给策略师的待核清单（白话）；inspect 当前全部条目（给 Agent）。
- 共用：作者派工把台账当前版列为必读（agent_dispatch prepare），作者交回时列出本稿用到的编号（agent_dispatch return --ledger-ref）；
  对外文字扫描（neibu_yongyu）对“待核 / 不得对外”的条目派生慎用说法；检核准备时，上次通过检核之后改过的条目所在页列为必看页
  （shijue_zengliang，即使像素没变）。
- 只记录与提示，不判断说法真假；台账是否维护到位靠规则（AGENTS.md），程序只证明工具可用。"""
import argparse
import json
import re
from pathlib import Path

from phase2_store import WorkflowError, local, now
from standalone_store import digest, text
from workspace_lock import serialized

LOG = "project/records/fact-ledger.jsonl"
STATUSES = ("已确认", "待核", "客户口述", "不得对外")
CAUTION = ("待核", "不得对外")  # 对外文字扫描派生慎用说法的状态
PACKAGE_TEMPLATES = Path(__file__).resolve().parents[1] / "templates/台账慎用说法.txt"
PROJECT_TEMPLATES = "project/inputs/台账慎用说法.txt"
PLACEHOLDER = "{关键词}"
APPENDIX = "## 技术附录（给 Agent）"
_ID = re.compile(r"F\d{3,}")


# ---------------- 记录读写 ----------------
def events(root):
    path = local(root, LOG)
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            raise WorkflowError(f"事实台账第 {number} 行损坏，没有改动原件。技术附录（给 Agent）：{LOG} 第 {number} 行不是 JSON") from None
        if isinstance(item, dict) and item.get("schema_version") == 1 and item.get("event") == "backfill" \
                and isinstance(item.get("dispatch_id"), str):  # 批次四开工补修 1：主控补记作者交回的待补引用（每个分派一条）
            rows.append(item); continue
        if isinstance(item, dict) and item.get("schema_version") == 1 and item.get("event") == "backfill_checked" \
                and isinstance(item.get("dispatch_id"), str) and isinstance(item.get("words"), str) and item["words"].strip():
            rows.append(item); continue  # 候选 r3 #6：主控已核对补记时台账里没有的编号（附原话）
        if not isinstance(item, dict) or item.get("schema_version") != 1 or item.get("event") not in {"add", "update", "cite"} \
                or not isinstance(item.get("fact_id"), str):
            raise WorkflowError(f"事实台账第 {number} 行格式不对，没有改动原件。技术附录（给 Agent）：{LOG} 第 {number} 行缺 schema_version/event/fact_id")
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


def state(root, rows=None):
    """{编号: 当前条目}：说法、状态、出处、关键词、引用（任务与页，去重保序）、历史（旧值与原因）。
    批次三返修 r2 #4：某条记录缺字段或字段类型不对时抛 WorkflowError（白话 + 技术附录），不抛裸 KeyError / TypeError。"""
    out = {}
    for number, e in enumerate(rows if rows is not None else events(root), 1):
        try:
            _apply(out, e)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise WorkflowError(f"事实台账第 {number} 条记录内容不全，没有改动原件。技术附录（给 Agent）：{LOG} 第 {number} 条"
                                f"（{e.get('event') if isinstance(e, dict) else '?'} {e.get('fact_id') if isinstance(e, dict) else ''}）"
                                f"缺字段或类型不对：{type(exc).__name__} {exc}") from None
    return out


def _apply(out, e):
    if e["event"] in ("backfill", "backfill_checked"):  # 补记标记、核对标记本身不改条目（引用由同一次补记写的 cite 带进来）
        return
    fid = e["fact_id"]
    if e["event"] == "add":
        out[fid] = {"fact_id": fid, "claim": e["claim"], "status": e["status"], "source": e.get("source"),
                    "keywords": list(e.get("keywords") or []), "refs": [], "history": [], "created_at": e["created_at"],
                    "updated_at": e["created_at"]}
        _add_refs(out[fid], e.get("refs") or [])
    elif fid in out:
        item = out[fid]
        if e["event"] == "update":
            item["history"].append({"previous": e["previous"], "changes": e["changes"], "reason": e["reason"],
                                    "at": e["created_at"], "actor": e.get("actor")})
            item.update({k: v for k, v in e["changes"].items() if k in {"claim", "status", "source", "keywords"}})
            item["updated_at"] = e["created_at"]
        else:
            _add_refs(item, e.get("refs") or [])


def _add_refs(item, refs):
    seen = {(r["task_id"], r.get("page")) for r in item["refs"]}
    for r in refs:
        key = (r["task_id"], r.get("page"))
        if key not in seen:
            seen.add(key); item["refs"].append({"task_id": r["task_id"], "page": r.get("page")})


# ---------------- 参数 ----------------
def parse_ref(value):
    """引用写法：任务编号:页（页为数字是第几页；也可写“全文”或页标识）；只写任务编号表示整份。"""
    from standalone_store import identifier
    raw = str(value or "").strip()
    task, _, page = raw.partition(":")
    task = task.strip()
    try:
        identifier(task)
    except WorkflowError:
        raise WorkflowError(f"引用写法看不懂：“{raw[:40]}”，这次没有记。请写成“任务编号:第几页”。技术附录（给 Agent）：--ref 须为 TASK 或 TASK:PAGE") from None
    page = page.strip()
    if not page:
        return {"task_id": task, "page": None}
    return {"task_id": task, "page": int(page) if page.isdigit() and int(page) > 0 else page}


def _source(root, path, position):
    if not path:
        if position:
            raise WorkflowError("写了出处位置却没写出处文件，这次没有记。技术附录（给 Agent）：--source-at 须与 --source-path 一起给")
        return None
    target = local(root, path)
    if not target.is_file():
        raise WorkflowError(f"出处文件找不到，这次没有记。技术附录（给 Agent）：--source-path {path} 不是工作包里的文件")
    import hashlib
    return {"path": Path(path).as_posix(), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "position": position or None}


def _status(value):
    if value not in STATUSES:
        raise WorkflowError("状态只能是已确认、待核、客户口述、不得对外四种之一，这次没有记。技术附录（给 Agent）：--status 取值 " + " / ".join(STATUSES))
    return value


def _keywords(values):
    out = []
    for v in values or []:
        v = str(v).strip()
        if v and v not in out:
            out.append(v)
    return out


# ---------------- 命令 ----------------
def add(root, claim, status, actor, source_path=None, source_at=None, refs=(), keywords=(), note=None):
    from registration_guard import actor_check
    actor_check(actor); text(claim, "说法"); _status(status)
    root = Path(root).resolve()
    with serialized(root):
        source = _source(root, source_path, source_at)
        if status == "已确认" and source is None:
            raise WorkflowError("标为已确认的说法要写出处（哪份资料、第几页或第几段），这次没有记。技术附录（给 Agent）：status=已确认 时 --source-path 必填")
        rows = events(root)
        current = state(root, rows)
        parsed = [parse_ref(r) for r in refs]
        words = _keywords(keywords)
        for item in current.values():  # 原样重试：同一说法、同一状态原样返回（补上新给的引用）
            if item["claim"] == claim.strip() and item["status"] == status:
                fresh = [r for r in parsed if (r["task_id"], r["page"]) not in {(x["task_id"], x["page"]) for x in item["refs"]}]
                if fresh:
                    _append(root, {"event": "cite", "fact_id": item["fact_id"], "refs": fresh, "actor": actor, "via": "add_retry"})
                return {**state(root)[item["fact_id"]], "status_of_call": "existing"}
        number = 1 + max([int(fid[1:]) for fid in current] or [0])
        fid = f"F{number:03d}"
        _append(root, {"event": "add", "fact_id": fid, "claim": claim.strip(), "status": status, "source": source, "refs": parsed,
                       "keywords": words, "actor": actor, "note": note})
        return {**state(root)[fid], "status_of_call": "recorded",
                **({"notice": "没写关键词，对外文字扫描只能按整句对照。技术附录（给 Agent）：可用 update --keyword 补"} if not words and status in CAUTION else {})}


def update(root, fact_id, reason, actor, status=None, claim=None, keywords=None, source_path=None, source_at=None):
    from registration_guard import actor_check
    actor_check(actor); text(reason, "改动原因")
    root = Path(root).resolve()
    with serialized(root):
        current = state(root)
        if fact_id not in current:
            raise WorkflowError(f"台账里没有这一条，这次没有改。技术附录（给 Agent）：--id {fact_id} 不存在，现有 " + "、".join(sorted(current)[:20]))
        item = current[fact_id]
        changes = {}
        if status is not None and _status(status) != item["status"]:
            changes["status"] = status
        if claim is not None and text(claim, "说法").strip() != item["claim"]:
            changes["claim"] = claim.strip()
        if keywords is not None and _keywords(keywords) != item["keywords"]:
            changes["keywords"] = _keywords(keywords)
        if source_path is not None:
            source = _source(root, source_path, source_at)
            if source != item["source"]:
                changes["source"] = source
        if not changes:
            return {**item, "status_of_call": "unchanged", "affected": affected(root, fact_id, locked=False)}
        if changes.get("status", item["status"]) == "已确认" and changes.get("source", item["source"]) is None:
            raise WorkflowError("改成已确认要写出处（哪份资料、第几页或第几段），这次没有改。技术附录（给 Agent）：status=已确认 时 --source-path 必填")
        previous = {k: item[k] for k in changes}
        record = _append(root, {"event": "update", "fact_id": fact_id, "changes": changes, "previous": previous, "reason": reason.strip(),
                                "actor": actor})
        out = affected(root, fact_id, locked=False)
        return {**state(root)[fact_id], "status_of_call": "recorded", "record_id": record["record_id"], "affected": out}


def cite(root, fact_id, refs, actor, via="manual", dispatch_id=None, locked=True):
    from registration_guard import actor_check
    actor_check(actor)
    root = Path(root).resolve()

    def run():
        current = state(root)
        if fact_id not in current:
            raise WorkflowError(f"台账里没有这一条，这次没有记引用。技术附录（给 Agent）：编号 {fact_id} 不存在")
        known = {(x["task_id"], x["page"]) for x in current[fact_id]["refs"]}
        fresh = [r for r in (parse_ref(x) if not isinstance(x, dict) else x for x in refs) if (r["task_id"], r["page"]) not in known]
        if fresh:
            _append(root, {"event": "cite", "fact_id": fact_id, "refs": fresh, "actor": actor, "via": via,
                           **({"dispatch_id": dispatch_id} if dispatch_id else {})})
        return {**state(root)[fact_id], "status_of_call": "recorded" if fresh else "existing"}
    if not locked:
        return run()
    with serialized(root):
        return run()


def _titles(root):
    from standalone_store import history
    try:
        rows = history(root)[0]
    except (OSError, ValueError, WorkflowError):
        return {}
    return {e["task_id"]: e["task"].get("title") or e["task_id"] for e in rows}


def _pages_text(pages):
    nums = sorted(p for p in pages if isinstance(p, int))
    others = [p for p in pages if not isinstance(p, int) and p is not None]
    parts = (["第 " + "、".join(map(str, nums)) + " 页"] if nums else []) + [f"“{p}”" for p in others]
    return "、".join(parts) or "整份"


def affected(root, fact_id, locked=True):
    """引用某条说法的任务与页（白话 + 结构化）。"""
    root = Path(root).resolve()
    current = state(root)
    if fact_id not in current:
        raise WorkflowError(f"台账里没有这一条。技术附录（给 Agent）：编号 {fact_id} 不存在")
    item = current[fact_id]
    titles = _titles(root)
    grouped = {}
    for r in item["refs"]:
        grouped.setdefault(r["task_id"], []).append(r["page"])
    tasks = [{"task_id": t, "task_title": titles.get(t, t), "pages": pages} for t, pages in grouped.items()]
    total = sum(len(t["pages"]) for t in tasks)
    used = "；".join(f"「{t['task_title']}」{_pages_text(t['pages'])}" for t in tasks) or "目前没有记下用到它的稿子"
    last = item["history"][-1] if item["history"] else None
    what = (f"台账里 {fact_id}（{_short(item['claim'])}）改了：" + "；".join(
        f"{_field(k)}从“{_value(last['previous'][k])}”改成“{_value(v)}”" for k, v in last["changes"].items()) + "。") if last \
        else f"台账里 {fact_id}（{_short(item['claim'])}）现在是“{item['status']}”。"
    lines = [f"发生了什么：{what}",
             f"对你有什么影响：用到这条说法的有 {total} 处：{used}。" + ("这些页下次检查会重点看，不会因为画面没变就跳过。" if total else ""),
             "要你做什么：" + ("看一下这些页的说法要不要跟着改；改好告诉我，我登记新版。" if total else "不用做。"),
             "", APPENDIX, f"编号 {fact_id}；引用：" + json.dumps(item["refs"], ensure_ascii=False)]
    return {"fact_id": fact_id, "claim": item["claim"], "status": item["status"], "tasks": tasks, "pages_total": total, "text": "\n".join(lines)}


def _short(claim, n=24):
    claim = claim.strip()
    return claim if len(claim) <= n else claim[:n] + "…"


def _field(key):
    return {"claim": "说法", "status": "状态", "source": "出处", "keywords": "关键词"}.get(key, "内容")


def _value(v):
    if isinstance(v, dict):
        return _source_text(v)
    if isinstance(v, list):
        return "、".join(map(str, v)) or "无"
    return "无" if v is None else str(v)


def _source_text(source):
    if not source:
        return "没写出处"
    stem = Path(source["path"]).stem
    name = stem if not re.search(r"[A-Za-z]", stem) else "一份资料（文件名见技术附录）"
    return name + (f"{source['position']}" if source.get("position") else "")


_ADVICE = {"待核": "还没核实，用之前要先确认", "客户口述": "是客户口头说的，用的话写明是客户提供", "不得对外": "不能对外说，稿子里不要出现"}


def checklist(root, task_id):
    """起草前给策略师的待核清单：本任务用到的未确认说法在前，项目里其他未确认的说法在后（起草时要用先核实）；已确认的只报条数。"""
    from standalone_store import identifier
    identifier(task_id)
    root = Path(root).resolve()
    current = state(root)
    title = _titles(root).get(task_id, "这项工作")
    mine = [i for i in current.values() if any(r["task_id"] == task_id for r in i["refs"])]
    others = [i for i in current.values() if i not in mine]
    pending = [i for i in mine if i["status"] != "已确认"]
    pending_other = [i for i in others if i["status"] != "已确认"]
    confirmed = [i for i in current.values() if i["status"] == "已确认"]

    def row(k, i, pages=True):
        where = _pages_text([r["page"] for r in i["refs"] if r["task_id"] == task_id]) if pages else ""
        return (f"{k}. {i['fact_id']}（{_short(i['claim'])}）——{_ADVICE[i['status']]}；出处：{_source_text(i['source'])}"
                + (f"；用在{where}" if pages and where else ""))
    count = len(pending) + len(pending_other)
    if not current:
        what = f"起草「{title}」之前查了事实台账：台账里还没有记任何说法。"
        effect, todo = "稿子里的关键说法（数字、合作、产线、销量这类）还没有记下出处和是否确认过。", \
            "不用马上做；起草时我会把关键说法记进台账，有拿不准的再问你。"
    elif not count:
        what = f"起草「{title}」之前查了事实台账：{len(confirmed)} 条说法都已确认。"
        effect, todo = "可以直接用，不用再核实。", "不用做。"
    else:
        what = f"起草「{title}」之前查了事实台账：有 {count} 条说法还没确认。"
        effect = "这些说法如果直接写进稿子当成已经确认的事，可能对客户说错话。"
        todo = "请逐条告诉我能不能用、用什么说法："
    lines = [f"发生了什么：{what}", f"对你有什么影响：{effect}", f"要你做什么：{todo}"]
    n = 0
    if pending:
        lines.append("这份稿子用到的：")
        for i in pending:
            n += 1; lines.append(row(n, i))
    if pending_other:
        lines.append("项目里其他还没确认的（起草时要用，先核实）：")
        for i in pending_other:
            n += 1; lines.append(row(n, i, pages=False))
    if count and confirmed:
        lines.append(f"已确认的 {len(confirmed)} 条可以直接用。")
    lines += ["", APPENDIX, f"任务 {task_id}；台账 {LOG}；未确认编号 " + "、".join(i["fact_id"] for i in pending + pending_other)]
    return {"task_id": task_id, "pending": [i["fact_id"] for i in pending], "pending_elsewhere": [i["fact_id"] for i in pending_other],
            "confirmed": len(confirmed), "text": "\n".join(lines)}


def inspect(root):
    root = Path(root).resolve()
    current = state(root)
    return {"record_path": LOG, "count": len(current), "facts": list(current.values()),
            "by_status": {s: sum(1 for i in current.values() if i["status"] == s) for s in STATUSES}}


# ---------------- 给其他模块用 ----------------
def ledger_ref(root):
    """台账当前版（路径 + 指纹）与条数；没有台账或台账为空返回 None。作者派工把它列为必读。"""
    path = local(root, LOG)
    if not path.is_file() or not path.read_bytes().strip():
        return None
    import hashlib
    return {"path": LOG, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "entries": len(state(root))}


def check_ids(root, values):
    """作者交回时列出的台账编号：F001 或 F001:页；“无”表示本稿没用到台账里的说法。返回 [(编号, 页)]。"""
    values = [str(v).strip() for v in values or [] if str(v).strip()]
    if values == ["无"]:
        return []
    current = state(root)
    out, bad = [], []
    for v in values:
        fid, _, page = v.partition(":")
        if not _ID.fullmatch(fid) or fid not in current:
            bad.append(v); continue
        page = page.strip()
        out.append((fid, int(page) if page.isdigit() and int(page) > 0 else (page or None)))
    if bad or not values:
        raise WorkflowError("交回时要列出这份稿用到了事实台账里的哪几条（没用到写“无”），这次没有收下。"
                            "技术附录（给 Agent）：--ledger-ref 写 F001 或 F001:页，可重复；没用到写 --ledger-ref 无"
                            + (f"；台账里没有：{'、'.join(bad[:6])}" if bad else ""))
    return out


def ledger_readable(root):
    """台账读得出来（没有台账也算）返回 True；有记录读不出来返回 False（作者交回据此降级）。"""
    try:
        state(root)
        return True
    except (WorkflowError, OSError, ValueError):
        return False


def check_format(values):
    """台账读不出来时作者交回只核编号格式（批次四开工补修 1）：F001 或 F001:页，可重复；没用到写“无”。返回待补引用列表（“无”为 []）。"""
    values = [str(v).strip() for v in values or [] if str(v).strip()]
    if values == ["无"]:
        return []
    bad = [v for v in values if not _ID.fullmatch(v.partition(":")[0]) or v.partition(":")[1] and not v.partition(":")[2].strip()]
    if bad or not values:
        raise WorkflowError("交回时要列出这份稿用到了事实台账里的哪几条（没用到写“无”），这次没有收下。"
                            "技术附录（给 Agent）：--ledger-ref 写 F001 或 F001:页，可重复；没用到写 --ledger-ref 无"
                            + (f"；写法不对：{'、'.join(bad[:6])}" if bad else ""))
    return list(dict.fromkeys(values))


def pending_backfills(root, rows=None):
    """作者交回时存下、还没补记的待补引用：[{分派编号, 任务, 待补}]。分派记录读不出来时抛出（调用方提示）。"""
    from agent_dispatch import read_log, replay
    done = {e.get("dispatch_id") for e in (rows if rows is not None else events(root)) if e.get("event") == "backfill"}
    return [{"dispatch_id": d["dispatch_id"], "task_id": d.get("task_id"), "pending": d["ledger_pending"]}
            for d in replay(read_log(root)).values() if d.get("ledger_pending") and d.get("dispatch_id") not in done]


def unchecked_missing(rows):
    """补记时台账里没有、主控还没登记“已核对”的编号：[(分派编号, 编号)]（候选 r3 #6，续做状态据此提示）。"""
    checked = {e.get("dispatch_id") for e in rows if e.get("event") == "backfill_checked"}
    return [(e.get("dispatch_id"), v) for e in rows if e.get("event") == "backfill" and e.get("dispatch_id") not in checked
            for v in e.get("missing") or []]


def backfill_checked(root, dispatch_id, words, actor):
    """候选 r3 #6：主控核对过补记时台账里没有的编号（作者写错了，或台账漏记已补上）后登记“已核对”，附策略师或主控原话；
    之后续做状态不再提示这一次。按分派编号只登记一次，重跑原样返回；这次补记没有台账里没有的编号时不用登记。"""
    from registration_guard import actor_check
    actor_check(actor)
    if not isinstance(words, str) or not words.strip():
        raise WorkflowError("登记“已核对”要附上策略师或主控核对后的原话，这次没有登记。技术附录（给 Agent）：--checked 须带 --words \"<原话>\"")
    root = Path(root).resolve()
    with serialized(root):
        try:
            rows = events(root)
        except WorkflowError as exc:
            raise WorkflowError("事实台账还有记录读不出来，这次没有登记“已核对”；台账修好后再登记。技术附录（给 Agent）："
                                + str(exc).split("技术附录（给 Agent）：")[-1]) from None
        done = next((e for e in rows if e.get("event") == "backfill_checked" and e.get("dispatch_id") == dispatch_id), None)
        if done:
            return {"dispatch_id": dispatch_id, "checked": done.get("missing"), "words": done.get("words"), "status_of_call": "existing",
                    "text": "这次补记的核对之前已经登记过了，这次没有重复记。"}
        filled = next((e for e in rows if e.get("event") == "backfill" and e.get("dispatch_id") == dispatch_id), None)
        if filled is None:
            raise WorkflowError("这次派工还没有补记，没有要核对的编号，这次没有登记。技术附录（给 Agent）：先 backfill --dispatch-id "
                                + dispatch_id + "，补记时有台账里没有的编号再登记 --checked")
        if not filled.get("missing"):
            raise WorkflowError("这次补记没有台账里没有的编号，不用登记核对。技术附录（给 Agent）：该分派的 backfill 记录 missing 为空")
        _append(root, {"event": "backfill_checked", "dispatch_id": dispatch_id, "task_id": filled.get("task_id"),
                       "missing": filled["missing"], "words": words.strip(), "actor": actor})
        return {"dispatch_id": dispatch_id, "checked": filled["missing"], "words": words.strip(), "status_of_call": "recorded",
                "text": f"记下了：补记时台账里没有的 {len(filled['missing'])} 个编号已经核对过，看进度时不再提醒这一次。"}


def backfill(root, dispatch_id, actor):
    """主控显式补记（批次四开工补修 1）：作者交回时台账读不出来而存下的引用，台账修好后按分派编号补记一次。
    与台账写入同一把锁；台账里没有的编号不补、报给主控；同一分派已补过返回 existing，不重复写。"""
    from registration_guard import actor_check
    from agent_dispatch import read_log, replay
    actor_check(actor)
    root = Path(root).resolve()
    with serialized(root):
        item = replay(read_log(root)).get(dispatch_id)
        if not item:
            raise WorkflowError(f"找不到这次派工，没有补记。技术附录（给 Agent）：分派编号 {dispatch_id} 不存在")
        pending = item.get("ledger_pending")
        if not pending:
            raise WorkflowError("这次派工的作者交回没有要补记的台账编号。技术附录（给 Agent）：交回记录没有 ledger_pending")
        try:
            rows = events(root)
            current = state(root, rows)
        except WorkflowError as exc:
            raise WorkflowError("事实台账还有记录读不出来，这次没有补记；台账修好后再补。技术附录（给 Agent）："
                                + str(exc).split("技术附录（给 Agent）：")[-1]) from None
        done = next((e for e in rows if e.get("event") == "backfill" and e.get("dispatch_id") == dispatch_id), None)
        if done:
            return {**{k: done.get(k) for k in ("dispatch_id", "written", "missing")}, "status_of_call": "existing",
                    "text": "这次派工的台账编号之前已经补记过了，这次没有重复记。"}
        written, missing = [], []
        for value in pending:
            fid, _, page = value.partition(":")
            if fid not in current:
                missing.append(value); continue
            page = page.strip()
            written.append((fid, int(page) if page.isdigit() and int(page) > 0 else (page or None)))
        for fid in dict.fromkeys(f for f, _ in written):
            known = {(x["task_id"], x["page"]) for x in current[fid]["refs"]}
            fresh = [{"task_id": item["task_id"], "page": p} for f, p in written if f == fid and (item["task_id"], p) not in known]
            if fresh:
                _append(root, {"event": "cite", "fact_id": fid, "refs": fresh, "actor": actor, "via": "author_return_backfill",
                               "dispatch_id": dispatch_id})
        names = [f"{f}:{p}" if p is not None else f for f, p in written]
        _append(root, {"event": "backfill", "dispatch_id": dispatch_id, "task_id": item.get("task_id"), "written": names,
                       "missing": missing, "actor": actor})
        said = [f"{f}（“{_short(current[f]['claim'])}”" + (f"，第 {pg} 页" if isinstance(pg, int) else "") + "）" for f, pg in written]
        lost = [f"{v.partition(':')[0]}（台账里没有这一条）" for v in missing]
        text_ = ("作者交回时列的台账编号已补记" + (f"：{'、'.join(said)}" if said else "（没有能补的）") + "。"
                 + (f"其中 {'、'.join(lost)} 没有记；请确认是作者写错了，还是台账漏记了这条说法（漏记就先补进台账）。" if missing else ""))
        return {"dispatch_id": dispatch_id, "written": names, "missing": missing, "status_of_call": "recorded", "text": text_}


def changed_pages(root, task_id, since, until=None):
    """检核用：since（上次通过检核的准备截止时间，批次三返修 r2 #1）之后、until（检核准备时间，None 为现在）之前改过的说法，本任务引用的整数页。
    返回 (页集合, {页: [编号…]})。台账读不出来时抛 WorkflowError（调用方按全页处理）。"""
    rows = [e for e in events(root) if not until or e.get("created_at", "") <= until]  # 引用也按截止时间算（之后补记的页不算进本次）
    current = state(root, rows)  # 坏记录在这里报 WorkflowError（调用方按全页 / 跳过台账对照）
    hit = {}
    for e in rows:
        if e["event"] != "update" or (since and e["created_at"] <= since):
            continue
        hit.setdefault(e["fact_id"], None)
    pages = {}
    for fid in hit:
        for r in current.get(fid, {}).get("refs", []):
            if r["task_id"] == task_id and isinstance(r["page"], int):
                pages.setdefault(r["page"], []).append(fid)
    return set(pages), pages


def changed_facts(root, task_id, since, until=None):
    """检核准备的说明用：since 之后改过、本任务引用的说法（含没写页的整份引用）→ [{编号, 说法, 状态, 页}]。"""
    rows = [e for e in events(root) if not until or e.get("created_at", "") <= until]
    current = state(root, rows)
    ids = list(dict.fromkeys(e["fact_id"] for e in rows if e["event"] == "update" and (not since or e["created_at"] > since)))
    out = []
    for fid in ids:
        item = current.get(fid)
        pages = [r["page"] for r in (item or {}).get("refs", []) if r["task_id"] == task_id]
        if item and pages:
            out.append({"fact_id": fid, "claim": item["claim"], "status": item["status"], "pages": pages})
    return out


_GROUP = re.compile(r"^\[([^\]{}]+)\]\s*(.+)$")  # “[组名] 适用词正则”：其后的模板只套在说法里含这些词的条目上
# 关键词两侧容许的引号、书名号、括号（批次三返修 r2 #9）
_OPEN = "[「『“‘\"'《〈【（(\\[]?"
_CLOSE = "[」』”’\"'》〉】）)\\]]?"
# 否定词前置（同一分句里、紧挨着说法之前）与“还在接触 / 洽谈 / 探讨”这类照实写法：模板派生的提示不报（整句说法、不得对外的关键词照报）
# 批次四开工补修 4：“前所未有”“未经”“未免”“未尝”这类成语里的“未”不算否定
_NEGATION = re.compile(r"(?:尚未|暂未|没有|还没|并未|未曾|(?<!前所)未(?!来|必|经|免|尝|有之))")
# 批次四开工补修 4：“沟通 / 洽谈”后跟“好、完、妥、定、成”（谈好了、洽谈成功）是谈成了，照常提示
_EXPLORING = re.compile(r"^.{0,4}?(?:初步接触|接触|洽谈|商谈|探讨|商讨|讨论|沟通|磋商|谈判)(?![好完妥定成])")
_CLAUSE = re.compile(r"[。！？；，、：,.!?;:\n]")


def _templates(root, notices=None):
    """[(组适用词正则或 None, 模板行)]。包内模板 + 项目追加；“[组名] 适用词”一行开一组（项目文件里组外的行一律适用）。
    组行的正则写错时该组按一律适用、给白话提示（notices）。"""
    lines = []
    for path in (PACKAGE_TEMPLATES, Path(root) / PROJECT_TEMPLATES):
        group = None
        if not path.is_file():
            continue
        for x in path.read_text(encoding="utf-8").splitlines():
            x = x.strip()
            if not x or x.startswith("#"):
                continue
            m = _GROUP.match(x) if PLACEHOLDER not in x else None
            if m:
                try:
                    group = re.compile(m.group(2))
                except re.error:
                    group = None
                    if notices is not None:
                        notices.append(("group", x))
                continue
            lines.append((group, x))
    return lines


def _key_pattern(word):
    """关键词：两侧容许引号、书名号、括号，字与字之间容许空白（“乙 巧克力”）。"""
    from neibu_yongyu import fold
    chars = [c for c in fold(word) if not c.isspace()]
    return _OPEN + r"\s*" + r"\s*".join(map(re.escape, chars)) + r"\s*" + _CLOSE


def softened(text, start, end):
    """模板派生的命中是否属于照实写法：同一分句里紧挨着前面有否定词（尚未、未、没有、暂未…；“前所未有”“未经”这类成语不算），
    命中段内部有否定词，或紧跟着“开始初步接触 / 洽谈 / 共同探讨”这类还在谈的说法（后跟“好、完、妥、定、成”的除外）。是则不提示。"""
    before = _CLAUSE.split(text[max(0, start - 8):start])[-1]
    after = _CLAUSE.split(text[end:end + 12])[0]
    inside = text[start:end]  # 批次四开工补修 4：命中段内部有否定词（“和乙还没有一起”“乙暂未联名”）也是照实写
    return bool(_NEGATION.search(before) or _NEGATION.search(inside) or _EXPLORING.search(after))


def caution_terms(root, notices=None):
    """对外文字扫描用：“待核 / 不得对外”条目派生的慎用说法 → [(类别, 正则, 编号, 状态, 模板派生)]。没有台账返回 []。
    派生：整句说法；关键词套进慎用说法模板（“已和{关键词}一起”“{关键词}联名”“{关键词}家喻户晓”这类，包内模板 + 项目
    project/inputs/台账慎用说法.txt 追加，每行一条正则，{关键词} 处换成关键词；“[组名] 适用词”开头的组只套在说法本身含适用词的
    条目上，一组都套不上时全部套，偏严）；“不得对外”的关键词本身出现也提示。
    批次三返修 r2 #4：notices 给出时（扫描、检核准备、对外导出），台账读不出来返回 [] 并记一条白话提示；模板某行正则写错只跳过
    那一行并提示。不给 notices 时照旧抛 WorkflowError。"""
    from neibu_yongyu import fold

    def note(text):
        if notices is None:
            raise WorkflowError(text)
        notices.append(text)
    try:
        current = state(root)
    except WorkflowError as exc:
        if notices is None:
            raise
        notices.append("事实台账有记录读不出来，这次没有对照台账里待核、不得对外的说法；台账修好后再扫一次。技术附录（给 Agent）："
                       + str(exc).split("技术附录（给 Agent）：")[-1])
        return []
    caution = [i for i in current.values() if i["status"] in CAUTION]
    if not caution:
        return []
    bad_groups = []
    templates = _templates(root, bad_groups)
    for _, line in bad_groups:
        note(f"台账慎用说法模板有一行写错了（{line[:30]}），这一组按全部条目都套。技术附录（给 Agent）：组行正则无效")
    compiled, bad = [], []
    for group, line in templates:
        if PLACEHOLDER not in line:
            continue
        try:
            re.compile(line.replace(PLACEHOLDER, "合成"))
            compiled.append((group, line))
        except re.error as exc:
            bad.append((line, exc))
    for line, exc in bad:
        note(f"台账慎用说法模板有一行写错了（{line[:30]}），这一行这次跳过，其余照常对照。技术附录（给 Agent）：正则无效（{exc}）")
    out = []
    for item in caution:
        label = f"台账对照（{item['fact_id']} {item['status']}）"
        claim = re.sub(r"\s+", "", fold(item["claim"]))
        if len(claim) >= 4:
            out.append((label, re.compile(r"\s*".join(map(re.escape, claim))), item["fact_id"], item["status"], False))
        fits = [line for group, line in compiled if group is not None and group.search(item["claim"])]
        use = [line for group, line in compiled if group is None or not fits or group.search(item["claim"])]
        for word in item["keywords"]:
            key = _key_pattern(word)
            for line in use:
                try:
                    out.append((label, re.compile(line.replace(PLACEHOLDER, key)), item["fact_id"], item["status"], True))
                except re.error as exc:
                    note(f"台账慎用说法模板有一行写错了（{line[:30]}），这一行这次跳过。技术附录（给 Agent）：正则无效（{exc}）")
            if item["status"] == "不得对外":
                out.append((label, re.compile(key), item["fact_id"], item["status"], False))
    return out


def main():
    parser = argparse.ArgumentParser(description="事实台账：记说法、改状态、列受影响页、起草前待核清单")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add"); a.add_argument("--claim", required=True); a.add_argument("--status", required=True)
    a.add_argument("--source-path"); a.add_argument("--source-at"); a.add_argument("--ref", action="append", default=[])
    a.add_argument("--keyword", action="append", default=[]); a.add_argument("--note"); a.add_argument("--actor", required=True)
    u = sub.add_parser("update"); u.add_argument("--id", required=True); u.add_argument("--reason", required=True)
    u.add_argument("--status"); u.add_argument("--claim"); u.add_argument("--keyword", action="append")
    u.add_argument("--source-path"); u.add_argument("--source-at"); u.add_argument("--actor", required=True)
    c = sub.add_parser("cite"); c.add_argument("--id", required=True); c.add_argument("--ref", action="append", required=True)
    c.add_argument("--actor", required=True)
    f = sub.add_parser("affected"); f.add_argument("--id", required=True)
    k = sub.add_parser("checklist"); k.add_argument("--task-id", required=True)
    sub.add_parser("inspect")
    b = sub.add_parser("backfill"); b.add_argument("--dispatch-id", required=True); b.add_argument("--actor", required=True)
    b.add_argument("--checked", action="store_true",
                   help="候选 r3：补记时台账里没有的编号核对过了（作者写错或台账漏记已补），登记“已核对”，续做状态不再提示；须带 --words")
    b.add_argument("--words", help="与 --checked 同用：策略师或主控核对后的原话")
    for p in (a, u, c, f, k, b) + tuple(x for x in sub.choices.values() if x.prog.endswith("inspect")):
        p.add_argument("--workspace", type=Path, required=True); p.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command in {"affected", "checklist", "inspect"}:
        # 批次三返修 r2 #3（F08 同类）：三个查询遇台账损坏如实报 blocked，退出码 1，重试守卫不计数；读得出来之后的报错照常计数
        try:
            state(args.workspace.resolve())
        except (WorkflowError, OSError, ValueError) as exc:
            from yunxing_rizhi import StateReport, command_error
            command_error(exc)
            print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False, indent=2))
            raise StateReport(1) from None
    if args.command == "add":
        result = add(args.workspace, args.claim, args.status, args.actor, args.source_path, args.source_at, args.ref, args.keyword, args.note)
    elif args.command == "update":
        result = update(args.workspace, args.id, args.reason, args.actor, args.status, args.claim, args.keyword, args.source_path, args.source_at)
    elif args.command == "cite":
        result = cite(args.workspace, args.id, args.ref, args.actor)
    elif args.command == "backfill" and args.checked:
        result = backfill_checked(args.workspace, args.dispatch_id, args.words, args.actor)
    elif args.command == "backfill":
        if args.words:
            raise WorkflowError("写了原话却没说是登记核对。技术附录（给 Agent）：--words 只与 --checked 同用")
        result = backfill(args.workspace, args.dispatch_id, args.actor)
    elif args.command == "affected":
        result = affected(args.workspace, args.id)
    elif args.command == "checklist":
        result = checklist(args.workspace, args.task_id)
    else:
        result = inspect(args.workspace)
    if args.json or args.command == "inspect":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result.get("text") or json.dumps(result, ensure_ascii=False, indent=2))
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
