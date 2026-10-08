#!/usr/bin/env python3
"""Record explicit fresh-Agent handoffs without pretending to spawn one."""
from __future__ import annotations
from workspace_lock import read_serialized
from task_validation import read_check

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dispatch_files import file_ref, local, text_ref, verify_refs, readback_ref
from phase2_store import WorkflowError, atomic_json, local as phase2_local
from workspace_lock import serialized
from registration_guard import actor_check, dispatch_write, request_metadata

LOG = "project/records/agent-dispatch.jsonl"
INDEX = "project/records/agent-dispatch-index.json"
ROLES = {
    "project-controller", "strategy-author", "proposal-author", "deck-builder",
    "design-expression-reviewer", "independent-reviewer",
}
ACTIVE = {"prepared", "assigned", "returned_for_readback"}
AUTHOR_ROLES = {"strategy-author", "proposal-author", "deck-builder", "project-controller"}


@read_check
def read_log(root: Path) -> list[dict]:
    path = root / LOG
    if not path.exists():
        return []
    records: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"分派记录损坏，第 {number} 行不能解析") from exc
        if not isinstance(value, dict):
            raise WorkflowError(f"分派记录损坏，第 {number} 行不是对象")
        records.append(value)
    return records


def append(root: Path, event: dict) -> dict:
    actor_check(event.get("actor"))
    path = root / LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"record_id": uuid.uuid4().hex,
              "created_at": datetime.now(timezone.utc).isoformat(), **event, **request_metadata()}
    atomic_json(phase2_local(root, INDEX), {"schema_version": 1, "last_record_id": record["record_id"]})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


def latest(records: list[dict], dispatch_id: str) -> dict | None:
    found = [item for item in records if item.get("dispatch_id") == dispatch_id]
    return found[-1] if found else None


def replay(records: list[dict]) -> dict[str, dict]:
    states: dict[str, dict] = {}
    for item in records:
        dispatch_id = item.get("dispatch_id")
        if not dispatch_id:
            raise WorkflowError("分派记录缺少 dispatch_id")
        states.setdefault(dispatch_id, {}).update(item)
    return states


def project_context(root: Path, task_id: str) -> tuple[dict, dict]:
    from project_memory_store import inspect as inspect_memory
    memory = inspect_memory(root)
    if memory["errors"]:
        raise WorkflowError("项目记忆无法安全加载：" + "；".join(memory["errors"]))
    memory_snapshot = {key: memory[key] for key in ("memory_version", "digest", "current_facts")}
    from task_context import resolve_task, business_signature
    has_task_records = ((root / "project/records/standalone-tasks.jsonl").exists()
                        or (root / "project/tasks/task-plan.json").exists())
    try:
        task = resolve_task(root, task_id)
        task_snapshot = {"status": "verified", "kind": task["kind"],
                         "reference": task["reference"],
                         "business_signature": business_signature(task["task"])}
    except WorkflowError:
        if has_task_records:
            raise
        task_snapshot = {"status": "unverified_legacy", "kind": None, "reference": None}
    return memory_snapshot, task_snapshot


def verify_context(root: Path, item: dict) -> None:
    verify_refs(root, item)
    expected_memory = item.get("project_memory")
    expected_task = item.get("task_context")
    if expected_memory is None and expected_task is None:
        return
    if item.get('review_group_id'):
        from shencha_zu import load,frozen
        frozen(root,load(root,item['review_group_id'])[1])
    if item.get('review_target'):
        from incremental_review import current_input
        current_input(root,item['review_target'])
    current_memory, current_task = project_context(root, item["task_id"])
    if expected_memory != current_memory:
        raise WorkflowError("项目记忆版本已变化或来源失效，须重新准备分派")
    if expected_task and "business_signature" in expected_task:
        same = (expected_task.get("business_signature") == current_task.get("business_signature")
                and expected_task.get("kind") == current_task.get("kind")
                and current_task.get("status") == "verified")
    else:
        legacy_current = {k: v for k, v in current_task.items() if k != "business_signature"}
        same = expected_task == legacy_current
    if not same:
        raise WorkflowError("任务上下文已变化，须重新准备分派")


import re as _re

WRITE_VERB = r"(?:写入|写到|写在|写进|保存到|保存在|保存至|输出到|输出至|生成到|生成在|存到|存放到|存放在|放到|放在|放进|产出到|交付到|落盘到|导出到|写)"
# 单字“不/别”只在不构成常用词时算否定：“分别、区别、不同、不断”等不是否定
NEGATION = (r"(?:不要|不得|不能|不准|不许|禁止|切勿|勿|(?<![分区类个特告识差级性鉴辨判离派送])别(?![的人处名致样])"
            r"|不(?!同|断|少|仅|过|论|管|久|足|错|一|如|然|妨|但|只|光|单|止|久|好|对|再|仅))")
