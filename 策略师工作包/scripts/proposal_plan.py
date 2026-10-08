#!/usr/bin/env python3
"""未签约提案项目：无合同也有一份可续做的提案计划。

每个阶段落成一个独立任务记录（各自计版本与改稿次数），可选把调研/品牌屋/内部提案
挂成正式计划（plan_type=proposal）以使用原有正式门禁与 deck_export。
不生成项管 Excel、不进合同结项、不伪造合同确认；签约后 upgrade 由合同计划引用提案成果，历史保留。"""
import argparse
import json
from datetime import date
from pathlib import Path

from phase2_store import WorkflowError, local, now
from standalone_store import digest, identifier, text
from workspace_lock import serialized

LOG = "project/records/proposal-plan.jsonl"
TEMPLATE = (
    ("S1", "任务简报", "整理提案目标、对象、截止时间与已知约束，形成任务简报"),
    ("S2", "调研证据", "收集并分析调研证据，区分事实、观点、推断"),
    ("S3", "品牌屋", "按证据形成品牌屋与推导（可删减）"),
    ("S4", "故事线/逐字稿", "写故事线与能直接念的逐字稿"),
    ("S5", "视觉方向", "确定视觉方向与样式"),
    ("S6", "HTML 演示稿", "按逐字稿制作 HTML 演示稿并独立检核"),
    ("S7", "对外交付", "对外导出、扫描内部用语，有检核或对外豁免后登记发客户"),
)
TASK_STAGES = {"S1", "S2", "S3", "S4", "S5", "S6"}
FORMAL = {"S2": ("RES", "调研分析"), "S3": ("BH", "品牌屋初稿"), "S6": ("PROP", "内部提案")}


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
            raise WorkflowError(f"提案计划记录第 {number} 行损坏，未修改原件") from exc
        if not isinstance(item, dict) or item.get("schema_version") != 1 or "plan_id" not in item:
            raise WorkflowError(f"提案计划记录第 {number} 行格式无效")
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
        raise WorkflowError("找不到提案计划：" + plan_id)
    state = {}
    for event in own:
        if event["event"] in {"created", "stages_changed"}:
            state.update(stages=event["stages"], removed=event.get("removed", state.get("removed", [])))
        if event["event"] == "created":
            state.update({k: event[k] for k in ("plan_id", "goal", "deadline", "project_label", "user_words")})
        if event["event"] == "formal_published":
            state["formal_plan"] = event["plan_ref"]; state["formal_tasks"] = event.get("formal_tasks")
        if event["event"] == "upgraded":
            state["upgraded_to"] = event["contract_plan_ref"]
    state["revision"] = len(own)
    state["events"] = [e["event"] for e in own]
    return state


def _request_once(root, request_id, body):
    """同一请求编号重复执行返回原事件；内容不同拒绝。"""
    digest_value = digest(body)
    for event in _events(root):
        if event.get("request_id") == request_id:
            if event.get("request_hash") != digest_value:
                raise WorkflowError("请求编号已用于不同内容，保留原记录")
            return event
    return None


def _stage_request(plan_id, stage, project_label, goal, history_refs=()):
    sid, name, describe = stage
    task_id = f"{plan_id}-{sid}"
    task = dict(title=f"提案·{name}", project_label=project_label,
                request_text=f"提案计划 {plan_id} 的“{name}”阶段。提案目标：{goal}。阶段要求：{describe}",
                goal=describe, status="planned", summary="提案计划建立，尚未开始",
                next_action=f"开始“{name}”：{describe}", owner=None, due_date=None,
                sources=[], deliverables=[], completion_evidence="",
                source_request_key=f"proposal:{plan_id}:{sid}")
    if history_refs:
        task["historical_refs"] = list(history_refs)
    return dict(task_id=task_id, request_id=f"{task_id}-create", expected_revision=0,
                reason=f"提案计划 {plan_id} 建立阶段任务", task=task)


