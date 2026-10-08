"""Observed-version previews and partial updates; the existing task journal owns writes."""
import argparse,copy,difflib,json,re,uuid
from pathlib import Path
from phase2_store import WorkflowError,sha
from standalone_store import history,digest,reference_path,FIELDS,OPTIONAL_FIELDS,text
from workspace_lock import serialized
from registration_guard import actor_check
from yunxing_rizhi import observed,note,classify,bind
from yunxing_chayi import TEXT_TYPES,MAX_BYTES,MAX_LINES

class RegistrationError(WorkflowError):
    def __init__(self,issues,reason_code='registration_invalid'):
        self.issues=issues;self.reason_code=reason_code
        super().__init__('；'.join(f"{i['field']}：{i['reason']}；{i['action']}" for i in issues))

def issue(field,reason,action='核对后形成新请求；不原样重试'):
    return dict(field=field,reason=reason,action=action)

def latest(root,task_id):
    value=next((e for e in reversed(history(root)[0]) if e['task_id']==task_id),None)
    if value is None:raise RegistrationError([issue('task_id','找不到任务')])
    return value

def difference(root,before,after):
    a=reference_path(root,after['path']);actual=sha(a) if a.is_file() else None
    if actual!=after['sha256']:raise RegistrationError([issue(after['path'],'观察版已改变')],'observation_conflict')
    data=dict(before=before,after=after,changed=None if before is None else before['sha256']!=actual,comparison='unknown',required=True,preview=[],preview_complete=False,locators=[])
    if before is None:data['reason']='no_snapshot';return data
    b=reference_path(root,before['path'])
    if not b.is_file() or sha(b)!=before['sha256']:data['reason']='snapshot_invalid';return data
    if before['sha256']==actual:data.update(comparison='unchanged',required=False,preview_complete=True);return data
    if a.suffix.lower() not in TEXT_TYPES:data['reason']='diff_type';return data
    if max(a.stat().st_size,b.stat().st_size)>MAX_BYTES:data['reason']='diff_size';return data
    try:old=b.read_bytes().decode('utf-8-sig');new=a.read_bytes().decode('utf-8-sig')
    except UnicodeError:data['reason']='diff_encoding';return data
    # Normalize display only; hashes above always bind exact bytes.
    x=old.replace('\r\n','\n').replace('\r','\n').splitlines();y=new.replace('\r\n','\n').replace('\r','\n').splitlines()
    if max(len(x),len(y))>MAX_LINES:data['reason']='diff_lines';return data
    # The bounded child contains the potentially expensive sequence matching.
    from incremental_review import bounded_difference
    d=bounded_difference(b,a,before['sha256'],actual)
    data['preview']=d['diff_preview'];data['comparison']='changed'
    data['preview_complete']=not d.get('preview_omitted') and len(data['preview'])<40
    if d.get('preview_omitted'):data['reason']=d['preview_omitted']
    else:data['reason']='preview_truncated' if not data['preview_complete'] else 'byte_change'
    ids=re.findall(r'(?:id=["\'](p\d+)["\']|^#{1,6}\s+(.+))',new,re.M)
    data['locators']=[a or b for a,b in ids][:100];data['duplicate_locators']=len(ids)!=len(set(ids))
    data['display_equal']=x==y
    return data

def prior_snapshot(root,ref):
    # Only immutable, actually captured review evidence; never fabricate old text.
    from phase6_events import valid_file
    for p in sorted((Path(root)/'project/records/review-baselines').glob('*/baseline.json')):
        value=json.loads(p.read_text())
        for f in value['files']:
            if f['original']=={'path':ref['path'],'sha256':ref['sha256']} and valid_file(root,f['snapshot']):return f['snapshot']
    return None

@observed('shougai.preview')
def preview(root,task_id):
    bind(task_id)
    with serialized(root):
        e=latest(root,task_id);files=[];observed_refs=[];problems=[]
        for field in ('sources','deliverables','process_refs','decision_refs'):
            for ref in e['task'].get(field,[]):
                if 'path' not in ref:continue
                try:
                    p=reference_path(root,ref['path']);current={'path':ref['path'],'sha256':sha(p)}
                    files.append(dict(field=field,registered={'path':ref['path'],'sha256':ref['sha256']},**difference(root,prior_snapshot(root,ref),current)))
                    observed_refs.append(current)
                except (OSError,ValueError):problems.append(issue(ref['path'],'文件缺失、不可读或不安全','归档可读原件后重新预览'))
        packet=dict(schema_version=1,task_id=task_id,expected_revision=e['revision'],request_id='edit-'+uuid.uuid4().hex,observations=observed_refs)
        packet['basis_sha256']=digest(packet)
        return dict(status='blocked' if problems else 'observed',basis=packet,files=files,issues=problems,written=False)