PATH = r"(project/[^\s，。；、,;：:\"'（()）【】]+)"
from author_budget import AUTHOR_BUDGET
DEFAULT_BUDGET = dict(AUTHOR_BUDGET)
QUOTE = _re.compile(r"【引用原话[^】]*】.*?【引用结束】", _re.S)


def _norm(path):
    return path.rstrip("/").rstrip("。.")


def _clauses(text):
    return [c for c in _re.split(r"[。；;！!？?\n]|，(?=[^，]*(?:不要|不得|禁止|别|但))", QUOTE.sub("", text)) if c.strip()]


def write_conflicts(task_text: str, permissions: list) -> list:
    """U06/K10：派工原文要求写入的路径必须在 permissions 允许写入的范围内；冲突逐项指名字段。
    否定句（“不要写入 project/records”）不算要求写入；“只读/仅读/不改/禁止写”算禁止，不算允许；只有写类动词才算允许。"""
    wanted = set()
    for clause in _clauses(task_text):
        for m in _re.finditer(WRITE_VERB + r"\s*`?" + PATH, clause):
            if not _negated(clause, max(0, m.start() - 3), m.start()):
                wanted.add(_norm(m.group(1)))
    allowed, denied = set(), set()
    for rule in permissions:
        for clause in _re.split(r"[，,；;。]", rule):
            for m in _re.finditer(PATH, clause):
                prefix, before, after = _norm(m.group(1)), clause[:m.start()], clause[m.end():]
                # 权限词可在路径前（“只写 project/…”）也可在路径后（“project/… 可写”“project/…（只读）”）
                after_deny = _re.match(r"\s*[（(【]?\s*(?:只读|仅读|只能读|只可读|不可写|不能写|禁止写|不改|不可改)", after)
                after_allow = _re.match(r"\s*[（(【]?\s*(?:可写|可写入|可修改|可改|允许写|允许修改)", after)
                if after_deny or _re.search(r"(?:只读|仅读|只能读|只可读|读取|查阅|参考)\s*$", before) or _re.search(NEGATION + r"\s*(?:改|写|修改|写入|动)", before):
                    denied.add(prefix)
                elif after_allow or _re.search(r"(?:写|改|输出|生成|保存|存放|产出|可写|允许)", before):
                    allowed.add(prefix)
            if _re.search(NEGATION + r"\s*(?:改|写|修改|写入)\s*(?:project/)?records", clause):
                denied.add("project/records")
    issues = []
    for path in sorted(wanted):
        if any(path == d or path.startswith(d + "/") for d in denied):
            issues.append(f"task_text 要求写入 {path}，但 permissions 禁止写该位置（只读或不改）")
        elif not any(path == a or path.startswith(a + "/") for a in allowed):
            issues.append(f"task_text 要求写入 {path}，不在 permissions 允许的写入范围（{('、'.join(sorted(allowed)) or '未写明可写位置')}）内")
    return issues


ON_SCREEN = _re.compile(r"(?:约束|口径|禁区|出处|来源|备注|待确认|未核实|注释)[^。；\n]{0,40}?"
                        r"(?P<verb>落到画面|落在画面|上屏|写在画面|写到画面|放进画面|放到画面|放在画面|画面页脚|落到页脚|落在页脚|标在页脚|标到页脚|"
                        r"写在页脚|写到页脚|放在页脚|放到页脚|印在页面|显示在页面|显示在画面|标在页面|写在页面上|出现在画面|出现在页面)")


def _negated(clause, start, verb_start):
    """否定词须在同一短语里：从关键词前 5 个字（不跨逗号）到动词之前。"""
    lead = _re.split(r"[，,]", clause[max(0, start - 5):start])[-1]
    if _re.search(NEGATION, lead + clause[start:verb_start]):
        return True
    # “请勿将/不要把 …… 写入/标在 ……”：否定词带“将/把”时管到同一短语里的动词
    phrase = _re.split(r"[，,]", clause[:verb_start])[-1]
    return bool(_re.search(NEGATION + r"\s*(?:将|把)", phrase))


def constraint_on_screen(task_text: str, request: dict) -> list:
    """U05/K10：讲者口径约束默认不上屏。作者派工原文若要求把约束/出处落到画面，须在 on_screen_disclosures 单独写明依据。
    否定句（“不要把约束落到画面”“约束不上屏”）不算；【引用原话…】…【引用结束】里的用户原话只作背景，不当本次指令。"""
    for clause in _clauses(task_text):
        for m in ON_SCREEN.finditer(clause):
            if not _negated(clause, m.start(), m.start("verb")) and not request.get("on_screen_disclosures"):
                return ["task_text 要求把讲者约束/出处落到画面；约束只约束说法、默认不上屏。确需上屏的披露请在 on_screen_disclosures 逐项写明内容与依据"]
    return []


