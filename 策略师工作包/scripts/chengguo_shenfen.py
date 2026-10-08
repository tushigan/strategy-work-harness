"""Explicit artifact lineage and a strict JSON management envelope."""
import json,re
from datetime import datetime
from standalone_store import text,reference_path
from phase2_store import WorkflowError,sha

def versions(root,entries,task,previous=None,stored=False):
    if not isinstance(entries,list):raise WorkflowError('artifact_versions 须为列表')
    outputs={r['path']:r['sha256'] for r in task['deliverables'] if 'path' in r};seen=set();paths=set();result=[]
    old={e['artifact_id']:e for e in (previous or {}).get('artifact_versions',[])}
    for e in entries:
        keys={'artifact_id','path','previous','dependencies'}
        if not isinstance(e,dict) or set(e) not in (keys,keys|{'sha256'}):raise WorkflowError('成果身份字段须为artifact_id/path/previous/dependencies，sha256由程序生成')
        text(e['artifact_id'],'成果身份');reference_path(root,e['path'],stored=stored)
        if e['artifact_id'] in seen or e['path'] in paths or e['path'] not in outputs:raise WorkflowError('成果身份/路径重复或不属于当前交付物')
        seen.add(e['artifact_id']);paths.add(e['path'])
        if not isinstance(e['dependencies'],list) or len(set(e['dependencies']))!=len(e['dependencies']):raise WorkflowError('成果依赖须为去重路径列表')
        effective={r['path'] for name in ('sources','decision_refs','process_refs') for r in task.get(name,[]) if 'path' in r}
        if not set(e['dependencies'])<=effective:raise WorkflowError('依赖须指向登记的有效依据/过程决定')
        prev=e['previous']
        if prev is not None and (not isinstance(prev,dict) or set(prev)!={'path','sha256'}):raise WorkflowError('前版须为确切path/sha256')
        if stored:
            if e.get('sha256')!=outputs[e['path']]:raise WorkflowError('已存成果身份与文件指纹冲突')
        else:
            prior=old.get(e['artifact_id'])
            if prev is not None and (not prior or prev not in ({'path':prior['path'],'sha256':prior['sha256']},prior['previous'])):raise WorkflowError('前版必须与同一显式成果身份已登记版本对应')
            if prior and (prior['path']!=e['path'] or prior['sha256']!=outputs[e['path']]) and prev!={'path':prior['path'],'sha256':prior['sha256']}:raise WorkflowError('换版须声明同一身份的确切前版')
        result.append({**e,'sha256':outputs[e['path']]})
    return result

def semantic(raw,artifact_id):
    data=json.loads(raw)
    if not isinstance(data,dict) or set(data)!={'harness_artifact','content'}:raise WorkflowError('不明管理字段结构，扩大检查')
    h=data['harness_artifact']
    if not isinstance(h,dict) or set(h)!={'schema_version','artifact_id','management'} or h['schema_version']!=1 or h['artifact_id']!=artifact_id:raise WorkflowError('管理字段身份未绑定，扩大检查')
    m=h['management']
    if not isinstance(m,dict) or set(m)!={'version_label','generated_at'} or not isinstance(m['version_label'],str) or not re.fullmatch(r'v[0-9]+(?:\.[0-9]+)*(?:-r[0-9]+)?',m['version_label']):raise WorkflowError('未知或伪标管理字段，扩大检查')
    if not isinstance(m['generated_at'],str):raise WorkflowError('管理生成时间无效')
    d=datetime.fromisoformat(m['generated_at'])
    if d.tzinfo is None:raise WorkflowError('管理生成时间须含时区')
    return json.dumps(data['content'],ensure_ascii=False,sort_keys=True).encode()

def correspondence(root,base,meta):
    from standalone_store import history
    old_products={(f['original']['path'],f['original']['sha256']) for f in base['files'] if f['kind']=='product'}
    previous=[e['task'] for e in history(root)[0] if e['task_id']==meta['task_id'] and {(r['path'],r['sha256']) for r in e['task']['deliverables'] if 'path' in r}==old_products]
    if not previous:return {}
    old={e['artifact_id']:e for e in previous[-1].get('artifact_versions',[])}
    mapping={}
    for e in meta['task'].get('artifact_versions',[]):
        if e['artifact_id'] in old:
            b=old[e['artifact_id']]
            # Follow only the registered same-id chain back to the full baseline.
            chain=[x for x in history(root)[0] if x['task_id']==meta['task_id']]
            current=b
            for event in chain:
                item=next((x for x in event['task'].get('artifact_versions',[]) if x['artifact_id']==e['artifact_id']),None)
                if item is None:continue
                if item['sha256']==current['sha256'] and item['path']==current['path']:continue
                if item['previous']=={'path':current['path'],'sha256':current['sha256']}:current=item
            if current['path']==e['path'] and current['sha256']==e['sha256']:mapping[e['path']]=b['path']
    return mapping
