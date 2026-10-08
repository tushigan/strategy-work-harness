"""Immutable full-review baselines; file-level reuse with conservative scope expansion."""
import argparse
import subprocess
import sys
from yunxing_chayi import TEXT_TYPES, MAX_BYTES, MAX_LINES
import hashlib
import json
import re
import uuid
from pathlib import Path
from phase2_store import WorkflowError
from phase5_common import atomic_bytes, json_bytes, local, read_json, sha, test_mode

EXTRA_FIELDS={'report_schema_version','review_mode','review_purpose','base_review','changed_scope','reused_coverage','visual_evidence','impact_assessment'}
PASS={'passed','passed_with_yellow'}


def schema(report):
    if 'report_schema_version' not in report:
        if (EXTRA_FIELDS-{'visual_evidence','impact_assessment'}).intersection(report):raise WorkflowError('增量字段须明确 report_schema_version=2')
        return 'full'
    if type(report['report_schema_version']) is not int or report['report_schema_version']!=2 or not EXTRA_FIELDS.issubset(report):
        raise WorkflowError('新报告须有完整的增量协议字段，版本为2')
    if report['review_mode'] not in {'full','incremental'} or report['review_purpose'] not in {'stage','revision','final'}:
        raise WorkflowError('检核模式或用途无效')
    assessment=report['impact_assessment']
    if (not isinstance(assessment,dict) or set(assessment)!={'change_kind','reason'}
            or assessment['change_kind'] not in {'local_text','strategy','unknown','source','numeric','visual','full'}
            or not isinstance(assessment['reason'],str) or not assessment['reason'].strip()):
        raise WorkflowError('独立审稿者须说明实质影响；判断不明不能缩小范围')
    if not isinstance(report['visual_evidence'],list):raise WorkflowError('visual_evidence 必须为列表')
    if report['review_mode']=='full':
        if report['base_review'] is not None or report['changed_scope']!=[] or report['reused_coverage']!=[]:
            raise WorkflowError('完整审稿不能伪写继承项')
    return report['review_mode']


def context(root,target,meta):
    from project_memory_store import inspect
    memory=inspect(root)
    if memory['errors']:raise WorkflowError('项目记忆失效，不能建立或复用基线')
    if target['space']=='standalone':
        task=meta['task'];business={k:task[k] for k in ('project_label','request_text','goal')}
        product={x['path'] for x in task['deliverables'] if 'path' in x}
        if task.get('artifact_versions'):
            business['artifact_dependencies']={a['artifact_id']:a['dependencies'] for a in task['artifact_versions']}
    elif target['space']=='phase3':
        business={'kind':meta['kind'],'task_id':meta['task_id'],'dependencies':meta.get('dependencies',[])}
        product={meta[x] for x in ('path','snapshot_path','markdown_path','markdown_snapshot')}
    elif target['space']=='html':
        business={'kind':'brand-house-html','task_id':target['key'],'document_id':meta['document_id']}
        product={meta['path']}
    else:
        business={'kind':meta['kind'],'task_id':meta['task_id'],'dependencies':meta.get('dependencies',[])}
        product={x['path'] for x in meta['current_files']+meta['snapshot_files']}
    return {'business':business,'memory_digest':memory['digest']},product


def excluded_instances(root,target,authors):
    from agent_dispatch import replay,read_log
    excluded=set(authors)
    # Diagnostic exclusions recorded by dispatch remain exclusions across revisions.
    for item in replay(read_log(root)).values():
        if item.get('task_id')==target['key'] or item.get('task_id')==target['key'].split('::')[0]:
            excluded.update(item.get('excluded_instances',[]))
    return excluded


