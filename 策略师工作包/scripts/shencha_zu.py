"""Frozen read-only review groups; native gates remain the authority for release."""
import argparse
import copy
import json
import uuid
from pathlib import Path
from phase2_store import WorkflowError,local,read_json
from phase5_common import json_bytes,sha
from phase6_events import append,events,archive,valid_file,actor_check
from workspace_lock import serialized
from incremental_review import current_input,default_plan,excluded_instances,load_base,PASS


def load(root,group_id):
    found=[r for r in events(root,'review_group_created') if r.get('group_id')==group_id]
    if len(found)!=1 or not valid_file(root,found[0]['plan']):raise WorkflowError('审查组计划缺失或失效')
    plan=read_json(local(root,found[0]['plan']['path']))
    from yunxing_rizhi import bind
    bind(plan['target']['key'].split('::')[0])
    return found[0],plan


def frozen(root,plan):
    meta,authors,expected=current_input(root,plan['target'])
    from incremental_review import context
    ctx,_=context(root,plan['target'],meta)
    if expected!=plan['all_sources'] or ctx!=plan['context'] or sorted(excluded_instances(root,plan['target'],authors))!=plan['excluded_instances']:
        raise WorkflowError('审查组中途换版、来源/记忆或独立性变化，不能继续或整体放行')
    if plan['base_review']:
        load_base(root,plan['target'],plan['base_review'],plan['simulation'],'group-plan-check',authors)
    return meta


def create(root,target,members,actor,simulation=False,change_kind='unknown',purpose='revision'):
    actor_check(root,actor,simulation)
    with serialized(root):
        meta,authors,expected=current_input(root,target)
        excluded=excluded_instances(root,target,authors)
        if not isinstance(members,list) or len(members) not in {2,3}:raise WorkflowError('审查组通常须2—3名只读独立实例，含一名跨部分审查者')
        if any(not isinstance(m,dict) or set(m)!={'member_id','role','scope','paths'} for m in members):raise WorkflowError('成员须有member_id/role/scope/paths')
        ids=[m['member_id'] for m in members]
        if len(set(ids))!=len(ids) or any(not isinstance(x,str) or not x.strip() for x in ids):raise WorkflowError('成员编号须唯一')
        if sum(m['role']=='consistency' for m in members)!=1 or any(m['role'] not in {'part','consistency'} for m in members):raise WorkflowError('须指定唯一跨部分一致性审查者')
        delta=default_plan(root,target,'group-plan-check',simulation,change_kind,purpose)
        if not delta['dispatch_required']:return delta
        required=delta['checked_sources_required'];allowed={f['path'] for f in required}
        union=set()
        for m in members:
            if not isinstance(m['scope'],list) or not m['scope'] or any(not isinstance(x,str) or not x.strip() for x in m['scope']):raise WorkflowError('成员范围须明确')
            if not isinstance(m['paths'],list) or not m['paths'] or len(set(m['paths']))!=len(m['paths']) or not set(m['paths'])<=allowed:raise WorkflowError('成员仅可读取此次所需文件，不能夹带聊天或递归审查报告')
            union.update(m['paths'])
            if m['role']=='consistency' and set(m['paths'])!=allowed:raise WorkflowError('一致性审查者须覆盖所有本次相关部分')
        if union!=allowed:raise WorkflowError('组计划漏查必要来源')
        from incremental_review import context
        ctx,_=context(root,target,meta)
        group_id='group-'+uuid.uuid4().hex
        plan={'schema_version':1,'group_id':group_id,'target':target,'actor':actor,'simulation':simulation,
              'members':members,'all_sources':expected,'context':ctx,'excluded_instances':sorted(excluded),
              'review_mode':delta['review_mode'],'review_purpose':purpose,'base_review':delta.get('base_review'),
              'changed_scope':delta.get('changed_scope',[]),'checked_sources_required':required,
              'reused_coverage':delta['reused_coverage'],'change_kind':change_kind}
        relative='project/records/review-groups/'+group_id+'/计划.json';path=local(root,relative);path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as f:f.write(json_bytes(plan))
        return append(root,'review_group_created',{'group_id':group_id,'target':target,'actor':actor,'review_mode':plan['review_mode'],
             'reason_codes':['review_group_frozen']+(plan['changed_scope'].get('reason_codes',[]) if isinstance(plan['changed_scope'],dict) else []),
             'plan':{'path':relative,'sha256':sha(path)}})