def _precheck(root, requests, actor):
    """写任何记录之前核对：阶段任务编号未被别的内容占用；上次中断留下的同内容阶段任务可原样续接。"""
    from standalone_store import history
    events = history(root)[0]
    for request in requests:
        prior = next((e for e in events if e["request_id"] == request["request_id"]), None)
        if prior and prior["request_hash"] != digest({"request": request, "actor": actor}):
            raise WorkflowError(f"阶段任务 {request['task_id']} 已存在且内容不同（可能是上次中断或另一份计划留下的）；先核对，未写任何记录")
        if not prior and any(e["task_id"] == request["task_id"] for e in events):
            raise WorkflowError(f"阶段任务编号 {request['task_id']} 已被占用；换 plan_id，未写任何记录")


def _stage_task(root, plan_id, stage, project_label, goal, actor, history_refs=()):
    from standalone_tasks import save
    request = _stage_request(plan_id, stage, project_label, goal, history_refs)
    save(root, request, actor)
    return request["task_id"]


def create(root, request, actor):
    """request: plan_id, request_id, user_words, goal, deadline(可空), project_label,
    remove_stages(可空: [{"stage":"S3","words":"原话"}]), attach(可空: {"stage":"S6","task_id":"旧任务"})
    先核对全部输入再写；中途失败时阶段任务按原请求编号可原样续接（同一请求重跑即补齐），不会留下无计划的不同内容。"""
    from registration_guard import actor_check
    root = Path(root).resolve(); actor_check(actor)
    allowed = {"plan_id", "request_id", "user_words", "goal", "deadline", "project_label", "remove_stages", "attach"}
    if not isinstance(request, dict) or not {"plan_id", "request_id", "user_words", "goal", "project_label"} <= set(request) \
            or set(request) - allowed:
        raise WorkflowError("提案计划请求须含 plan_id/request_id/user_words/goal/project_label，不接受其它字段")
    plan_id = identifier(request["plan_id"]); identifier(request["request_id"])
    for name in ("user_words", "goal", "project_label"):
        text(request[name], name)
    deadline = request.get("deadline")
    if deadline is not None:
        try:
            date.fromisoformat(deadline)
        except (TypeError, ValueError) as exc:
            raise WorkflowError("截止时间须为 YYYY-MM-DD，或留空") from exc
    removed = request.get("remove_stages") or []
    ids = {s[0] for s in TEMPLATE}
    if not isinstance(removed, list) or any(not isinstance(r, dict) or set(r) != {"stage", "words"}
                                            or r["stage"] not in ids or r["stage"] == "S7" for r in removed):
        raise WorkflowError("删减阶段须写明阶段编号（S1—S6）及策略师原话；对外交付关口不能删")
    for r in removed:
        text(r["words"], "删减阶段原话")
    attach = request.get("attach")
    with serialized(root):
        prior = _request_once(root, request["request_id"], {"request": request, "actor": actor})
        if prior:
            return {**prior, "status": "existing"}
        if any(e["plan_id"] == plan_id for e in _events(root)):
            raise WorkflowError("提案计划编号已存在；修改阶段用 stages 命令，不能重建清零")
        history_refs = _attach_refs(root, attach) if attach else []
        kept = [s for s in TEMPLATE if s[0] not in {r["stage"] for r in removed}]
        requests = {s[0]: _stage_request(plan_id, s, request["project_label"], request["goal"],
                                         history_refs if attach and attach["stage"] == s[0] else ())
                    for s in kept if s[0] in TASK_STAGES}
        _precheck(root, requests.values(), actor)
        from standalone_tasks import save
        stages = []
        for stage in kept:
            entry = {"stage": stage[0], "name": stage[1], "describe": stage[2]}
            if stage[0] in TASK_STAGES:
                save(root, requests[stage[0]], actor); entry["task_id"] = requests[stage[0]]["task_id"]
                if attach and attach["stage"] == stage[0] and history_refs:
                    entry["history_from"] = {"task_id": attach["task_id"], "note": "旧任务及其成果作历史来源（按旧记录登记的指纹）；不改写旧记录、不继承旧检核为通过"}
            stages.append(entry)
        event = {"schema_version": 1, "event": "created", "plan_id": plan_id, "request_id": request["request_id"],
                 "request_hash": digest({"request": request, "actor": actor}), "created_at": now(), "actor": actor,
                 "user_words": request["user_words"], "goal": request["goal"], "deadline": deadline,
                 "project_label": request["project_label"], "stages": stages, "removed": removed,
                 "scope": "未签约提案计划；不是合同确认，不生成项管 Excel，不进合同结项统计"}
        return _append(root, event)