def capture(root,target,report,evidence,expected,meta,authors):
    if schema(report)!='full' or report['status'] not in PASS:return None
    if target['space']=='standalone':
        from standalone_visual import validate
        try:validate(root,meta,report,target=target)
        except WorkflowError:return None  # Legacy report remains readable but is not a visual baseline.
    ctx,product=context(root,target,meta)
    # A group retry may resume after capture but before native registration.
    # Its validated, immutable aggregate always owns the same snapshot folder.
    group=report.get('review_group')
    if group:
        from shencha_zu import verify_aggregate
        verify_aggregate(root,report)
    folder=f"project/records/review-baselines/{group['group_id'] if group else uuid.uuid4().hex}"
    existing=local(root,folder+'/baseline.json')
    if group and existing.exists():
        data=read_json(existing)
        if (data.get('target')!=target or data.get('report')!=evidence
                or data.get('context')!=ctx or data.get('scope')!=report['scope']
                or data.get('simulation')!=report['simulation']
                or data.get('reviewer_instance')!=report['reviewer_instance']
                or data.get('authors')!=sorted(excluded_instances(root,target,authors))
                or [x['original'] for x in data['files']]!=expected):
            raise WorkflowError('已有组基线与重试内容冲突')
        from phase6_events import valid_file
        if any(not valid_file(root,x['snapshot']) or x['snapshot']['sha256']!=x['original']['sha256'] for x in data['files']):
            raise WorkflowError('已有组基线快照失效，不能覆盖或重复生成')
        return {'path':folder+'/baseline.json','sha256':sha(existing)}
    files=[]
    for ref in expected:
        path=local(root,ref['path']);raw=path.read_bytes()
        if sha(path)!=ref['sha256']:raise WorkflowError('保存基线期间来源发生变化')
        # U16：快照按内容指纹存一份，多次检核共用；从不覆盖，已有同名快照必须字节一致。
        snapshot=f"project/records/review-baselines/blobs/{ref['sha256']}{path.suffix.lower()}"
        stored_path=local(root,snapshot)
        if stored_path.exists():
            if stored_path.read_bytes()!=raw:raise WorkflowError('已有内容快照与原件不一致，不能覆盖；请人工核对')
        else:atomic_bytes(stored_path,raw)
        stored={'path':snapshot,'sha256':sha(local(root,snapshot))}
        if stored['sha256']!=ref['sha256']:raise WorkflowError('基线快照与已读原件不一致')
        files.append({'original':ref,'snapshot':stored,'kind':'product' if ref['path'] in product else 'source'})
    data={'schema_version':1,'target':target,'report':evidence,'simulation':report['simulation'],
          'reviewer_instance':report['reviewer_instance'],'authors':sorted(excluded_instances(root,target,authors)),
          'context':ctx,'scope':report['scope'],'files':files}
    name=folder+'/baseline.json';atomic_bytes(local(root,name),json_bytes(data))
    return {'path':name,'sha256':sha(local(root,name))}


def event_stream(root,target):
    if target['space']=='phase3':
        from phase3_events import events
        records=[r for p in (Path(root)/'project/records').glob('phase3-*.jsonl') for r in events(root,p.stem)]
        return sorted(records,key=lambda x:x['created_at']), 'phase3-reviews'
    if target['space']=='html':
        from phase3_events import events
        records=[r for p in (Path(root)/'project/records').glob('phase4-*.jsonl') for r in events(root,p.stem)]
        return sorted(records,key=lambda x:x['created_at']), 'phase4-reviews'
    if target['space']=='standalone':
        from phase6_events import events
        return events(root), 'standalone_review'
    from phase5_common import events
    return events(root), 'review'


def _records(root,target):
    records,name=event_stream(root,target)
    return [x for x in records if x.get('event',x.get('record_type'))==name]


