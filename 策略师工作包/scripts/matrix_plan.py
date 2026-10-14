#!/usr/bin/env python3
"""产品矩阵策略的计划：一份产品矩阵计划，每个阶段落成一个独立任务（各自计版本、改稿次数、检核与交付）。

参照 proposal_plan.py 的做法（阶段表、一阶段一任务、可删减、对外交付关口）。区别：
- 起点二选一写进计划：客户给了成本资料 → 正推；只给了定价锚点（如“12.8 元/斤”）→ 反推，反推时成本资料列“待补”，不得写成已知；
- 每个阶段都可删减（不能全部删光），删减要记一句理由；
- 不挂正式计划、不做签约升级；
- 本文件不含任何利润、价格、成本计算：数字只来自产品线测算平台已保存的数据（见 pingtai_kuaizhao.py）。
另外提供两个“能填能检核”的表格检查：选品打分表（check-scoring）、竞品对照表（check-competitors）——只查填没填全、有没有来源，不打分、不算综合分。"""
import argparse
import json
import re
from datetime import date
from pathlib import Path

from phase2_store import WorkflowError, local, now
from standalone_store import digest, identifier, text
from workspace_lock import serialized

LOG = "project/records/matrix-plan.jsonl"
TEMPLATE = (
    ("M1", "任务简报与起点", "写明起点（正推或反推）、客户给了什么资料、成本资料是已提供还是待补，以及本项目要做哪几步（每一步做不做记一句理由）"),
    ("M2", "市场与品类研判", "趋势、竞品（规格、价格、渠道，带日期与来源）、原料成本波动、口味趋势；证据登记可回查，查不到如实写未查到，不编数字"),
    ("M3", "选品判断", "多维度打分表加四象限，写明做什么、不做什么及理由（维度可按项目增删）"),
    ("M4", "矩阵结构与产品角色", "按价格带、利润、渠道定每款的角色（形象款、引流款、利润款、基础盘），用金字塔或系列分类摆出矩阵"),
    ("M5", "产品卡", "每款一张卡：角色、定位、产品、规格、零售/促销价、主推渠道、表现策略；规格与价格取自平台已保存的数据，不手抄"),
    ("M6", "平台测算", "用产品线测算平台测算（正推或反推，多轮方案比较，保存后再查看一遍结果）；平台用不了时标“待平台测算”，不给任何利润数字"),
    ("M7", "产品线规划表格", "平台导出的 HTML 提报与数据一起存档并登记快照；给客户前核对负责人、状态、审核意见有没有保留"),
    ("M8", "逐字稿", "按已定的矩阵和测算结论写能直接念的逐字稿"),
    ("M9", "HTML 演示稿", "按逐字稿做 HTML 演示稿；产品卡区块的数字与绑定的平台快照一致，并经独立检核"),
    ("M10", "对外交付", "对外导出、扫描内部用语，有检核或策略师说不用检核之后，登记发给客户"),
)
TASK_STAGES = {s[0] for s in TEMPLATE} - {"M10"}
KEEP = set()  # 没有不能删的阶段（起点与成本状态记在计划里，不靠 M1 任务）；删减要记一句理由，且不能把阶段全部删光
START_NAMES = {"forward": "正推", "reverse": "反推"}
# 定价锚点的写法（只用来提示起点，不做任何换算）：“12.8 元一斤”“12.8元/斤”“每斤 12.8 元”“一斤 12.8 元”。
# 成本或周期的写法不算锚点：“21000 元/吨”“5000 元一个月”。
_NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"  # 收尾小修 P2-D：带千分位的数（21,000）不截断
_UNIT_AFTER = re.compile(r"(" + _NUM + r")\s*元\s*(?:/|每|一|／)\s*([^\s，。；,;]{1,4})")
_UNIT_BEFORE = re.compile(r"(?:每|一|1)\s*([^\s\d，。；,;是为卖定价]{1,3})\s*(?:[是为卖定价]{0,2})\s*(\d+(?:\.\d+)?)\s*元")
NOT_ANCHOR_UNITS = ("吨", "月", "个月", "年", "天", "日", "周", "季", "小时", "次")
WEIGHT_UNITS = ("斤", "公斤", "千克", "kg", "KG", "Kg")
# 全盘细查返修 P2-10：原话里的成本写法（只认字，不算数）——“21000 元/吨”这类按吨的价一律算成本；“18 元/公斤（千克、kg）”前面紧挨着
# 投料、原料、成本、包材、采购这类字时也算成本。认出来又有定价锚点，就是“两样都有”，先问策略师。
_COST_TON = re.compile(_NUM + r"\s*元\s*(?:/|每|一|／)\s*吨")
# 成本字眼和数之间若出现“卖、售、希望、想、定价、零售”，说的是想卖的价（“采购方希望卖 25 元/公斤”），不是成本
_COST_KG = re.compile(r"(?:投料|原料|原材料|物料|成本|包材|采购|进价|进货价|料价)(?:(?!卖|售|希望|想|定价|零售)[^，。；,;\d]){0,6}" + _NUM + r"\s*元\s*(?:/|每|一|／)\s*(?:公斤|千克|kg|KG|Kg)")


