"""按工作区路径读取运行索引；明确选范围后生成确定性的中文复盘。"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
import sys
import uuid
from yunxing_rizhi import (ROOT_NAME, EVENT_LIMIT, RUN_LIMIT, TOTAL_LIMIT, META_RESERVE,
 ITEM_LIMIT, INDEX_LIMIT, DiagnosticGap, directory, locked, load_index, _read, _replace,
 _json, _open_dir, _names, opaque, persist, OPERATIONS, REASONS, task_index)
import os

FIELDS={'schema_version','event_id','utc_time','run_id','invocation_id','span_id','parent_span_id',
 'task_id','dispatch_id','host','operation','event_type','status','duration_ms','attempt','reason_code',
 'observation_source','business_version','review_mode'}

def event_valid(e):
    if not isinstance(e,dict) or set(e) not in (FIELDS,FIELDS|{"file_counts"}) or e['schema_version']!=1:return False
    if 'file_counts' in e and (not isinstance(e['file_counts'],dict) or set(e['file_counts'])-{'compared','changed','required','inherited','uncovered'} or any(type(v) is not int or not 0<=v<=1000000 for v in e['file_counts'].values())):return False
    if e['operation'] not in OPERATIONS or e['reason_code'] not in REASONS:return False
    for name in ('event_id','run_id','invocation_id','span_id'):
        if not re.fullmatch(r'[0-9a-f]{32}',str(e[name])):return False
    if e['parent_span_id'] is not None and not re.fullmatch(r'[0-9a-f]{32}',str(e['parent_span_id'])):return False
    for name in ('task_id','dispatch_id'):
        if e[name] is not None and not re.fullmatch(r'id-[0-9a-f]{24}',str(e[name])):return False
    if e['host'] not in {'codex','claude','unknown'} or e['event_type'] not in {'start','end','error','wait','retry','skipped'}:return False
    if e['status'] not in {'ok','error','busy','blocked','unknown'} or e['observation_source'] not in {'python_function','python_cli','business_event'}:return False
    if type(e['attempt']) is not int or e['attempt']<1:return False
    d=e['duration_ms']
    if d is not None and (type(d) not in (int,float) or not 0<=d<86400000):return False
    if e['business_version'] is not None and type(e['business_version']) is not int:return False
    if e['review_mode'] not in {None,'full','incremental','reuse'}:return False
    try:
        time=datetime.fromisoformat(e['utc_time'])
        if time.utcoffset() is None:return False
    except (ValueError,TypeError):return False
    return True

def read_events(fd,name):
    folder,leaf=name.split('/');child=_open_dir(fd,folder)
    try:raw=_read(child,leaf,RUN_LIMIT)
    finally:os.close(child)
    records=[];gaps=[]
    for number,line in enumerate(raw.splitlines(),1):
        try:
            e=json.loads(line)
            if len(line)+1>EVENT_LIMIT or not event_valid(e):raise ValueError()
            records.append(e)
        except (ValueError,TypeError):gaps.append('无效或截断事件：'+name+':'+str(number))
    if raw and not raw.endswith(b'\n'):gaps.append('末行未完整提交：'+name)
    return records,gaps

def rebuild(root):
    # Explicit maintenance only; never a startup repair. Retains all existing data.
    with locked(root) as fd:
        files={};notes=[]
        try:previous=json.loads(_read(fd,'index.json',INDEX_LIMIT))
        except (OSError,ValueError):previous={}
        for folder in ('runs','复盘报告'):
            try:child=_open_dir(fd,folder)
            except FileNotFoundError:continue
            try:
                for leaf in _names(child):
                    if not re.fullmatch(r'[0-9a-f]{32}\.(jsonl|md)',leaf):raise DiagnosticGap('unsafe_entry')
                    name=folder+'/'+leaf;size=os.stat(leaf,dir_fd=child,follow_symlinks=False).st_size
                    task=None;run=leaf.split('.')[0]
                    old=previous.get('files',{}).get(name,{}) if isinstance(previous,dict) and isinstance(previous.get('files'),dict) else {}
                    item={'size':size,'task_id':None,'run_id':run,'task_ids':[],
                          'task_overflow':False,'automatic':isinstance(old,dict) and old.get('automatic') is True}
                    if folder=='runs':
                        events,gaps=read_events(fd,name);notes.extend(gaps)
                        for event in events:
                            if event['run_id']!=run:notes.append('运行编号与分段不一致：'+name)
                            task_index(item,event['task_id'])
                    files[name]=item
            finally:os.close(child)
        if len(files)>ITEM_LIMIT:raise DiagnosticGap('metadata_limit')
        if sum(x['size'] for x in files.values())+META_RESERVE>TOTAL_LIMIT:raise DiagnosticGap('capacity_unknown')
        full=sum(x['size'] for x in files.values())>=TOTAL_LIMIT-META_RESERVE or len(files)>=ITEM_LIMIT
        idx={'schema_version':2,'pending':False,'sealed':bool(notes) or full,'files':files}
        try:os.unlink('.index-writing',dir_fd=fd)
        except FileNotFoundError:pass
        if len(_json(idx))>INDEX_LIMIT:raise DiagnosticGap('metadata_limit')
        _replace(fd,'index.json',_json(idx))
        return {'rebuilt':True,'read_scope':'明确维护：全部受控分段和报告元数据','gaps':notes}

def union_ms(intervals):
    total=0;right=None
    for left,end in sorted(intervals):
        if right is None:total+=end-left;right=end
        elif end>right:total+=end-max(left,right);right=end
    return round(total*1000,3)

def select_task(records,target):
    """Only infer a missing task from its own call/host/span ancestry.

    Descendant tasks contribute to parent scope. A parent containing multiple tasks
    remains ambiguous; its unbound start cannot be charged to the final task.
    """
    spans={};calls={}
    for e in records:
        call=(e['run_id'],e['invocation_id'],e['host'])
        key=call+(e['span_id'],)
        item=spans.setdefault(key,{'tasks':set(),'parent':e['parent_span_id']})
        if e['task_id'] is not None:
            item['tasks'].add(e['task_id']);calls.setdefault(call,set()).add(e['task_id'])
    for key,item in list(spans.items()):
        parent=item['parent'];seen={key[-1]}
        for _ in range(128):
            if parent is None or parent in seen:break
            seen.add(parent);ancestor=spans.get(key[:3]+(parent,))
            if ancestor is None:break
            ancestor['tasks'].update(item['tasks']);parent=ancestor['parent']
    selected=[];unknown=[]
    for e in records:
        call=(e['run_id'],e['invocation_id'],e['host'])
        task=e['task_id']
        if task is None:
            candidates=spans[call+(e['span_id'],)]['tasks'] or calls.get(call,set())
            if len(candidates)==1:task=next(iter(candidates))
        if task is None:unknown.append(e['event_id'])
        elif task==target:selected.append(e)
    return selected,unknown

def business_dispatches(root,task_id):
    # Existing business events are the authority for dispatch lifecycle.
    path=Path(root)/'project/records/agent-dispatch.jsonl'
    if not path.exists():return []
    if path.is_symlink():raise DiagnosticGap('unsafe_business_reference')
    states={}
    with path.open(encoding='utf-8') as f:
        for line in f:
            if not line.strip():continue
            e=json.loads(line)
            raw=e.get('dispatch_id');key=opaque(raw)
            if e.get('event')=='prepared' and opaque(e.get('task_id'))==task_id:
                states.setdefault(key,{})
            if key not in states:continue
            label={'returned':'returned_for_readback'}.get(e.get('event'),e.get('event'))
            if label in {'prepared','assigned','returned_for_readback','completed','cancelled'}:
                states[key][label]=(e.get('created_at'),opaque(e.get('record_id')))
    result=[]
    for key,entries in states.items():
        start=entries.get('assigned') or entries.get('prepared')
        end=entries.get('returned_for_readback') or entries.get('completed') or entries.get('cancelled')
        elapsed=None
        if start and end:
            try:elapsed=(datetime.fromisoformat(end[0])-datetime.fromisoformat(start[0])).total_seconds()*1000
            except (TypeError,ValueError):pass
        result.append({'dispatch_id':key,'elapsed_ms':elapsed,'evidence_ids':[x[1] for x in entries.values()],
                       'notice':'分派到返回历时含排队与等待；不是模型生成耗时'})
    return result

def inspect(root,task_id=None,run_id=None):
    root=Path(root)
    if not (root/ROOT_NAME).exists():
        return {'status':'no_records','summary':'此前没有运行记录；是否关闭或丢失未知。','coverage':'模型内部思考、直接读写、网络细项和用户等待未观测'}
    with directory(root) as fd:
        try:idx=load_index(fd)
        except (OSError,ValueError,TypeError) as exc:
            return {'status':'diagnostic_gap','summary':'运行索引缺失、过时或损坏；请明确请求 --rebuild-index 后再复盘。','reason':str(exc) if isinstance(exc,DiagnosticGap) else 'index_invalid'}
        entries=[(name,x) for name,x in idx['files'].items() if name.startswith('runs/')]
        available=[{'run_id':x['run_id'],'task_id':x['task_id'],'task_ids':x['task_ids'],
                    'task_index_complete':not x['task_overflow'],'bytes':x['size']} for _,x in entries]
        if not task_id and not run_id:return {'status':'choose_scope','runs':available,'sealed':idx['sealed'],
            'summary':'先选择任务或运行。每分段最多列32个任务；列表不完整时按业务任务编号检索候选分段，仍逐事件核实归属。无记录不能证明已关闭；未经过本包的动作未观测。'}
        target=task_id if task_id and re.fullmatch(r'id-[0-9a-f]{24}',task_id) else opaque(task_id)
        selected=[(name,x) for name,x in entries if (not task_id or target in x['task_ids'] or x['task_overflow']) and (not run_id or x['run_id']==run_id)]
        records=[];gaps=[]
        for name,_ in selected:
            events,problems=read_events(fd,name);records.extend(events);gaps.extend(problems)
    unknown=[]
    if task_id:records,unknown=select_task(records,target)
    else:unknown=[e['event_id'] for e in records if e['task_id'] is None]
    if unknown:gaps.append('未绑定或不能唯一归属任务的事件：'+str(len(unknown))+'；未计入选定任务。')
    def span_key(e):return (e['run_id'],e['invocation_id'],e['host'],e['span_id'])
    starts={span_key(e):e for e in records if e['event_type']=='start'}
    ends={span_key(e):e for e in records if e['event_type'] in {'end','error'}}
    interrupted=[e['event_id'] for key,e in starts.items() if key not in ends]
    intervals=[];steps=[]
    for key,e in ends.items():
        start=starts.get(key)
        if not start:continue
        left=datetime.fromisoformat(start['utc_time']).timestamp();right=datetime.fromisoformat(e['utc_time']).timestamp()
        if right>=left:intervals.append((left,right))
        steps.append({'operation':e['operation'],'duration_ms':e['duration_ms'],'event_id':e['event_id'],
                      'business_version':e['business_version'],'status':e['status'],'review_mode':e['review_mode'],'file_counts':e.get('file_counts')})
    times=sorted(e['utc_time'] for e in records)
    elapsed=(datetime.fromisoformat(times[-1])-datetime.fromisoformat(times[0])).total_seconds()*1000 if times else None
    counts=Counter(e['operation'] for e in records if e['event_type']=='start')
    reasons=[{'event_id':e['event_id'],'reason_code':e['reason_code'],'operation':e['operation']} for e in records if e['reason_code']!='none']
    relationships=business_dispatches(root,target) if task_id else []
    version='未知'
    description=root/'统一包说明.md'
    if description.is_file() and not description.is_symlink():
        with description.open(encoding='utf-8') as source:header=source.readline(256)
        match=re.search(r'统一包 (\d+\.\d+\.\d+)',header)
        if match:version=match.group(1)
    result={'package_version':version,'status':'recorded' if records else 'no_matching_records','schema_version':1,'task_id':target if task_id else None,
     'run_ids':[x['run_id'] for _,x in selected],'read_scope':[name for name,_ in selected], 'time_range':times[::len(times)-1] if len(times)>1 else times,
     'elapsed_ms':elapsed,'observed_union_ms':union_ms(intervals),'steps':sorted(steps,key=lambda x:x['duration_ms'] or 0,reverse=True),
     'attempt_counts':dict(counts),'waits':[{'duration_ms':e['duration_ms'],'event_id':e['event_id'],'status':e['status']} for e in records if e['event_type']=='wait'],'reasons':reasons,'dispatch_intervals':relationships,'interrupted_event_ids':interrupted,
     'unbound_event_ids':unknown,
     'gaps':gaps+(['已封顶，详细采集停止，覆盖不完整'] if idx['sealed'] else []),
     'not_observed':['模型内部思考与用量/费用','未经过本包的直接读写','网络浏览细项','缺边界的用户等待'],
     'notice':'总区间与去重区间按UTC估算，前提是同机且时钟连续；跨机或时钟调整时不可视为精确耗时。单步骤duration才是同进程单调钟实测；不跨机器精确相加。重复操作不等于无效重做。缺记录不能证明未发生或已关闭。'}
    result['report']=markdown(result)
    return result

LABELS={'command':'命令执行','workspace_lock':'工作区锁等待','validate_project':'项目检查',
 'agent_dispatch.prepare':'准备分派','agent_dispatch.assign':'绑定执行实例','agent_dispatch.return_candidate':'回传候选',
 'agent_dispatch.complete':'主控回读','agent_dispatch.cancel':'取消分派','agent_dispatch.inspect':'检查分派',
 'standalone_tasks.save':'登记独立任务','standalone_tasks.inspect':'查看独立任务','standalone_review.record_review':'登记独立审稿',
 'standalone_review.gate':'核对审稿门槛','incremental_review.plan':'计算增量复核范围',
 'project_handoff.resume':'日常续做检查','project_handoff.inspect':'正式移交检查','project_handoff.prepare':'准备移交',
 'project_handoff.accept':'接收移交','phase2':'合同与计划处理','phase3':'策略产出与检核','phase5':'提案产出与检核',
 'deck_browser':'浏览器检查','deck_export':'导出演示','knowledge_connectors':'外部连接命令'}
LABELS.update({'shougai.preview':'观察当前改稿','shougai.patch':'局部登记/手改接纳','jiaojie.summary':'生成事实交接','agent_dispatch.integrate':'续接登记候选','incremental_review.default_plan':'选择有效基线与累计范围'})
LABELS.update({phase+'.'+action:prefix+'：'+title for phase,prefix,actions in [('phase2','合同与计划',{'contract-read':'读取合同','contract-revise':'逐项解释修订','plan-build':'生成计划','gantt-export':'导出排期','review':'登记独立检核','confirm':'登记确认','reject':'登记退回','status':'查看状态','allow-more':'登记追加许可'}),('phase3','策略',{'source':'登记来源','source-status':'查看来源','source-verify':'核对来源','publish':'登记产出','review':'登记独立检核','confirm':'登记确认','reject':'登记退回','feedback':'登记反馈','bind-feedback':'绑定反馈','status':'查看状态','start':'开始任务','complete':'完成任务','recover':'恢复','decision':'登记决定','restore-registered':'恢复已登记稿','review-sources':'列出检核来源','capabilities':'检查能力','scaffold':'准备结构'}),('phase5','提案',{'status':'查看状态','impact-scan':'检查影响','recover':'恢复','allow-more':'登记追加许可','reject':'登记退回','task-start':'开始任务','task-complete':'完成任务','script-publish':'登记逐字稿','script-sources':'列出逐字稿来源','script-review':'登记逐字稿检核','script-gate':'核对逐字稿门槛','script-confirm':'确认逐字稿','deck-generate':'登记演示稿','deck-sources':'列出演示稿来源','deck-browser-check':'检查演示页面','deck-review':'登记演示稿检核','deck-gate':'核对演示稿门槛','deck-confirm':'确认演示稿','design-submit':'登记设计提交','design-sources':'列出设计来源','design-review':'登记设计检核','design-gate':'核对设计门槛','design-status':'查看设计状态'})] for action,title in actions.items()})
REASON_LABELS={'user_rejection':'用户退回','review_not_passed':'独立检核退回或不通过','revision':'真实业务修订','user_feedback':'版本绑定的用户追加决定','new_scope':'新增范围',
 'idempotent_request':'同请求安全重试','lock_contention':'工作区锁竞争','workspace_lock_timeout':'工作区锁超时',
 'preview_unavailable_expand_scope':'差异预览不足，扩大复核范围','source_or_upstream_changed':'来源或上游改变',
 'number_or_unit_changed':'数字或单位改变','independent_impact_expansion':'独立影响判断扩大范围',
 'visual_scope_all_pages':'视觉变化，全页检查','business_or_memory_changed':'业务或记忆改变',
 'source_removed_or_moved':'来源删除或移动','workflow_error':'业务检查未完成','unknown':'原因待核'}
REASON_LABELS.update({'registration_invalid':'登记字段不符合规则','observation_conflict':'观察版本改变或未经采用','request_conflict':'相同请求号不同内容','revision_conflict':'任务已被别的操作更新','no_change':'没有新变化，未增修订','changed_accepted':'按具体观察版接纳手改','partial_update':'局部字段更新','candidate_preserved':'冲突候选保留，待核对登记','handoff_reused':'复用同状态交接','handoff_generated':'生成当前事实交接','management_only':'仅显式管理信息变化，仍检查成果','management_schema_unknown':'管理字段不明，扩大检查','product_identity_unknown':'成果身份关系不明','product_removed_or_moved':'成果移除或路径变化'})
def review_label(mode):return {'full':'完整','incremental':'增量','reuse':'有效当前检查复用'}.get(mode,'未观测')
def label(op):return LABELS.get(op,op)

def reason_label(reason):return REASON_LABELS.get(reason,reason)

def markdown(r):
    rows=['# 运行复盘（确定性摘要）','',f"工作包版本：{r['package_version']}；日志协议版本：1；任务：{r['task_id'] or '按运行选定'}。",
      f"时间范围：{' — '.join(r['time_range']) or '无记录'}。",
      f"总区间历时：{r['elapsed_ms']} 毫秒；已观测区间去重：{r['observed_union_ms']} 毫秒。",
      '父子和并行步骤没有重复累加为总耗时。分派到返回含排队和等待，不是模型生成耗时。','', '## 事实与证据','']
    for x in r['steps'][:10]:rows.append(f"- {label(x['operation'])}：{x['duration_ms']} 毫秒，{'完成' if x['status']=='ok' else '未完成'}；事件 {x['event_id']}，业务版本 {x['business_version'] if x['business_version'] is not None else '未观测'}，检核模式 {review_label(x['review_mode'])}。")
    for x in r['steps']:
        if x.get('file_counts'):rows.append('- 实测文件范围 '+label(x['operation'])+'：'+json.dumps(x['file_counts'],ensure_ascii=False)+'；required=本轮必查，inherited=有效继承，uncovered=未覆盖。')
    rows+=['', '操作尝试次数：'+json.dumps({label(k):v for k,v in r['attempt_counts'].items()},ensure_ascii=False)+'。']
    for x in r['waits']:rows.append(f"- 已观测业务锁等待：{x['duration_ms']} 毫秒；状态 {'繁忙' if x['status']=='busy' else x['status']}；事件 {x['event_id']}。")
    for x in r['reasons']:rows.append(f"- 已记录原因 {reason_label(x['reason_code'])}；事件 {x['event_id']}。")
    for x in r['dispatch_intervals']:rows.append(f"- 分派 {x['dispatch_id']}：{x['elapsed_ms']} 毫秒；业务事件 {', '.join(x['evidence_ids'])}。")
    rows+=['', '## 缺口与待核','',r['notice'], '未观测：'+'；'.join(r['not_observed'])+'。',
      '真实修订、用户反馈追加、新范围与技术重试，仅在原因事件已采集时分类；缺少原因不能自行归因。',
      '中断或尚未结束的开始事件：'+(', '.join(r['interrupted_event_ids']) or '选定记录内未发现')+'。']
    rows+=['- '+x for x in r['gaps']]
    rows+=['','## 推断与建议（最多五项）','']
    repeats=[op for op,count in r['attempt_counts'].items() if count>1]
    if repeats:rows.append('- 推断：有重复执行迹象，先核对对应事件和业务版本；重复次数本身不能证明无效返工。证据操作：'+', '.join(label(x) for x in repeats)+'。')
    if r['interrupted_event_ids']:rows.append('- 优先核对未结束事件，再决定是否重做；不要直接重试未知结果。')
    if any(x['reason_code']=='lock_contention' for x in r['reasons']):rows.append('- 已观测锁竞争；核对并发操作安排，保留五秒退出保护。')
    if not repeats and not r['interrupted_event_ids']:rows.append('- 本次记录不足以断言存在可删除的重复工作；先继续真实项目观测。')
    return '\n'.join(rows)+'\n'

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workspace',type=Path,required=True)
    p.add_argument('--task-id');p.add_argument('--run-id');p.add_argument('--json',action='store_true')
    p.add_argument('--output',nargs='?',const='auto');p.add_argument('--rebuild-index',action='store_true');args=p.parse_args()
    if args.rebuild_index:rebuild(args.workspace)
    result=inspect(args.workspace,args.task_id,args.run_id)
    if args.output and result.get('report'):
        if args.output!='auto':
            parts=PurePosixPath(args.output).parts
            if parts[:3]!=('project','运行日志','复盘报告') or len(parts)!=4 or '..' in parts or not re.fullmatch(r'[0-9a-f]{32}\.md',parts[-1]):
                p.error('报告只能保存为 project/运行日志/复盘报告/ 下的32位编号.md，建议 --output auto')
        leaf=uuid.uuid4().hex+'.md' if args.output=='auto' else parts[-1]
        run=result['run_ids'][0] if result['run_ids'] else uuid.uuid4().hex
        try:
            persist(args.workspace,'复盘报告/'+leaf,result['report'].encode(),result['task_id'],run,True)
            result['saved_to']=ROOT_NAME+'/复盘报告/'+leaf
        except Exception:result['save_notice']='诊断预算或存储异常，未保存报告；以下摘要仍可阅读。'
    print(json.dumps(result,ensure_ascii=False,indent=2) if args.json else (result.get('report') or result.get('summary','无摘要')))

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError):
        print(json.dumps({'status':'diagnostic_gap','summary':'记录无法完整读取；没有据此判断业务结果。'},ensure_ascii=False));sys.exit(1)