def effective_base(root,target,record):
    """Replay later decisions; a return permanently retires that coverage.

    A fresh full independent review may establish a NEW baseline after correction.
    Neither a later pass nor an author's resolution flag revives an older one.
    """
    records,review_event=event_stream(root,target)
    position=next((i for i,r in enumerate(records) if r.get('record_id')==record['record_id']),None)
    if position is None:raise WorkflowError('完整审稿记录已缺失')
    for later in records[position+1:]:
        dest=later.get('target',{})
        same=dest.get('space')==target['space'] and dest.get('key')==target['key']
        linked=later.get('review_id')==record['record_id'] or later.get('supersedes')==record['record_id']
        status=later.get('status')
        if (same or linked) and (status in {'returned','insufficient_evidence','revoked','withdrawn','superseded','rejected'}
                or any(f.get('level')=='red' for f in later.get('findings',[]))
                or later.get('uncovered')):
            raise WorkflowError('旧基线已被后续退回、撤销或未解决意见否定；先修复并完整独立重审，建立新基线')
        if same and later.get('event',later.get('record_type'))==review_event and later.get('status') in PASS and later.get('review_mode','full')=='full':
            raise WorkflowError('旧基线已由新的完整审稿替代；使用最新有效完整基线')


def load_base(root,target,reference,simulation,reviewer,authors):
    if not isinstance(reference,dict) or set(reference)!={'record_id','evidence','baseline'}:
        raise WorkflowError('须绑定完整审稿的记录号、报告原件指纹与基线指纹')
    record=next((x for x in _records(root,target) if x['record_id']==reference['record_id']),None)
    if not record or any(record.get(k)!=reference[k] for k in ('evidence','baseline')):
        raise WorkflowError('基线不是已登记的完整独立审稿')
    effective_base(root,target,record)
    from phase6_events import valid_file
    if not valid_file(root,reference['evidence']) or not valid_file(root,reference['baseline']):
        raise WorkflowError('基线报告或清单被修改/缺失；须完整重审')
    report=read_json(local(root,reference['evidence']['path']))
    if report.get('review_group'):
        from shencha_zu import verify_aggregate
        verify_aggregate(root,report,current=False)
    data=read_json(local(root,reference['baseline']['path']))
    old=report.get('target',{})
    if (old.get('space')!=target['space'] or old.get('key')!=target['key']
            or (target['space']!='html' and (type(old.get('version')) is not int or old['version']>target['version']))
            or data.get('target')!=old or record.get('target')!=old):
        raise WorkflowError('基线任务或版本不匹配')
    if schema(report)!='full' or report.get('status') not in PASS or report.get('uncovered') or any(
            x.get('level')=='red' for x in report.get('findings',[])):
        raise WorkflowError('基线必须为完整通过报告，不能形成增量继承链')
    if any(record.get(k)!=v for k,v in report.items()) or data.get('report')!=reference['evidence']:
        raise WorkflowError('基线记录与报告原件冲突')
    if type(simulation) is not bool or simulation!=report.get('simulation') or data.get('simulation')!=simulation or (simulation and not test_mode(root)):
        raise WorkflowError('基线与当前模拟状态不一致，不能用于真实项目')
    excluded=excluded_instances(root,target,authors)|set(data.get('authors',[]))
    if (reviewer in excluded or report.get('reviewer_instance') in excluded
            or data.get('reviewer_instance')!=report.get('reviewer_instance')):
        raise WorkflowError('作者/诊断者或当前作者不能充当基线或增量独立审稿者')
    if report.get('review_group'):
        from shencha_zu import load
        _,group_plan=load(root,report['review_group']['group_id'])
        from agent_dispatch import current
        if any(current(root,part['dispatch_id']).get('instance') in excluded for part in report['review_group']['parts']):
            raise WorkflowError('基线组审查成员后来参与创作，不再独立')
    original={(r['path'],r['sha256']) for r in report['checked_sources']}
    stored={(x['original']['path'],x['original']['sha256']) for x in data['files']}
    if original!=stored:raise WorkflowError('基线原文覆盖集合不完整')
    for entry in data['files']:
        if not valid_file(root,entry['snapshot']) or entry['snapshot']['sha256']!=entry['original']['sha256']:
            raise WorkflowError('基线原文快照失效；须完整重审')
    return data


def _signals(raw):
    # Numeric/unit changes are machine-detectable. Semantic strategy changes are the
    # independent reviewer's judgement; unknown dependencies expand instead of guessing.
    text=raw.decode('utf-8',errors='replace')
    return re.findall(r'\d+(?:[.,]\d+)*\s*(?:%|％|万元|亿元|元|吨|克|公斤|kg|g|ml|mL|万|亿|人|年|月|日)?',text)