def _attach_refs(root, attach):
    """挂入旧任务：按旧任务记录里登记的指纹引用（不是现场文件的指纹）；旧文件已改动或已移走也可挂入，作历史来源。"""
    from standalone_store import history
    if not isinstance(attach, dict) or set(attach) != {"stage", "task_id"} or attach["stage"] not in TASK_STAGES:
        raise WorkflowError("挂入须写明阶段（S1—S6）与旧独立任务编号")
    own = [e for e in history(root)[0] if e["task_id"] == attach["task_id"]]
    if not own:
        raise WorkflowError("找不到要挂入的旧独立任务")
    task = own[-1]["task"]
    refs = [{"label": f"旧任务{attach['task_id']}：{r['label']}", "path": r["path"], "sha256": r["sha256"]}
            for r in task["deliverables"] if "path" in r and r.get("sha256")]
    return refs


def _set_task_status(root, task_id, status, words, actor, reason):
    from standalone_store import history, request_task
    from standalone_tasks import save
    own = [e for e in history(root)[0] if e["task_id"] == task_id]
    if not own:
        return None
    current = own[-1]
    task = request_task(current["task"])
    if status is None:
        # 恢复：回到删减前的状态（含完成依据与下一步），不一律改成 planned
        before = next((e for e in reversed(own) if e["task"]["status"] != "cancelled"), None)
        if current["task"]["status"] != "cancelled":
            return current
        old = before["task"] if before else {"status": "planned", "completion_evidence": "", "next_action": task["next_action"]}
        status = old["status"]
        task.update(status=status, completion_evidence=old.get("completion_evidence", ""), next_action=old.get("next_action", task["next_action"]),
                    summary=f"{reason}，回到删减前的状态（策略师原话：{words[:80]}）")
    else:
        if current["task"]["status"] == status:
            return current
        task.update(status=status, completion_evidence="" if status != "completed" else task["completion_evidence"],
                    summary=f"{reason}（策略师原话：{words[:80]}）",
                    next_action="本阶段已删减，不再推进" if status == "cancelled" else task["next_action"])
    return save(root, {"task_id": task_id, "request_id": f"{task_id}-{status}-{current['revision'] + 1}",
                       "expected_revision": current["revision"], "reason": reason, "task": task}, actor)


def change_stages(root, plan_id, words, remove=(), restore=(), actor=None, request_id=None):
    """删减/恢复阶段：须有策略师原话；删减的阶段任务标为已取消，恢复时重新打开同一任务并回到删减前的状态（不新建、不清零）。
    签约升级后提案计划只读；什么都不改的命令不写事件；同一请求重试返回原事件。"""
    from registration_guard import actor_check
    actor_check(actor); text(words, "策略师原话")
    remove, restore = list(remove or []), list(restore or [])
    if not remove and not restore:
        raise WorkflowError("没有要删减或恢复的阶段（--remove / --restore），未写事件")
    if set(remove) & set(restore):
        raise WorkflowError("同一阶段不能同时删减和恢复，未写事件")
    body = {"plan_id": plan_id, "words": words, "remove": sorted(remove), "restore": sorted(restore), "actor": actor}
    request_id = identifier(request_id) if request_id else "stages-" + digest(body)[:32]
    with serialized(root):
        prior = _request_once(root, request_id, body)
        if prior:
            return {**prior, "status": "existing"}
        state = current(root, plan_id)
        if state.get("upgraded_to"):
            raise WorkflowError("提案计划已签约升级为合同计划，阶段不再修改（提案记录只读保留）")
        stages = list(state["stages"]); removed = list(state.get("removed", []))
        for sid in remove:
            if sid == "S7" or sid not in {s["stage"] for s in stages}:
                raise WorkflowError(f"不能删除阶段 {sid}")
        for sid in restore:
            entry = next((s for s in TEMPLATE if s[0] == sid), None)
            if not entry or sid in {s["stage"] for s in stages}:
                raise WorkflowError(f"不能恢复阶段 {sid}")
        for sid in remove:
            gone = next(s for s in stages if s["stage"] == sid)
            stages = [s for s in stages if s["stage"] != sid]
            removed.append({"stage": sid, "words": words})
            if gone.get("task_id"):
                _set_task_status(root, gone["task_id"], "cancelled", words, actor, f"提案计划删减阶段 {sid}")
        for sid in restore:
            entry = next(s for s in TEMPLATE if s[0] == sid)
            item = {"stage": sid, "name": entry[1], "describe": entry[2]}
            if sid in TASK_STAGES:
                task_id = f"{plan_id}-{sid}"
                reopened = _set_task_status(root, task_id, None, words, actor, f"提案计划恢复阶段 {sid}")
                item["task_id"] = task_id if reopened else _stage_task(root, plan_id, entry, state["project_label"], state["goal"], actor)
            stages.append(item)
            removed = [r for r in removed if r["stage"] != sid]
        stages.sort(key=lambda s: s["stage"])
        event = _append(root, {"schema_version": 1, "event": "stages_changed", "plan_id": plan_id, "created_at": now(),
                               "request_id": request_id, "request_hash": digest(body),
                               "actor": actor, "user_words": words, "stages": stages, "removed": removed})
        if state.get("formal_plan") and set(remove + restore) & set(FORMAL):
            event = {**event, "formal_notice": "已有正式计划与阶段不再一致：运行 proposal_plan.py formal 重新发布正式计划"}
        return event


