"""Cumulative standalone revision series; decisions bind an exact preceding version."""
import json
import re
from pathlib import Path
from standalone_store import digest, history, storage, identifier, text
from phase2_store import WorkflowError, now
from workspace_lock import serialized
from task_context import business_signature

DECISIONS = "project/records/standalone-decisions.jsonl"

def substantive(task):
    deps={a["artifact_id"]:a["dependencies"] for a in task.get("artifact_versions",[])}
    paths={p for values in deps.values() for p in values}
    effective=[r for r in task.get("process_refs",[]) if r.get("path") in paths]
    extra={"artifact_dependencies":deps,"artifact_dependency_files":effective} if deps else {}
    return {**extra,**{k: task[k] for k in ("request_text", "goal", "sources", "deliverables")}, **({"decision_refs":task["decision_refs"]} if "decision_refs" in task else {})}

def derive(events, task_id):
    own = [e for e in events if e['task_id'] == task_id]
    count = 0; has_output = False; previous = None
    for event in own:
        value = substantive(event['task'])
        if previous is not None and value != previous and has_output and not event.get('iteration'):
            count += 1
        has_output = has_output or bool(event['task']['deliverables'])
        previous = value
    if not own: return {"series_id":digest({"task_id":task_id}),"count":0,"used_decisions":[]}
    stored = own[-1].get('revision_budget')
    if stored:
        if (set(stored)!={'series_id','count','used_decisions'} or type(stored['count']) is not int
                or stored['count'] < count or not isinstance(stored['used_decisions'],list)
                or not re.fullmatch(r'[0-9a-f]{64}',str(stored['series_id']))
                or any(not re.fullmatch(r'[0-9a-f]{64}',str(x)) for x in stored['used_decisions'])
                or len(set(stored['used_decisions'])) != len(stored['used_decisions'])):
            raise WorkflowError('修订预算损坏，预算待核实')
        return dict(stored)
    return {"series_id":digest({"task_id":task_id,"source_request_key":own[0]['task'].get('source_request_key')}),
            "count":count,"used_decisions":[]}

def decisions(root):
    path=storage(root).parent/'standalone-decisions.jsonl'
    if path.is_symlink():raise WorkflowError('追加授权不能是符号链接')
    if not path.exists():return []
    result=[]
    for line in path.read_text().splitlines():
        e=json.loads(line)
        if set(e)!={'decision_id','task_id','base_revision','base_sha256','evidence','actor','simulation','created_at'}:
            raise WorkflowError('追加轮次决定损坏，预算待核实')
        result.append(e)
    return result

def allow_more(root,task_id,evidence,actor,simulation=False):
    from registration_guard import actor_check
    from phase2_store import test_mode
    from dispatch_files import file_ref
    actor_check(actor);identifier(task_id)
    if simulation and not test_mode(root):raise WorkflowError('模拟授权只能用于合成包')
    with serialized(root):
        own=[e for e in history(root)[0] if e['task_id']==task_id]
        if not own:raise WorkflowError('找不到任务')
        last=own[-1]; ref=file_ref(root,evidence,'真实用户追加决定原文')
        if not (Path(root)/ref['path']).read_text().strip():raise WorkflowError('须保留用户原文')
        value=dict(task_id=task_id,base_revision=last['revision'],base_sha256=digest(last['task']),
                   evidence=ref,actor=actor,simulation=simulation)
        decision_id=digest(value)
        for e in decisions(root):
            if e['decision_id']==decision_id:return e
        event=dict(value,decision_id=decision_id,created_at=now())
        with (Path(root)/DECISIONS).open('a',encoding='utf-8') as f:
            f.write(json.dumps(event,ensure_ascii=False)+'\n')
        return event

def next_budget(root,events,current,task_id,task):
    budget=derive(events,task_id)
    has_output=any(e['task_id']==task_id and e['task']['deliverables'] for e in events)
    if not current or substantive(current['task'])==substantive(task) or not has_output:
        return budget
    if budget['count']:
        from phase6_events import events as review_events, decision_valid
        from phase6_targets import catalog
        target=catalog(root)['standalone/'+task_id]['target']
        reviews=[r for r in review_events(root,'standalone_review') if r.get('target')==target]
        if not reviews or not decision_valid(root,reviews[-1]):
            raise WorkflowError('上一轮修订尚未完成本版本独立复核，不能连续改稿')
    if budget['count'] >= 2:
        from phase6_events import valid_file
        # Original decisions stay byte-for-byte intact. Resolve their exact historical
        # snapshot, then compare conservative business context (only progress excluded).
        bases={e['revision']:e for e in events if e['task_id']==task_id}
        matches=[e for e in decisions(root) if e['task_id']==task_id
            and e['base_revision'] in bases
            and e['base_sha256']==digest(bases[e['base_revision']]['task'])
            and business_signature(bases[e['base_revision']]['task'])==business_signature(current['task'])
            and all(business_signature(x['task'])==business_signature(current['task'])
                    for revision,x in bases.items() if revision>=e['base_revision'])
            and e['decision_id'] not in budget['used_decisions'] and valid_file(root,e['evidence'])]
        if not matches:raise WorkflowError('自动修订已达两轮，须策略师绑定本版本追加一轮；预算不清零')
        budget['used_decisions']=budget['used_decisions']+[matches[-1]['decision_id']]
    budget['count']+=1
    return budget
