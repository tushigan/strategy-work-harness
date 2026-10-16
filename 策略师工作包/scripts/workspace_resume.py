from workspace_lock import read_serialized
"""Read-only daily recovery. Formal handoff remains a separate full inventory."""
import json
import re
from pathlib import Path
from task_validation import evaluation
from handoff_inventory import pending_files, root_extras
from handoff_inspection import external_dependencies, state_attention, unsafe_storage_report
from handoff_records import pending_markers
from phase2_store import WorkflowError


GROUP_LIMIT=15
_PATHISH=re.compile(r"""[^\s：:，,；;（）()“”"'「」、]*/[^\s：:，,；;（）()“”"'「」、]*""")


_SUFFIX=re.compile(r'\.[A-Za-z0-9]{1,8}$')


def _is_path(fragment):
    """带“/”的片段只有含 project/ 或带文件后缀才算路径（返修 #16：“检核/交付关口”之类中文词不算）。"""
    return 'project/' in fragment or bool(_SUFFIX.search(fragment))


_LINE_NO=re.compile(r':\d+(?=\s|$)')
_MISSING=re.compile(r'^(?P<source>.+?) 引用目标不存在: (?P<target>\S.*)$')
RARE_COUNT=2
ARCHIVED_TYPE='已归档（可搬回）'


def _folder(path):
    """分组目录（候选 r3 #3）：版本数字归一——rN、vN 当作一类（r3、r10 同组；导出目录 r11-01 记 rN-N）；
    视觉指纹记录的两位分片目录记 xx。其他目录原样。"""
    parts=path.rsplit('/',1)[0].split('/') if '/' in path else ['']
    out=[]
    for k,part in enumerate(parts):
        if re.fullmatch(r'[rv]\d+',part):part=part[0]+'N'
        elif re.fullmatch(r'[rv]\d+-\d+',part):part=part[0]+'N-N'
        elif k and parts[k-1]=='visual-fingerprints' and re.fullmatch(r'[0-9a-f]{2}',part):part='xx'
        out.append(part)
    return '/'.join(out)