def inspect(root, plan_id=None, tasks=None):
    """当前阶段 = 第一个未完成阶段；对外交付阶段以发客户登记为准。tasks 可传入已核对过的任务列表复用，避免重复读原件。"""
    from standalone_tasks import inspect as task_inspect
    events = _events(root)
    plans = sorted({e["plan_id"] for e in events})
    if plan_id:
        plans = [plan_id]
    if not plans:
        return {"status": "empty", "plans": [], "record_path": LOG}
    tasks_given = tasks is not None
    tasks = {t["task_id"]: t for t in (tasks if tasks is not None else task_inspect(root, include_archived=True)["tasks"])}
    result = []
    for pid in plans:
        state = current(root, pid)
        rows = []
        for stage in state["stages"]:
            if stage["stage"] == "S7":
                done = _delivered(root, state)
                rows.append({**stage, "status": "completed" if done else "planned",
                             "next_action": "已登记发客户" if done else "对外导出→内部用语扫描→有检核或对外豁免后登记发客户"})
                continue
            t = tasks.get(stage["task_id"], {})
            missing = "cancelled" if tasks_given else "missing"  # resume 只给当前任务；不在其中即已取消/归档
            rows.append({**stage, "status": t.get("effective_status", missing), "revision": t.get("revision"),
                         "next_action": t.get("next_action", "阶段任务缺失，请核对")})
        open_rows = [r for r in rows if r["status"] not in {"completed", "cancelled", "archived"}]
        cur = open_rows[0] if open_rows else None
        result.append({"plan_id": pid, "goal": state["goal"], "deadline": state["deadline"],
                       "current_stage": cur["name"] if cur else "全部阶段完成",
                       "next_action": (f"{cur['name']}：{cur['next_action']}" if cur else "提案已发客户；等待反馈或签约后升级为合同项目"),
                       "stages": rows, "removed": state.get("removed", []),
                       "formal_plan": state.get("formal_plan"), "upgraded_to": state.get("upgraded_to"),
                       "scope": "未签约提案计划；不代表合同确认或客户批准"})
    return {"status": "recorded" if result else "empty", "plans": result, "record_path": LOG}


def _delivered(root, state):
    try:
        from jiaofu_guankou import delivered
    except ImportError:
        return False
    deck = next((s for s in state["stages"] if s["stage"] == "S6"), None)
    return bool(deck and delivered(root, deck["task_id"]))