def bounded_difference(before_path, after_path, before_sha=None, after_sha=None):
    omitted = None
    paths = [p for p in (before_path, after_path) if p is not None]
    if after_path.suffix.lower() not in TEXT_TYPES: omitted = 'diff_type'
    elif any(p.stat().st_size > MAX_BYTES for p in paths): omitted = 'diff_size'
    if omitted and after_sha is not None:
        value=json.dumps({'before_sha256':before_sha,'after_sha256':after_sha},sort_keys=True).encode()
        return {'diff_sha256':hashlib.sha256(value).hexdigest(),'diff_preview':[],'preview_omitted':omitted}
    h = hashlib.sha256()
    for index, path in enumerate((before_path, after_path)):
        if index: h.update(b'\0')
        if path is not None:
            with path.open('rb') as f:
                for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
    if omitted: return {'diff_sha256': h.hexdigest(), 'diff_preview': [], 'preview_omitted': omitted}
    worker=Path(__file__).with_name('yunxing_chayi.py')
    if not worker.is_file():
        worker=next(parent.parent/'scripts/yunxing_chayi.py' for parent in after_path.parents if parent.name=='project')
    try:
        proc = subprocess.run([sys.executable, str(worker),
            str(before_path) if before_path else '-', str(after_path)],capture_output=True,timeout=1)
        data=json.loads(proc.stdout) if proc.returncode==0 else {'diff_preview':[], 'preview_omitted':'diff_type'}
    except subprocess.TimeoutExpired: data={'diff_preview':[], 'preview_omitted':'diff_timeout'}
    return {'diff_sha256': h.hexdigest(), **data}


def snapshot_correspondence(root,target,base,meta):
    """Map only registered version snapshots with a stable business identity."""
    if target['space']=='standalone':
        from chengguo_shenfen import correspondence
        return correspondence(root,base,meta)
    if target['space']=='phase3':
        from phase3_io import registry
        from phase3_store import ref
        history=registry(root)['artifacts'].get(target['key'],[])
        previous=[m for m in history if ref(m)==base['target']]
        if len(previous)!=1:return {}
        old_meta=previous[0]
        if any(old_meta.get(k)!=meta.get(k) for k in ('key','kind','task_id')):return {}
        return {meta[field]:old_meta[field] for field in ('snapshot_path','markdown_snapshot')}
    if target['space']=='phase5':
        from phase5_common import registry,ref
        previous=[m for m in registry(root)['artifacts'].get(target['key'],[]) if ref(m)==base['target']]
        if len(previous)!=1:return {}
        def identities(m):
            pointers=m.get('current_files',[]);snapshots=m.get('snapshot_files',[])
            if len(pointers)!=len(snapshots) or len({p['path'] for p in pointers})!=len(pointers):return {}
            return {p['path']:s['path'] for p,s in zip(pointers,snapshots) if p['sha256']==s['sha256']}
        old=identities(previous[0]);new=identities(meta)
        return {s:old[p] for p,s in new.items() if p in old}
    return {}


def independent_dependency_scope(target,base,meta,expected,product,mapped,changes,ctx):
    """Only complete, stable, registered graphs may narrow source propagation."""
    if target['space']!='standalone' or ctx!=base['context']:return None
    entries=meta['task'].get('artifact_versions',[])
    old_products={f['original']['path'] for f in base['files'] if f['kind']=='product'}
    if not entries or {e['path'] for e in entries}!=product:return None
    if any(not e['dependencies'] or mapped.get(e['path']) not in old_products for e in entries):return None
    source_paths={r['path'] for r in expected}-product
    declared={p for e in entries for p in e['dependencies']}
    if not source_paths<=declared:return None
    changed_sources={c['path'] for c in changes if c['path'] not in product}
    if not changed_sources:return None
    required={c['path'] for c in changes}
    affected=[e for e in entries if set(e['dependencies'])&changed_sources or e['path'] in required]
    for entry in affected:required.update([entry['path'],*entry['dependencies']])
    if not required<={r['path'] for r in expected}:return None
    return required