LISTS={'sources','deliverables','process_refs','historical_refs','decision_refs','artifact_versions'}
def merge_task(task,changes,collect=False):
    result=copy.deepcopy(task);problems=[]
    if not isinstance(changes,dict):raise RegistrationError([issue('changes','必须为对象')])
    for k,v in changes.items():
        if k not in FIELDS|OPTIONAL_FIELDS:problems.append(issue(k,'未知或程序只读字段'));continue
        if k in LISTS and isinstance(v,dict):
            if set(v)-{'add','remove'} or any(not isinstance(x,list) for x in v.values()):problems.append(issue(k,'列表使用 replace列表 或 add/remove对象'));continue
            values=copy.deepcopy(result.get(k,[]))
            identity=lambda x: {a:b for a,b in x.items() if a!='sha256'} if isinstance(x,dict) else x
            for item in v.get('remove',[]):
                match=next((x for x in values if identity(x)==identity(item)),None)
                if match is None:problems.append(issue(k,'删除项不在当前列表'));continue
                values.remove(match)
            for item in v.get('add',[]):
                if not any(identity(x)==identity(item) for x in values):values.append(item)
            result[k]=values
        else:result[k]=v
    if collect:return result,problems
    if problems:raise RegistrationError(problems)
    return result

@observed('shougai.patch')
def patch(root,request,actor):
    actor_check(actor)
    required={'basis','changes','reason','accept_paths','user_decision'}
    if not isinstance(request,dict) or set(request)!=required:raise RegistrationError([issue(k,'缺失或未知字段') for k in sorted(required-set(request) if isinstance(request,dict) else required)]+[issue(k,'未知字段') for k in sorted(set(request)-required if isinstance(request,dict) else [])])
    basis=request['basis']
    keys={'schema_version','task_id','expected_revision','request_id','observations','basis_sha256'}
    if not isinstance(basis,dict) or set(basis)!=keys or basis['schema_version']!=1 or digest({k:v for k,v in basis.items() if k!='basis_sha256'})!=basis['basis_sha256']:raise RegistrationError([issue('basis','须原样引用程序预览凭证')])
    from standalone_store import identifier
    bind(basis['task_id'])
    identifier(basis['task_id']);identifier(basis['request_id']);text(request['reason'],'原因')
    fingerprint=digest({'patch':request,'actor':actor})
    with serialized(root):
        events,_=history(root)
        from phase6_events import events as records,append
        noop=next((e for e in records(root,'standalone_patch_noop') if e.get('request_id')==basis['request_id']),None)
        if noop:
            if noop.get('task_id')!=basis['task_id'] or noop['request_hash']!=fingerprint:raise RegistrationError([issue('request_id','相同编号不同内容')],'request_conflict')
            classify('idempotent_request');return dict(status='unchanged',event=next(e for e in events if e['task_id']==noop['task_id'] and e['revision']==noop['revision']),written=False)
        prior=next((e for e in events if e['request_id']==basis['request_id']),None)
        if prior:
            if prior['task_id']!=basis['task_id'] or prior['request_hash']!=fingerprint:raise RegistrationError([issue('request_id','相同编号不同内容')],'request_conflict')
            classify('idempotent_request');return dict(status='reused',event=prior,written=False)
        e=latest(root,basis['task_id'])
        if e['revision']!=basis['expected_revision']:raise RegistrationError([issue('expected_revision','任务已被其他操作更新')],'revision_conflict')
        task,problems=merge_task(e['task'],request['changes'],collect=True)
        accepts=request['accept_paths'];observations=basis['observations']
        if not isinstance(accepts,list) or any(not isinstance(x,str) for x in accepts):raise RegistrationError([issue('accept_paths','须为本次采用文件列表')])
        if not isinstance(observations,list) or any(not isinstance(r,dict) or set(r)!={'path','sha256'} for r in observations):raise RegistrationError([issue('observations','观察凭证无效')])
        ob={r['path']:r['sha256'] for r in observations}
        if accepts and (not isinstance(request['user_decision'],str) or not request['user_decision'].strip()):problems.append(issue('user_decision','没有采用手改的用户原文授权','保留候选，仅展示差异'))
        for path in accepts:
            if path not in ob:problems.append(issue(path,'未列入本次观察'))
        # All effective files must remain exactly observed, including unaccepted changes.
        previous={r['path']:r['sha256'] for name in ('sources','deliverables','process_refs','decision_refs') for r in e['task'].get(name,[]) if 'path' in r}
        # Removed/replaced originals still belong to this observation until first commit.
        effective={r['path'] for name in ('sources','deliverables','process_refs','decision_refs') for r in task.get(name,[]) if isinstance(r,dict) and 'path' in r}
        for path in previous:
            removed=path not in effective
            if path not in ob:
                if removed:problems.append(issue(path,'替换或移除原件未列入本次观察'))
                continue
            try:
                live=sha(reference_path(root,path))
                if live!=ob[path]:problems.append(issue(path,'预览后文件又改变'))
                if removed and live!=previous[path] and (not isinstance(request['user_decision'],str) or not request['user_decision'].strip()):problems.append(issue('user_decision','替换手改原件须非空用户原文授权','保留双方，重新观察并明确选择'))
            except (OSError,ValueError):problems.append(issue(path,'观察原件缺失、不可读或不安全'))
        for name in ('sources','deliverables','process_refs','decision_refs'):
            values=task.get(name,[])
            if not isinstance(values,list):problems.append(issue(name,'必须为列表'));continue
            for ref in values:
                if not isinstance(ref,dict):problems.append(issue(name,'引用必须为对象'));continue
                if 'path' not in ref:continue
                path=ref['path']
                try:
                    p=reference_path(root,path);live=sha(p)
                    if path in ob and live!=ob[path]:problems.append(issue(path,'预览后文件又改变'))
                    elif path in previous and live!=previous[path] and path not in accepts:problems.append(issue(path,'本次未授权采用此改变'))
                    elif path not in previous and path not in ob:problems.append(issue(path,'新文件尚未观察；先 preview --extra-path'))
                except (OSError,ValueError):problems.append(issue(path,'文件缺失、不可读或不安全'))
        # Validate each field independently to return all safely decidable errors.
        from standalone_store import task_value
        for k in request['changes']:
            if k not in FIELDS|OPTIONAL_FIELDS:continue
            trial=copy.deepcopy(task if k=='artifact_versions' else e['task']);trial[k]=task.get(k)
            if k!='artifact_versions':trial.pop('artifact_versions',None)
            for name in ('sources','deliverables','process_refs','historical_refs','decision_refs'):
                if isinstance(trial.get(name),list):trial[name]=[{a:b for a,b in r.items() if a!='sha256'} if isinstance(r,dict) else r for r in trial[name]]
            try:task_value(root,trial,previous=e['task'],accept_changed_references=ob if accepts else False)
            except (ValueError,TypeError,KeyError):problems.append(issue(k,'字段值不符合任务规则','按原字段格式提供；空值只用于允许为空字段'))
        if problems:raise RegistrationError(problems,'observation_conflict' if any(i['field'] in previous for i in problems) else 'registration_invalid')
        # Preserve unchanged fingerprints. Only these specific observed paths may be adopted.
        for name in ('sources','deliverables','process_refs','historical_refs','decision_refs'):
            if name in task:
                task[name]=[{k:v for k,v in r.items() if k!='sha256'} for r in task[name]]
        if accepts:
            task['status']='in_progress';task['completion_evidence']=''
        from standalone_tasks import save
        # Commit consistency is separate from permission to adopt changes. Keep
        # removed originals and observed candidates bound to this preview, while
        # unobserved unchanged current files remain bound to their stored version.
        commit_observations={p:ob.get(p,previous.get(p)) for p in set(previous)|effective|set(accepts)}
        event=save(root,dict(task_id=basis['task_id'],request_id=basis['request_id'],expected_revision=basis['expected_revision'],reason=request['reason'],task=task),actor,_observations={p:ob[p] for p in accepts},_commit_observations=commit_observations,_request_hash=fingerprint,_adoption={"basis":basis,"accepted_paths":accepts,"user_original":request["user_decision"]})
        unchanged=event['revision']==e['revision']
        if unchanged:append(root,'standalone_patch_noop',dict(task_id=basis['task_id'],revision=e['revision'],request_id=basis['request_id'],request_hash=fingerprint,actor=actor),unique=True)
        classify('no_change' if unchanged else 'changed_accepted' if accepts else 'partial_update')
        from standalone_tasks import RETENTION
        retention=RETENTION.get(basis['task_id']) if not unchanged else None
        return dict(status='unchanged' if unchanged else 'saved',event=event,written=not unchanged,**({'version_retention':retention} if retention else {}))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['preview','patch','compare']);p.add_argument('--workspace',type=Path,required=True);p.add_argument('--task-id');p.add_argument('--request',type=Path);p.add_argument('--actor');p.add_argument('--before');p.add_argument('--after');p.add_argument('--extra-path',action='append',default=[]);p.add_argument('--json',action='store_true');a=p.parse_args()
    try:
        if a.action=='preview':
            result=preview(a.workspace,a.task_id)
            for path in a.extra_path:
                ref={'path':path,'sha256':sha(reference_path(a.workspace,path))};result['basis']['observations'].append(ref)
            packet=result['basis'];packet['basis_sha256']=digest({k:v for k,v in packet.items() if k!='basis_sha256'})
        elif a.action=='patch':result=patch(a.workspace,json.loads(a.request.read_text()),a.actor)
        else:result=difference(a.workspace,{'path':a.before,'sha256':sha(reference_path(a.workspace,a.before))} if a.before else None,{'path':a.after,'sha256':sha(reference_path(a.workspace,a.after))})
        print(json.dumps(result,ensure_ascii=False,indent=2));return int(result.get('status')=='blocked')
    except (OSError,ValueError,TypeError,KeyError) as e:
        print(json.dumps(dict(status='blocked',written=False,reason_code=getattr(e,'reason_code','io_or_structure_error'),issues=getattr(e,'issues',[issue('request','读取或结构失败')]),errors=[str(e)] if isinstance(e,RegistrationError) else ['文件或请求不可用']),ensure_ascii=False));return 1
if __name__=='__main__':
    from yunxing_rizhi import cli
    raise SystemExit(cli(main,__file__))
