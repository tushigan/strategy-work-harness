"""Generate evidence-bound facts from native records; never approves or transfers."""
import json,argparse
from pathlib import Path
from phase2_store import WorkflowError,now,sha
from phase5_common import atomic_bytes
from workspace_lock import serialized
from standalone_store import digest
from dispatch_files import local,file_ref
from yunxing_rizhi import observed,bind,classify
DIRECTORY='project/records/handoff-facts'

def snapshot(root,task_id=None):
    from workspace_resume import resume
    from standalone_store import history
    from phase6_targets import catalog,identity
    from phase6_events import events,decision_valid
    from agent_dispatch import replay,read_log
    qualifier=None;requested=task_id
    if task_id and ':' in task_id:
        qualifier,requested=task_id.split(':',1)
        if qualifier not in {'formal','standalone'}:raise WorkflowError('任务空间须为 formal 或 standalone')
    report=resume(root,task_id,True)
    items=catalog(root)
    choices={( 'standalone',t['task_id']) for t in report.get('current_tasks',[])}
    native=report.get('workflow_snapshot',{})
    for stage in ('phase3','phase5'):
        for t in native.get(stage,{}).get('tasks',[]):
            tid=t.get('task_id',t.get('id'))
            if tid and t.get('status') not in {'archived','cancelled'}:choices.add(('formal',tid))
    for item in items.values():
        tid=item.get('task_id');space=item['target']['space']
        if tid:choices.add(('standalone' if space=='standalone' else 'formal',tid))
    if requested:
        matches=[c for c in choices if c[1]==requested and (qualifier is None or c[0]==qualifier)]
        if len(matches)>1:raise WorkflowError('同名任务跨空间有歧义；请明确 '+ '、'.join(k+':'+v for k,v in sorted(matches)))
        if not matches and (choices or not report.get('errors')):raise WorkflowError('找不到指定当前任务：'+task_id)
        selected=requested;selected_kind=matches[0][0] if matches else qualifier
    else:
        if len(choices)>1:raise WorkflowError('有多个当前任务，须明确 --task-id：'+ '、'.join(k+':'+v for k,v in sorted(choices)))
        selected_kind,selected=next(iter(choices)) if choices else (None,None)
    bind(selected)
    errors=list(report.get('errors',[]));facts=[];proofs=[]
    try:
        reviews=events(root);task_events=history(root)[0]
        for marker,item in items.items():
            target=item['target'];owner=item.get('task_id')
            kind='standalone' if target['space']=='standalone' else 'formal'
            if selected and owner and (owner!=selected or kind!=selected_kind):continue
            if not owner and target['space'] in {'phase3','phase5','html','design-submission'}:
                errors.append('正式成果无法核实任务归属：'+marker);continue
            # Unassigned project sources/contract controls remain common evidence.

            from review_gate import gate as unified_gate
            space=target['space']
            if space=='phase2':
                import phase2_store as p2
                native=p2.events(root,'reviews')
            elif space in {'phase3','html'}:
                from phase3_events import events as events3
                native=events3(root,'phase3-reviews' if space=='phase3' else 'phase4-reviews')
            elif space in {'phase5','design-submission'}:
                from phase5_common import events as events5
                native=events5(root,'review' if space=='phase5' else 'design_review')
            else:native=events(root,'standalone_review' if space=='standalone' else 'control_review')
            def same(record):
                t=record.get('target',{});return (t==target or target['space']=='phase2' and t=={k:target[k] for k in ('kind','version','sha256')})
            own=[r for r in native if same(r)]
            historical=[r for r in native if not same(r) and r.get('target',{}).get('key',r.get('target',{}).get('kind'))==target['key']]
            current_review='source_not_deliverable' if target['space']=='source' else 'not_executed';review_refs=[]
            if own:
                last=own[-1];current_review=last.get('status','unknown')
                review_refs=[dict(record_id=last['record_id'],evidence=last.get('evidence'),simulation=last.get('simulation'),actual=last.get('checked_sources',[]),inherited=last.get('reused_coverage',[]),uncovered=last.get('uncovered',[]))]
                if current_review in {'passed','passed_with_yellow'}:
                    try:unified_gate(root,target)
                    except (ValueError,OSError,KeyError,TypeError):current_review='invalid_evidence'
            space=target['space']
            if space=='phase2':
                import phase2_store as p2
                confirmations=p2.events(root,'confirmations')
            elif space=='phase3':
                from phase3_events import events as events3
                confirmations=events3(root,'phase3-confirmations')
            elif space=='phase5':
                from phase5_common import events as events5
                confirmations=events5(root,'confirmation')
            else:confirmations=events(root,'confirmation')
            confirmations=[r for r in confirmations if same(r) and r.get('role','strategist')=='strategist']
            human='not_recorded';confirmation_refs=[]
            if confirmations:
                c=confirmations[-1];human=c.get('status','unknown')+'_recorded_not_validated'
                confirmation_refs=[dict(record_id=c['record_id'],evidence=c.get('evidence'),status=c.get('status'),simulation=c.get('simulation'))]
                if c.get('status')=='confirmed':
                    try:unified_gate(root,target,human=True);human='confirmed_current'
                    except (ValueError,OSError,KeyError,TypeError):pass
            acceptance=[]
            if target['space']=='standalone':
                own_tasks=[e for e in task_events if e['task_id']==target['key']]
                for e in own_tasks:
                    # Adoption wording is not an independent-review conclusion.
                    if e.get('reason'):acceptance.append(dict(revision=e['revision'],reason=e['reason'],actor=e['actor'],observed_adoption=e.get('observed_adoption'),record_path='project/records/standalone-tasks.jsonl'))
            user_adoptions=[dict(revision=e['revision'],record_path='project/records/standalone-tasks.jsonl',adoption=e['observed_adoption']) for e in task_events if target['space']=='standalone' and e['task_id']==target['key'] and e.get('observed_adoption',{}).get('accepted_paths')]
            files=[]
            for f in item['files']:
                if not isinstance(f,dict) or not f.get('path'):continue
                try:ref=file_ref(root,f,'当前有效原件');files.append({**ref,'valid':True});proofs.append(ref)
                except (ValueError,OSError):files.append(dict(path=f.get('path'),valid=False));errors.append('当前原件无法核实：'+str(f.get('path')))
            facts.append(dict(target=target,files=files,human_confirmation=human,human_confirmation_evidence=confirmation_refs,user_adoptions=user_adoptions,registration_reasons=acceptance,independent_review=current_review,current_review_evidence=review_refs,historical_review_evidence=[dict(record_id=r['record_id'],target=r['target'],status=r.get('status'),evidence=r.get('evidence'),simulation=r.get('simulation')) for r in historical]))
        dispatches=[]
        from current_dependencies import dispatch_issues
        dependency_report=dispatch_issues(root,current_only=True)
        errors.extend(dependency_report["current_errors"])
        for d in replay(read_log(root)).values():
            if selected and (d.get('task_id')!=selected or d.get('task_context',{}).get('kind') not in {None,selected_kind}):continue
            prefix='分派 '+d['dispatch_id']+'：'
            issues=[x for x in dependency_report['current_errors'] if x.startswith(prefix)]
            historical=[x for x in dependency_report['historical_issues'] if x.startswith(prefix)]
            dispatches.append({k:d.get(k) for k in ('dispatch_id','task_id','status','instance','host_instance','prepared_at','created_at','invocation_details','return_invocation_details','conflicts','registration_status','integration')})
            dispatches[-1].update(readback_verified=d.get('readback_verified',False),independent_review=d.get('review_status','not_registered'),evidence_issues=issues,historical_evidence_issues=historical,historical_verification='native_current_only',record_path='project/records/agent-dispatch.jsonl')
    except (ValueError,OSError,KeyError,TypeError) as exc:
        errors.append('原生记录不能核实：'+str(exc));dispatches=[]
    from task_receipts import history as receipt_history
    try:receipts=[e for e in receipt_history(root)[0] if not selected or e.get('task_id')==selected and ('standalone' if e.get('task_ref',{}).get('space')=='standalone' else 'formal')==selected_kind]
    except (OSError,ValueError,KeyError):receipts=[];errors.append('执行回执记录无法核实')
    # Facts refer to effective business inputs only. Our output directory is never an input.
    proofs=[p for p in proofs if not p['path'].startswith(DIRECTORY+'/')]
    result=dict(schema_version=1,mode='facts_only',selected_task_id=selected,selected_task_space=selected_kind,status='blocked' if errors else report.get('status','unknown'),formal_handoff='not_executed_by_summary',facts=facts,dispatches=dispatches,execution_receipts=receipts,current_tasks=[t for t in report.get('current_tasks',[]) if not selected or selected_kind=='standalone' and t['task_id']==selected],available_tasks=[dict(space=k,task_id=v) for k,v in sorted(choices)],formal_artifacts=report.get('formal_artifacts',[]),memory=report.get('memory',{}),state_attention=report.get('state_attention',[]),pending_transactions=report.get('pending_transactions',[]),errors=sorted(set(errors)),warnings=report.get('warnings',[]),checked_scope=report.get('checked_scope',[]),not_checked=report.get('not_checked',[]),record_paths=['project/state.json','project/records/standalone-tasks.jsonl','project/records/phase6-events.jsonl','project/records/agent-dispatch.jsonl'],proofs=proofs)
    return result