def validate_request(root: Path, request: dict) -> dict:
    role = request.get("role")
    if role not in ROLES:
        raise WorkflowError("未知分派角色，不能绕过六个工作角色")
    task_id = request.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise WorkflowError("分派必须绑定 task_id")
    text = request.get("task_text")
    if not isinstance(text, str) or not text.strip():
        raise WorkflowError("分派必须保留完整任务原文")
    for name in ("permissions", "completion_criteria"):
        value = request.get(name)
        if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value):
            raise WorkflowError(f"{name}必须是非空文字列表")
    inputs = request.get("input_files")
    if not isinstance(inputs, list) or not inputs:
        raise WorkflowError("分派必须提供至少一个原文或输入文件")
    issues = write_conflicts(text, request["permissions"]) + (constraint_on_screen(text, request) if role in AUTHOR_ROLES else [])
    if issues:
        raise WorkflowError("派工请求自相矛盾，未登记分派：" + "；".join(issues))
    input_refs = [file_ref(root, value, "输入文件") for value in inputs]
    required = request.get("required_reading")
    if required is not None:
        if not isinstance(required, list) or not required:
            raise WorkflowError("required_reading 须为 input_files 中必读文件的非空列表")
        required_refs = [file_ref(root, value, "必读文件") for value in required]
        if any(r not in input_refs for r in required_refs):
            raise WorkflowError("required_reading 只能从 input_files 中选；其余自动列为备查")
    else:
        required_refs = None
    budget = request.get("budget")
    if budget is not None and (not isinstance(budget, dict) or set(budget) != {"minutes", "tool_calls"}
                               or any(type(budget[k]) is not int or budget[k] <= 0 for k in budget)):
        raise WorkflowError("budget 须为 {minutes: 正整数, tool_calls: 正整数}")
    disclosures = request.get("on_screen_disclosures")
    if disclosures is not None and (not isinstance(disclosures, list) or any(
            not isinstance(d, dict) or set(d) != {"content", "basis"} or not all(isinstance(d[k], str) and d[k].strip() for k in d)
            for d in disclosures)):
        raise WorkflowError("on_screen_disclosures 每项须有 content 与 basis 文字")
    upstream = request.get("upstream_refs", [])
    if not isinstance(upstream, list) or any(not isinstance(item, dict) for item in upstream):
        raise WorkflowError("上游版本必须是对象列表")
    if any(not all(isinstance(item.get(key), (str, int)) for key in ("space", "key", "version", "sha256"))
           for item in upstream):
        raise WorkflowError("上游版本必须包含 space/key/version/sha256")
    author = request.get("author_instance")
    if author is not None and (not isinstance(author, str) or not author.strip()):
        raise WorkflowError("author_instance 不能为空")
    diagnostic = request.get("diagnostic_instance")
    if diagnostic is not None and (not isinstance(diagnostic, str) or not diagnostic.strip()):
        raise WorkflowError("diagnostic_instance 不能为空")
    excluded = request.get("excluded_instances", [])
    if not isinstance(excluded, list) or any(not isinstance(x, str) or not x.strip() for x in excluded):
        raise WorkflowError("excluded_instances必须是文字列表")
    excluded = list(dict.fromkeys(excluded + [x for x in (author, diagnostic) if x]))
    if role == "independent-reviewer" and not excluded:
        raise WorkflowError("独立检核必须提供作者/诊断者实例排除集")
    memory_snapshot, task_snapshot = project_context(root, task_id)
    payload = {
        "role": role, "task_id": task_id, "task_text": text,
        "input_files": input_refs, "upstream_refs": upstream,
        "permissions": list(request["permissions"]),
        "completion_criteria": list(request["completion_criteria"]),
        "author_instance": author, "diagnostic_instance": diagnostic,
        "excluded_instances": excluded,
        "independent_required": role == "independent-reviewer",
        "project_memory": memory_snapshot,
        "task_context": task_snapshot,
    }
    if role in AUTHOR_ROLES:
        # U11/U18：作者派工写明必读/备查、时间与调用预算（默认 60 分钟/80 次，停下来问，不是失败）。
        payload["budget"] = dict(budget or DEFAULT_BUDGET)
        payload["required_reading"] = required_refs or input_refs  # 备查 = input_files 减去必读，读取时推导，不重复存
        if disclosures:
            payload["on_screen_disclosures"] = disclosures
    if request.get('review_group_id') or request.get('review_member_id'):
        if not all(isinstance(request.get(k),str) and request[k].strip() for k in ('review_group_id','review_member_id')):raise WorkflowError('审查组与成员编号须齐全')
        payload.update({k:request[k] for k in ('review_group_id','review_member_id')})
        from shencha_zu import validate_dispatch
        group_plan=validate_dispatch(root,payload)
        payload['review_mode']=group_plan['review_mode']
        scope=group_plan['changed_scope']
        payload['reason_codes']=['review_group_frozen']+(scope.get('reason_codes',[]) if isinstance(scope,dict) else [])
    elif role=='independent-reviewer' and request.get('review_target'):
        from incremental_review import default_plan
        review_plan=default_plan(root,request['review_target'],'dispatch-plan-check',request.get('simulation',False),request.get('change_kind','unknown'),request.get('review_purpose','revision'))
        payload['review_target']=request['review_target'];payload['review_plan']=review_plan
        payload['review_mode']=review_plan['review_mode']
        payload['reason_codes']=review_plan.get('changed_scope',{}).get('reason_codes',[]) if isinstance(review_plan.get('changed_scope'),dict) else review_plan.get('reason_codes',[])
        from shencha_zu import context_packet
        payload['review_context']=context_packet(root,{**review_plan,'base_review':review_plan.get('base_review'),
             'changed_scope':review_plan.get('changed_scope',[])},{'paths':[f['path'] for f in review_plan['checked_sources_required']]})
        if review_plan['dispatch_required'] and input_refs!=review_plan['checked_sources_required']:
            raise WorkflowError('分派原文须精确对应累计增量计划；不复制无关全文或聊天')
    return payload