def formal(root, plan_id, actor, as_of=None):
    """把保留的调研/品牌屋/内部提案阶段挂成正式计划，使用原有 phase3/phase5 门禁与 deck_export。
    正式逐字稿须绑定品牌屋与推导：删了品牌屋则内部提案不挂正式，继续走阶段任务。"""
    from registration_guard import actor_check
    from phase2_store import publish, archive
    actor_check(actor)
    root = Path(root).resolve()
    with serialized(root):
        state = current(root, plan_id)
        if state.get("upgraded_to"):
            if state.get("formal_plan"):
                return {"status": "existing", "plan_ref": state["formal_plan"], "notice": "已签约升级，提案计划只读"}
            raise WorkflowError("提案计划已签约升级为合同计划，不再挂提案正式计划")
        kept = {s["stage"] for s in state["stages"]}
        wanted = [f"{plan_id}-{FORMAL[sid][0]}" for sid in ("S2", "S3", "S6")
                  if sid in kept and not (sid == "S6" and "S3" not in kept)]
        if state.get("formal_plan"):
            if wanted == state.get("formal_tasks"):
                return {"status": "existing", "plan_ref": state["formal_plan"]}
            if not wanted:
                raise WorkflowError("阶段删减后没有可挂正式流程的阶段；已发布的正式计划保留为历史")
        start = as_of or date.today().isoformat()
        end = state["deadline"] or start
        tasks, previous = [], None
        for sid in ("S2", "S3", "S6"):
            if sid not in kept or (sid == "S6" and "S3" not in kept):
                continue
            code, title = FORMAL[sid]
            task_id = f"{plan_id}-{code}"
            tasks.append({"task_id": task_id, "source_item_ids": [], "title": title, "task_type": "提案阶段",
                          "dependencies": [previous] if previous else [], "deliverable": title, "acceptance": "按正式门禁",
                          "start": start, "end": end, "status": "未开始", "proposal_stage": sid})
            previous = task_id
        if not tasks:
            raise WorkflowError("没有可挂正式流程的阶段（调研证据/品牌屋/演示稿均已删减）")
        words = local(root, f"project/records/proposal-plans/{plan_id}-策略师原话.md")
        words.parent.mkdir(parents=True, exist_ok=True)
        if not words.exists():
            words.write_text(state["user_words"] + "\n", encoding="utf-8")
        payload = {"schema_version": "0.2", "plan_type": "proposal", "proposal_plan_id": plan_id,
                   "contract_ref": None, "common": {}, "projects": {}, "tasks": tasks,
                   "project_start": start, "hard_deadline": state["deadline"], "status": "proposal",
                   "scope": "未签约提案计划；不是合同排期，不导出项管 Excel"}
        meta = publish(root, "plan", payload, actor, dependencies=[],
                       source_files=[{"path": words.relative_to(root).as_posix(), "sha256": __import__("phase2_store").sha(words)}])
        ref = {"kind": "plan", "version": meta["version"], "sha256": meta["sha256"]}
        _append(root, {"schema_version": 1, "event": "formal_published", "plan_id": plan_id, "created_at": now(),
                       "actor": actor, "plan_ref": ref, "formal_tasks": [t["task_id"] for t in tasks]})
        return {"status": "republished" if state.get("formal_plan") else "published", "plan_ref": ref, "formal_tasks": tasks}


def formal_gate(root, meta, data):
    """phase2 gate 的提案计划分支：结构有效、由提案计划记录发布、策略师原话原件未变。不要求合同或项管模板。"""
    from phase2_store import sha
    if data.get("plan_type") != "proposal" or data.get("contract_ref") is not None:
        raise WorkflowError("不是提案计划")
    state = current(root, data.get("proposal_plan_id"))
    ref = state.get("formal_plan")
    if ref != {"kind": "plan", "version": meta["version"], "sha256": meta["sha256"]}:
        raise WorkflowError("当前正式计划不是由提案计划记录发布的版本")
    for source in meta["source_files"]:
        path = local(root, source["path"])
        if not path.is_file() or sha(path) != source["sha256"]:
            raise WorkflowError("提案计划策略师原话原件改变或丢失")
    if not data.get("tasks") or any(t.get("proposal_stage") not in FORMAL for t in data["tasks"]):
        raise WorkflowError("提案计划正式任务结构无效")
    return meta


def published_versions(root):
    """提案计划记录里登记过的正式计划版本（version, sha256）。"""
    return {(e["plan_ref"]["version"], e["plan_ref"]["sha256"]) for e in _events(root) if e.get("event") == "formal_published"}


def is_proposal_plan(root):
    from phase2_store import registry, read_json
    history = registry(root)["artifacts"].get("plan", [])
    if not history:
        return False
    try:
        return read_json(local(root, history[-1]["path"])).get("plan_type") == "proposal"
    except (OSError, ValueError):
        return False