def context_packet(root,plan,member):
    pending=[]
    if plan['base_review']:
        from incremental_review import _records
        records=_records(root,plan['target']);start=next(i for i,x in enumerate(records) if x['record_id']==plan['base_review']['record_id'])
        for record in records[start:]:
            if record.get('target',{}).get('key')==plan['target']['key']:
                for finding in record.get('findings',[]):
                    if finding.get('level')=='yellow' and finding not in pending:pending.append(finding)
    scope=plan['changed_scope']
    if isinstance(scope,dict):
        scope={**scope,'files':[x for x in scope['files'] if x['path'] in member['paths']],
               'recheck_sources':[x for x in scope['recheck_sources'] if x['path'] in member['paths']]}
    return {'target':plan['target'],'review_mode':plan['review_mode'],'base_review':plan['base_review'],
            'changed_scope':scope,'unresolved_findings':pending,
            'notice':'基线引用供证据核对，不递归审查报告；仅阅读成员分工内必要原文，不复制聊天。'}


def request(root,group_id,member_id,task_text):
    _,plan=load(root,group_id);frozen(root,plan)
    member=next((m for m in plan['members'] if m['member_id']==member_id),None)
    if not member:raise WorkflowError('未知组成员')
    return {'role':'independent-reviewer','task_id':plan['target']['key'].split('::')[0],
            'task_text':task_text,'input_files':[f for f in plan['checked_sources_required'] if f['path'] in member['paths']],
            'upstream_refs':[plan['target']],'permissions':['read_only'],
            'completion_criteria':member['scope'],'excluded_instances':plan['excluded_instances'],
            'review_group_id':group_id,'review_member_id':member_id,'review_context':context_packet(root,plan,member)}


def validate_dispatch(root,payload):
    _,plan=load(root,payload['review_group_id']);frozen(root,plan)
    member=next((m for m in plan['members'] if m['member_id']==payload['review_member_id']),None)
    if not member or payload['role']!='independent-reviewer' or payload['permissions']!=['read_only']:
        raise WorkflowError('审查组只允许计划内只读独立审查')
    if payload['task_id']!=plan['target']['key'].split('::')[0] or payload['upstream_refs']!=[plan['target']]:raise WorkflowError('组分派任务或冻结版本不一致')
    required=[f for f in plan['checked_sources_required'] if f['path'] in member['paths']]
    if payload['input_files']!=required or payload['completion_criteria']!=member['scope'] or set(payload['excluded_instances'])!=set(plan['excluded_instances']):
        raise WorkflowError('组分派范围/原文/独立性须精确对应冻结计划')
    from agent_dispatch import replay,read_log
    if any(x.get('review_group_id')==plan['group_id'] and x.get('review_member_id')==member['member_id'] and x.get('status')!='cancelled' for x in replay(read_log(root)).values()):
        raise WorkflowError('此成员已有分派，不重复分派')
    payload['review_context']=context_packet(root,plan,member)
    return plan


def assignment_check(root,item,instance,host_instance):
    _,plan=load(root,item['review_group_id']);frozen(root,plan)
    if instance in plan['excluded_instances'] or instance==plan['actor']:raise WorkflowError('作者/诊断者/登记主控不能充当组审查者')
    from agent_dispatch import replay,read_log
    if any(x.get('review_group_id')==plan['group_id'] and (x.get('instance')==instance or x.get('host_instance')==host_instance) for x in replay(read_log(root)).values()):
        raise WorkflowError('审查组成员须使用不同实际执行实例与宿主实例')


