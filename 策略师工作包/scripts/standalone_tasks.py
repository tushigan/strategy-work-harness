"""Natural-language controller entry point for small, unplanned project tasks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from phase2_store import WorkflowError, now
from workspace_lock import serialized
from standalone_store import (JOURNAL, digest, history, identifier, reference_path,
                              storage, task_value, text)


def save(root, request, actor, *, _observations=None, _commit_observations=None, _request_hash=None, _adoption=None, _iteration=None):
    root = Path(root).resolve()
    storage(root)  # Check before acquiring a lock that creates directories.
    required_fields = {"task_id", "request_id", "expected_revision", "reason", "task"}
    if (not isinstance(request, dict)
            or set(request) not in (required_fields,
                                    required_fields | {"accept_changed_references"})):
        raise WorkflowError("请求字段不完整或含未支持字段")
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
                raise WorkflowError("请求编号已用于不同内容，保留原记录")
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
            commit_references = {ref["path"]: ref["sha256"] for ref in
                                 normalized_task["sources"] + normalized_task["deliverables"]
                                 + normalized_task.get("process_refs",[]) + normalized_task.get("decision_refs",[])
                                 if "path" in ref}
            for ref_path, observed_sha in (_commit_observations or {}).items():
                if ref_path in commit_references and commit_references[ref_path] != observed_sha:
                    raise WorkflowError("登记期间观察版改变；重新核对后形成新请求，未写业务事件")
                commit_references[ref_path] = observed_sha
            try:
                for ref_path, expected_sha in commit_references.items():
                    live = reference_path(root, ref_path)
                    if not live.is_file() or hashlib.sha256(live.read_bytes()).hexdigest() != expected_sha:
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
            return prior
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
        task["next_action"] = "等策略师看新版；对外交付前须有独立检核或针对对外交付的豁免原话"
        task["completion_evidence"] = ""
        iteration = {"round": rounds, "mode": "主控改稿（作者与主控同实例，如实标注）",
                     "words_ref": {"path": WORDS_DIR + f"/{task_id}.jsonl", "request_id": request_id, "sha256": digest(words)},
                     "changes": changes.strip(), "previous": before, "reviewed": False}
        return save(root, {"task_id": task_id, "request_id": request_id, "expected_revision": current["revision"],
                           "reason": f"策略师在线改稿第{rounds}轮", "task": task},
                    actor, _iteration=iteration, _observations=dict(resolved), _commit_observations=dict(resolved))


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
                    next_action="准备独立检核（jianhe_zhunbei.py）或交策略师查看；对外交付前须检核或对外豁免")
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
    for event in latest.values():
        task = event["task"]
        if task["status"] == "archived" and not include_archived:
            continue
        if current_only and task["status"] in {"archived", "cancelled"}:
            from current_dependencies import current_references
            if not any((r.get("path"),r.get("sha256")) in current_references(root, exclude_task=event["task_id"])
                       for r in task["sources"] + task["deliverables"]):
                continue
        issues, links, archived_refs = [], [], []
        from guidang import archived_index
        archived = archived_index(root)
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
                    issues.append({"path": ref["path"], "issue": "文件已变化，需回读并登记新版本"})
            except (OSError, ValueError):
                issues.append({"path": ref["path"], "issue": "文件不可读取或引用不安全"})
        issues.extend(words_issues(root, [e for e in events if e["task_id"] == event["task_id"]]))
        item = {**task, "task_id": event["task_id"], "revision": event["revision"],
                "updated_at": event["created_at"], "actor": event["actor"],
                "reason": event["reason"], "file_issues": issues, "external_links": links,
                **({"archived_refs": archived_refs} if archived_refs else {}),
                "effective_status": "needs_attention" if issues else task["status"]}
        result["tasks"].append(item)
    result["count"] = len(result["tasks"])
    result["active_count"] = sum(t["status"] in {"planned", "in_progress", "waiting"}
                                  for t in result["tasks"])
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
            result = save(args.workspace, json.loads(args.request.read_text(encoding="utf-8")), args.actor)
        else:
            result = inspect(args.workspace, args.include_archived)
        if args.command in {"save", "iterate", "adopt"} and RETENTION.get(result.get("task_id")):
            result = {**result, "version_retention": RETENTION[result["task_id"]]}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if result.get("errors") else 0
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        from workspace_lock import WorkspaceBusy
        if isinstance(exc,WorkspaceBusy):
            print(json.dumps({'status':'busy','reason_code':exc.reason_code,'message':str(exc),'written':False},ensure_ascii=False))
        else:print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False))
        return 1


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
