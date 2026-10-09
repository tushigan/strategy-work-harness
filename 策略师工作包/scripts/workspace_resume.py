from workspace_lock import read_serialized
"""Read-only daily recovery. Formal handoff remains a separate full inventory."""
import json
from pathlib import Path
from task_validation import evaluation
from handoff_inventory import pending_files, root_extras
from handoff_inspection import external_dependencies, state_attention, unsafe_storage_report
from handoff_records import pending_markers
from phase2_store import WorkflowError


def brief(value, limit=400):
    text = str(value or '')
    return text if len(text) <= limit else text[:limit] + '…（原件可展开）'


def formal_problems(root,workflows,tasks,selected,selected_space=None):
    """Summarize CURRENT formal material and its actual consumers, never old inventories."""
    from phase6_targets import catalog,resolve,identity
    from task_validation import reuse
    from current_dependencies import file_references
    items=reuse(catalog,root);problems=[]
    for marker,item in items.items():
        target=item['target'];space=target['space']
        if space=='standalone':continue
        if space=='phase2':status=workflows.get(space,{}).get(target['key'],{})
        else:status=workflows.get(space,{}).get('artifacts',{}).get(target['key'],{})
        reason=status.get('reason') if status.get('status')=='stale' else None
        # The native phase views already verified these versions. Only uncovered
        # registries (source/control/HTML/design) need an additional resolution.
        if not status or space not in {'phase2','phase3','phase5'}:
            try:reuse(resolve,root,target)
            except (OSError,ValueError,KeyError,TypeError) as exc:reason=str(exc)
        if not reason:continue
        affected={marker};refs=file_references(item['files']);pending=[marker]
        while pending:
            upstream=pending.pop()
            for key,consumer in items.items():
                if key not in affected and any(identity(dep)==upstream for dep in consumer['dependencies']):
                    affected.add(key);pending.append(key);refs.update(file_references(consumer['files']))
        owners={('standalone' if items[k]['target']['space']=='standalone' else 'formal',items[k].get('task_id')) for k in affected if items[k].get('task_id')}
        owners.update(('standalone',t['task_id']) for t in tasks if file_references(t['sources']+t['deliverables'])&refs)
        ids={tid for _,tid in owners}
        formal_tasks=workflows.get('phase5',workflows.get('phase3',{})).get('tasks',[])
        if space=='phase2':
            owners.update(('formal',t.get('task_id',t.get('id'))) for t in formal_tasks)
            ids.update(tid for _,tid in owners)
        relevant=selected is None or ((selected_space,selected) in owners if selected_space else selected in ids)
        problems.append({'target':target,'material':marker,'reason':str(reason),
            'affected_tasks':sorted(x for x in ids if x),'blocks_selected_task':relevant,
            'impact':'依赖该材料的任务须暂停；其他任务可独立继续',
            'next_action':'保留旧版本，核对文件与登记指纹；恢复原件或按正式流程登记新版，并重新审核受影响成果'})
    return problems


VOLUME_BYTES=300*1024**2
VOLUME_FILES=1000


def volume(root):
    """U17：项目体积、文件数与近 7 天增长最快的目录（只读文件元数据，不读内容、不算指纹）。"""
    import os,time
    project=Path(root)/'project';total=files=0;by_dir={};recent={};cutoff=time.time()-7*86400
    for current,dirs,names in os.walk(project):
        # 诊断目录不属于业务体积；开/关诊断时业务输出一致。
        dirs[:]=[d for d in dirs if not os.path.islink(os.path.join(current,d)) and not (Path(current)==project and d=='运行日志')]
        rel=Path(current).relative_to(project).parts;key='/'.join(rel[:2]) or '.'
        for name in names:
            try:info=os.lstat(os.path.join(current,name))
            except OSError:continue
            total+=info.st_size;files+=1;by_dir[key]=by_dir.get(key,0)+info.st_size
            if info.st_mtime>=cutoff:recent[key]=recent.get(key,0)+info.st_size
    over=total>VOLUME_BYTES or files>VOLUME_FILES
    mb=lambda b:round(b/1024**2,1)
    return {'bytes':total,'mb':mb(total),'files':files,'threshold':{'mb':mb(VOLUME_BYTES),'files':VOLUME_FILES},
            'largest_dirs':[{'dir':k,'mb':mb(v)} for k,v in sorted(by_dir.items(),key=lambda x:-x[1])[:5]],
            'fastest_growing_7d':[{'dir':k,'mb':mb(v)} for k,v in sorted(recent.items(),key=lambda x:-x[1])[:5]],
            'over_threshold':over,
            'advice':(f"项目已 {mb(total)}MB / {files} 个文件，超过 {mb(VOLUME_BYTES)}MB 或 {VOLUME_FILES} 个文件；"
                      "建议请策略师授权后用 guidang.py 把旧稿移到项目同级归档目录") if over else '未超过阈值'}