def plan(root,target,base_review,expected,meta,authors,simulation,reviewer,change_kind='local_text'):
    base=load_base(root,target,base_review,simulation,reviewer,authors)
    old={x['original']['path']:x for x in base['files']};ctx,product=context(root,target,meta)
    mapped=snapshot_correspondence(root,target,base,meta)
    mapped_old=set()
    changes=[];force=change_kind in {'strategy','unknown','source','numeric'};reasons=['independent_impact_expansion'] if force else []
    for ref in expected:
        previous=old.get(ref['path'])
        if previous is None and ref['path'] in product:
            previous=old.get(mapped.get(ref['path']))
            if previous and previous['kind']=='product':mapped_old.add(previous['original']['path'])
            else:
                previous=None;force=True;reasons.append('product_identity_unknown')
        if previous and previous['original']==ref:continue
        before_path=local(root,previous['snapshot']['path']) if previous else None
        after_path=local(root,ref['path'])
        difference=bounded_difference(before_path, after_path,previous['original']['sha256'] if previous else None,ref['sha256'])
        limited=bool(difference.get('preview_omitted'))
        before=before_path.read_bytes() if before_path and not limited else b''
        after=after_path.read_bytes() if not limited else b''
        if limited: force=True;reasons.append('preview_unavailable_expand_scope')
        if sha(local(root,ref['path']))!=ref['sha256']:raise WorkflowError('当前文件指纹已改变')
        reason='changed' if previous else 'new'
        changes.append({'path':ref['path'],'baseline':previous['original'] if previous else None,
                        'current':ref,'change':reason,
                        **difference})
        if ref['path'] not in product:
            force=True;reasons.append('source_or_upstream_changed')
        numeric_before, numeric_after = before, after
        if target['space']=='standalone' and previous and not limited and Path(ref['path']).suffix=='.json':
            entry=next((a for a in meta['task'].get('artifact_versions',[]) if a['path']==ref['path']),None)
            if entry:
                from chengguo_shenfen import semantic
                try:
                    numeric_before,numeric_after=semantic(before,entry['artifact_id']),semantic(after,entry['artifact_id'])
                    if numeric_before==numeric_after:reasons.append('management_only')
                except (ValueError,TypeError,KeyError):
                    force=True;reasons.append('management_schema_unknown')
        if target['space']=='html' and previous and not limited:
            # Fonts, CSS sizes and revision metadata are display numbers. Business
            # numbers live in the validated current field text, not every HTML digit.
            from brand_house_html import extract
            from brand_house_data import content
            numeric_before=json_bytes(content(extract(before)['history'][-1]['snapshot']))
            numeric_after=json_bytes(content(extract(after)['history'][-1]['snapshot']))
        if target['space'] in {'phase3','phase5'} and not limited:
            # Native JSON carries version numbers and revision bookkeeping; they
            # are not business numbers. Compare only the generated semantic payload.
            if Path(ref['path']).suffix=='.json' and previous:
                def semantic(raw):
                    data=json.loads(raw)
                    return json_bytes(data.get('sections',data.get('pages',data)))
                numeric_before,numeric_after=semantic(before),semantic(after)
            else:
                numeric_before=numeric_after=b''
        if previous and _signals(numeric_before)!=_signals(numeric_after):
            force=True;reasons.append('number_or_unit_changed')
        if Path(ref['path']).suffix.lower() in {'.css','.html','.htm','.woff','.woff2','.ttf'}:
            reasons.append('visual_scope_all_pages')
    removed=sorted(set(old)-{r['path'] for r in expected})
    if removed:
        # Formal snapshots change pathname every version. These are new, hence actually
        # checked; unknown removed sources are conservatively re-read in current context.
        if any(old[p]['kind']=='source' for p in removed):force=True;reasons.append('source_removed_or_moved')
        if any(old[p]['kind']=='product' and p not in mapped_old for p in removed):
            force=True;reasons.append('product_removed_or_moved')
    if ctx!=base['context']:force=True;reasons.append('business_or_memory_changed')
    required=set(r['path'] for r in expected) if force else {x['path'] for x in changes}
    # Every other expansion (numeric, unknown, missing, global, graph/context drift)
    # remains conservative. This is a proven local source edge, not semantic inference.
    if force and not removed and set(reasons)<={'source_or_upstream_changed','management_only'}:
        narrowed=independent_dependency_scope(target,base,meta,expected,product,mapped,changes,ctx)
        if narrowed is not None:
            required=narrowed;reasons.append('explicit_independent_dependency_scope')

    recheck=[r for r in expected if r['path'] in required]
    reused=[r for r in expected if r['path'] not in required]
    return {'base_review':base_review,'changed_scope':{'files':changes,'removed_paths':removed,
                'recheck_sources':recheck,'reason_codes':sorted(set(reasons)),
                'coverage_granularity':'file; changed HTML/PDF has no inherited page visuals'},
            'reused_coverage':reused,'checked_sources_required':recheck,
            'notice':'基线到当前的累计差异；继承不是本轮阅读。语义影响不明扩大复核；最终核对完整覆盖链。'}