def warning_groups(warnings, archived=()):
    """G10（v1.7.5）：提醒按“类型 + 目录”归并计数，每组给 1 条示例，一屏可读（最多 15 组，其余并为一组）；原 warnings 仍保留供 Agent 读。
    类型 = 去掉路径（连同紧跟的“:行号”）与数字后的提醒文字；目录 = 提醒里第一个路径所在的目录（只认含 project/ 或带文件后缀的片段），
    版本数字归一（rN、vN 当作一类）。候选 r3 #3：
    - 引用目标在归档记录（archive.jsonl，archived 为已归档的路径集合）里有记录的，单列一组“已归档（可搬回）”，不算“引用目标不存在”；
    - 只有一两条的少见类型排在最前面、不参与折叠（未登记的对外版本、外部能力这类不被大量同类提醒淹没）；
    - 其余按条数从多到少，超过 15 组的部分并为一组（少见类型与已归档组占的位置先扣除，至少留一组给“其余”）。"""
    groups,archived_rows={},[]
    archived=set(archived)
    for text in warnings:
        hit=_MISSING.match(text)
        if hit and hit.group('target').strip() in archived:
            archived_rows.append(text);continue
        found=next((m.group(0) for m in _PATHISH.finditer(text) if _is_path(m.group(0))),None)
        folder=_folder(_LINE_NO.sub('',found)) if found else '（无路径）'
        kind=_PATHISH.sub(lambda m:'' if _is_path(m.group(0)) else m.group(0),text)
        kind=re.sub(r'(?:(?<=\s)|^):\d+(?=\s|$)','',kind)  # 路径去掉后剩下的“:行号”
        kind=re.sub(r'\d+','N',kind)
        kind=re.sub(r'\s+',' ',kind).strip(' ：:，,；;。')[:60] or '其他提醒'
        item=groups.setdefault((kind,folder),{'type':kind,'directory':folder,'count':0,'example':text})
        item['count']+=1
    totals={}
    for (kind,_),item in groups.items():totals[kind]=totals.get(kind,0)+item['count']
    rare=sorted((x for x in groups.values() if totals[x['type']]<=RARE_COUNT),key=lambda x:(x['type'],x['directory']))
    for x in rare:x['rare']=True
    common=sorted((x for x in groups.values() if totals[x['type']]>RARE_COUNT),key=lambda x:(-x['count'],x['directory'],x['type']))
    tail=[]
    if archived_rows:
        folders={(_folder(_MISSING.match(t).group('source')) if _MISSING.match(t) else '') for t in archived_rows}
        tail=[{'type':ARCHIVED_TYPE,'directory':folders.pop() if len(folders)==1 else '（多处）','count':len(archived_rows),
               'example':archived_rows[0],'archived':True}]
    room=max(1,GROUP_LIMIT-len(rare)-len(tail))
    if len(common)>room:
        rest=common[room-1:]
        common=common[:room-1]+[{'type':f'其余 {len(rest)} 类提醒','directory':'（多处）','count':sum(x['count'] for x in rest),
                                 'example':rest[0]['example']}]
    return rare+common+tail


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
    largest=[{'dir':k,'mb':mb(v)} for k,v in sorted(by_dir.items(),key=lambda x:-x[1])[:5]]
    # 批次三返修 r2 #6：占地大的目录里有检核留存的资料副本时，建议里给出先预览、再整理的命令（命令放技术附录，正文白话）
    baselines=any(d['dir']=='records/review-baselines' for d in largest)
    advice=(f"项目已 {mb(total)}MB、{files} 个文件，超过提示线（{mb(VOLUME_BYTES)}MB 或 {VOLUME_FILES} 个文件）；"
            "建议请策略师同意后，把用不到的旧稿移到项目旁边的归档文件夹（移走不删，要用可以搬回）。"
            +("占地多的还有检查时留存的资料副本：可以先看一眼哪些已经没人再用，再整理到归档（不删，下次检查照常）。" if baselines else "")
            +"技术附录（给 Agent）：旧稿按策略师当次授权用 guidang.py 归档"
            +("；检核基线副本先预览 python3 scripts/banben_baoliu.py --workspace . --baselines --dry-run --json，"
              "确认后执行 python3 scripts/banben_baoliu.py --workspace . --baselines --json（只移没人再用的副本）" if baselines else ""))
    # v1.7.8 J06：超线时按类别说清（过程、暂存、看名字像过程的、没登记成任务的大文件夹各多少）并建议说“整理过程文件”；
    # 有不属于任何任务的大文件夹时，不论总量是否超线都提醒一句“要不要登记成任务”。只提醒，不阻断，不移动任何文件。
    # 续做状态每次开工都跑，所以只给“估计”：不读文件内容、不算指纹（重复副本要看整理预览才算得准），几个 GB 的项目也不多花时间。
    breakdown=None;unregistered=[];unregistered_advice=''
    try:
        from guocheng_zhengli import UNREGISTERED_BYTES,classify,summarize,unregistered_dirs,_human
        if over:
            found=classify(root,hash_duplicates=False);info=summarize(found)
            breakdown={'estimate':True,'movable':info['movable'],'suspect':{k:info['suspect'][k] for k in ('files','bytes','largest')},
                       'unregistered_dirs':found['unregistered_dirs'],'incomplete':found['incomplete'],
                       'totals':{k:v for k,v in found['totals'].items() if v['files']},'largest_movable':info['largest_movable']}
            unregistered=found['unregistered_dirs']
            if not found['incomplete'] and (info['movable']['files'] or info['suspect']['files'] or unregistered):
                names={'process':'过程文件','temp':'暂存'}
                parts=[f"{names[k]} {v['files']} 个（{_human(v['bytes'])}）" for k,v in info['movable']['by_category'].items() if k in names]
                if info['suspect']['files']:parts.append(f"看名字像过程的 {info['suspect']['files']} 个（{_human(info['suspect']['bytes'])}）")
                if unregistered:parts.append('不属于任何任务的大文件夹 '+str(len(unregistered))+' 个（'+_human(sum(d['bytes'] for d in unregistered))+'）')
                advice=(f"项目已 {mb(total)}MB、{files} 个文件，超过提示线（{mb(VOLUME_BYTES)}MB 或 {VOLUME_FILES} 个文件）。其中（估计，只看名字和位置）："+'、'.join(parts)+"；"
                        "重复副本要看整理预览才算得准。"
                        "建议你说“整理过程文件”，我先列清单给你看，你回“好”我再移到项目旁边的归档文件夹（移走不删，要用可以搬回）。"
                        "技术附录（给 Agent）：预览 python3 scripts/guocheng_zhengli.py preview --workspace . --json；策略师回“好”后 python3 scripts/guocheng_zhengli.py organize --workspace . --words \"<原话>\" --actor <实例>"
                        "（只移确定的加 --only-sure；策略师点名要移的文件夹加 --include <project/ 开头的路径>，可重复）；"
                        "旧稿按策略师当次授权用 guidang.py 归档"
                        +("；检核基线副本先预览 python3 scripts/banben_baoliu.py --workspace . --baselines --dry-run --json，"
                          "确认后执行 python3 scripts/banben_baoliu.py --workspace . --baselines --json（只移没人再用的副本）" if baselines else ""))
        elif any(v>=UNREGISTERED_BYTES for k,v in by_dir.items() if k.split('/')[0] in {'outputs','exports'}):
            unregistered=unregistered_dirs(root)
        if unregistered:
            shown='、'.join('「'+d['dir'].rsplit('/',1)[-1]+'」'+_human(d['bytes']) for d in unregistered[:3])
            unregistered_advice=(f"有不属于任何任务的大文件夹：{shown}。要不要把它登记成一个任务？登记后交接、整理都按任务来；不登记也行，我不会动它。"
                                 "技术附录（给 Agent）：未登记大文件夹的判定见 scripts/guocheng_zhengli.py，提醒线 "+str(mb(UNREGISTERED_BYTES))+"MB。")
    except Exception as exc:  # 提醒功能出错不影响续做状态
        breakdown={'status':'unknown','reason':str(exc)[:200]}
    return {'bytes':total,'mb':mb(total),'files':files,'threshold':{'mb':mb(VOLUME_BYTES),'files':VOLUME_FILES},
            'largest_dirs':largest,
            'fastest_growing_7d':[{'dir':k,'mb':mb(v)} for k,v in sorted(recent.items(),key=lambda x:-x[1])[:5]],
            'over_threshold':over,
            'advice':advice if over else '未超过阈值',
            **({'breakdown':breakdown} if breakdown is not None else {}),
            **({'unregistered_dirs':unregistered,'unregistered_advice':unregistered_advice} if unregistered_advice else {})}


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
    warnings.extend(package.get('warnings',[]))
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
    try:  # 批次四开工补修 1：作者交回时台账读不出来、存下待补的台账编号，补记之前一直提醒
        from shishi_taizhang import pending_backfills, events as ledger_events
        try:
            ledger_rows = ledger_events(root)
            waiting = pending_backfills(root, rows=ledger_rows)
        except WorkflowError:
            ledger_rows = []
            waiting = pending_backfills(root, rows=[])  # 台账本身还读不出来：按都没补记提醒
        # 候选 r2 #12：补记时台账里没有的编号（不补、算补过一次）合起来提示一条，请主控核对；
        # 候选 r3 #6：主控核对后登记“已核对”（附原话）的那次不再提示
        from shishi_taizhang import unchecked_missing
        lost = unchecked_missing(ledger_rows)
        if lost:
            warnings.append(f"补记时有 {len(lost)} 个编号台账里没有，请核对：可能是作者写错了编号，也可能是台账漏记了这条说法（漏记就先补进台账）。"
                            "核对完记一笔，这条提醒就不再显示。"
                            "技术附录（给 Agent）：" + "；".join(f"{d} {v}" for d, v in lost[:6])
                            + "；确认是台账漏记的，先 shishi_taizhang.py add，再按页 cite；核对完运行 shishi_taizhang.py backfill --workspace . "
                            "--dispatch-id <分派编号> --checked --words \"<策略师或主控原话>\" --actor <主控>（每个分派一次）")
        for w in waiting:
            warnings.append(f"有一次作者交回时列的 {len(w['pending'])} 条事实台账说法还没记进台账：台账修好之后要补记一次。"
                            f"技术附录（给 Agent）：待补 {'、'.join(w['pending'][:6])}；"
                            f"shishi_taizhang.py backfill --dispatch-id {w['dispatch_id']} --actor <主控>")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        warnings.append('台账待补记无法列出：'+str(exc))
    try:
        from proposal_plan import inspect as proposal_inspect
        proposals=[p for p in proposal_inspect(root,tasks=tasks)['plans']]
    except (OSError,ValueError,KeyError,TypeError) as exc:
        errors.append('提案计划无法核实：'+str(exc));proposals=[]
    try:
        from matrix_plan import inspect as matrix_inspect
        matrices=matrix_inspect(root,tasks=tasks)['plans']
    except ImportError:
        matrices=[]
    except (OSError,ValueError,KeyError,TypeError) as exc:
        errors.append('产品矩阵计划无法核实：'+str(exc));matrices=[]
    platform=None
    try:
        from pingtai_kuaizhao import resume_summary, platform_line
        platform=resume_summary(root)
        warnings.extend(platform.get('warnings',[]))
    except ImportError:
        platform=None
    except (OSError,ValueError,KeyError,TypeError) as exc:
        warnings.append('平台快照记录无法读取：'+str(exc))
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
            'errors':sorted(set(errors)),'warnings':package.get('warnings',[])+[w for w in sorted(set(warnings)) if w not in package.get('warnings',[])],  # 升级后残留旧文件的提示放最前面（续做状态只展示前几条提醒，不能被淹没）
            
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
            if gate.get('since_pass'):item['since_pass']=gate['since_pass']  # G02（v1.7.5）：始终显示“自上次检查通过已隔 N 版”
            if gate['delivered']:item['delivered']=[d['to'] for d in gate['delivered']]
            if gate.get('deliveries'):item['deliveries']=gate['deliveries'][-12:]  # G06：各版发客户记录（补登记的写两个时间）
            if gate['strategist_pending']:item['strategist_pending']=len(gate['strategist_pending'])
        except (OSError,ValueError,KeyError,TypeError) as exc:
            item['review_label']='检核状态无法核实：'+brief(str(exc),120)
            result['warnings'].append(f"任务 {item['task_id']} 的检核/交付关口状态无法核实（记录可能损坏，未修改原件）："+brief(str(exc),160))
            if result['status']=='ready':result['status']='attention'
    result['proposal_plans']=[{'plan_id':p['plan_id'],'current_stage':p['current_stage'],'next_action':brief(p['next_action']),
        'deadline':p['deadline'],'upgraded_to':p['upgraded_to'],'stages':[{'stage':x['stage'],'name':x['name'],'task_id':x.get('task_id'),
        'status':x['status']} for x in p['stages']]} for p in proposals]
    result['matrix_plans']=[{'plan_id':p['plan_id'],'start_line':p['start_line'],'cost_status':p['cost_status'],'current_stage':p['current_stage'],
        'next_action':brief(p['next_action']),'deadline':p['deadline'],'platform_pending':p['platform_pending'],
        'platform_status':p.get('platform_status'),'platform_snapshot':p.get('platform_snapshot'),
        'stages':[{'stage':x['stage'],'name':x['name'],'task_id':x.get('task_id'),'status':x['status']} for x in p['stages']]} for p in matrices]
    if platform and platform.get('snapshots'):
        result['platform_snapshots']=platform
    active_matrix=next((p for p in matrices if p['current_stage']!='全部阶段完成'),None)
    if active_matrix:  # 产品矩阵计划单列一行，不覆盖品牌屋等正式主线的“当前阶段”
        result['project']['matrix_line']='产品矩阵：'+active_matrix['current_stage']+'；'+brief(active_matrix['next_action'])+(
            '；'+active_matrix['platform_status'] if active_matrix.get('platform_status') else '')  # W3：平台测算状态随快照同步
    if matrices and platform is not None:  # P10（v1.7.7）：建过产品矩阵计划才出现；只提醒，不阻断
        result['project']['platform_line']=platform_line(platform.get('last_identity_check'))
    active_plan=next((p for p in proposals if not p['upgraded_to']),None)
    if active_plan and result['project'].get('progress_source')!='live_closure':
        result['project'].update(current_stage='提案：'+active_plan['current_stage'],next_action=brief(active_plan['next_action']),
                                 progress_source='proposal_plan')
    try:
        result['project_volume']=volume(root)
        if result['project_volume']['over_threshold']:
            result['warnings'].append(result['project_volume']['advice'])
            if result['status']=='ready':result['status']='attention'
        if result['project_volume'].get('unregistered_advice'):
            result['warnings'].append(result['project_volume']['unregistered_advice'])
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
    try:
        # G09（v1.7.5）：对外目录里有比已登记版本更新、又没登记过的文件
        from jiaofu_guankou import unregistered_exports
        found=unregistered_exports(root)
        if found:
            result['unregistered_exports']=found
            result['warnings'].extend(f"有未登记的对外版本：{x['path']}" for x in found)
            if result['status']=='ready':result['status']='attention'
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result['warnings'].append('对外目录无法核对是否有未登记的版本：'+brief(str(exc),160))
    reminders=[]
    try:
        # G02（v1.7.5）：先交后检发现的问题，策略师确认前置顶
        from jiaofu_guankou import reminders as strategist_reminders
        reminders=[x for x in strategist_reminders(root) if task_id is None or selected_space!='standalone' or x['task_id']==task_id]
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result['warnings'].append('检查提醒无法读取：'+brief(str(exc),160))
    finals=[]
    try:
        # G07（v1.7.5；候选 r5 按用户 2026-10-11 拍板简化）：最终版没有一次检查通过的记录时只摆一行事实，策略师说不用查后不再出现
        from jiaofu_guankou import final_checks
        finals=[x for x in final_checks(root) if task_id is None or selected_space!='standalone' or x['task_id']==task_id]
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result['warnings'].append('最终版检查记录无法读取：'+brief(str(exc),160))
    if (reminders or finals) and result['status']=='ready':result['status']='attention'
    handed=[]
    try:  # v1.7.8 J01：这份工作区交出去过，顶部提醒一句（只提醒，不阻断）
        from jiaojie_dabao import handed_over
        handed=handed_over(root)[:3]
    except (ImportError,OSError,ValueError,KeyError,TypeError):
        handed=[]
    result={**{k:result[k] for k in ('schema_version','mode','status')},**({'handed_over':handed} if handed else {}),'strategist_reminders':reminders,'final_unchecked':finals,**result}
    try:  # 候选 r3 #3：引用目标在归档记录里的单列“已归档（可搬回）”；归档记录读不出来时照旧按“引用目标不存在”归组
        from guidang import archived_index
        archived={path for path,_ in archived_index(root)}
    except (OSError,ValueError,KeyError,TypeError):
        archived=set()
    result['warning_groups']=warning_groups(result['warnings'],archived)  # 文字输出（project_handoff.print_result）也按组打印
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
