"""Current dependencies use registered versions/fingerprints; history is never rewritten."""
import json
from pathlib import Path
from phase2_store import local
from task_validation import read_check


def paths(value):
    result=set()
    if isinstance(value,dict):
        for key,v in value.items():
            if (key in {'path','snapshot_path','markdown_path','markdown_snapshot_path','file'} or key.endswith('_path')) and isinstance(v,str):
                result.add(v)
            result.update(paths(v))
    elif isinstance(value,list):
        for v in value:result.update(paths(v))
    return result


def file_references(value):
    """No pathname-only equality, and no process/history fields masquerading as sources."""
    found=set()
    if isinstance(value,dict):
        if isinstance(value.get('path'),str) and isinstance(value.get('sha256'),str):
            found.add((value['path'],value['sha256']))
        for key,child in value.items():
            if key not in {'historical_refs','base_review','changed_scope','revision'}:
                found.update(file_references(child))
    elif isinstance(value,list):
        for child in value:found.update(file_references(child))
    return found


@read_check
def current_references(root,exclude_task=None):
    from phase6_targets import catalog,identity
    from standalone_store import history
    items=catalog(root);result=set();kept={}
    events,_=history(root);tasks={e['task_id']:e['task'] for e in events}
    for marker,item in items.items():
        key=item['target']['key']
        if item['target']['space']=='standalone':
            if key==exclude_task or tasks[key]['status'] in {'cancelled','archived'}:continue
            result.update(file_references(tasks[key].get('process_refs',[])))
        kept[marker]=item
    # An explicit version dependency can keep a retired task live, only if it matches.
    pending=list(kept.values())
    while pending:
        item=pending.pop();result.update(file_references(item['files']))
        for dep in item['dependencies']:
            marker=identity(dep);upstream=items.get(marker)
            if upstream and upstream['target']==dep and marker not in kept:
                kept[marker]=upstream;pending.append(upstream)
    targets=[item['target'] for item in kept.values()]
    records=Path(root)/'project/records'
    for name in ('phase5-events.jsonl','phase6-events.jsonl','phase3-reviews.jsonl','phase4-reviews.jsonl','phase3-confirmations.jsonl','reviews.jsonl','confirmations.jsonl'):
        p=records/name
        if not p.exists():continue
        latest={}
        for event in [json.loads(line) for line in p.read_text().splitlines() if line.strip()]:
            target=event.get('target')
            if isinstance(target,dict) and 'kind' in target and 'space' not in target:
                target={'space':'phase2','key':target['kind'],**target}
            if target in targets:
                latest[(json.dumps(target,sort_keys=True),event.get('event',name))]=event
        for event in latest.values():
            result.update(file_references(event))
            evidence=event.get('evidence',{})
            if isinstance(evidence,dict) and isinstance(evidence.get('path'),str):
                p=local(root,evidence['path'])
                if p.is_file() and p.suffix=='.json':result.update(file_references(json.loads(p.read_text())))
    for name,key in (('project-memory.jsonl','memory_id'),('task-receipts.jsonl','record_id')):
        p=records/name
        if not p.exists():continue
        entries=[json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        for e in {x[key]:x for x in entries}.values():
            if e.get('status')!='retired' and e.get('data',{}).get('status')!='retired':
                result.update(file_references(e))
    p=Path(root)/'project/state.json'
    if p.exists():result.update(file_references(json.loads(p.read_text()).get('references',[])))
    return result


def referenced_paths(root,exclude_task=None):
    return {p for p,_ in current_references(root,exclude_task)}


@read_check
def dispatch_issues(root,current_only=False):
    from agent_dispatch import read_log,replay,ACTIVE
    from dispatch_files import required_refs,reference_issues,local as dispatch_local
    states=replay(read_log(root));dependencies=current_references(root)
    current,historical=[],[];unchecked=0;archived_count=0
    from guidang import archived_index
    archived=archived_index(root)
    for item in states.values():
        refs=required_refs(item)
        def matches(value):
            return isinstance(value,dict) and (value.get('path'),value.get('sha256')) in dependencies
        live=[(label,ref) for label,ref in refs if matches(ref)]
        if item.get('status') in ACTIVE:
            live=refs
        elif any(label=='回传候选' for label,ref in live):
            # Only an adopted candidate of the exact registered version retains
            # its calling/controller proof. Shared inputs do not imply adoption.
            # Inline originals are checked above by exact current references;
            # do not reactivate other candidates/versions in an old readback.
            live += [(label,ref) for label,ref in refs if label in {'调用凭证','返回调用凭证','回读凭证','接手证据'} and (label,ref) not in live]
        retired=[pair for pair in refs if pair not in live]
        prefix=f"分派 {item['dispatch_id']}："
        current.extend(prefix+x for x in reference_issues(root,live))
        if not current_only:
            historical.extend(prefix+x for x in reference_issues(root,retired))
        else:
            # Daily recovery never hashes unrelated old media. Missing references and
            # known version replacements are cheap to explain; other history is deferred.
            for label,ref in retired:
                if not isinstance(ref,dict) or not isinstance(ref.get('path'),str):
                    historical.append(prefix+label+' 历史凭证引用不完整');continue
                p=dispatch_local(root,ref['path'])
                if not p.is_file() and (ref['path'],ref.get('sha256')) in archived:archived_count+=1
                elif not p.is_file():historical.append(prefix+label+' '+ref['path']+'：纯历史文件缺失')
                elif any(path==ref['path'] and digest!=ref.get('sha256') for path,digest in dependencies):
                    historical.append(prefix+label+' '+ref['path']+'：旧版指纹已被正常登记的当前版本替代')
                else:unchecked+=1
    return {'current_errors':current,'historical_issues':historical,'historical_unchecked_refs':unchecked,'historical_archived':archived_count,
            'cancelled_count':sum(x.get('status')=='cancelled' for x in states.values())}