def coverage(root,target,report,expected,meta,authors):
    if report.get('review_group'):
        from shencha_zu import verify_aggregate
        verify_aggregate(root,report)
    if schema(report)=='full':return []
    actual=plan(root,target,report['base_review'],expected,meta,authors,report['simulation'],report['reviewer_instance'],report['impact_assessment']['change_kind'])
    if report['changed_scope']!=actual['changed_scope']:
        raise WorkflowError('报告改动范围与程序实际累计差异不一致（含新增/变动文件）')
    inherited=report['reused_coverage'];checked=report['checked_sources']
    if not isinstance(inherited,list):raise WorkflowError('reused_coverage 必须为列表')
    if any(not isinstance(x,dict) or set(x)!={'path','sha256'} for x in inherited):
        raise WorkflowError('继承覆盖须使用程序给出的精确路径与指纹')
    allowed={(x['path'],x['sha256']) for x in actual['reused_coverage']}
    reuse={(x['path'],x['sha256']) for x in inherited};seen={(x['path'],x['sha256']) for x in checked}
    if not reuse<=allowed or reuse&seen:
        raise WorkflowError('继承项无有效基线，或冒充本轮重新阅读')
    required={(x['path'],x['sha256']) for x in actual['checked_sources_required']}
    if not required<=seen:raise WorkflowError('差异、关联来源或必要原文未实际覆盖')
    # Unresolved yellow findings stay visible when any coverage is inherited. Clearing
    # them requires actual complete rechecking, never a self-filled resolved flag.
    if inherited:
        records=_records(root,target)
        start=next(i for i,r in enumerate(records) if r['record_id']==report['base_review']['record_id'])
        pending=[f for r in records[start:] if r.get('target',{}).get('key')==target['key']
                 for f in r.get('findings',[]) if f.get('level')=='yellow']
        if any(f not in report['findings'] for f in pending):
            raise WorkflowError('继承覆盖不能丢弃基线或后续未解决黄灯意见；须保留意见或完整实际重审')
    return inherited


def current_input(root,target):
    if target['space']=='standalone':
        from standalone_review import _required
        meta,item,expected=_required(root,target);return meta,item['authors'],expected
    if target['space']=='phase3':
        from phase3_store import current,ref
        from phase3_io import registry
        from phase3_review import required_sources
        meta=current(root,target['key'])
        if ref(meta)!=target:raise WorkflowError('产物不是当前版本')
        return meta,[x['author_instance'] for x in registry(root)['artifacts'][target['key']]],required_sources(root,meta)
    if target['space']=='html':
        from brand_house_store import live,target as html_target
        from brand_house_review import required_sources
        from phase3_io import registry
        record,data,meta=live(root,target['key'])
        if html_target(record)!=target:raise WorkflowError('HTML不是当前版本')
        authors=set(record['author_instances'])|{x['author_instance'] for x in registry(root)['artifacts'][meta['key']]}
        return record,sorted(authors),required_sources(root,record,data,meta)
    from phase5_common import current,required_sources,registry,ref
    meta=current(root,target['key'])
    if ref(meta)!=target:raise WorkflowError('产物不是当前版本')
    return meta,[x['author_instance'] for x in registry(root)['artifacts'][target['key']]],required_sources(root,meta)


