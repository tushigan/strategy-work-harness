"""Frozen readable inputs and exclusive new candidates using native dispatch events."""
import json,uuid
from pathlib import Path
from datetime import datetime,timezone
from phase2_store import WorkflowError,sha,test_mode
from phase5_common import atomic_bytes
from dispatch_files import file_ref,local,verify_refs
from standalone_store import digest

def prepare_targets(root,request,payload,dispatch_id):
    ids=request.get('modify_targets')
    if ids is None:return payload
    if payload['role'] not in {'strategy-author','proposal-author','deck-builder'} or 'write_candidates' not in payload['permissions']:raise WorkflowError('修改目标与角色/权限冲突；作者角色须明确write_candidates权限')
    if not isinstance(ids,list) or not ids or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):raise WorkflowError('modify_targets须为去重成果身份列表')
    from task_context import resolve_task
    task=resolve_task(root,payload['task_id'])
    if task['kind']!='standalone':raise WorkflowError('本轮修改目标协议适用于显式登记的独立任务；正式阶段沿用原生发布协议')
    artifacts={x['artifact_id']:x for x in task['task'].get('artifact_versions',[])}
    errors=[i for i in ids if i not in artifacts]
    if errors:raise WorkflowError('未授权或未登记的修改目标：'+','.join(errors))
    targets=[];replacements={}
    for i in ids:
        e=artifacts[i];origin=file_ref(root,{'path':e['path'],'sha256':e['sha256']},'修改目标')
        if origin not in payload['input_files']:raise WorkflowError('作者须实际读取每份修改目标；input_files须包含目标原件')
        prefix=f'project/records/dispatch-snapshots/{dispatch_id}'
        snapshot=f'{prefix}/{len(targets):04d}{Path(e["path"]).suffix}'
        raw=local(root,e['path']).read_bytes();path=local(root,snapshot)
        if path.exists():raise WorkflowError('修改前快照已存在，不能覆盖')
        atomic_bytes(path,raw);frozen=file_ref(root,snapshot,'修改前快照')
        if frozen['sha256']!=origin['sha256'] or file_ref(root,origin,'修改目标')!=origin:raise WorkflowError('准备期间修改目标变化，未准备分派')
        output=f'project/exports/candidates/{dispatch_id}/{len(targets):04d}{Path(e["path"]).suffix}'
        if local(root,output).exists():raise WorkflowError('候选位置已有文件')
        local(root,output).parent.mkdir(parents=True,exist_ok=True)
        dependency_refs=[file_ref(root,p,'显式成果依赖') for p in e['dependencies']]
        if any(r not in payload['input_files'] for r in dependency_refs):raise WorkflowError('输入缺成果声明依赖，先补齐必要原件再准备：'+','.join(r['path'] for r in dependency_refs if r not in payload['input_files']))
        targets.append(dict(artifact_id=i,original=origin,snapshot=frozen,candidate_path=output,dependencies=e['dependencies'],dependency_refs=dependency_refs))
        replacements[e['path']]=frozen
    return {**payload,'dispatch_protocol':2,'modification_targets':targets,'input_files':[replacements.get(r['path'],r) for r in payload['input_files']]}

def invocation(root,item,evidence,instance,host):
    from registration_guard import actor_check
    actor_check(instance);actor_check(host)
    ref=file_ref(root,evidence,'真实宿主调用凭证')
    if ref['path'] in {r['candidate_path'] for r in item['modification_targets']} or ref in [t['original'] for t in item['modification_targets']]:raise WorkflowError('成品/原件不能充当调用证明')
    try:p=json.loads(local(root,ref['path']).read_text())
    except (ValueError,UnicodeError):raise WorkflowError('修改分派须独立JSON调用凭证；真实接口无凭证保留未验证')
    keys={'kind','dispatch_id','instance','host_instance','started_at','returned_at','simulation','status','tool_call_id'}
    if not isinstance(p,dict) or set(p)!=keys or p['kind']!='host_invocation' or any(p[k]!=v for k,v in [('dispatch_id',item['dispatch_id']),('instance',instance),('host_instance',host)]):raise WorkflowError('调用凭证身份/分派不匹配；不能用成品补造')
    if type(p['simulation']) is not bool or p['simulation'] and not test_mode(root):raise WorkflowError('模拟调用只限合成项目')
    if p['status'] not in {'started','returned'} or not isinstance(p['tool_call_id'],str) or not p['tool_call_id'].strip():raise WorkflowError('须实际可观测调用号与状态')
    start=datetime.fromisoformat(p['started_at']);prepared=datetime.fromisoformat(item.get('prepared_at',item['created_at']));current=datetime.now(timezone.utc)
    if start.tzinfo is None or not prepared<=start<=current:raise WorkflowError('调用时序不符合先准备后调用')
    if p['status']=='started' and p['returned_at'] is not None:raise WorkflowError('尚在执行的调用不能填返回时间')
    if p['status']=='returned':
        end=datetime.fromisoformat(p['returned_at'])
        if end.tzinfo is None or not start<=end<=current:raise WorkflowError('同步返回时间无效')
    return ref,p