def version_text(target):
    if 'version' in target:return '第'+str(target['version'])+'版'
    return '提交编号 '+str(target['key'])+'；校验值 '+str(target['sha256'])

def markdown(facts,identifier):
    rows=['# 程序交接事实','',f'事实编号：{identifier}；任务：{facts["selected_task_id"] or "空白或正式项目"}；状态：{facts["status"]}。','这是当前记录的事实摘要；未执行正式 prepare/accept，不代表批准。','']
    for item in facts['facts']:
        t=item['target'];rows.append(f'- {t["space"]}/{t["key"]} {version_text(t)}：人工确认={item["human_confirmation"]}；独立复核={item["independent_review"]}。')
        for r in item['current_review_evidence']:rows.append(f'  当前报告 {r["record_id"]}，合成={r["simulation"]}；实查{len(r["actual"])}，继承{len(r["inherited"])}，未覆盖{len(r["uncovered"])}。原件：{r["evidence"]}')
        for r in item['historical_review_evidence']:rows.append(f'  历史{version_text(r["target"])}：{r["status"]}，合成={r["simulation"]}；不作为当前通过。原件：{r["evidence"]}')
        for r in item['files']:rows.append(f'  有效原件：{r["path"]}，可核实={r["valid"]}。')
    for r in facts['execution_receipts']:rows.append(f'- 原生执行回执 {r.get("record_id")}：{r.get("data",{}).get("outcome")}；绑定任务 {r.get("task_ref")}；记录 project/records/task-receipts.jsonl。')
    for d in facts['dispatches']:rows.append(f'- 分派 {d["dispatch_id"]}：{d["status"]}；回读={d["readback_verified"]}；登记={d["integration"]}；冲突={d["conflicts"]}；独立复核={d["independent_review"]}。')
    rows+=['','阻断：'+('；'.join(facts['errors']) or '当前程序检查未发现')+'。','尚未验证：'+'；'.join(facts['not_checked'])+'。','','恢复方法：从本项目根目录运行 `python scripts/project_handoff.py resume --workspace .'+(' --task-id '+str(facts.get('selected_task_space')+':'+facts['selected_task_id'] if facts.get('selected_task_space') else facts['selected_task_id']) if facts['selected_task_id'] else '')+' --details --json`；先核对当前记录，再继续待做工作。','建议：保留候选和手稿，先处理阻断与当前版待审；建议不等于已执行。']
    return '\n'.join(rows)+'\n'