def cost_in_words(*texts):
    """原话里提到的成本写法（那段原文），没有返回 None。只认字，不换算。"""
    for text_value in texts:
        if isinstance(text_value, str):
            found = _COST_TON.search(text_value) or _COST_KG.search(text_value)
            if found:
                return found.group(0)
    return None
# v1.7.6 W2：按重量给的锚点只提示怎么问，不换算（换算是平台的事）
WEIGHT_ANCHOR_HINT = ("这个价格是按斤（重量）给的：先问清每一款是散装按斤卖，还是定量装（一袋、一盒固定重量）。"
                      "散装的直接用平台的散装反推（平台按 500 克算）；定量装的按平台测算技能的问法问清每袋（盒）想卖多少钱，工作包不自己换算；"
                      "问不清或平台走不通的款，平台测算这一步写“待平台测算”并写明原因。")


def anchor_unit(anchor_text):
    """锚点原文里的单位（“12.8 元/斤”→“斤”）；读不出返回 None。只认字，不换算。"""
    if not isinstance(anchor_text, str):
        return None
    found = _UNIT_AFTER.search(anchor_text)
    if found:
        return found.group(2)
    found = _UNIT_BEFORE.search(anchor_text)
    return found.group(1) if found else None


def anchor_hint(anchor_text):
    """W2：锚点是按重量给的（斤、公斤……）时返回提问提示，否则 None。"""
    unit = anchor_unit(anchor_text)
    return WEIGHT_ANCHOR_HINT if unit and unit.startswith(WEIGHT_UNITS) else None


def find_anchor(*texts):
    """在文字里找定价锚点，找到返回那段原文，找不到返回 None。单位是吨、月、年这类成本或周期的不算。"""
    for text_value in texts:
        if not isinstance(text_value, str):
            continue
        # 收尾小修 P2-D：先把原话里的成本片段拿掉，再找锚点（“原料 18 元/公斤，希望卖 12.8 元一斤”的锚点是 12.8 元一斤）
        text_value = _COST_TON.sub("，", _COST_KG.sub("，", text_value))
        for found in _UNIT_AFTER.finditer(text_value):
            if not found.group(2).startswith(NOT_ANCHOR_UNITS):
                return found.group(0)
        for found in _UNIT_BEFORE.finditer(text_value):
            if not found.group(1).startswith(NOT_ANCHOR_UNITS):
                return found.group(0)
    return None


def _events(root):
    path = local(root, LOG)
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"产品矩阵计划记录第 {number} 行损坏，未修改原件") from exc
        if not isinstance(item, dict) or item.get("schema_version") != 1 or "plan_id" not in item:
            raise WorkflowError(f"产品矩阵计划记录第 {number} 行格式无效")
        rows.append(item)
    return rows