def terminal_invocation(root,item,evidence):
    initial=item['invocation_details']
    if initial['status']=='returned':
        if evidence is not None:raise WorkflowError('同步返回已有不可变凭证，不重复补造')
        return {}
    if evidence is None:raise WorkflowError('异步调用尚缺真实返回凭证；保留候选等待回传，不重派')
    ref,p=invocation(root,item,evidence,item['instance'],item['host_instance'])
    if p['status']!='returned' or any(p[k]!=initial[k] for k in ('tool_call_id','started_at','simulation')) or ref==item['invocation_evidence']:raise WorkflowError('异步返回必须对应同一调用且用独立返回凭证，不能覆盖启动凭证')
    return {'return_invocation_evidence':ref,'return_invocation_details':p}

def candidates(root,item,values):
    refs=[file_ref(root,v,'回传候选') for v in values]
    expected={r['candidate_path'] for r in item['modification_targets']}
    if {r['path'] for r in refs}!=expected or len(refs)!=len(expected):raise WorkflowError('须回传本分派专属候选，不能写原件或别的目标')
    if item.get('instance') is None:raise WorkflowError('未登记调用身份')
    return refs

def conflicts(root,item):
    result=[]
    for t in item['modification_targets']:
        try:file_ref(root,t['original'],'用户当前原件')
        except (OSError,ValueError):result.append(dict(path=t['original']['path'],candidate_path=t['candidate_path'],reason='original_changed',action='双方保留；主控展示差异，按当前观察版及用户原文明确选择，不重派作者'))
    seen={r['path'] for r in result}
    for ref in item['input_files']:
        try:file_ref(root,ref,'实际输入')
        except (OSError,ValueError):
            if ref['path'] not in seen:
                result.append(dict(path=ref['path'],candidate_paths=[t['candidate_path'] for t in item['modification_targets']],reason='input_changed',action='保留已完成候选；主控核对实际输入和当前依据，不重派相同成果'));seen.add(ref['path'])
    return result

from yunxing_rizhi import observed,classify

@observed("agent_dispatch.integrate")
def integrate(root,dispatch_id,packet,actor):
    from workspace_lock import serialized
    from agent_dispatch import current,append,controller_of,verify_context
    from shougai_dengji import patch
    with serialized(root):
        item=current(root,dispatch_id)
        if item.get('dispatch_protocol')!=2 or item.get('status')!='completed':raise WorkflowError('修改候选须先完成真实回传与主控回读；旧分派沿用原登记')
        if controller_of(root,item)!=actor:raise WorkflowError('须由本次登记主控续接')
        if not isinstance(packet,dict) or not isinstance(packet.get('basis'),dict) or packet['basis'].get('task_id')!=item['task_id']:
            raise WorkflowError('续接预览凭证与分派不属于同一任务')
        if item.get('integration'):
            if item['integration'].get('task_id',item['task_id'])!=item['task_id']:raise WorkflowError('已有续接回执任务归属冲突')
            if item['integration']['request_hash']!=digest(packet):raise WorkflowError('同分派登记请求内容冲突')
            # Revalidate the candidate proof, but do not repeat business registration.
            verify_refs(root,item)
            classify("idempotent_request")
            return item['integration']
        verify_refs(root,item)
        changes=packet.get('changes',{});refs=changes.get('deliverables');identity=changes.get('artifact_versions')
        if not isinstance(refs,list) or not isinstance(identity,list):raise WorkflowError('续接须列出完整当前成果指向与显式版本关系；由回传值构造')
        candidate_set={(r['path'],r['sha256']) for r in item['candidates']}
        for t in item['modification_targets']:
            entry=next((e for e in identity if e.get('artifact_id')==t['artifact_id']),None)
            if not entry or (entry['path'],sha(local(root,entry['path']))) not in candidate_set or entry['previous']!=t['original']:raise WorkflowError('登记成果须绑定本分派真实候选与修改前版')
            if not any(r.get('path')==entry['path'] for r in refs):raise WorkflowError('候选未列入当前交付物')
        # Recover a committed request before first-write checks: later original edits
        # cannot undo an already registered candidate or consume another revision.
        from standalone_store import history
        request_id=packet['basis'].get('request_id')
        already=next((e for e in history(root)[0] if e['request_id']==request_id),None)
        if already is not None and already['task_id']!=item['task_id']:raise WorkflowError('既有请求不属于本分派任务')
        if already is None:
            original_conflicts=[x for x in conflicts(root,item) if x['reason']=='original_changed']
            if original_conflicts:
                decision=packet.get('user_decision')
                if not isinstance(decision,str) or not decision.strip():raise WorkflowError('原件已有用户改稿；须非空用户原文明确选择候选，双方保留')
                observed=packet['basis'].get('observations',[])
                paths={r.get('path') for r in observed if isinstance(r,dict)}
                if any(c['path'] not in paths or c['candidate_path'] not in paths for c in original_conflicts):raise WorkflowError('冲突原件和候选须均绑定本次观察版；先重新预览')
            for ref in item['input_files']:file_ref(root,ref,'本次实际输入')
            verify_context(root,item)
        result=patch(root,packet,actor)
        event=result['event']
        if event['task_id']!=item['task_id']:raise WorkflowError('业务回执不属于本分派任务')
        integration=dict(request_hash=digest(packet),task_id=event['task_id'],task_revision=event['revision'],task_request_id=event['request_id'],task_sha256=digest(event['task']),status='registered_not_reviewed')
        append(root,dict(event='candidate_registered',dispatch_id=dispatch_id,status='completed',actor=actor,integration=integration))
        return integration