def _parts(root,plan,records):
    from agent_dispatch import current
    from dispatch_files import verify_refs
    result=[];instances=set();hosts=set()
    if len(records)!=len(plan['members']) or {r['member_id'] for r in records}!={m['member_id'] for m in plan['members']}:raise WorkflowError('审查组有漏查或重复返回')
    for entry in records:
        if not valid_file(root,entry['report']) or not valid_file(root,entry['invocation']) or not valid_file(root,entry['archive']) or entry['archive']['sha256']!=entry['report']['sha256']:raise WorkflowError('组报告或真实调用证据失效')
        report=read_json(local(root,entry['report']['path']))
        member=next(m for m in plan['members'] if m['member_id']==entry['member_id'])
        item=current(root,entry['dispatch_id'])
        # Business inputs are checked against the frozen plan (and historical
        # baselines below). Dispatch proofs must remain intact even after v2.
        verify_refs(root,{**item,'input_files':[]})
        if (item.get('review_group_id')!=plan['group_id'] or item.get('review_member_id')!=member['member_id'] or item.get('status')!='completed' or not item.get('readback_verified')
            or item.get('invocation_evidence')!=entry['invocation'] or entry['report'] not in item.get('candidates',[])):
            raise WorkflowError('组报告未由实际分派回传并回读')
        if report.get('reviewer_instance')!=item.get('instance') or report.get('target')!=plan['target'] or report.get('simulation')!=plan['simulation']:
            raise WorkflowError('报告身份/模拟状态或冻结版本不一致')
        if item['instance'] in instances or item['host_instance'] in hosts or item['instance'] in plan['excluded_instances'] or item['instance']==plan['actor']:
            raise WorkflowError('审查组身份重复或参与创作')
        instances.add(item['instance']);hosts.add(item['host_instance'])
        expected=[f for f in plan['checked_sources_required'] if f['path'] in member['paths']]
        if (item.get('input_files')!=expected or item.get('upstream_refs')!=[plan['target']]
                or item.get('permissions')!=['read_only'] or item.get('completion_criteria')!=member['scope']):
            raise WorkflowError('分派原文、版本或分工与冻结计划不一致')
        if report.get('checked_sources')!=expected or report.get('scope')!=member['scope'] or report.get('uncovered')!=[]:
            raise WorkflowError('返回报告漏查、超出分工或有缺口')
        findings=report.get('findings');assessment=report.get('assessment')
        if report.get('status') not in PASS or not isinstance(findings,list) or any(f.get('level')=='red' for f in findings):raise WorkflowError('组成员失败或有红灯，不能整体放行')
        if not isinstance(assessment,dict) or not assessment or any(not isinstance(v,str) or not v.strip() for v in assessment.values()):raise WorkflowError('成员报告缺实质判断')
        if any(not isinstance(f,dict) or f.get('level') not in {'red','yellow','green'} for f in findings):raise WorkflowError('成员意见格式无效')
        if any(f['level']=='yellow' for f in findings)!=(report['status']=='passed_with_yellow'):raise WorkflowError('成员黄灯与结论不一致')
        if member['role']=='consistency' and not assessment.get('cross_consistency','').strip():raise WorkflowError('跨部分一致性缺实质判断')
        result.append((member,report))
    # A disagreement remains blocked even if another reviewer says green.
    locations={}
    for _,report in result:
        for f in report['findings']:
            key=f.get('location');signature=(f.get('level'),f.get('action'))
            if key in locations and locations[key]!=signature:raise WorkflowError('审查组意见冲突，须保留原报告并重新核对冲突')
            locations[key]=signature
    return result


def submit(root,group_id,member_id,dispatch_id,report_path,actor):
    with serialized(root):
        _,plan=load(root,group_id);frozen(root,plan)
        if actor!=plan['actor']:raise WorkflowError('须由冻结计划主控回读登记')
        from agent_dispatch import current,verify_context
        item=current(root,dispatch_id);verify_context(root,item)
        if item.get('review_member_id')!=member_id or item.get('review_group_id')!=group_id or item.get('status')!='completed':raise WorkflowError('先完成原生回传与主控回读')
        evidence=archive(root,report_path)
        if evidence['sha256'] not in {f['sha256'] for f in item.get('candidates',[])}:raise WorkflowError('报告不是本次回传原件')
        prior=[x for x in events(root,'review_group_part') if x.get('group_id')==group_id and x.get('member_id')==member_id]
        if prior:raise WorkflowError('组成员已有返回；重新审查须建立新冻结组，保留失败')
        return append(root,'review_group_part',{'group_id':group_id,'member_id':member_id,'dispatch_id':dispatch_id,
             'report':next(f for f in item['candidates'] if f['sha256']==evidence['sha256']),
             'archive':evidence,'invocation':item['invocation_evidence'],'actor':actor,'review_mode':plan['review_mode']})