@observed('jiaojie.summary')
def generate(root,task_id=None):
    root=Path(root).resolve()
    with serialized(root):
        facts=snapshot(root,task_id);identifier=digest(facts)
        folder=local(root,DIRECTORY);folder.mkdir(parents=True,exist_ok=True)
        paths=[local(root,DIRECTORY+'/'+identifier+ext) for ext in ('.json','.md')]
        data=[(json.dumps({'facts_sha256':identifier,'facts':facts},ensure_ascii=False,indent=2)+'\n').encode(),markdown(facts,identifier).encode()]
        if snapshot(root,task_id)!=facts:raise WorkflowError('生成期间当前记录或文件改变，未保存摘要；重新读取当前状态')
        reused=all(p.exists() for p in paths)
        for p,raw in zip(paths,data):
            if p.exists():
                if p.read_bytes()!=raw:raise WorkflowError('同状态交接原件损坏，不能覆盖')
            else:atomic_bytes(p,raw)
        classify('handoff_reused' if reused else 'handoff_generated')
        return dict(status=facts['status'],facts_sha256=identifier,reused=reused,files=[file_ref(root,p.relative_to(root).as_posix(),'交接摘要') for p in paths],facts=facts,written=not reused)

def verify(root,path,task_id=None):
    p=local(root,path);value=json.loads(p.read_text());facts=value['facts']
    if value['facts_sha256']!=digest(facts):raise WorkflowError('交接摘要损坏')
    current=snapshot(root,task_id or ((facts['selected_task_space']+':'+facts['selected_task_id']) if facts.get('selected_task_space') and facts['selected_task_id'] else facts['selected_task_id']));return dict(status='current' if current==facts else 'stale',formal_handoff='not_executed_by_summary',facts_sha256=value['facts_sha256'])