def _append(root, event):
    path = local(root, LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def current(root, plan_id):
    own = [e for e in _events(root) if e["plan_id"] == plan_id]
    if not own:
        raise WorkflowError("找不到产品矩阵计划：" + plan_id)
    state = {}
    for event in own:
        if event["event"] in {"created", "stages_changed"}:
            state.update(stages=event["stages"], removed=event.get("removed", state.get("removed", [])))
        if event["event"] == "created":
            state.update({k: event[k] for k in ("plan_id", "goal", "deadline", "project_label", "user_words", "start", "price_anchor",
                                                 "cost_status", "cost_sources")})
    state["revision"] = len(own)
    return state


def _request_once(root, request_id, body):
    value = digest(body)
    for event in _events(root):
        if event.get("request_id") == request_id:
            if event.get("request_hash") != value:
                raise WorkflowError("请求编号已用于不同内容，保留原记录")
            return event
    return None


def decide_start(request):
    """起点二选一（P01）。返回 (start, cost_status, cost_sources, price_anchor)。

    - 写了 start 就按写的；没写：只给了定价锚点（原话或 price_anchor 里有“数字 + 元/单位”）且没有成本资料 → 反推；
      有成本资料且没有锚点 → 正推；两样都有或两样都没有 → 说不准，要问策略师，不替她猜。
    - 反推：成本资料没写出处就一律“待补”，不得写成已知成本；正推：必须写明客户给了哪些成本资料。"""
    costs = request.get("cost_sources") or []
    if not isinstance(costs, list) or any(not isinstance(c, str) or not c.strip() for c in costs):
        raise WorkflowError("成本资料（cost_sources）须是一组非空说明，每条写清客户给的是什么、出处在哪")
    anchor = request.get("price_anchor")
    if anchor is not None:
        text(anchor, "定价锚点")
    found = find_anchor(anchor, request["user_words"])
    has_anchor = bool(anchor) or bool(found)
    said_cost = cost_in_words(request["user_words"])
    start = request.get("start")
    if start is None:
        if has_anchor and not costs and not said_cost:
            start = "reverse"
        elif costs and not has_anchor:
            start = "forward"
        else:
            both = (f"原话里既提到了成本（“{said_cost}”），又有定价锚点（“{anchor or found}”）。" if said_cost and has_anchor and not costs
                    else f"原话里提到了成本（“{said_cost}”），但还没写明是客户给的哪份成本资料。" if said_cost and not costs else "")
            raise WorkflowError("起点说不准：" + both + "先问策略师——客户是给了成本资料（投料吨价、包材报价等，走正推），"
                                "还是给了一个定价锚点（如“12.8 元/斤”，走反推）？问清后再建计划，不替她猜")
    if start not in START_NAMES:
        raise WorkflowError("起点只能是 forward（正推）或 reverse（反推）")
    if start == "forward" and not costs:
        raise WorkflowError("正推要写明客户给了哪些成本资料（cost_sources）；没有成本资料就不是正推，改选反推或先把成本列为待补")
    if start == "reverse" and not has_anchor:
        raise WorkflowError("反推要写明客户给的定价锚点（price_anchor，如“12.8 元/斤”）")
    anchor_text = anchor or found
    return start, ("已提供" if costs else "待补"), costs, anchor_text


def _stage_request(plan_id, stage, project_label, goal, start_line):
    sid, name, describe = stage
    task_id = f"{plan_id}-{sid}"
    task = dict(title=f"产品矩阵·{name}", project_label=project_label,
                request_text=f"产品矩阵计划 {plan_id} 的“{name}”阶段。项目目标：{goal}。起点：{start_line}。阶段要求：{describe}",
                goal=describe, status="planned", summary="产品矩阵计划建立，尚未开始",
                next_action=f"开始“{name}”：{describe}", owner=None, due_date=None,
                sources=[], deliverables=[], completion_evidence="",
                source_request_key=f"matrix:{plan_id}:{sid}")
    return dict(task_id=task_id, request_id=f"{task_id}-create", expected_revision=0,
                reason=f"产品矩阵计划 {plan_id} 建立阶段任务", task=task)


def start_line(start, cost_status, anchor):
    return (f"{START_NAMES[start]}" + (f"（客户给的定价锚点：{anchor}）" if anchor else "") + f"；成本资料：{cost_status}")


def _removed_list(value, ids, what):
    if not isinstance(value, list) or any(not isinstance(r, dict) or not {"stage", "reason"} <= set(r) or set(r) - {"stage", "reason", "words"}
                                          or r["stage"] not in ids or r["stage"] in KEEP for r in value):
        raise WorkflowError(f"{what}须写明阶段编号（M1—M10）和一句理由")
    for r in value:
        text(r["reason"], what + "理由")
    return value


def create(root, request, actor):
    """request: plan_id, request_id, user_words, goal, project_label, [start, price_anchor, cost_sources, deadline, remove_stages]
    remove_stages: [{"stage":"M2","reason":"客户已给齐竞品资料"}]。先核对全部输入再写；同一请求重跑返回原事件。"""
    from registration_guard import actor_check
    root = Path(root).resolve(); actor_check(actor)
    allowed = {"plan_id", "request_id", "user_words", "goal", "deadline", "project_label", "remove_stages", "start", "price_anchor", "cost_sources"}
    if not isinstance(request, dict) or not {"plan_id", "request_id", "user_words", "goal", "project_label"} <= set(request) \
            or set(request) - allowed:
        raise WorkflowError("产品矩阵计划请求须含 plan_id/request_id/user_words/goal/project_label，不接受其它字段")
    plan_id = identifier(request["plan_id"]); identifier(request["request_id"])
    for name in ("user_words", "goal", "project_label"):
        text(request[name], name)
    deadline = request.get("deadline")
    if deadline is not None:
        try:
            date.fromisoformat(deadline)
        except (TypeError, ValueError) as exc:
            raise WorkflowError("截止时间须为 YYYY-MM-DD，或留空") from exc
    removed = _removed_list(request.get("remove_stages") or [], {s[0] for s in TEMPLATE}, "删减阶段")
    if len({r["stage"] for r in removed}) >= len(TEMPLATE):
        raise WorkflowError("阶段不能全部删光，至少留一个")
    start, cost_status, costs, anchor = decide_start(request)
    line = start_line(start, cost_status, anchor)
    with serialized(root):
        prior = _request_once(root, request["request_id"], {"request": request, "actor": actor})
        if prior:
            return {**prior, "status": "existing"}
        if any(e["plan_id"] == plan_id for e in _events(root)):
            raise WorkflowError("产品矩阵计划编号已存在；修改阶段用 stages 命令，不能重建清零")
        kept = [s for s in TEMPLATE if s[0] not in {r["stage"] for r in removed}]
        requests = {s[0]: _stage_request(plan_id, s, request["project_label"], request["goal"], line) for s in kept if s[0] in TASK_STAGES}
        from proposal_plan import _precheck
        _precheck(root, requests.values(), actor)
        from standalone_tasks import save
        stages = []
        for stage in kept:
            entry = {"stage": stage[0], "name": stage[1], "describe": stage[2]}
            if stage[0] in TASK_STAGES:
                save(root, requests[stage[0]], actor); entry["task_id"] = requests[stage[0]]["task_id"]
            stages.append(entry)
        event = {"schema_version": 1, "event": "created", "plan_id": plan_id, "request_id": request["request_id"],
                 "request_hash": digest({"request": request, "actor": actor}), "created_at": now(), "actor": actor,
                 "user_words": request["user_words"], "goal": request["goal"], "deadline": deadline,
                 "project_label": request["project_label"], "stages": stages, "removed": removed,
                 "start": start, "price_anchor": anchor, "cost_status": cost_status, "cost_sources": costs, "start_line": line,
                 "scope": "产品矩阵计划；数字只来自产品线测算平台已保存的数据，本计划不含任何计算"}
        _append(root, event)
        hint = anchor_hint(anchor)
        return {**event, "anchor_hint": hint} if hint else event


def change_stages(root, plan_id, reason, remove=(), restore=(), actor=None, request_id=None):
    """删减 / 恢复阶段，须写一句理由；删减的阶段任务标为已取消，恢复时重新打开同一任务，不新建、不清零。"""
    from registration_guard import actor_check
    from proposal_plan import _set_task_status
    actor_check(actor); text(reason, "理由")
    remove, restore = list(remove or []), list(restore or [])
    if not remove and not restore:
        raise WorkflowError("没有要删减或恢复的阶段（--remove / --restore），未写事件")
    if set(remove) & set(restore):
        raise WorkflowError("同一阶段不能同时删减和恢复，未写事件")
    body = {"plan_id": plan_id, "reason": reason, "remove": sorted(remove), "restore": sorted(restore), "actor": actor}
    with serialized(root):
        state = current(root, plan_id)
        events = [e for e in _events(root) if e["plan_id"] == plan_id]
        # 自动请求编号带上当前计划版本：删减、恢复、再删减同一个阶段不会被当成重复操作；紧接着原样重试（上一条事件就是同样的内容）才返回原事件
        explicit = identifier(request_id) if request_id else None
        if not explicit and events[-1]["event"] == "stages_changed" and events[-1].get("body_hash") == digest(body):
            return {**events[-1], "status": "existing"}      # 紧接着原样重试：上一条事件就是同样的内容
        request_id = explicit or "stages-" + digest({**body, "revision": state["revision"]})[:32]
        prior = _request_once(root, request_id, body)
        if prior:
            return {**prior, "status": "existing"}
        stages = list(state["stages"]); removed = list(state.get("removed", []))
        for sid in remove:
            if sid not in {s["stage"] for s in stages}:
                raise WorkflowError(f"不能删除阶段 {sid}：计划里本来就没有这个阶段")
        if len(stages) - len(remove) < 1:
            raise WorkflowError("阶段不能全部删光，至少留一个")
        for sid in restore:
            if not any(s[0] == sid for s in TEMPLATE) or sid in {s["stage"] for s in stages}:
                raise WorkflowError(f"不能恢复阶段 {sid}")
        for sid in remove:
            gone = next(s for s in stages if s["stage"] == sid)
            stages = [s for s in stages if s["stage"] != sid]
            removed.append({"stage": sid, "reason": reason})
            if gone.get("task_id"):
                _set_task_status(root, gone["task_id"], "cancelled", reason, actor, f"产品矩阵计划删减阶段 {sid}")
        line = start_line(state["start"], state["cost_status"], state.get("price_anchor"))
        for sid in restore:
            entry = next(s for s in TEMPLATE if s[0] == sid)
            item = {"stage": sid, "name": entry[1], "describe": entry[2]}
            if sid in TASK_STAGES:
                task_id = f"{plan_id}-{sid}"
                reopened = _set_task_status(root, task_id, None, reason, actor, f"产品矩阵计划恢复阶段 {sid}")
                if not reopened:
                    from standalone_tasks import save
                    save(root, _stage_request(plan_id, entry, state["project_label"], state["goal"], line), actor)
                item["task_id"] = task_id
            stages.append(item)
            removed = [r for r in removed if r["stage"] != sid]
        order = {s[0]: n for n, s in enumerate(TEMPLATE)}
        stages.sort(key=lambda s: order[s["stage"]])
        return _append(root, {"schema_version": 1, "event": "stages_changed", "plan_id": plan_id, "created_at": now(),
                              "request_id": request_id, "request_hash": digest(body), "body_hash": digest(body), "actor": actor, "reason": reason,
                              "stages": stages, "removed": removed})


def _delivered(root, state):
    try:
        from jiaofu_guankou import delivered
    except ImportError:
        return False
    deck = next((s for s in state["stages"] if s["stage"] == "M9"), None)
    return bool(deck and delivered(root, deck["task_id"]))


def _plan_snapshot(root, plan_id):
    """W3：计划关联的最新一版平台快照；快照记录读不出来不拦计划查询，照实写一句。"""
    try:
        from pingtai_kuaizhao import plan_snapshot
    except ImportError:
        return None, None
    try:
        return plan_snapshot(root, plan_id), None
    except (WorkflowError, OSError, ValueError, KeyError, TypeError):
        return None, "平台快照记录读不出来，平台测算状态核实不了"


def inspect(root, plan_id=None, tasks=None):
    """当前阶段 = 第一个未完成阶段；对外交付以发客户登记为准。tasks 可传入已核对过的任务列表，避免重复读原件。"""
    from standalone_tasks import inspect as task_inspect
    events = _events(root)
    plans = sorted({e["plan_id"] for e in events})
    if plan_id:
        plans = [plan_id]
    if not plans:
        return {"status": "empty", "plans": [], "record_path": LOG}
    given = tasks is not None
    if not given:
        found = task_inspect(root, include_archived=True)
        if found.get("status") == "blocked":
            raise WorkflowError("独立任务记录读不出来，产品矩阵计划的阶段进度这次核对不了：" + "；".join(map(str, found.get("errors") or []))[:300])
        tasks = found["tasks"]
    tasks = {t["task_id"]: t for t in tasks}
    result = []
    for pid in plans:
        state = current(root, pid)
        rows = []
        for stage in state["stages"]:
            if stage["stage"] == "M10":
                done = _delivered(root, state)
                rows.append({**stage, "status": "completed" if done else "planned",
                             "next_action": "已登记发客户" if done else "对外导出→内部用语扫描→有检核或对外豁免后登记发客户"})
                continue
            t = tasks.get(stage["task_id"], {})
            missing = "cancelled" if given else "missing"
            rows.append({**stage, "status": t.get("effective_status", missing), "revision": t.get("revision"),
                         "next_action": t.get("next_action", "阶段任务缺失，请核对")})
        open_rows = [r for r in rows if r["status"] not in {"completed", "cancelled", "archived"}]
        cur = open_rows[0] if open_rows else None
        m6_open = "M6" in {s["stage"] for s in state["stages"]} and any(
            r["stage"] == "M6" and r["status"] not in {"completed", "cancelled", "archived"} for r in rows)
        snapshot, snapshot_note = _plan_snapshot(root, pid)
        result.append({"plan_id": pid, "goal": state["goal"], "deadline": state["deadline"],
                       "start": state["start"], "start_name": START_NAMES[state["start"]], "cost_status": state["cost_status"],
                       "price_anchor": state.get("price_anchor"),
                       "start_line": start_line(state["start"], state["cost_status"], state.get("price_anchor")),
                       "current_stage": cur["name"] if cur else "全部阶段完成",
                       "next_action": (f"{cur['name']}：{cur['next_action']}" if cur else "产品矩阵已发客户；等待反馈"),
                       "stages": rows, "removed": state.get("removed", []),
                       "platform_pending": m6_open and snapshot is None,
                       "platform_status": (snapshot["label"] if snapshot else snapshot_note or ("平台测算待做" if m6_open else None)),
                       "platform_snapshot": snapshot, "anchor_hint": anchor_hint(state.get("price_anchor")),
                       "scope": "产品矩阵计划；不代表客户批准"})
    return {"status": "recorded", "plans": result, "record_path": LOG}


# ---------------- 能填能检核的表格（只查填全、范围、来源，不打分、不算综合分） ----------------
DECISIONS = ("做", "不做", "观察")


def _load_table(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"表格读不出来：{exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError("表格须是一个 JSON 对象（照 templates 里的模板填）")
    return data


def check_scoring(path):
    """选品打分表：维度可增删；每个候选在每个维度都有一个落在该维度刻度内的评价、一个四象限位置、做 / 不做 / 观察的结论和理由。"""
    data = _load_table(path)
    problems = []
    dims = data.get("dimensions")
    if not isinstance(dims, list) or not dims:
        problems.append("维度（dimensions）至少要有一个；维度可增删，但不能是空的")
        dims = []
    names = []
    for d in dims:
        if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not d["name"].strip() \
                or not isinstance(d.get("scale"), list) or len(d["scale"]) < 2:
            problems.append("每个维度要写名称（name）和至少两档的刻度（scale，如 [\"高\",\"中\",\"低\"]）")
            continue
        if d["name"] in names:
            problems.append(f"维度“{d['name']}”写了两次")
        names.append(d["name"])
    quadrants = data.get("quadrants")
    labels = [q.get("label") for q in quadrants] if isinstance(quadrants, list) and all(isinstance(q, dict) for q in quadrants) else []
    if len(labels) < 2 or any(not isinstance(x, str) or not x.strip() for x in labels):
        problems.append("四象限（quadrants）每格要写名称（label）")
    items = data.get("candidates")
    if not isinstance(items, list) or not items:
        problems.append("候选（candidates）至少要有一个")
        items = []
    seen = set()
    for index, item in enumerate(items, 1):
        who = item.get("name") if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip() else f"第 {index} 个候选"
        if not isinstance(item, dict):
            problems.append(f"{who}：格式不对"); continue
        if who in seen:
            problems.append(f"{who}：名称重复")
        seen.add(who)
        scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
        for d in dims:
            if not isinstance(d, dict) or d.get("name") not in names:
                continue
            value = scores.get(d["name"])
            if value is None or (isinstance(value, str) and not value.strip()):
                problems.append(f"{who}：维度“{d['name']}”还没填")
            elif value not in d["scale"]:
                problems.append(f"{who}：维度“{d['name']}”的评价“{value}”不在刻度 {d['scale']} 里")
        extra = set(scores) - set(names)
        if extra:
            problems.append(f"{who}：有评价对不上维度：{'、'.join(sorted(extra))}")
        if labels and item.get("quadrant") not in labels:
            problems.append(f"{who}：四象限位置要从 {labels} 里选")
        decision = item.get("decision")
        if decision not in DECISIONS:
            problems.append(f"{who}：结论要写“做”“不做”或“观察”")
        elif not isinstance(item.get("reason"), str) or not item["reason"].strip():
            problems.append(f"{who}：结论“{decision}”要写一句理由")
    return {"status": "ok" if not problems else "incomplete", "problems": problems, "candidates": len(items), "dimensions": len(names),
            "note": "只查有没有填全、是否落在刻度内；不替你打分，也不算综合分——综合判断由策略师写在理由里"}


def check_competitors(path):
    """竞品对照表：每一行的价格都要有来源和日期；没有来源的数字不能进演示稿。查不到的如实写“未查到”，不写数字。"""
    data = _load_table(path)
    rows = data.get("rows")
    problems, usable, unfound = [], [], []
    if not isinstance(rows, list) or not rows:
        return {"status": "incomplete", "problems": ["竞品（rows）至少要有一行"], "usable_rows": [], "not_found_rows": []}
    ids = set()
    for index, row in enumerate(rows, 1):
        who = f"第 {index} 行"
        if not isinstance(row, dict):
            problems.append(f"{who}：格式不对"); continue
        rid = row.get("id")
        if not isinstance(rid, str) or not rid.strip():
            problems.append(f"{who}：要有编号（id），演示稿靠它找到这一行")
            continue
        who = f"竞品 {rid}"
        if rid in ids:
            problems.append(f"{who}：编号重复")
        ids.add(rid)
        for name in ("brand", "product", "spec", "channel"):
            if not isinstance(row.get(name), str) or not row[name].strip():
                problems.append(f"{who}：{ {'brand': '品牌', 'product': '产品', 'spec': '规格', 'channel': '渠道'}[name] }还没填")
        price = row.get("price")
        if price == "未查到":
            unfound.append(rid)
            continue
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            problems.append(f"{who}：价格要写数字，查不到就写“未查到”，不要编"); continue
        date_text, source = row.get("as_of"), row.get("source")
        try:
            date.fromisoformat(date_text)
        except (TypeError, ValueError):
            problems.append(f"{who}：价格要有查到的日期（as_of，YYYY-MM-DD）"); continue
        if not isinstance(source, str) or not source.strip():
            problems.append(f"{who}：价格要有来源（source：链接、证据编号或资料名），没有来源的数字不进演示稿"); continue
        usable.append(rid)
    return {"status": "ok" if not problems else "incomplete", "problems": problems, "usable_rows": usable, "not_found_rows": unfound,
            "note": "usable_rows 是价格有来源有日期、可以进演示稿的行；not_found_rows 在演示稿里只能写“未查到”"}


def main():
    parser = argparse.ArgumentParser(description="产品矩阵计划：建立、阶段删减与恢复、查看进度，以及选品打分表、竞品对照表的检查")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "inspect", "stages"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", type=Path, required=True)   # 全盘细查返修 P2-3：总是输出 JSON，不再接受不起作用的 --json
        if name != "inspect":
            p.add_argument("--actor", required=True)
        if name == "create":
            p.add_argument("--request", type=Path, required=True)
        else:
            p.add_argument("--plan-id", required=name != "inspect")
        if name == "stages":
            p.add_argument("--reason", required=True)
            p.add_argument("--request-id")
            p.add_argument("--remove", action="append", default=[])
            p.add_argument("--restore", action="append", default=[])
    for name in ("check-scoring", "check-competitors"):
        p = sub.add_parser(name)
        p.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "check-scoring":
        result = check_scoring(args.file)
    elif args.command == "check-competitors":
        result = check_competitors(args.file)
    elif args.command == "create":
        result = create(args.workspace, json.loads(args.request.read_text(encoding="utf-8")), args.actor)
    elif args.command == "stages":
        result = change_stages(args.workspace, args.plan_id, args.reason, args.remove, args.restore, args.actor, args.request_id)
    else:
        try:  # 同 proposal_plan：计划记录或独立任务记录读不出来时如实报 blocked，不计入“同类报错 2 次即停”
            if _events(args.workspace):
                from standalone_tasks import inspect as task_inspect
                found = task_inspect(args.workspace, include_archived=True)
                if found.get("status") == "blocked":
                    raise WorkflowError("独立任务记录读不出来，产品矩阵计划的阶段进度这次核对不了：" + "；".join(map(str, found.get("errors") or []))[:300])
        except (WorkflowError, OSError, ValueError) as exc:
            from yunxing_rizhi import StateReport, command_error
            command_error(exc)
            print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False, indent=2))
            raise StateReport(1) from None
        result = inspect(args.workspace, args.plan_id)
    if args.command in {"create", "stages", "inspect"}:
        from standalone_tasks import with_retention
        result = with_retention(result)
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
