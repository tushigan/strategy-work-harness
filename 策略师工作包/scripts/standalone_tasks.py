"""Natural-language controller entry point for small, unplanned project tasks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from phase2_store import WorkflowError, now
from workspace_lock import serialized
from standalone_store import (ACTIVE_STATUSES, JOURNAL, digest, history, identifier, reference_path,
                              storage, task_value, text)


class RequestIdConflict(WorkflowError):
    """同一请求编号已登记过不同内容（r3 #8：原样重试读“已用”那份时据此改报白话，不靠匹配报错原文）。"""


def save(root, request, actor, *, _observations=None, _commit_observations=None, _request_hash=None, _adoption=None, _iteration=None):
    root = Path(root).resolve()
    storage(root)  # Check before acquiring a lock that creates directories.
    required_fields = {"task_id", "request_id", "expected_revision", "reason", "task"}
    if not isinstance(request, dict):
        raise WorkflowError("登记单的写法不对，这次没有登记。技术附录（给 Agent）：请求须为 JSON 对象（请求字段不完整或含未支持字段）")
    if set(request) not in (required_fields, required_fields | {"accept_changed_references"}):
        # G08（v1.7.5）：点名多了 / 少了哪几项
        extra = sorted(set(request) - required_fields - {"accept_changed_references"})
        missing = sorted(required_fields - set(request))
        plain = "、".join(x for x in (f"多了 {len(extra)} 项不认识的内容" if extra else "", f"少了 {len(missing)} 项必填内容" if missing else "") if x)
        raise WorkflowError(f"登记单{plain}，这次没有登记。技术附录（给 Agent）：请求顶层"
                            + (f" 多余字段 {', '.join(extra)}" if extra else "") + (f" 缺字段 {', '.join(missing)}" if missing else "")
                            + "；应为 task_id、request_id、expected_revision、reason、task，可选 accept_changed_references")
    accept_changed_references = request.get("accept_changed_references", False)
    if type(accept_changed_references) is not bool:
        raise WorkflowError("accept_changed_references 须为布尔值")
    identifier(request["task_id"])
    identifier(request["request_id"])
    from registration_guard import actor_check
    actor_check(actor)
    text(actor, "记录者")
    text(request["reason"], "更新原因")
    if type(request["expected_revision"]) is not int or request["expected_revision"] < 0:
        raise WorkflowError("expected_revision 须为非负整数")
    hash_request = dict(request)
    if hash_request.get("accept_changed_references") is False:
        hash_request.pop("accept_changed_references")
    request_hash = _request_hash or digest({"request": hash_request, "actor": actor})
    with serialized(root):
        events, raw = history(root)
        prior = next((e for e in events if e["request_id"] == request["request_id"]), None)
        if prior:
            if prior["request_hash"] != request_hash:
                raise RequestIdConflict("请求编号已用于不同内容，保留原记录")
            from yunxing_rizhi import note
            note("standalone_tasks.save","retry",reason_code="idempotent_request",version=prior["revision"])
            return prior
        current = next((e for e in reversed(events) if e["task_id"] == request["task_id"]), None)
        previous_source_key = current["task"].get("source_request_key") if current else None
        requested_source_key = request["task"].get("source_request_key")
        if previous_source_key and requested_source_key not in (None, previous_source_key):
            raise WorkflowError("来源请求键已绑定，不能在后续版本中更换")
        source_key = previous_source_key or requested_source_key
        if source_key:
            duplicate = next((e for e in reversed(events)
                              if e["task"].get("source_request_key") == source_key
                              and e["task_id"] != request["task_id"]), None)
            if duplicate:
                raise WorkflowError(f"同一来源请求已登记为任务 {duplicate['task_id']}，请续做原任务")
        revision = current["revision"] if current else 0
        if request["expected_revision"] != revision:
            raise WorkflowError("任务版本已变化，请重新读取后更新，不能覆盖其它对话的进度")
        task_request = dict(request["task"])
        if previous_source_key and requested_source_key is None:
            task_request["source_request_key"] = previous_source_key
        normalized_task = task_value(
            root, task_request,
            previous=current["task"] if current else None,
            accept_changed_references=_observations if _observations is not None else accept_changed_references,
        )
        references_changed = bool(current and any(
            normalized_task[name] != current["task"][name]
            for name in ("sources", "deliverables")
        ))
        if current and current["task"]["status"] == "completed" and references_changed:
            if not (accept_changed_references or _observations):
                raise WorkflowError("来源或交付物已换版，须显式接受新版")
            if (normalized_task["status"] == "completed"
                    or normalized_task["completion_evidence"].strip()):
                raise WorkflowError("接受新版后须先进入待核对状态并清空旧完成依据")
        if current and current["task"]["status"] != "completed" \
                and normalized_task["status"] == "completed":
            previous_completed = next((event for event in reversed(events)
                                       if event["task_id"] == request["task_id"]
                                       and event["task"]["status"] == "completed"), None)
            if (previous_completed
                    and any(current["task"][name] != previous_completed["task"][name]
                            for name in ("sources", "deliverables"))
                    and normalized_task["completion_evidence"]
                    == previous_completed["task"]["completion_evidence"]):
                raise WorkflowError("新版交付物须提交新的完成依据，不能沿用旧版文案")
        if current and normalized_task == current["task"] and _request_hash is not None:
            return current
        from standalone_budget import next_budget, derive as derive_budget
        # 策略师逐条指令的在线改稿（U02）不计入自动改稿两轮，也不要求每版先检核。
        budget = derive_budget(events, request["task_id"]) if _iteration else next_budget(root, events, current, request["task_id"], normalized_task)
        event = {"revision_budget": budget, "schema_version": 1, "task_id": request["task_id"],
                 "request_id": request["request_id"], "request_hash": request_hash,
                 "revision": revision + 1, "created_at": now(), "actor": actor,
                 "reason": request["reason"], "task": normalized_task}
        if _adoption is not None:event["observed_adoption"]=_adoption
        if _iteration is not None:event["iteration"]=_iteration
        from standalone_budget import derive
        derive(events + [event], request["task_id"])  # Same rule must read back before any write.
        path = storage(root)
        fd, temporary = tempfile.mkstemp(prefix=".writing-standalone-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw + (b"\n" if raw and not raw.endswith(b"\n") else b""))
                from standalone_store import stored_event
                stored = stored_event(event, current["task"] if current else None)
                from standalone_store import event_digest
                stored["prev_line_sha256"] = event_digest(events[-1]) if events else None
                stream.write((json.dumps(stored, ensure_ascii=False, allow_nan=False) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            if (path.read_bytes() if path.exists() else b"") != raw:
                raise WorkflowError("保存期间记录被修改，请重新读取；未覆盖原件")
            # v1.7.6 H13：判定键与 phase6_targets.resolve 统一为路径——同一文件既登记为交付物、又以另一个指纹登记为来源 / 过程 / 决定引用，
            # 检核登记会按交付物整文件核对它的每个指纹而必然失败；这里先拒，不写入一条之后登不了检核的记录
            delivered_sha = {ref["path"]: ref["sha256"] for ref in normalized_task["deliverables"] if "path" in ref}
            clash = sorted({ref["path"] for ref in normalized_task["sources"] + normalized_task.get("process_refs", [])
                            + normalized_task.get("decision_refs", []) if ref.get("path") in delivered_sha
                            and ref.get("sha256") != delivered_sha[ref["path"]]})
            if clash:
                raise WorkflowError("同一个文件既是来源又是交付物，但两处登记的版本不一样，这次没有登记。技术附录（给 Agent）："
                                    + "、".join(clash) + " 在 sources / process_refs / decision_refs 与 deliverables 里指纹不同；"
                                    "交付物按整文件核对，先把来源里的这条引用换成当前版本（accept_changed_references）或从交付物里拿掉，未写业务事件")
            commit_references = {ref["path"]: ref["sha256"] for ref in
                                 normalized_task["sources"] + normalized_task["deliverables"]
                                 + normalized_task.get("process_refs",[]) + normalized_task.get("decision_refs",[])
                                 if "path" in ref}
            for ref_path, observed_sha in (_commit_observations or {}).items():
                if ref_path in commit_references and commit_references[ref_path] != observed_sha:
                    raise WorkflowError("登记期间观察版改变；重新核对后形成新请求，未写业务事件")
                commit_references[ref_path] = observed_sha
            try:
                from dispatch_files import appended
                delivered = {ref["path"] for ref in normalized_task["deliverables"] if "path" in ref}
                for ref_path, expected_sha in commit_references.items():
                    live = reference_path(root, ref_path)
                    if not live.is_file() or hashlib.sha256(live.read_bytes()).hexdigest() != expected_sha:
                        # F09（v1.7.4）：来源 / 过程 / 决定引用里的 project/records/*.jsonl 登记部分原样、之后只追加 → 仍按登记指纹写入，
                        # 不算改变；交付物不放宽（r2，与 unchanged_elsewhere 一致）。
                        if ref_path not in delivered and live.is_file() and appended(root, {"path": ref_path, "sha256": expected_sha}) is not None:
                            continue
                        raise WorkflowError(f"{ref_path} 与登记指纹不符（登记期间或之前已改变）；先核对并显式接受新版，未写业务事件")
            except WorkflowError:
                raise  # 已指名文件与原因的业务错误原样给出，不换成笼统说法
            except (OSError, ValueError) as exc:
                raise WorkflowError("登记期间观察文件改变、缺失、不可读或不安全；未写业务事件") from exc
            os.replace(temporary, path)
            from standalone_store import write_head
            write_head(root, len(events) + 1, {**stored, "task": normalized_task})
            event = {**stored, "task": normalized_task}  # 与读回的形态一致：第2版变化字段 + 补全后的任务
        finally:
            Path(temporary).unlink(missing_ok=True)
        from yunxing_rizhi import classify
        if current and budget['count'] > current.get('revision_budget',{}).get('count',0):
            classify('user_feedback' if budget['used_decisions'] != current.get('revision_budget',{}).get('used_decisions',[]) else 'revision')
        if references_changed or not current:
            # W02：新版本登记成功后自动只留最新 2 版；失败只报告，不影响本次登记。
            from banben_baoliu import retain
            RETENTION.pop(request["task_id"], None)
            RETENTION[request["task_id"]] = retain(root, request["task_id"])
        return event


from dengji_qingqiu import SameNameCopies, move_used, used_copy  # G10（v1.7.5）：登记请求目录与“已用”只在 dengji_qingqiu 定义


def used_request(root, request_path):
    """原路径已不在时“已用”里对应的那份；同名多份且请求编号不同（r3 #5）→ 报“有多份可能同名的登记单，请指明用哪一份”，不自动挑。"""
    try:
        return used_copy(root, request_path)
    except SameNameCopies as exc:
        listed = "、".join(f"{rel}（request_id {rid!r}）" for rel, rid in exc.copies)
        raise WorkflowError("有多份可能同名的登记单，请指明用哪一份，这次没有登记。"  # r4 #6：提示文字（行为不变）
                            f"技术附录（给 Agent）：{request_path} 已移走，“已用”里有 {len(exc.copies)} 份同名、请求编号不同：{listed}；"
                            "用 --request 指向其中要用的那一份重试，或按任务当前版本另写新请求") from None


def retry_used(root, request_path, used, actor):
    """G10（v1.7.5 返修）：原路径已移到“已用”时的原样重试——读“已用”那份交给 save 走原有幂等：
    同一请求编号、内容相同返回已有结果；那份不在任务记录里或与登记时内容不同则拒，不重复登记。"""
    root = Path(root).resolve()
    request = json.loads((root / used).read_text(encoding="utf-8"))
    rid = request.get("request_id") if isinstance(request, dict) else None
    events, _ = history(root)
    prior = next((e for e in events if e["request_id"] == rid), None) if isinstance(rid, str) else None
    tail = f"技术附录（给 Agent）：{request_path} 已移到 {used}"
    if prior is None:
        raise WorkflowError("这份登记单已经登记过，文件已移到“已用”里，但任务记录里找不到它，这次没有登记。要登记请按任务当前版本另写一份新的登记单。"
                            + tail + f"（request_id {rid!r} 不在任务记录里）；重新读取任务当前版本后写新请求")
    try:
        return save(root, request, actor)
    except RequestIdConflict:  # r3 #8：按异常子类判断，不匹配报错原文
        raise WorkflowError("这份登记单已经登记过，文件已移到“已用”里，但那份与登记时的内容不同，这次没有重复登记。要改内容请按任务当前版本另写一份新的登记单。"
                            + tail + "（同一 request_id 内容或记录者不同）；重新读取任务当前版本后写新请求") from None


WORDS_DIR = "project/records/策略师原话"
RETENTION = {}  # 本进程内最近一次自动保留结果（W02），供命令输出；不写入任务记录


def with_retention(result):
    """r3：其他调 save 的入口（proposal_plan、agent_dispatch 等）输出也带本次自动保留结果：一个任务时原样，多个时按任务编号。"""
    if not RETENTION or not isinstance(result, dict):
        return result
    return {**result, "version_retention": next(iter(RETENTION.values())) if len(RETENTION) == 1 else dict(RETENTION)}


def unchanged_elsewhere(root, task, accepted):
    """iterate/adopt 只接受新交付物：其余来源/过程/决定引用若已变化，指名拒绝（不静默换成新指纹）。"""
    from phase2_store import sha
    changed = []
    for name in ("sources", "deliverables", "process_refs", "decision_refs"):
        for ref in task.get(name, []):
            if "path" not in ref or ref["path"] in accepted:
                continue
            try:
                path = reference_path(root, ref["path"])
                ok = path.is_file() and sha(path) == ref["sha256"]
                # F09（v1.7.4）：project/records/*.jsonl 登记部分原样、之后只追加 → 不算变化，仍绑定登记时的指纹。
                from dispatch_files import appended
                if not ok and name != "deliverables" and appended(root, ref) is not None:
                    continue
            except (OSError, ValueError):
                ok = False
            if not ok and name != "deliverables":
                changed.append(f"{name} {ref['path']}")
    if changed:
        raise WorkflowError("这些引用已变化（与登记指纹不符）：" + "、".join(changed[:6])
                            + "。iterate/adopt 只接受新交付物，不会顺带接受被改过的来源；先核对，确需采用新版用 save 显式 accept_changed_references，或恢复原件")


def iterate(root, task_id, words, deliverables, changes, actor, label=None, request_id=None):
    """策略师在线改稿一轮（U02）：一条命令记原话、新版文件与指纹、改动摘要、上一版。
    新版成为当前工作版本，标“主控改稿、未检核”；不计入自动改稿两轮；原话另存，不撑大任务记录。"""
    from registration_guard import actor_check
    from standalone_store import request_task
    from phase2_store import sha
    root = Path(root).resolve(); actor_check(actor); identifier(task_id)
    issues = []
    if not isinstance(words, str) or not words.strip():
        issues.append("--words：须为策略师本轮原话，不能为空")
    if not isinstance(changes, str) or not changes.strip():
        issues.append("--changes：须写改动摘要")
    if not deliverables:
        issues.append("--deliverable：须给出新版文件（project/ 内相对路径）")
    resolved = []
    for value in deliverables or []:
        try:
            path = reference_path(root, value)
        except (ValueError, OSError) as exc:
            issues.append(f"--deliverable {value}：{exc}"); continue
        if not path.is_file():
            issues.append(f"--deliverable {value}：文件不存在（新版目录或文件未落盘）"); continue
        resolved.append((value, sha(path)))
    if issues:
        raise WorkflowError("；".join(issues))
    with serialized(root):
        events, _ = history(root)
        own = [e for e in events if e["task_id"] == task_id]
        if not own:
            raise WorkflowError("找不到任务：" + task_id)
        current = own[-1]
        before = [{"path": r["path"], "sha256": r["sha256"]} for r in current["task"]["deliverables"] if "path" in r]
        request_id = request_id or "iter-" + digest({"task": task_id, "words": words, "files": resolved, "changes": changes.strip()})[:32]
        prior = next((e for e in events if e["request_id"] == request_id), None)
        if prior:
            same = (prior["task_id"] == task_id and isinstance(prior.get("iteration"), dict)
                    and prior["iteration"]["words_ref"].get("sha256") == digest(words)
                    and prior["iteration"].get("changes") == changes.strip()
                    and sorted((r["path"], r["sha256"]) for r in prior["task"]["deliverables"] if "path" in r) == sorted(resolved))
            if not same:
                raise WorkflowError("--request-id：请求编号已用于不同的原话、文件或改动摘要，保留原记录；新一轮请换编号或不填由程序生成")
            return handed_over(root, task_id, prior, actor)
        if {h for _, h in resolved} <= {r["sha256"] for r in before}:
            raise WorkflowError("--deliverable：新版与上一版指纹相同，没有新改动可记")
        unchanged_elsewhere(root, current["task"], {v for v, _ in resolved})
        rounds = sum(1 for e in own if e.get("iteration")) + 1
        words_path = local_words(root, task_id)
        line = {"request_id": request_id, "task_id": task_id, "round": rounds, "base_revision": current["revision"],
                "words": words, "actor": actor, "created_at": now()}
        existing = words_path.read_text(encoding="utf-8").splitlines() if words_path.exists() else []
        if not any(json.loads(x).get("request_id") == request_id for x in existing if x.strip()):
            words_path.parent.mkdir(parents=True, exist_ok=True)
            with words_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(line, ensure_ascii=False) + "\n")
                stream.flush(); os.fsync(stream.fileno())
        task = request_task(current["task"])
        task["deliverables"] = [{"label": label or f"在线改稿第{rounds}轮", "path": value} for value, _ in resolved]
        task["status"] = "in_progress"
        task["summary"] = f"迭代中：主控按策略师原话改稿第{rounds}轮（作者与主控同实例），未检核。改动：{changes.strip()[:200]}"
        task["next_action"] = ITERATE_NEXT
        task["completion_evidence"] = ""
        iteration = {"round": rounds, "mode": "主控改稿（作者与主控同实例，如实标注）",
                     "words_ref": {"path": WORDS_DIR + f"/{task_id}.jsonl", "request_id": request_id, "sha256": digest(words)},
                     "changes": changes.strip(), "previous": before, "reviewed": False}
        event = save(root, {"task_id": task_id, "request_id": request_id, "expected_revision": current["revision"],
                            "reason": f"策略师在线改稿第{rounds}轮", "task": task},
                     actor, _iteration=iteration, _observations=dict(resolved), _commit_observations=dict(resolved))
        return handed_over(root, task_id, event, actor)


# iterate / adopt 写进任务的下一步（候选 r2 #5）；候选 r3 #8：派出背后检核或已有结果后，查询时按实际情况改说（任务记录原样不改）
ITERATE_NEXT = "已交给策略师（交出已登记）；接着派背后检核，结果出来告诉她（先交后检）"
ADOPT_NEXT = "交给策略师时当场登记交出，接着派背后检核（先交后检）"


def live_next_action(root, task_id, recorded):
    """任务记录里的下一步若是 iterate / adopt 写的“接着派背后检核”，而当前版本已派出背后检核或已有检核结果，
    返回按实际情况改说的下一步；其他情况（含读不出检核状态）返回 None，照记录原样显示。只读。"""
    if recorded not in (ITERATE_NEXT, ADOPT_NEXT):
        return None
    try:
        from jiaofu_guankou import _handed_for, _state, _target
        target = _target(root, task_id)
        state, _ = _state(root, task_id, target)
        handed = bool(_handed_for(root, task_id, target))
    except (OSError, ValueError, KeyError, TypeError, WorkflowError):
        return None
    said = {"checking": "背后检核已派出，结果出来告诉她", "passed": "背后检核已有结果：检查通过",
            "passed_with_notes": "背后检核已有结果：通过，有可改进处，见给策略师的提醒",
            "found_issues": "背后检核已有结果：发现问题，先把给策略师的提醒转述给她",
            "insufficient": "背后检核已有结果：资料不够，没看完（不算检查过），先把给策略师的提醒转述给她"}.get(state)
    if not said:
        return None
    # adopt 登记的版本还没登记交出时照实说（交给她时当场登记交出）
    return ("已交给策略师；" if handed else "还没登记交给策略师（交给她时当场登记交出）；") + said + "（先交后检）"


def handed_over(root, task_id, event, actor):
    """G01（v1.7.5）：在线改稿登记成功即登记“已交给策略师”，附给策略师的状态行与“下一步：派背后检核”的指引（G02）。
    交出登记失败不影响已完成的改稿登记，如实给出原因。原样重试时，只在登记的仍是当前版本时补交出记录。"""
    own = [e for e in history(root)[0] if e["task_id"] == task_id]
    if not own or own[-1]["revision"] != event["revision"]:
        return {**event, "handover_skipped": "这一轮已不是任务当前版本，没有补登记交出"}
    try:
        from jiaofu_guankou import handover
        record = handover(root, task_id, actor, source="iterate")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**event, "handover_error": f"这一版已登记，但“已交给你”的记录没记上：{exc}"}
    # 批次二返修 #4：交出新版这一步收尾旧版本未结束的检核分派（结果已登记的记“不需要了”，未登记的记“被新版取代”），
    # 不在这里派新检核；续做状态因此不在“新版登记后、派背后检核前”报“当前分派无法核实”。
    try:
        from jiaofu_guankou import close_old_reviews
        closed = {"closed_reviews": close_old_reviews(root, task_id, actor, "superseded",
                                                      f"交出第 {record.get('version')} 版时收尾（先交后检：被新版取代）")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        closed = {"closed_reviews_error": f"这一版已登记、已交出，但旧版的检查派工没收尾：{exc}"}
    return {**event, "handover": {k: record.get(k) for k in ("record_id", "version", "versions_since_pass", "last_pass_version", "status")},
            "status_line": record["status_line"], **closed,
            "next_step": {"behind_review": f"python scripts/jianhe_zhunbei.py --workspace . --task-id {task_id} --behind --actor {actor}",
                          "notice": "先交后检：这一版已交给策略师，接着派背后检核（只看最后通过版以来的累计改动；旧的未完成检核会被取代）；"
                                    "检核结果登记后，绿灯在下一次状态行顺带，发现问题时续做状态置顶给策略师的提醒"}}


def handover_step(task_id, actor):
    """save / adopt 登记新版后的下一步（候选 r2 #5）：交给策略师时当场登记交出，接着派背后检核（先交后检）。"""
    import shlex
    tid, who = shlex.quote(task_id), shlex.quote(actor)
    return {"handover": f"python scripts/jiaofu_guankou.py handover --workspace . --task-id {tid} --actor {who}",
            "behind_review": f"python scripts/jianhe_zhunbei.py --workspace . --task-id {tid} --behind --actor {who}",
            "notice": "先交后检：把这一版交给策略师时当场登记交给策略师（交出），接着派背后检核；不用等检核通过再交。"}


def adopt(root, task_id, dispatch_id, actor, reason=None):
    """把已回读（complete）的作者分派候选登记为当前版本（一条命令）。按正常改稿计轮与检核规则，不算检核通过。"""
    from registration_guard import actor_check
    from standalone_store import request_task
    from agent_dispatch import current as dispatch_current, AUTHOR_ROLES
    from phase2_store import sha
    root = Path(root).resolve(); actor_check(actor); identifier(task_id)
    with serialized(root):
        item = dispatch_current(root, dispatch_id)
        if item.get("task_id") != task_id or item.get("status") != "completed" or item.get("role") not in AUTHOR_ROLES:
            raise WorkflowError("--dispatch-id：须是本任务已回读（complete）的作者分派")
        events, _ = history(root)
        own = [e for e in events if e["task_id"] == task_id]
        if not own:
            raise WorkflowError("找不到任务：" + task_id)
        current = own[-1]
        request_id = "adopt-" + dispatch_id[-40:]
        prior = next((e for e in events if e["request_id"] == request_id), None)
        if prior:
            # 登记成功后重试（例如没收到响应）：候选与已登记的一致就原样返回，不报“编号已用于不同内容”
            done = sorted((r["path"], r["sha256"]) for r in prior["task"]["deliverables"] if "path" in r)
            if prior["task_id"] == task_id and done == sorted((c["path"], c["sha256"]) for c in item["candidates"]):
                return {**prior, "status": "existing", "notice": f"该分派候选已在第 {prior['revision']} 版登记，原样返回，未重复登记"}
            raise WorkflowError(f"分派 {dispatch_id} 的候选已登记过（第 {prior['revision']} 版），与现在的候选不同；先核对再处理")
        for c in item["candidates"]:
            if sha(reference_path(root, c["path"])) != c["sha256"]:
                raise WorkflowError(f"候选 {c['path']} 在回读后又改变；先核对再登记")
        unchanged_elsewhere(root, current["task"], {c["path"] for c in item["candidates"]})
        task = request_task(current["task"])
        task["deliverables"] = [{"label": f"分派候选 {Path(c['path']).parent.name or Path(c['path']).name}", "path": c["path"]} for c in item["candidates"]]
        task.update(status="in_progress", completion_evidence="",
                    summary=f"已把分派 {dispatch_id} 的候选登记为当前版本；未检核",
                    next_action=ADOPT_NEXT)
        accepted = {c["path"]: c["sha256"] for c in item["candidates"]}
        return save(root, {"task_id": task_id, "request_id": request_id, "expected_revision": current["revision"],
                           "reason": reason or f"登记分派 {dispatch_id} 的已回读候选为当前版本", "task": task},
                    actor, _observations=accepted, _commit_observations=accepted)


def words_issues(root, own):
    """在线改稿登记的策略师原话要能对上：原话文件里有该请求编号且内容摘要一致，否则指名提示（不改原件）。"""
    iterations = [e for e in own if isinstance(e.get("iteration"), dict)]
    if not iterations:
        return []
    ref_path = iterations[-1]["iteration"]["words_ref"]["path"]
    try:
        lines = [json.loads(x) for x in reference_path(root, ref_path).read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return [{"path": ref_path, "issue": "策略师原话记录缺失或损坏，在线改稿的原话无法回查"}]
    by_id = {x.get("request_id"): x for x in lines if isinstance(x, dict)}
    bad = [e["revision"] for e in iterations
           if digest(by_id.get(e["iteration"]["words_ref"]["request_id"], {}).get("words")) != e["iteration"]["words_ref"]["sha256"]]
    return [{"path": ref_path, "issue": f"第 {', '.join(map(str, bad))} 版在线改稿的策略师原话与登记摘要不符（原话被改或缺失）"}] if bad else []


def local_words(root, task_id):
    from phase2_store import local
    return local(root, WORDS_DIR + f"/{task_id}.jsonl")


def inspect(root, include_archived=False, current_only=False):
    root = Path(root).resolve()
    result = {"status": "empty", "count": 0, "active_count": 0, "tasks": [],
              "errors": [], "record_path": JOURNAL,
              "scope": "独立任务进度；不代表合同交付、独立检核或客户批准"}
    try:
        events, _ = history(root)
    except (OSError, ValueError, RecursionError) as exc:
        result.update(status="blocked", errors=[str(exc)])
        return result
    latest = {e["task_id"]: e for e in events}
    from guidang import archived_index
    try:  # 批次四开工补修 7（F08 同类）：归档记录坏一行时如实报读不出（续做状态不再 Traceback），不当成崩溃
        archived = archived_index(root) if latest else {}  # 没有任务时不读（同修前）
    except (OSError, ValueError) as exc:
        result.update(status="blocked", errors=[str(exc)])
        return result
    for event in latest.values():
        task = event["task"]
        if task["status"] == "archived" and not include_archived:
            continue
        if current_only and task["status"] in {"archived", "cancelled"}:
            from current_dependencies import current_references
            if not any((r.get("path"),r.get("sha256")) in current_references(root, exclude_task=event["task_id"])
                       for r in task["sources"] + task["deliverables"]):
                continue
        issues, links, archived_refs, appended = [], [], [], []
        for ref in task["sources"] + task["deliverables"] + task.get("process_refs",[]) + task.get("decision_refs",[]):
            if "url" in ref:
                links.append({**ref, "availability": "外部链接未随包复制；本次未联网核验"})
                continue
            try:
                path = reference_path(root, ref["path"])
                if not path.is_file() and (ref["path"], ref["sha256"]) in archived and task["status"] in {"completed", "cancelled", "archived", "paused"}:
                    from guidang import archived_copy_issue
                    problem = archived_copy_issue(root, (ref["path"], ref["sha256"]), archived[(ref["path"], ref["sha256"])])
                    if problem:
                        issues.append({"path": ref["path"], "issue": "记录为已归档，但" + problem})
                    else:
                        archived_refs.append({"path": ref["path"], "archived_to": archived[(ref["path"], ref["sha256"])]})
                elif not path.is_file():
                    issues.append({"path": ref["path"], "issue": "文件缺失" + ("（已归档，但任务仍在进行，需核对是否误归档）" if (ref["path"], ref["sha256"]) in archived else "")})
                elif __import__("task_validation").file_digest(path) != ref["sha256"]:
                    # F06（v1.7.3）：project/records/*.jsonl 只追加；登记部分原样、之后只是追加不算变化。
                    # 进行中任务给一句提示（resume 的 warnings），已结束任务不提示。
                    added = __import__("task_validation").appended_records(path, ref["path"], ref["sha256"])
                    if added is None:
                        issues.append({"path": ref["path"], "issue": "文件已变化，需回读并登记新版本"})
                    elif task["status"] in ACTIVE_STATUSES:
                        appended.append({"path": ref["path"], "added": added,
                                         "notice": f"{ref['path']} 自登记后新增 {added} 条记录，下次登记时顺手更新引用"})
            except (OSError, ValueError):
                issues.append({"path": ref["path"], "issue": "文件不可读取或引用不安全"})
        issues.extend(words_issues(root, [e for e in events if e["task_id"] == event["task_id"]]))
        live = live_next_action(root, event["task_id"], task.get("next_action")) if task["status"] in ACTIVE_STATUSES else None
        item = {**task, **({"next_action": live, "recorded_next_action": task["next_action"]} if live else {}),
                "task_id": event["task_id"], "revision": event["revision"],
                "updated_at": event["created_at"], "actor": event["actor"],
                "reason": event["reason"], "file_issues": issues, "external_links": links,
                **({"archived_refs": archived_refs} if archived_refs else {}),
                **({"record_appends": appended} if appended else {}),
                "effective_status": "needs_attention" if issues else task["status"]}
        result["tasks"].append(item)
    result["count"] = len(result["tasks"])
    result["active_count"] = sum(t["status"] in ACTIVE_STATUSES for t in result["tasks"])
    if latest:
        result["status"] = "attention" if any(t["file_issues"] for t in result["tasks"]) else "recorded"
    return result


def main():
    parser = argparse.ArgumentParser(description="独立任务记录与恢复，不改变合同计划")
    commands = parser.add_subparsers(dest="command", required=True)
    it = commands.add_parser("iterate", help="策略师在线改稿一轮：一条命令登记原话、新版、改动摘要")
    it.add_argument("--workspace", type=Path, required=True)
    it.add_argument("--task-id", required=True)
    it.add_argument("--words", required=True, help="策略师本轮原话")
    it.add_argument("--deliverable", action="append", default=[], help="新版文件，可重复")
    it.add_argument("--changes", required=True, help="改动摘要")
    it.add_argument("--label")
    it.add_argument("--request-id")
    it.add_argument("--actor", required=True)
    it.add_argument("--json", action="store_true")
    ad = commands.add_parser("adopt", help="把已回读的作者分派候选登记为当前版本")
    ad.add_argument("--workspace", type=Path, required=True); ad.add_argument("--task-id", required=True)
    ad.add_argument("--dispatch-id", required=True); ad.add_argument("--reason"); ad.add_argument("--actor", required=True)
    ad.add_argument("--json", action="store_true")
    for command in ("save", "inspect", "allow-more"):
        sub = commands.add_parser(command)
        sub.add_argument("--workspace", type=Path, required=True)
        sub.add_argument("--json", action="store_true", help="输出始终为 JSON")
        if command in {"save", "allow-more"}:
            if command == "save":
                sub.add_argument("--request", type=Path, required=True)
            else:
                sub.add_argument("--task-id", required=True)
                sub.add_argument("--evidence", required=True)
                sub.add_argument("--simulation", action="store_true")
            sub.add_argument("--actor", required=True)
        else:
            sub.add_argument("--include-archived", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "adopt":
            result = adopt(args.workspace, args.task_id, args.dispatch_id, args.actor, args.reason)
        elif args.command == "iterate":
            result = iterate(args.workspace, args.task_id, args.words, args.deliverable, args.changes, args.actor,
                             args.label, args.request_id)
        elif args.command == "allow-more":
            from standalone_budget import allow_more
            result = allow_more(args.workspace, args.task_id, args.evidence, args.actor, args.simulation)
        elif args.command == "save":
            used = used_request(args.workspace, args.request) if not args.request.exists() else None
            if used is not None:
                result = {**retry_used(args.workspace, args.request, used, args.actor), "request_moved_to": used}
            else:
                result = save(args.workspace, json.loads(args.request.read_text(encoding="utf-8")), args.actor)
                if not result.get("errors"):
                    moved = move_used(args.workspace, args.request)
                    if moved:
                        result = {**result, "request_moved_to": moved}
        else:
            result = inspect(args.workspace, args.include_archived)
        if args.command in {"save", "adopt"} and not result.get("errors") and result.get("task_id") \
                and (result.get("task") or {}).get("deliverables") and "next_step" not in result:
            result = {**result, "next_step": handover_step(result["task_id"], args.actor)}
        if args.command in {"save", "iterate", "adopt"} and RETENTION.get(result.get("task_id")):
            result = {**result, "version_retention": RETENTION[result["task_id"]]}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if result.get("errors"):
            from yunxing_rizhi import command_error
            command_error(WorkflowError(str(result["errors"][0])))
            failed = True
        else:
            failed = False
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        from workspace_lock import WorkspaceBusy
        from yunxing_rizhi import command_error
        command_error(exc)  # G04：入口自己捕获、打印后返回 1 的报错，命令级运行日志记它的首行
        if isinstance(exc,WorkspaceBusy):
            print(json.dumps({'status':'busy','reason_code':exc.reason_code,'message':str(exc),'written':False},ensure_ascii=False))
        else:print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False))
        failed = True
    if failed and args.command == "inspect":
        from yunxing_rizhi import StateReport
        raise StateReport(1)  # F08 漏网（v1.7.5 批次三 B，写法同 r4 #1）：查询如实报出 blocked，退出码仍 1，重试守卫不计数；真崩（未捕获异常）照常计数
    return 1 if failed else 0


from yunxing_rizhi import observed
save = observed("standalone_tasks.save")(save)
inspect = observed("standalone_tasks.inspect")(inspect)

if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main
    def main():
        return cli(_business_main, __file__)

if __name__ == "__main__":
    raise SystemExit(main())