def assembled(root,plan,records,template):
    parts=_parts(root,plan,records)
    lead=next(report for member,report in parts if member['role']=='consistency')
    aggregate=copy.deepcopy(template)
    aggregate.update(target=plan['target'],reviewer_instance=lead['reviewer_instance'],simulation=plan['simulation'],
       report_schema_version=2,review_mode=plan['review_mode'],review_purpose=plan['review_purpose'],
       base_review=plan['base_review'],changed_scope=plan['changed_scope'],reused_coverage=plan['reused_coverage'],
       checked_sources=plan['checked_sources_required'],uncovered=[],scope=list(dict.fromkeys(s for member,_ in parts for s in member['scope'])),
       findings=list({json.dumps(f,sort_keys=True):f for _,rep in parts for f in rep['findings']}.values()),
       assessment={k:'\n'.join(member['member_id']+': '+str(rep['assessment'].get(k,'')) for member,rep in parts) for k in sorted(set().union(*(rep['assessment'] for _,rep in parts)))},
       impact_assessment={'change_kind':plan['change_kind'] if plan['review_mode']=='incremental' else 'full','reason':'冻结计划按累计改动及关联影响检查，实际与继承分列'})
    aggregate['status']='passed_with_yellow' if any(f['level']=='yellow' for f in aggregate['findings']) else 'passed'
    aggregate['summary']='冻结审查组分别检查并由独立成员核对跨部分一致性；详见各份原件。'
    if plan['base_review']:
        from incremental_review import _records
        prior=_records(root,plan['target']);start=next(i for i,x in enumerate(prior) if x['record_id']==plan['base_review']['record_id'])
        for record in prior[start:]:
            if record.get('target',{}).get('key')==plan['target']['key']:
                for f in record.get('findings',[]):
                    if f.get('level')=='yellow' and f not in aggregate['findings']:aggregate['findings'].append(f)
        if any(f['level']=='yellow' for f in aggregate['findings']):aggregate['status']='passed_with_yellow'
    return aggregate


def verify_aggregate(root,report,current=True):
    link=report.get('review_group')
    if not isinstance(link,dict) or set(link)!={'group_id','plan','parts'}:raise WorkflowError('组汇总须有冻结计划及分报告引用')
    record,plan=load(root,link['group_id'])
    if record['plan']!=link['plan'] or report['target']!=plan['target']:raise WorkflowError('汇总版本或计划引用冲突')
    registered=[x for x in events(root,'review_group_part') if x.get('group_id')==plan['group_id']]
    if link['parts']!=registered:raise WorkflowError('组汇总引用与登记分报告不一致')
    if current:
        frozen(root,plan)
        from agent_dispatch import current as dispatch_current,verify_context
        for part in registered:verify_context(root,dispatch_current(root,part['dispatch_id']))
    else:
        from incremental_review import _records
        bases=[r for r in _records(root,plan['target']) if r.get('target')==plan['target']
               and r.get('review_group',{}).get('group_id')==plan['group_id'] and r.get('baseline')]
        if len(bases)!=1 or not valid_file(root,bases[0]['baseline']):
            raise WorkflowError('历史组缺有效基线快照')
        baseline=read_json(local(root,bases[0]['baseline']['path']))
        if (baseline.get('target')!=plan['target'] or baseline.get('report')!=bases[0]['evidence']
                or [x['original'] for x in baseline['files']]!=plan['all_sources']
                or any(not valid_file(root,x['snapshot']) or x['snapshot']['sha256']!=x['original']['sha256'] for x in baseline['files'])):
            raise WorkflowError('历史组输入与基线快照不一致或失效')
    computed=assembled(root,plan,registered,report)
    for key in ('target','reviewer_instance','simulation','review_mode','review_purpose','base_review','changed_scope','reused_coverage','checked_sources','uncovered','scope','findings','assessment','impact_assessment','status','summary'):
        if key=='assessment' and plan['target']['space']=='standalone':continue
        if computed[key]!=report.get(key):raise WorkflowError('汇总内容与独立分报告冲突：'+key)