@evaluation
@read_serialized
@__import__('yunxing_rizhi').observed('project_handoff.resume')
def resume(root, task_id=None, details=False):
    root = Path(root).resolve()
    unsafe = unsafe_storage_report(root)
    if unsafe:
        return {**unsafe, 'mode':'resume', 'checked_scope':['存储安全'],
                'not_checked':['业务状态','正式移交全量清单'], 'local_work_allowed':False}
    from validate_project import check
    from handoff_package import inspect_package
    from workspace_status import inspect_workflows, project_progress
    errors, warnings, capabilities = check(root, current_only=True)
    package = inspect_package(root)
    errors.extend(package['errors'])
    workflows = inspect_workflows(root, warnings, current_only=True)
    state = json.loads((root/'project/state.json').read_text()) if not errors else {}
    project = project_progress(state, workflows)
    tasks = workflows.get('standalone', {}).get('tasks', [])
    selected_space=None
    if task_id:
        from phase6_targets import catalog
        from task_validation import reuse
        selector=task_id
        if ':' in selector:
            selected_space,task_id=selector.split(':',1)
            if selected_space not in {'formal','standalone'} or not task_id:
                errors.append('任务空间须为 formal 或 standalone，且须有任务编号')
        choices={('standalone',t['task_id']) for t in tasks}
        for stage in ('phase3','phase5'):
            choices.update(('formal',t.get('task_id',t.get('id'))) for t in workflows.get(stage,{}).get('tasks',[])
                           if t.get('task_id',t.get('id')) and t.get('status') not in {'archived','cancelled'})
        try:
            choices.update(('standalone' if i['target']['space']=='standalone' else 'formal',i['task_id'])
                           for i in reuse(catalog,root).values() if i.get('task_id'))
        except (OSError,ValueError,KeyError,TypeError) as exc:
            errors.append('当前任务归属无法核实：'+str(exc))
        matches=[c for c in choices if c[1]==task_id and (selected_space is None or c[0]==selected_space)]
        if len(matches)>1:errors.append('同名任务跨空间有歧义；请明确 '+ '、'.join(k+':'+v for k,v in sorted(matches)))
        elif not matches:errors.append('找不到指定当前任务：'+selector)
        else:selected_space=matches[0][0]
    for t in tasks:
        if task_id is None or selected_space=='standalone' and t['task_id']==task_id:
            errors.extend(f"任务 {t['task_id']}：{x['path']}：{x['issue']}" for x in t['file_issues'])
            warnings.extend(f"任务 {t['task_id']}：{x['notice']}" for x in t.get('record_appends',[]))
    for name in ('standalone','project_memory','task_receipts','connectors'):
        errors.extend(workflows.get(name,{}).get('errors', []))
    formal_artifacts=workflows.get('phase5',{}).get('artifacts',{})
    try:formal_issues=formal_problems(root,workflows,tasks,task_id,selected_space)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        errors.append('当前正式材料无法核实：'+str(exc));formal_issues=[]
    for issue in formal_issues:
        message=f"正式材料 {issue['material']}：{issue['reason']}；{issue['impact']}；下一步：{issue['next_action']}"
        (errors if issue['blocks_selected_task'] else warnings).append(message)
    from agent_dispatch import inspect as dispatch_inspect, verify_context
    try:
        dispatch = dispatch_inspect(root)
        for item in dispatch['active']:
            verify_context(root, item)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append('当前分派无法核实：'+str(exc));dispatch={'active':[]}
    try:
        from proposal_plan import inspect as proposal_inspect
        proposals=[p for p in proposal_inspect(root,tasks=tasks)['plans']]
    except (OSError,ValueError,KeyError,TypeError) as exc:
        errors.append('提案计划无法核实：'+str(exc));proposals=[]
    try:
        from agent_dispatch import unregistered_candidates
        waiting_candidates=unregistered_candidates(root)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        warnings.append('已回读候选无法列出：'+str(exc));waiting_candidates=[]
    pending=pending_files(root)
    if pending: errors.append('存在待恢复写入：'+', '.join(pending))
    extras=root_extras(root)
    if extras: errors.append('根目录有未归档业务文件：'+', '.join(extras))
    markers=pending_markers(root)
    result={'schema_version':1,'mode':'resume','status':'blocked' if errors else 'attention' if warnings else 'ready',
            'local_work_allowed':not errors, 'handoff_ready':None,
            'checked_scope':['代码/配置/双宿主完整性','全部记录结构与当前依赖','当前任务与来源指纹','项目记忆','回执与未结束分派','中断写入'],
            'not_checked':['无关历史媒体内容指纹','正式交接全量清单','模型业务独立复核','外部账号实时可用性'],
            'project':{k:brief(project.get(k)) for k in ('project_name','current_stage','next_action','progress_source')},
            'current_tasks':[{'task_id':t['task_id'],'revision':t['revision'],'status':t['effective_status'],
                              'title':brief(t['title']),'next_action':brief(t['next_action']),
                              # U17：默认只给当前版本与来源数量，来源清单按需 --details 展开。
                              'current_deliverables':[x.get('path') or x.get('url') for x in t['deliverables']],
                              **({'sources':[{'path':x.get('path'),'url':x.get('url')} for x in t['sources']]} if details else {'source_count':len(t['sources'])}),
                              'record_path':'project/records/standalone-tasks.jsonl'} for t in tasks],
            'formal_artifacts':[{'key':key,'status':value.get('status'),'review':value.get('review'),
                'human_confirmed':value.get('human_confirmed'),'reason':brief(value.get('reason'))}
                for key,value in formal_artifacts.items()],
            'formal_issues':formal_issues,
            'historical_dispatch_issues':capabilities.get('historical_dispatch_issues',{'count':0,'unchecked_references':0}),
            'selected_task_id':task_id,'selected_task_space':selected_space,'active_dispatches':[{'dispatch_id':d['dispatch_id'],'task_id':d['task_id'],
                'status':d['status']} for d in dispatch['active']],
            'memory':{k:workflows.get('project_memory',{}).get(k) for k in ('status','memory_version','digest','record_path')},
            'state_attention':state_attention(state), 'pending_transactions':pending,
            'embedded_pending':markers,'external_dependencies':external_dependencies(state),
            'errors':sorted(set(errors)),'warnings':sorted(set(warnings)),
            'details_command':'python scripts/project_handoff.py resume --workspace . --details --json',
            'formal_handoff_command':'python scripts/project_handoff.py inspect --workspace . --json'}
    result['readback_unregistered']=waiting_candidates
    try:
        from banben_baoliu import last_batch
        moved=last_batch(root)
        if moved:result['version_retention']=moved
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result['warnings'].append('自动移出记录无法读取：'+str(exc))
    try:
        from jiyibi import recent
        result['off_package_actions']=[{k:a[k] for k in ('kind','summary','result','task_id','created_at')} for a in recent(root,5)]
    except (OSError,ValueError,KeyError) as exc:
        result['warnings'].append('记一笔记录无法读取：'+str(exc))
    try:
        from waibu_wendang import latest_probes,current as external_docs
        probes=latest_probes(root)
        for item in result['external_dependencies']:
            found=probes.get(item['name'])
            if found and not found.get('stale'):item.update(status=found['status'],last_checked=found['checked_at'],source='实测探针')
            elif found:item.update(status='unknown',last_checked=found['checked_at'],last_result=found['status'],
                                   source=f"实测已过期（{found.get('age_hours')} 小时前），需要时重新 probe")
            else:item.update(recorded_status=item['status'],status='unknown',source='未实测（记录值仅为初始状态）')
        result['external_documents']=[{'task_id':d['task_id'],'url':d['url'],'label':d['label'],'snapshot':d['snapshot'],'read_at':d['read_at']} for d in external_docs(root)]
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result['warnings'].append('外部文档/探针记录无法读取：'+str(exc))
    from yunxing_rizhi import probe
    for item in result['current_tasks']:
        source=next((t for t in tasks if t['task_id']==item['task_id']),None)
        if not source or not source['deliverables']:continue
        try:
            from jiaofu_guankou import status as gate_status
            with probe():
                gate=gate_status(root,item['task_id'],light=True,file_issues=source.get('file_issues'))
            item['review_label']=gate['review_label']
            if gate['delivered']:item['delivered']=[d['to'] for d in gate['delivered']]
            if gate['strategist_pending']:item['strategist_pending']=len(gate['strategist_pending'])
        except (OSError,ValueError,KeyError,TypeError) as exc:
            item['review_label']='检核状态无法核实：'+brief(str(exc),120)
            result['warnings'].append(f"任务 {item['task_id']} 的检核/交付关口状态无法核实（记录可能损坏，未修改原件）："+brief(str(exc),160))
            if result['status']=='ready':result['status']='attention'
    result['proposal_plans']=[{'plan_id':p['plan_id'],'current_stage':p['current_stage'],'next_action':brief(p['next_action']),
        'deadline':p['deadline'],'upgraded_to':p['upgraded_to'],'stages':[{'stage':x['stage'],'name':x['name'],'task_id':x.get('task_id'),
        'status':x['status']} for x in p['stages']]} for p in proposals]
    active_plan=next((p for p in proposals if not p['upgraded_to']),None)
    if active_plan and result['project'].get('progress_source')!='live_closure':
        result['project'].update(current_stage='提案：'+active_plan['current_stage'],next_action=brief(active_plan['next_action']),
                                 progress_source='proposal_plan')
    try:
        result['project_volume']=volume(root)
        if result['project_volume']['over_threshold']:
            result['warnings'].append(result['project_volume']['advice'])
            if result['status']=='ready':result['status']='attention'
    except OSError as exc:
        result['project_volume']={'status':'unknown','reason':str(exc)}
    memory=workflows.get('project_memory',{})
    for key in ('current_facts','needs_confirmation','needs_attention'):
        entries=memory.get(key,[])
        result['memory'][key+'_count']=len(entries)
        result['memory'][key]=[{'memory_id':e['memory_id'],'revision':e['revision'],
            'topic':brief(e['topic']),'current_value':brief(e['current_value']),
            'source_refs':e.get('source_refs',[]),'value_complete':len(str(e['current_value']))<=400}
            for e in entries[:12]]
        if len(entries)>12:result['memory'][key+'_omitted']=len(entries)-12
    result['memory']['summary_complete']=not any(k.endswith('_omitted') for k in result['memory']) and all(
        item['value_complete'] for key in ('current_facts','needs_confirmation','needs_attention') for item in result['memory'][key])
    if details:
        result['workflow_snapshot']=workflows
    else:
        # A budget never turns an omitted issue into a pass. Keep counts and explicit detail link.
        for key in ('errors','warnings','current_tasks','active_dispatches','embedded_pending','formal_artifacts','formal_issues'):
            values=result[key];result[key+'_count']=len(values)
            result[key]=values[:12]
            if len(values)>12:result[key+'_omitted']=len(values)-12
        result['summary_complete']=not any(k.endswith('_omitted') for k in result)
    return result