@dispatch_write
def prepare(root: Path, request: dict, actor: str) -> dict:
    with serialized(root):
        payload = validate_request(root, request)
        if payload.get('review_plan',{}).get('dispatch_required') is False:
            from yunxing_rizhi import classify
            classify('unchanged_valid_review')
            return {'status':'reused','dispatch_required':False,**payload['review_plan']}
        active=[item for item in replay(read_log(root)).values() if item.get('status') in ACTIVE and item.get('task_id')==payload['task_id']]
        allowed_group=payload.get('review_group_id') and all(item.get('review_group_id')==payload['review_group_id'] and item.get('role')=='independent-reviewer' for item in active)
        if active and not allowed_group:
            raise WorkflowError("同一任务已有未结束分派；回传后由当前主控回读并执行 complete，"
                                "新主控先执行 takeover；确需放弃时明确 cancel，不重复占用")
        dispatch_id = "dispatch-" + uuid.uuid4().hex
        if payload.get("role") in AUTHOR_ROLES:
            payload["progress_path"] = f"project/outputs/分派进度/{dispatch_id}.md"
            # K09：系统生成的进度文件随派工写进权限，作者按规则写进度不与“只写成果目录”矛盾
            payload["permissions"] = payload["permissions"] + [f"可写入本分派进度文件 {payload['progress_path']} （只写进度行，不写成果）"]
        from xiugai_fenpai import prepare_targets
        payload=prepare_targets(root,request,payload,dispatch_id)
        verify_context(root, {**payload})
        return append(root, {"event": "prepared", "dispatch_id": dispatch_id,
                             "status": "prepared", "actor": actor,
                             "controller_instance": actor, "prepared_at":datetime.now(timezone.utc).isoformat(), **payload})


def reading(item: dict) -> dict:
    """必读 / 备查：备查 = input_files 减去 required_reading，读取时推导，不重复存。"""
    if "required_reading" not in item:
        return {}
    required = {(r["path"], r["sha256"]) for r in item["required_reading"]}
    return {"reference_only": [r for r in item.get("input_files", []) if (r["path"], r["sha256"]) not in required]}


def current(root: Path, dispatch_id: str) -> dict:
    item = replay(read_log(root)).get(dispatch_id)
    if not item:
        raise WorkflowError("找不到分派记录")
    from yunxing_rizhi import bind
    bind(item.get('task_id'),dispatch_id)
    return item


@dispatch_write
def assign(root: Path, dispatch_id: str, instance: str, evidence: object, actor: str,
           host_instance: str) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") != "prepared":
            raise WorkflowError("只有 prepared 分派可以绑定实际实例")
        if (not isinstance(instance, str) or not instance.strip()
                or not isinstance(host_instance, str) or not host_instance.strip()):
            raise WorkflowError("必须提供真实host实例和执行实例标识")
        if item.get("independent_required") and instance in set(item.get("excluded_instances", [])):
            raise WorkflowError("独立检核实例不能与作者或设计诊断实例相同")
        if item.get('review_group_id'):
            from shencha_zu import assignment_check
            assignment_check(root,item,instance,host_instance)
        elif item.get('review_target'):
            from incremental_review import current_input,excluded_instances
            _,authors,_=current_input(root,item['review_target'])
            if instance in excluded_instances(root,item['review_target'],authors) or instance==item.get('controller_instance'):
                raise WorkflowError('实际审查实例不能参与创作、诊断或充当本次登记主控')
        if item.get("dispatch_protocol")==2:
            from xiugai_fenpai import invocation
            evidence_ref,invocation_details=invocation(root,item,evidence,instance,host_instance)
        else:
            evidence_ref = file_ref(root, evidence, "调用证据")
            invocation_details=None
        verify_context(root, item)
        return append(root, {"event": "assigned", "dispatch_id": dispatch_id,
                             "status": "assigned", "actor": actor,
                             "review_mode":item.get('review_mode'),
                             "instance": instance, "host_instance": host_instance,
                             "invocation_evidence": evidence_ref, **({"invocation_details":invocation_details} if invocation_details else {})})