def upgrade(root, plan_id, context_path, actor, words):
    """签约后：合同计划生成时写入 proposal_origin 引用提案阶段成果；提案记录与旧版本全部保留。"""
    from registration_guard import actor_check
    actor_check(actor); text(words, "签约升级原话")
    root = Path(root).resolve()
    with serialized(root):
        state = current(root, plan_id)
        if state.get("upgraded_to"):
            return {"status": "existing", "contract_plan_ref": state["upgraded_to"]}
        report = inspect(root, plan_id)["plans"][0]
        origin = {"proposal_plan_id": plan_id, "record_path": LOG,
                  "stages": [{"stage": s["stage"], "name": s["name"], "task_id": s.get("task_id"), "status": s["status"],
                              "revision": s.get("revision"), "outputs": _stage_outputs(root, s.get("task_id"))} for s in report["stages"]],
                  "formal_plan": state.get("formal_plan"), "formal_outputs": _formal_outputs(root, state.get("formal_tasks") or []),
                  "note": "提案阶段成果按登记版本（路径+指纹）作为合同计划的已有来源引用；不继承检核或客户批准"}
        from build_schedule import build
        result = build(root, context_path, actor, proposal_origin=origin)
        meta = result["artifact"]
        ref = {"kind": "plan", "version": meta["version"], "sha256": meta["sha256"]}
        _append(root, {"schema_version": 1, "event": "upgraded", "plan_id": plan_id, "created_at": now(), "actor": actor,
                       "user_words": words, "contract_plan_ref": ref})
        return {"status": "upgraded", "contract_plan": result, "proposal_origin": origin}


def _stage_outputs(root, task_id):
    """阶段任务当前登记的成果版本：路径+指纹（外部链接按 URL），并标出现场文件是否仍是该版本。"""
    if not task_id:
        return []
    import hashlib
    from standalone_store import history
    own = [e for e in history(root)[0] if e["task_id"] == task_id]
    if not own:
        return []
    result = []
    for ref in own[-1]["task"]["deliverables"]:
        if "url" in ref:
            result.append({"label": ref["label"], "url": ref["url"]}); continue
        path = local(root, ref["path"])
        live = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == ref["sha256"]
        result.append({"label": ref["label"], "path": ref["path"], "sha256": ref["sha256"], "revision": own[-1]["revision"],
                       "live_matches": live})
    return result


def _formal_outputs(root, formal_tasks):
    """挂正式门禁的提案阶段（调研/品牌屋/内部提案）当前正式成果版本。"""
    if not formal_tasks:
        return []
    try:
        from phase6_targets import catalog
        return [{"task_id": item["task_id"], "target": item["target"]} for item in catalog(root).values()
                if item.get("task_id") in set(formal_tasks)]
    except (OSError, ValueError, KeyError):
        return [{"status": "unknown", "note": "正式成果目录无法读取，未绑定"}]


def main():
    parser = argparse.ArgumentParser(description="未签约提案项目：提案计划建立、阶段删减、正式门禁挂接与签约升级")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "inspect", "stages", "formal", "upgrade"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", type=Path, required=True)
        p.add_argument("--json", action="store_true")
        if name != "inspect":
            p.add_argument("--actor", required=True)
        if name == "create":
            p.add_argument("--request", type=Path, required=True)
        else:
            p.add_argument("--plan-id", required=name != "inspect")
        if name == "stages":
            p.add_argument("--words", required=True)
            p.add_argument("--request-id")
            p.add_argument("--remove", action="append", default=[])
            p.add_argument("--restore", action="append", default=[])
        if name == "upgrade":
            p.add_argument("--context", type=Path, required=True)
            p.add_argument("--words", required=True)
    args = parser.parse_args()
    if args.command == "create":
        result = create(args.workspace, json.loads(args.request.read_text(encoding="utf-8")), args.actor)
    elif args.command == "stages":
        result = change_stages(args.workspace, args.plan_id, args.words, args.remove, args.restore, args.actor, args.request_id)
    elif args.command == "formal":
        result = formal(args.workspace, args.plan_id, args.actor)
    elif args.command == "upgrade":
        result = upgrade(args.workspace, args.plan_id, args.context, args.actor, args.words)
    else:
        result = inspect(args.workspace, args.plan_id)
    from standalone_tasks import with_retention
    print(json.dumps(with_retention(result), ensure_ascii=False, indent=2))
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