def merge(root,group_id,template,actor):
    with serialized(root):
        record,plan=load(root,group_id);frozen(root,plan)
        if actor!=plan['actor']:raise WorkflowError('须由计划主控合并')
        records=[x for x in events(root,'review_group_part') if x.get('group_id')==group_id]
        report=assembled(root,plan,records,template)
        report['review_group']={'group_id':group_id,'plan':record['plan'],'parts':records}
        verify_aggregate(root,report)
        # Native standalone schema has no assessment; per-member judgments remain in raw reports.
        if plan['target']['space']=='standalone':report.pop('assessment',None)
        from incremental_review import _records
        prior=[r for r in _records(root,plan['target']) if r.get('review_group',{}).get('group_id')==group_id]
        folder='project/records/review-groups/'+group_id
        intent_path=local(root,folder+'/合并请求.json')
        intent={'actor':actor,'template':template}
        if intent_path.exists():
            if read_json(intent_path)!=intent:raise WorkflowError('同组重复合并请求冲突，须保留原结果')
        elif not prior:
            from phase5_common import atomic_bytes
            atomic_bytes(intent_path,json_bytes(intent))
        if prior:
            if len(prior)!=1:raise WorkflowError('同组已有多份登记，须核对历史冲突，不能追加')
            result=prior[0]
            if (not valid_file(root,result.get('evidence')) or read_json(local(root,result['evidence']['path']))!=report
                    or any(result.get(k)!=v for k,v in report.items())):
                raise WorkflowError('同组已登记结果失效或与重试冲突')
            from review_gate import gate
            gate(root,plan['target'])
            if result.get('baseline'):
                load_base(root,plan['target'],{k:result[k] for k in ('record_id','evidence','baseline')},
                          plan['simulation'],report['reviewer_instance'],plan['excluded_instances'])
            # r1 generated a unique path; use its immutable evidence name to
            # recover that original file rather than generating a second one.
            candidates=[p for p in local(root,folder).glob('汇总*.json') if p.read_bytes()==local(root,result['evidence']['path']).read_bytes()]
            if len(candidates)!=1:raise WorkflowError('原合并报告缺失或存在冲突副本')
            path=candidates[0];relative=path.relative_to(Path(root).resolve()).as_posix()
            return {'group_id':group_id,'review_mode':plan['review_mode'],'native_review':result,
                    'report':{'path':relative,'sha256':sha(path)}}
        relative=folder+'/汇总.json';path=local(root,relative)
        if path.exists():
            if path.read_bytes()!=json_bytes(report):raise WorkflowError('中断后汇总内容冲突或失效，不能覆盖')
        else:
            from phase5_common import atomic_bytes
            atomic_bytes(path,json_bytes(report))
        if plan['target']['space']=='standalone':
            from standalone_review import record_review
            result=record_review(root,plan['target'],path)
        elif plan['target']['space']=='phase3':
            from phase3_review import record_review
            result=record_review(root,plan['target']['key'],path)
        elif plan['target']['space']=='html':
            from brand_house_review import record_review
            result=record_review(root,plan['target']['key'],path)
        else:
            from phase5_review import record_review
            result=record_review(root,plan['target']['key'],path)
        return {'group_id':group_id,'review_mode':plan['review_mode'],'native_review':result,'report':{'path':relative,'sha256':sha(path)}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['create','request','submit','merge'])
    parser.add_argument('--workspace',type=Path,required=True);parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--actor',required=True)
    args=parser.parse_args();data=read_json(args.input)
    if args.action=='create':return create(args.workspace,actor=args.actor,**data)
    if args.action=='request':return request(args.workspace,**data)
    if args.action=='submit':return submit(args.workspace,actor=args.actor,**data)
    return merge(args.workspace,actor=args.actor,**data)
from yunxing_rizhi import observed
create=observed('shencha_zu.create')(create)
submit=observed('shencha_zu.submit')(submit)
merge=observed('shencha_zu.merge')(merge)
if __name__=='__main__':
    from yunxing_rizhi import cli
    from phase6_events import run_cli
    run_cli(lambda:cli(main,__file__))