def default_plan(root,target,reviewer,simulation=False,change_kind='unknown',purpose='revision'):
    """No model dispatch for an already valid current review; cumulative base otherwise."""
    meta,authors,expected=current_input(root,target)
    if not isinstance(reviewer,str) or not reviewer.strip() or reviewer in excluded_instances(root,target,authors):
        raise WorkflowError('审查者必须独立于历版作者与诊断者')
    records=[x for x in _records(root,target) if x.get('target',{}).get('key')==target['key']]
    baseline=None;failures=[]
    for candidate in reversed(records):
        if not candidate.get('baseline'):continue
        reference={k:candidate[k] for k in ('record_id','evidence','baseline')}
        try:
            load_base(root,target,reference,simulation,reviewer,authors)
            baseline=reference;break
        except (OSError,ValueError,KeyError,TypeError) as exc:failures.append(str(exc))
    current=[x for x in records if x.get('target')==target]
    if baseline and current:
        try:
            from review_gate import gate
            gate(root,target)
            last=current[-1]
            if last.get('simulation')!=simulation:raise WorkflowError('当前报告模拟状态不一致')
            return {'review_mode':'reuse','dispatch_required':False,'target':target,'review_purpose':purpose,
                    'current_review':{'record_id':last['record_id'],'evidence':last['evidence']},
                    'base_review':baseline,'checked_sources_required':[],'reused_coverage':expected,
                    'uncovered':[],'reason_codes':['unchanged_valid_review']}
        except (OSError,ValueError,KeyError,TypeError) as exc:failures.append(str(exc))
    if baseline:
        result=plan(root,target,baseline,expected,meta,authors,simulation,reviewer,change_kind)
        return {**result,'target':target,'review_mode':'incremental','review_purpose':purpose,
                'dispatch_required':True,'uncovered':[]}
    return {'target':target,'review_mode':'full','review_purpose':purpose,'dispatch_required':True,
            'base_review':None,'checked_sources_required':expected,'reused_coverage':[],
            'uncovered':[],'reason_codes':['baseline_unavailable'],'baseline_errors':failures}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--target',type=Path,required=True,help='当前 target JSON 原件')
    parser.add_argument('--base-record-id')
    parser.add_argument('--reviewer',required=True)
    parser.add_argument('--simulation',action='store_true')
    parser.add_argument('--purpose',choices=['stage','revision','final'],default='revision')
    parser.add_argument('--change-kind',choices=['local_text','strategy','unknown','source','numeric','visual'],default='unknown')
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args();target=read_json(args.target)
    if args.base_record_id:
        meta,authors,expected=current_input(args.workspace,target)
        record=next((r for r in _records(args.workspace,target) if r['record_id']==args.base_record_id),None)
        if not record or not record.get('baseline'):raise WorkflowError('缺有效基线；先完整复核')
        result=plan(args.workspace,target,{k:record[k] for k in ('record_id','evidence','baseline')},expected,meta,authors,args.simulation,args.reviewer,args.change_kind)
    else:result=default_plan(args.workspace,target,args.reviewer,args.simulation,args.change_kind,args.purpose)
    print(json.dumps(result,ensure_ascii=False,indent=2))
from yunxing_rizhi import observed
plan = observed("incremental_review.plan")(plan)
default_plan = observed("incremental_review.default_plan")(default_plan)

if __name__ == "__main__":
    from yunxing_rizhi import cli
    try:cli(main,__file__)
    except (OSError,ValueError,KeyError,TypeError) as exc:raise SystemExit('未完成：'+str(exc))