PROGRESS_LINE = _re.compile(r"调用\s*[≈~约]?\s*(\d+)")


def progress(root: Path, dispatch_id: str) -> dict:
    """U11/U18：主控直接读出作者进度文件，并对照派工预算（时间按 assign 起算，调用次数以作者自报为准）。"""
    with serialized(root, write=False):
        item = current(root, dispatch_id)
        records = [r for r in read_log(root) if r.get("dispatch_id") == dispatch_id]
    budget = item.get("budget")
    if not budget:
        return {"dispatch_id": dispatch_id, "status": "no_budget", "message": "旧分派或非作者分派，没有预算与进度文件"}
    extra = [r for r in records if r.get("event") == "budget_extended"]
    minutes = budget["minutes"] + sum(r["added"]["minutes"] for r in extra)
    calls = budget["tool_calls"] + sum(r["added"]["tool_calls"] for r in extra)
    assigned = next((r for r in records if r.get("event") == "assigned"), None)
    elapsed = None
    if assigned:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(assigned["created_at"])).total_seconds() / 60
    lines, reported = [], None
    path = root / item["progress_path"]
    if path.is_file():
        lines = [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        found = [int(m.group(1)) for x in lines for m in [PROGRESS_LINE.search(x)] if m]
        reported = found[-1] if found else None
    over = []
    if elapsed is not None and elapsed >= minutes:
        over.append(f"已用约 {elapsed:.0f} 分钟，达到时间预算 {minutes} 分钟")
    if reported is not None and reported >= calls:
        over.append(f"作者自报调用约 {reported} 次，达到调用预算 {calls} 次")
    return {"dispatch_id": dispatch_id, "status": item.get("status"), "budget": {"minutes": minutes, "tool_calls": calls},
            "elapsed_minutes": None if elapsed is None else round(elapsed, 1), "reported_calls": reported,
            "progress_path": item["progress_path"], "progress_lines": lines[-12:], "last_progress": lines[-1] if lines else None,
            "required_reading": [r["path"] for r in item.get("required_reading", [])],
            "reference_only": [r["path"] for r in reading(item).get("reference_only", [])],
            "over_budget": over,
            "report": ("到预算：作者先落盘进度并交回（return），主控向策略师报告做到哪、卡在哪、已用时间/调用、可选下一步；"
                       "策略师说继续时用 extend 记原话追加预算。不丢弃已做成果。") if over else "未到预算"}


@dispatch_write
def budget_reached(root: Path, dispatch_id: str, actor: str) -> dict:
    """到预算时登记一次超限事件（带当时的进度文件指纹），运行日志同步记录。"""
    with serialized(root):
        report = progress(root, dispatch_id)
        if not report.get("over_budget"):
            raise WorkflowError("尚未到预算，不登记超限")
        path = root / report["progress_path"]
        proof = file_ref(root, report["progress_path"], "进度文件") if path.is_file() else None
        from yunxing_rizhi import note
        note("command", "skipped", status="blocked", reason_code="budget_reached")  # 运行日志记到预算（与机械重试分开），不存正文
        return append(root, {"event": "budget_reached", "dispatch_id": dispatch_id, "status": current(root, dispatch_id)["status"],
                             "actor": actor, "over_budget": report["over_budget"], "progress_ref": proof,
                             "elapsed_minutes": report["elapsed_minutes"], "reported_calls": report["reported_calls"]})


@dispatch_write
def extend(root: Path, dispatch_id: str, words: str, minutes: int, tool_calls: int, actor: str) -> dict:
    """策略师说“继续”：记原话并追加一段预算。"""
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") not in ACTIVE or not item.get("budget"):
            raise WorkflowError("只有未结束且有预算的作者分派可以追加预算")
        if not isinstance(words, str) or not words.strip():
            raise WorkflowError("追加预算须记录策略师原话")
        if type(minutes) is not int or type(tool_calls) is not int or minutes < 0 or tool_calls < 0 or minutes + tool_calls == 0:
            raise WorkflowError("追加的分钟数与调用次数须为非负整数且不全为 0")
        return append(root, {"event": "budget_extended", "dispatch_id": dispatch_id, "status": item["status"], "actor": actor,
                             "words": words, "added": {"minutes": minutes, "tool_calls": tool_calls}})


@dispatch_write
def return_candidate(root: Path, dispatch_id: str, candidate: object, actor: str, return_evidence=None, tradeoffs=None) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") != "assigned":
            raise WorkflowError("只有已绑定实际实例的分派可以回传")
        values = candidate if isinstance(candidate, list) else [candidate]
        conflict_items=[];terminal={}
        if item.get("dispatch_protocol")==2:
            from xiugai_fenpai import candidates,conflicts
            if actor!=item["instance"]:raise WorkflowError("修改候选须由已登记实际作者回传")
            from xiugai_fenpai import terminal_invocation
            terminal=terminal_invocation(root,item,return_evidence)
            candidate_refs=candidates(root,item,values)
            conflict_items=conflicts(root,item)
            try:verify_context(root,item)
            except (ValueError,OSError):conflict_items.append({"reason":"context_changed","action":"已完成候选保留，待主控核对输入/任务上下文；不重派相同成果"})
        else:
            verify_context(root, item)
            candidate_refs = [file_ref(root, value, "回传候选") for value in values]
        if conflict_items:
            from yunxing_rizhi import classify
            classify("candidate_preserved")
        if tradeoffs is not None and (not isinstance(tradeoffs, list) or any(not isinstance(t, str) or not t.strip() for t in tradeoffs)):
            raise WorkflowError("可讨论取舍须为非空文字")
        return append(root, {"event": "returned", "dispatch_id": dispatch_id,
                             **({"tradeoffs": tradeoffs} if tradeoffs else {}),
                             "status": "returned_for_readback", "actor": actor,
                             "review_mode":item.get('review_mode'),
                             "instance": item["instance"], "host_instance": item["host_instance"],
                             "candidates": candidate_refs, "readback_verified": False,
                             "review_status": "not_registered", "passed": False, **({"conflicts":conflict_items,"registration_status":"pending",**terminal} if item.get("dispatch_protocol")==2 else {})})


def controller_of(root: Path, item: dict) -> str | None:
    return item.get("controller_instance") or next((record.get("actor")
        for record in read_log(root) if record.get("dispatch_id") == item["dispatch_id"]
        and record.get("event") == "prepared"), None)


@dispatch_write
def takeover(root: Path, dispatch_id: str, evidence: object, reason: str, actor: str) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        controller = controller_of(root, item)
        if item.get("status") not in ACTIVE:
            raise WorkflowError("只有未结束分派可以登记主控接手")
        if (not isinstance(actor, str) or not actor.strip() or actor == "未登记实例"
                or actor in (controller, item.get("instance"))):
            raise WorkflowError("须由不同的新主控接手，不能由候选执行者接手")
        if not isinstance(reason, str) or not reason.strip():
            raise WorkflowError("主控接手须保留原因")
        verify_context(root, item)
        proof = text_ref(root, evidence)
        return append(root, {"event": "controller_taken_over", "dispatch_id": dispatch_id,
            "status": item["status"], "actor": actor, "reason": reason,
            "original_controller_instance": item.get("original_controller_instance", controller),
            "previous_controller_instance": controller, "controller_instance": actor,
            "takeover_refs": [*item.get("takeover_refs", []), proof]})


def follow_up(root: Path, item: dict, record: dict) -> dict:
    """回读完成后自动接续（U01）：只改独立任务的 summary/next_action，不接受文件为当前版本、不派检核。
    任务在分派期间已被更新时只提示不写，避免覆盖其它对话的进度。"""
    context = item.get("task_context") or {}
    if context.get("kind") != "standalone":
        return {"status": "not_applicable", "reason": "仅独立任务/提案阶段任务自动接续"}
    if item.get("role") not in AUTHOR_ROLES:
        # 检核/诊断分派完成不改任务版本（审查组冻结目标版本），下一步按报告登记。
        return {"status": "not_applicable", "reason": "检核报告已回读；用 standalone_review.py review 登记，登记前不能称已检核"}
    if item.get("dispatch_protocol") == 2:
        # 修改目标分派由 integrate 按观察凭据登记候选；这里不插入修订，避免打乱其绑定的任务版本。
        return {"status": "not_applicable", "reason": "修改目标分派下一步用 agent_dispatch.py integrate 登记候选"}
    from standalone_store import history
    from standalone_tasks import save
    events, _ = history(root)
    own = [e for e in events if e["task_id"] == item["task_id"]]
    if not own:
        return {"status": "skipped", "reason": "找不到任务记录"}
    current_event = own[-1]
    prepared_revision = (context.get("reference") or {}).get("version")
    candidates = [c["path"] for c in item.get("candidates", [])]
    summary = f"{item.get('role')} 分派 {item['dispatch_id']} 已回读候选 {len(candidates)} 份；尚未登记为当前交付物，未检核"
    next_action = ("待检核或待策略师查看：候选 " + "、".join(candidates[:3]) + ("…" if len(candidates) > 3 else "")
                   + "；接受为当前版本用 standalone_tasks.py adopt（或 iterate/save）明确登记，不要时用 discard 记策略师原话放弃")
    if current_event["revision"] != prepared_revision:
        return {"status": "notice_only", "reason": "分派期间任务已被更新，未自动改写；请人工核对后更新下一步",
                "suggested_summary": summary, "suggested_next_action": next_action}
    from standalone_store import request_task
    task = request_task(current_event["task"]); task.update(summary=summary, next_action=next_action)
    try:
        event = save(root, {"task_id": item["task_id"], "request_id": "auto-" + item["dispatch_id"][-40:],
                            "expected_revision": current_event["revision"], "reason": "分派回读完成自动接续（不接受新版、不派检核）",
                            "task": task}, record["actor"])
    except (ValueError, OSError) as exc:
        return {"status": "notice_only", "reason": "自动接续未写入：" + str(exc),
                "suggested_summary": summary, "suggested_next_action": next_action}
    return {"status": "updated", "task_revision": event["revision"], "next_action": next_action}


def unregistered_candidates(root: Path) -> list[dict]:
    """每个独立任务最近一次已回读的作者分派里，尚未登记为当前交付物的候选（resume 显示，不称已检核）。"""
    from standalone_store import history
    events, _ = history(root)
    latest_task, changed_at = {}, {}
    for e in events:
        prev = latest_task.get(e["task_id"])
        if prev is None or prev["task"]["deliverables"] != e["task"]["deliverables"]:
            changed_at[e["task_id"]] = e["created_at"]
        latest_task[e["task_id"]] = e
    done = [d for d in replay(read_log(root)).values()
            if d.get("status") == "completed" and d.get("role") in AUTHOR_ROLES and d.get("task_id") in latest_task]
    result = []
    for task_id in sorted({d["task_id"] for d in done}):
        last = max((d for d in done if d["task_id"] == task_id), key=lambda d: d.get("created_at", ""))
        task = latest_task[task_id]["task"]
        if last.get("candidates_discarded"):
            continue
        current = {(r.get("path"), r.get("sha256")) for r in task["deliverables"]}
        if task["status"] in {"archived", "cancelled"} or last.get("created_at", "") < changed_at.get(task_id, ""):
            continue
        for c in last.get("candidates", []):
            if (c["path"], c["sha256"]) not in current:
                result.append({"task_id": task_id, "dispatch_id": last["dispatch_id"], "role": last.get("role"),
                               "path": c["path"], "sha256": c["sha256"], "review": "未检核",
                               "next_action": "待检核或待策略师查看；接受为当前版本须明确登记（iterate/save）"})
    return result


@dispatch_write
def complete(root: Path, dispatch_id: str, evidence: object, actor: str) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") != "returned_for_readback":
            raise WorkflowError("只有已回传候选的分派可以登记主控回读")
        controller = controller_of(root, item)
        if not actor or actor == "未登记实例" or actor != controller or actor == item.get("instance"):
            raise WorkflowError("必须由当前登记主控回读；新主控先登记接手，候选作者不能代替")
        (verify_refs if item.get("dispatch_protocol")==2 else verify_context)(root, item)
        proof = readback_ref(root, evidence, item.get("candidates",[]))
        (verify_refs if item.get("dispatch_protocol")==2 else verify_context)(root,item)
        if proof.get("kind")=="readback_summary":
            for value in proof["references"]:file_ref(root,value,"回读原件")
        # 先接续（只改 summary/next_action，允许在未结束分派期间进行），结果写进回读记录本身，重试原样返回。
        follow = follow_up(root, item, {"actor": actor})
        return append(root, {"event": "completed", "dispatch_id": dispatch_id,
                             "status": "completed", "actor": actor,
                             "review_mode":item.get('review_mode'),
                             "readback_evidence": proof, "readback_verified": True,
                             "review_status": "not_registered", "passed": False, "follow_up": follow})


@dispatch_write
def discard(root: Path, dispatch_id: str, words: str, actor: str) -> dict:
    """策略师明确不要某次已回读的作者候选：记原话，之后 resume/检核准备不再把它列为“待登记”。不删文件。"""
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") != "completed" or item.get("role") not in AUTHOR_ROLES:
            raise WorkflowError("只有已回读（complete）的作者分派可以放弃其候选")
        if not isinstance(words, str) or not words.strip():
            raise WorkflowError("放弃候选须记录策略师原话（--words）")
        return append(root, {"event": "candidates_discarded", "dispatch_id": dispatch_id, "status": "completed", "actor": actor,
                             "words": words, "candidates_discarded": True,
                             "notice": "只记录不采用，不删除候选文件"})


@dispatch_write
def cancel(root: Path, dispatch_id: str, reason: str, actor: str) -> dict:
    with serialized(root):
        item = current(root, dispatch_id)
        if item.get("status") not in ACTIVE:
            raise WorkflowError("当前分派不能取消")
        if not isinstance(reason, str) or not reason.strip():
            raise WorkflowError("取消必须保留原因")
        return append(root, {"event": "cancelled", "dispatch_id": dispatch_id,
                             "status": "cancelled", "actor": actor, "reason": reason})


@read_serialized
def inspect(root: Path) -> dict:
    with serialized(root):
        records = read_log(root)
        latest_records = replay(records)
        return {"schema_version": 1, "records": len(records),
                "dispatches": list(latest_records.values()),
                "active": [item for item in latest_records.values() if item.get("status") in ACTIVE]}


def main() -> int:
    parser = argparse.ArgumentParser(description="策略工作 SubAgent 显式分派记录")
    parser.add_argument("action", choices=("prepare", "assign", "return", "takeover", "complete", "cancel", "inspect", "integrate",
                                           "progress", "budget-reached", "extend", "discard"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--dispatch-id")
    parser.add_argument("--instance")
    parser.add_argument("--host-instance")
    parser.add_argument("--evidence")
    parser.add_argument("--candidate", action="append")
    parser.add_argument("--return-evidence")
    parser.add_argument("--readback-summary")
    parser.add_argument("--readback-reference",action="append")
    parser.add_argument("--reason")
    parser.add_argument("--actor")
    parser.add_argument("--request-id")
    parser.add_argument("--tradeoff", action="append", help="作者交回时的可讨论取舍，进入策略师待决清单首位")
    parser.add_argument("--words", help="extend / discard：策略师原话")
    parser.add_argument("--minutes", type=int, default=0)
    parser.add_argument("--calls", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    if args.action not in {"inspect", "progress"}:
        actor_check(args.actor)
    if args.action == "prepare":
        if not args.request:
            raise WorkflowError("prepare 缺少 --request")
        result = prepare(root, json.loads(args.request.read_text(encoding="utf-8")), args.actor, request_id=args.request_id)
        if isinstance(result, dict):
            result = {**result, **reading(result)}  # 备查清单在输出时推导（首次与重试一致），不写进记录
    elif args.action == "assign":
        result = assign(root, args.dispatch_id, args.instance, args.evidence, args.actor,
                        args.host_instance, request_id=args.request_id)
    elif args.action == "return":
        result = return_candidate(root, args.dispatch_id, args.candidate, args.actor, return_evidence=args.return_evidence,
                                  tradeoffs=args.tradeoff, request_id=args.request_id)
    elif args.action == "progress":
        result = progress(root, args.dispatch_id)
    elif args.action == "budget-reached":
        result = budget_reached(root, args.dispatch_id, args.actor, request_id=args.request_id)
    elif args.action == "extend":
        result = extend(root, args.dispatch_id, args.words, args.minutes, args.calls, args.actor, request_id=args.request_id)
    elif args.action == "complete":
        if args.readback_summary is not None and args.evidence is not None:
            raise WorkflowError("回读说明与旧evidence二选一")
        evidence={"summary":args.readback_summary,"references":args.readback_reference or []} if args.readback_summary is not None else args.evidence
        result = complete(root, args.dispatch_id, evidence, args.actor, request_id=args.request_id)
    elif args.action == "takeover":
        result = takeover(root, args.dispatch_id, args.evidence, args.reason, args.actor, request_id=args.request_id)
    elif args.action == "integrate":
        from xiugai_fenpai import integrate
        result=integrate(root,args.dispatch_id,json.loads(args.request.read_text()),args.actor)
    elif args.action == "cancel":
        result = cancel(root, args.dispatch_id, args.reason, args.actor, request_id=args.request_id)
    elif args.action == "discard":
        result = discard(root, args.dispatch_id, args.words, args.actor, request_id=args.request_id)
    else:
        result = inspect(root)
    from standalone_tasks import with_retention
    print(json.dumps(with_retention(result), ensure_ascii=False, indent=2))
    return 0


from yunxing_rizhi import observed
prepare = observed("agent_dispatch.prepare")(prepare)
assign = observed("agent_dispatch.assign")(assign)
return_candidate = observed("agent_dispatch.return_candidate")(return_candidate)
complete = observed("agent_dispatch.complete")(complete)
cancel = observed("agent_dispatch.cancel")(cancel)
inspect = observed("agent_dispatch.inspect")(inspect)

if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main
    def main():
        return cli(_business_main, __file__)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
