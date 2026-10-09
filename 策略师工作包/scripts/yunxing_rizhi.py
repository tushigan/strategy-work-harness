"""Bounded, best-effort local diagnostics. No business content or raw exceptions."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import fcntl
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

ROOT_NAME = 'project/运行日志'
EVENT_LIMIT = 4096
RUN_LIMIT = 20 * 1024**2
TOTAL_LIMIT = 200 * 1024**2
META_RESERVE = 1024**2
INDEX_LIMIT = 512 * 1024
ITEM_LIMIT = 256
TASK_ITEM_LIMIT = 32
AUTO_HEADROOM = 64 * 1024
LOCK_SECONDS = .010
_state = ContextVar('diagnostic_state', default=None)
REASONS = {'none','workflow_error','io_error','internal_error','workspace_lock_timeout',
 'lock_contention','idempotent_request','independent_impact_expansion','source_or_upstream_changed',
 'number_or_unit_changed','source_removed_or_moved','business_or_memory_changed',
 'visual_scope_all_pages','preview_unavailable_expand_scope','baseline_unavailable','diff_timeout','diff_size','diff_type','diff_lines',
 'unchanged_valid_review','review_group_frozen','revision','user_feedback','user_rejection','review_not_passed','new_scope','unknown','registration_invalid','observation_conflict','request_conflict','revision_conflict','no_change','changed_accepted','partial_update','candidate_preserved','handoff_reused','handoff_generated','management_only','management_schema_unknown','product_identity_unknown','product_removed_or_moved','retry_limit','retry_override','budget_reached'}
OPERATIONS = {'shougai.preview','shougai.patch','jiaojie.summary','agent_dispatch.integrate','command','workspace_lock','validate_project','agent_dispatch.prepare','agent_dispatch.assign',
 'agent_dispatch.return_candidate','agent_dispatch.complete','agent_dispatch.cancel','agent_dispatch.inspect',
 'standalone_tasks.save','standalone_tasks.inspect','standalone_review.record_review','standalone_review.gate',
 'incremental_review.plan','incremental_review.default_plan','shencha_zu.create','shencha_zu.submit','shencha_zu.merge','project_handoff.resume','project_handoff.inspect','project_handoff.prepare',
 'project_handoff.accept','phase2','phase3','phase5','deck_browser','deck_export','knowledge_connectors'}
FORMAL_ACTIONS={
 'phase2':{'contract-read','contract-revise','plan-build','gantt-export','review','confirm','reject','status','allow-more'},
 'phase3':{'source','source-status','source-verify','publish','review','confirm','reject','feedback','bind-feedback','status',
           'start','complete','recover','decision','restore-registered','review-sources','capabilities','scaffold'},
 'phase5':{'status','impact-scan','recover','allow-more','reject','task-start','task-complete',
           'script-publish','script-sources','script-review','script-gate','script-confirm','deck-generate','deck-sources',
           'deck-browser-check','deck-review','deck-gate','deck-confirm','design-submit','design-sources','design-review',
           'design-gate','design-status'}}
OPERATIONS.update(phase+'.'+action for phase,actions in FORMAL_ACTIONS.items() for action in actions)

class DiagnosticGap(ValueError): pass

GUARD_NAME='重试守卫.json'
GUARD_LIMIT=2  # U18：同一命令同类报错连续 2 次即停


@contextmanager
def probe():
    """状态探测（如 resume 内查看检核/影响状态）：被拒是正常状态，记为 blocked，不记 error。
    整条命令层面的对应做法是 StateReport：resume 等探测入口如实报出 blocked 时抛它，退出码仍为 1，重试守卫不计数（F08）。"""
    state=_state.get()
    if not state:
        yield;return
    state['probe']=state.get('probe',0)+1
    try:yield
    finally:state['probe']-=1


def detect_host():
    """显式 HARNESS_HOST 优先；否则按宿主自带环境变量识别（Claude Code 设 CLAUDECODE=1；Codex 进程带 CODEX_ 前缀变量，推断）。"""
    value=os.environ.get('HARNESS_HOST')
    if value in {'codex','claude'}:return value
    if os.environ.get('CLAUDECODE')=='1':return 'claude'
    if any(k.startswith('CODEX_') for k in os.environ):return 'codex'
    return 'unknown'

def opaque(value):
    return ('id-'+hashlib.sha256(str(value).encode()).hexdigest()[:24]) if value is not None else None

def _json(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()

def _open_dir(parent,name,create=False):
    if create:
        try:os.mkdir(name,0o700,dir_fd=parent)
        except FileExistsError:pass
    return os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)

@contextmanager
def directory(root,create=False):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in ('project','运行日志'):
            child=_open_dir(fd,part,create);os.close(fd);fd=child
        yield fd
    finally:os.close(fd)

def _names(fd):
    with os.scandir(fd) as stream:
        names=[e.name for e in itertools.islice(stream,ITEM_LIMIT+4)]
    if len(names)>ITEM_LIMIT+2:raise DiagnosticGap('metadata_limit')
    return set(names)

def _read(fd,name,limit):
    f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    try:
        info=os.fstat(f)
        if not stat.S_ISREG(info.st_mode) or info.st_size>limit:raise DiagnosticGap('index_invalid')
        chunks=[]; size=0
        while data:=os.read(f,min(65536,limit+1-size)):
            chunks.append(data);size+=len(data)
            if size>limit:raise DiagnosticGap('index_invalid')
        return b''.join(chunks)
    finally:os.close(f)

def _replace(fd,name,data):
    temp='.index-writing'
    f=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try:
        with os.fdopen(f,'wb') as out:out.write(data)
        os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd)
    finally:
        try:os.unlink(temp,dir_fd=fd)
        except FileNotFoundError:pass

@contextmanager
def locked(root,create=False):
    with directory(root,create) as fd:
        f=os.open('.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=fd)
        acquired=False
        try:
            deadline=time.monotonic()+LOCK_SECONDS
            while True:
                try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);acquired=True;break
                except BlockingIOError:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise DiagnosticGap('diagnostic_lock_busy')
                    time.sleep(min(.001,remaining))
            yield fd
        finally:
            if acquired:fcntl.flock(f,fcntl.LOCK_UN)
            os.close(f)

def load_index(fd,initialize=False):
    names=_names(fd)
    if names-{'index.json','.lock','runs','复盘报告',GUARD_NAME}:raise DiagnosticGap('index_uncommitted')
    if '.lock' in names:
        lockinfo=os.stat('.lock',dir_fd=fd,follow_symlinks=False)
        if not stat.S_ISREG(lockinfo.st_mode) or lockinfo.st_size>4096:raise DiagnosticGap('metadata_limit')
    if 'index.json' not in names:
        if names- {'.lock',GUARD_NAME} or not initialize:raise DiagnosticGap('index_missing')
        idx={'schema_version':2,'pending':False,'sealed':False,'files':{}}
    else:
        idx=json.loads(_read(fd,'index.json',INDEX_LIMIT))
    if (set(idx)!={'schema_version','pending','sealed','files'} or idx['schema_version'] not in {1,2}
        or type(idx['pending']) is not bool or type(idx['sealed']) is not bool
        or not isinstance(idx['files'],dict) or len(idx['files'])>ITEM_LIMIT):raise DiagnosticGap('index_invalid')
    if idx['pending']:raise DiagnosticGap('index_uncommitted')
    actual={}
    for folder in ('runs','复盘报告'):
        if folder not in names:continue
        child=_open_dir(fd,folder)
        try:
            for name in _names(child):
                if not re.fullmatch(r'[0-9a-f]{32}\.(jsonl|md)',name):raise DiagnosticGap('index_invalid')
                info=os.stat(name,dir_fd=child,follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):raise DiagnosticGap('unsafe_entry')
                actual[folder+'/'+name]=info.st_size
        finally:os.close(child)
    expected={}
    for name,item in idx['files'].items():
        if not re.fullmatch(r'(runs|复盘报告)/[0-9a-f]{32}\.(jsonl|md)',name):raise DiagnosticGap('index_invalid')
        fields={'size','task_id','run_id'}
        if idx['schema_version']==2:fields|={'task_ids','task_overflow','automatic'}
        if set(item)!=fields or type(item['size']) is not int or item['size']<0:
            raise DiagnosticGap('index_invalid')
        if item['task_id'] is not None and not re.fullmatch(r'id-[0-9a-f]{24}',str(item['task_id'])):
            raise DiagnosticGap('index_invalid')
        if not re.fullmatch(r'[0-9a-f]{32}',str(item['run_id'])):raise DiagnosticGap('index_invalid')
        if idx['schema_version']==2:
            if (not isinstance(item['task_ids'],list) or len(item['task_ids'])>TASK_ITEM_LIMIT
                or len(set(item['task_ids']))!=len(item['task_ids'])
                or any(not re.fullmatch(r'id-[0-9a-f]{24}',str(x)) for x in item['task_ids'])
                or type(item['task_overflow']) is not bool or type(item['automatic']) is not bool):
                raise DiagnosticGap('index_invalid')
        expected[name]=item['size']
    if actual!=expected:raise DiagnosticGap('index_stale')
    if sum(actual.values())+META_RESERVE>TOTAL_LIMIT:raise DiagnosticGap('capacity_unknown')
    if idx['schema_version']==1:
        # Old per-run task_id may have overwritten other/late-bound tasks. Treat it
        # as a hint only; selected-task lookup must scan old segments conservatively.
        idx['schema_version']=2
        for item in idx['files'].values():
            item.update(task_ids=[item['task_id']] if item['task_id'] else [],
                        task_overflow=True,automatic=False)
    return idx

def task_index(item,task_id):
    if task_id is not None and task_id not in item['task_ids']:
        if len(item['task_ids'])<TASK_ITEM_LIMIT:item['task_ids'].append(task_id)
        else:item['task_overflow']=True
    item['task_id']=task_id

def persist(root,name,data,task_id,run_id,report=False,*,automatic=False,event=None):
    """Reserve in index BEFORE append; any crash between commits halts normal capture."""
    with locked(root,True) as fd:
        idx=load_index(fd,True)
        if idx['sealed']:raise DiagnosticGap('capacity_sealed')
        files=idx['files']
        if automatic and not report:
            # One default collector run spans ordinary commands. Rotate before a
            # command starts, leaving bounded headroom for its final events.
            candidates=[x for n,x in files.items() if n.startswith('runs/') and x['automatic']
                        and x['size']<=RUN_LIMIT-AUTO_HEADROOM]
            if candidates:run_id=candidates[-1]['run_id']
            name='runs/'+run_id+'.jsonl'
        if event is not None:
            event['run_id']=run_id;data=_json(event)+b'\n'
        previous=files.get(name,{'size':0,'task_id':None,'run_id':run_id,
                                'task_ids':[],'task_overflow':False,'automatic':automatic})
        total=sum(x['size'] for x in files.values())
        # RUN_LIMIT covers event bytes only, consistently with automatic selection.
        # Associated reports remain part of the workspace-wide diagnostic budget.
        run_total=sum(x['size'] for n,x in files.items()
                      if n.startswith('runs/') and x['run_id']==run_id)
        if not report and run_total+len(data)>RUN_LIMIT:
            raise DiagnosticGap('run_capacity')
        if ((name not in files and len(files)>=ITEM_LIMIT) or total+len(data)>TOTAL_LIMIT-META_RESERVE
            ):
            # One fixed-sized sealed flag; no growing per-dropped-event ledger.
            idx['sealed']=True;_replace(fd,'index.json',_json(idx));raise DiagnosticGap('capacity_sealed')
        idx['pending']=True
        files[name]={**previous,'task_ids':list(previous['task_ids']),'size':previous['size']+len(data)}
        task_index(files[name],task_id)
        encoded=_json(idx)
        if len(encoded)>INDEX_LIMIT:raise DiagnosticGap('metadata_limit')
        _replace(fd,'index.json',encoded)
        folder,leaf=name.split('/'); child=_open_dir(fd,folder,True)
        try:
            f=os.open(leaf,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=child)
            try:
                if os.fstat(f).st_size!=previous['size']:raise DiagnosticGap('index_stale')
                # Regular local file; lock protects the complete append.
                with os.fdopen(f,'ab') as out:out.write(data)
            except BaseException:
                # fdopen owns the descriptor after it is constructed.
                try:os.close(f)
                except OSError:pass
                raise
        finally:os.close(child)
        idx['pending']=False;_replace(fd,'index.json',_json(idx))
        return run_id

def emit(event):
    state=_state.get()
    if not state or state['gap'] or not state['enabled']:return
    try:
        from yunxing_fupan import event_valid
        if not event_valid(event):raise DiagnosticGap('event_invalid')
        raw=_json(event)+b'\n'
        if len(raw)>EVENT_LIMIT:raise DiagnosticGap('event_limit')
        run=persist(state['root'],'runs/'+state['run_id']+'.jsonl',raw,state['task_id'],state['run_id'],
                    automatic=state.get('automatic',False),event=event)
        state['run_id']=run;state['automatic']=False
    except Exception as exc:
        state['gap']=str(exc) if isinstance(exc,DiagnosticGap) else 'diagnostic_unavailable'

def note(operation,event_type='end',*,duration_ms=None,reason_code='none',status='ok',span_id=None,parent_span_id=None,version=None,mode=None,file_counts=None):
    state=_state.get()
    if not state:return
    if operation not in OPERATIONS:operation='command'
    if event_type not in {'start','end','error','wait','retry','skipped'}:event_type='skipped'
    if reason_code not in REASONS:reason_code='unknown'
    if status not in {'ok','error','busy','blocked','unknown'}:status='unknown'
    if duration_ms is not None and (not isinstance(duration_ms,(int,float)) or not math.isfinite(duration_ms) or duration_ms<0):duration_ms=None
    event={'schema_version':1,'event_id':uuid.uuid4().hex,'utc_time':datetime.now(timezone.utc).isoformat(),
     'run_id':state['run_id'],'invocation_id':state['invocation_id'],'span_id':span_id or state['span_id'],
     'parent_span_id':parent_span_id,'task_id':state['task_id'],'dispatch_id':state['dispatch_id'],
     'host':state['host'],'operation':operation,'event_type':event_type,'status':status,
     'duration_ms':duration_ms,'attempt':state['attempts'].get(operation,1),'reason_code':reason_code,
     'observation_source':'python_function','business_version':version if type(version) is int else None,
     'review_mode':mode if mode in {'full','incremental','reuse'} else None}
    if file_counts is not None and isinstance(file_counts,dict):
        event["file_counts"]={k:v for k,v in file_counts.items() if k in {"compared","changed","required","inherited","uncovered"} and type(v) is int and 0<=v<=1000000}
    emit(event)

def bind(task_id=None,dispatch_id=None):
    state=_state.get()
    if state:
        if task_id is not None:state['task_id']=opaque(task_id)
        if dispatch_id is not None:state['dispatch_id']=opaque(dispatch_id)


def classify(reason_code):
    state=_state.get()
    if state and reason_code in REASONS:state.setdefault('span_reasons',{})[state['span_id']]=reason_code

def formal_action(phase,action,task_id=None):
    """Called once after native parsing; no argument/body copy or new tool call."""
    state=_state.get()
    if not state or not state['enabled'] or action not in FORMAL_ACTIONS.get(phase,set()):return
    bind(task_id)
    span=uuid.uuid4().hex;parent=state['span_id']
    state['formal_action']={'operation':phase+'.'+action,'span_id':span,'parent':parent,
                           'start':time.monotonic(),'version':None,'mode':None,'reason':'none'}
    state['span_id']=span
    if action=='reject':state['formal_action']['reason']='user_rejection'
    note(phase+'.'+action,'start',span_id=span,parent_span_id=parent)

def business_result(result,*,full_review=False,reason_code=None):
    """Read only already-returned native fields; never reopen reports or payloads."""
    state=_state.get();action=state.get('formal_action') if state else None
    if not action or not isinstance(result,dict):return
    try:
        metadata=result
        for name in ('artifact','target','submission'):
            if isinstance(result.get(name),dict):metadata=result[name];break
        version=metadata.get('version')
        if type(version) is int:action['version']=version
        mode=result.get('review_mode')
        if mode in {'full','incremental','reuse'}:action['mode']=mode
        elif full_review:action['mode']='full'
        revision=metadata.get('revision')
        if isinstance(revision,dict) and revision and action['operation'].endswith(('.publish','-publish','-generate','-submit')):
            action['reason']='user_feedback' if revision.get('allowance_id') else 'revision'
        if result.get('status') in {'failed','rejected','returned','insufficient_evidence','needs_revision'} and 'review' in action['operation']:
            action['reason']='review_not_passed'
        scope=result.get('changed_scope')
        reasons=scope.get('reason_codes',[]) if isinstance(scope,dict) else []
        for reason in reasons[:8] if isinstance(reasons,list) else []:
            if isinstance(reason,str) and reason in REASONS:action['reason']=reason;break
        if metadata.get('task_id'):bind(metadata['task_id'])
        if reason_code in REASONS:action['reason']=reason_code
    except (TypeError,ValueError,KeyError):
        state['gap']='diagnostic_unavailable'


def counts_of(result):
    if not isinstance(result,dict):return None
    counts={}
    if isinstance(result.get('files'),list) and result['files'] and all(isinstance(f,dict) and 'changed' in f for f in result['files']):
        counts['compared']=len(result['files']);counts['changed']=sum(f.get('changed') is True for f in result['files'] if isinstance(f,dict))
    for k,field in [('required','checked_sources_required'),('inherited','reused_coverage'),('uncovered','uncovered')]:
        if isinstance(result.get(field),list):counts[k]=len(result[field])
    return counts or None

def observed(operation):
    def decorate(function):
        @wraps(function)
        def call(*args,**kwargs):
            state=_state.get()
            if not state or not state['enabled']:return function(*args,**kwargs)
            task = kwargs.get('task_id')
            if len(args)>1 and isinstance(args[1],dict): task=args[1].get('task_id',task) or (args[1]['key'].split('::')[0] if isinstance(args[1].get('key'),str) else None)
            if task is not None:state['task_id']=opaque(task)
            parent=state['span_id']; span=uuid.uuid4().hex;state['span_id']=span
            state['attempts'][operation]=state['attempts'].get(operation,0)+1
            start=time.monotonic();note(operation,'start',span_id=span,parent_span_id=parent)
            result=None; error=None
            try:result=function(*args,**kwargs);return result
            except BaseException as exc:error=exc;raise
            finally:
                try:
                    version=next((x for x in (result.get('revision'),result.get('event',{}).get('revision') if isinstance(result.get('event'),dict) else None,result.get('version'),result.get('target',{}).get('version') if isinstance(result.get('target'),dict) else None) if type(x) is int),None) if isinstance(result,dict) else None
                    if isinstance(result,dict) and result.get('dispatch_id'):state['dispatch_id']=opaque(result['dispatch_id'])
                    # 状态探测里“未检核/被拒”是正常状态记 blocked；记录损坏仍记 error，不被探测掩盖
                    probing=bool(error) and state.get('probe',0)>0 and not isinstance(error,(OSError,KeyError,TypeError)) and not re.search(r'损坏|格式无效|不能解析|无法解析',str(error))
                    note(operation,'error' if error and not probing else 'end',duration_ms=(time.monotonic()-start)*1000,
                         status=('blocked' if probing else 'error') if error else 'ok',reason_code=('review_not_passed' if probing else getattr(error,'reason_code','workflow_error')) if error else state.get('span_reasons',{}).pop(span,'none'),
                         span_id=span,parent_span_id=parent,version=version,
                         mode=('incremental' if operation=='incremental_review.plan' else result.get('review_mode')) if isinstance(result,dict) else None,
                         file_counts=counts_of(result))
                    if isinstance(result,dict):
                        scope=result.get('changed_scope')
                        reasons=scope.get('reason_codes',[]) if isinstance(scope,dict) else result.get('reason_codes',[])
                        if not isinstance(reasons,list):reasons=[]
                        for reason in reasons[:8]:note(operation,'skipped',reason_code=reason,span_id=span,parent_span_id=parent)
                except Exception:
                    state['gap']='diagnostic_unavailable'
                finally:state['span_id']=parent
        return call
    return decorate

class _Tee:
    """透传输出，同时保留尾部 4KiB 供重试守卫判断报错类别；不写盘、不进日志。"""
    def __init__(self,stream,sink):self.stream=stream;self.sink=sink
    def write(self,text):
        self.sink.append(text)
        if sum(len(x) for x in self.sink)>8192:del self.sink[:len(self.sink)//2]
        return self.stream.write(text)
    def __getattr__(self,name):return getattr(self.stream,name)


KEY_FIELDS=('task_id','dispatch_id','plan_id','key','task','stage','kind','target','html','path','file','deliverable','name','url','report')
REQUEST_IDS=('task_id','dispatch_id','plan_id','group_id','manifest_id','stage','target','key')  # 请求文件里的业务对象（不含 request_id）


def _norm_text(text):
    import unicodedata
    return unicodedata.normalize('NFC',str(text)).casefold()


def _norm_path(root,value):
    """路径先按工作区解析（../、./、重复斜杠、工作区内绝对路径都归到同一相对写法），再 NFC + casefold（macOS 文件名不区分大小写）。"""
    import posixpath
    text=str(value)
    try:
        if root is not None:
            base=Path(root).resolve();target=Path(text) if Path(text).is_absolute() else base/text
            target=Path(os.path.realpath(posixpath.normpath(str(target))))  # 系统目录别名（如临时目录的软链接）也归一
            if target.is_relative_to(base):text=target.relative_to(base).as_posix() or '.'
    except (OSError,ValueError):pass
    return _norm_text(posixpath.normpath(text))


def _request_objects(root,value):
    """--request 类命令：按请求文件里的业务对象编号区分（同一文件换成另一任务的请求不误拦）；读不了才退回文件路径。"""
    try:
        path=Path(value) if Path(str(value)).is_absolute() or root is None else Path(root)/str(value)
        data=json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data,dict):
            found=[k+'='+str(data[k]) for k in REQUEST_IDS if isinstance(data.get(k),(str,int))]
            if found:return found
    except (OSError,ValueError):pass
    return ['request='+_norm_path(root,value)]


def command_key(filename,argv):
    """解析失败（用法错误）时的保守键：脚本名 + 用法错误；解析成功后改用 parsed_key。"""
    return Path(filename).stem+':usage'


def parsed_key(filename,namespace,root=None):
    """按解析后的子命令 + 业务对象区分（参数顺序、路径写法、大小写与 Unicode 形态不影响；换请求编号不算换对象）。"""
    values=vars(namespace) if namespace is not None else {}
    sub='/'.join(str(values[k]) for k in ('command','action','cmd','subcommand','kind_command') if isinstance(values.get(k),str))
    ids=[]
    for k in KEY_FIELDS:
        v=values.get(k)
        if v in (None,[],''):continue
        items=v if isinstance(v,(list,tuple)) else [v]
        norm=[_norm_path(root,x) if ('/' in str(x) or str(x).startswith('.') or isinstance(x,Path)) else str(x) for x in items]
        ids.append(k+'='+','.join(sorted(norm)))
    if values.get('request') not in (None,''):
        ids.extend(_request_objects(root,values['request']))
    key=Path(filename).stem+':'+sub+('|'+';'.join(ids) if ids else '')
    return key if len(key)<=160 else key[:120]+'#'+hashlib.sha256(key.encode()).hexdigest()[:16]


def reason_valid(text):
    """放行理由须是一句人话：至少 8 个字、不是同一字符重复，含中文或至少三个词。"""
    text=(text or '').strip()
    return len(text)>=8 and len(set(text))>=4 and (len(re.findall(r'[\u3400-\u9fff]',text))>=4 or len(text.split())>=3)


class RetryBlocked(SystemExit):
    reason_code='retry_limit'


class StateReport(SystemExit):
    """F08（v1.7.3）：状态探测命令（resume、inspect、启动检查）如实报出 blocked 是成功，不是失败。
    退出码仍为 1（既有约定）；cli 的重试守卫看到它不计数、不清零、不写放行记录、不打印“已停止重试”。
    命令自身崩溃（未捕获异常）仍按原规则计数；记录损坏被命令捕获并如实列进 blocked 报告的，属于状态，同样不计数。
    运行日志仍按 v1.7.2 记 error / workflow_error（复盘统计口径不变），只是守卫不计数。"""


# v1.7.2 及以前把这些探测入口的 blocked 计成失败；这些旧计数（无 rule=2 标记）不再据以拦截，
# 下一次如实报出状态时顺手清掉（只清旧版计数，新版守卫记录照常保留）。
STATE_PROBES=('project_handoff:resume','project_handoff:inspect','validate_project:')


def _legacy_probe(key,entry):
    return (isinstance(entry,dict) and entry.get('rule')!=2
            and any(key==p or key.startswith(p+'|') for p in STATE_PROBES))


OVERRIDES='project/records/重试放行.jsonl'
OVERRIDE_KEEP=200   # 放行记录滚动保留最近 200 条
USED_KEEP=64        # 守卫记住最近 64 次“键 + 理由”，同键同理由不重复放行


def override_digest(key,reason):
    return hashlib.sha256((key+'\0'+re.sub(r'\s+','',_norm_text(reason))).encode()).hexdigest()[:16]


def record_override(root,key,previous,reason):
    """放行理由写进业务记录（可回查原文，滚动保留最近 OVERRIDE_KEEP 条），运行日志只记 retry_override 类别。"""
    path=Path(root)/OVERRIDES
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.is_symlink():raise OSError('symlink')
        row={'schema_version':1,'created_at':datetime.now(timezone.utc).isoformat(),'command':key,'previous_count':previous.get('count'),
             'signature':previous.get('signature'),'reason':reason.strip()[:400],'host':detect_host()}
        lines=path.read_text(encoding='utf-8').splitlines() if path.is_file() else []
        lines=(lines+[json.dumps(row,ensure_ascii=False)])[-OVERRIDE_KEEP:]
        temp=path.with_name(path.name+'-writing')
        temp.write_text('\n'.join(lines)+'\n',encoding='utf-8');os.replace(temp,path)
    except OSError:
        print('放行理由未能写入 '+OVERRIDES+'（不影响本次执行）',file=sys.stderr)
    note('command','retry',status='ok',reason_code='retry_override')


PATHISH=re.compile(r"""[^\s：:，,；;（）()“”"'「」、]*/[^\s：:，,；;（）()“”"'「」、]*""")


def error_signature(message):
    """报错类别指纹：先 NFC + casefold，路径（任何写法）一律换成 P，数字与长十六进制换成 #。"""
    text=PATHISH.sub('P',_norm_text(message))
    text=re.sub(r'[0-9a-f]{8,}|\d+','#',text)
    text=re.sub(r'\s+',' ',text).strip()[:160]
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _guard_io(root,update=None):
    with locked(root,create=True) as fd:
        try:data=json.loads(_read(fd,GUARD_NAME,65536))
        except FileNotFoundError:data={}
        if not isinstance(data,dict):data={}
        if update is None:return data
        update(data)
        keys=[k for k in data if not k.startswith('~')]
        if len(keys)>32:
            for key in sorted(keys,key=lambda k:data[k].get('updated',''))[:len(keys)-32]:data.pop(key)
        if isinstance(data.get('~used_overrides'),list):data['~used_overrides']=data['~used_overrides'][-USED_KEEP:]
        temp='.guard-writing'
        f=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(f,'wb') as out:out.write(_json(data))
        os.replace(temp,GUARD_NAME,src_dir_fd=fd,dst_dir_fd=fd)
        return data


def stop_report(cmd,count,message):
    return (f"【已停止重试】{cmd} 同类报错已连续 {count} 次。不要换参数或换花样再试，先停下报告：\n"
            f"1. 报错原文：{str(message).strip()[:400]}\n2. 已试做法：同一命令连续 {count} 次，报错类别相同\n"
            "3. 卡在哪/建议：查明原因（属工作包缺陷的记入复盘）；向策略师或主控说明后，"
            "设置 HARNESS_RETRY_REASON=\"已查明的原因（一句人话）\" 再执行一次；修正参数后的第 3 次也须写明原因（守卫按同一命令同一对象计数）。")


def cli(function,filename):
    argv=sys.argv[1:]
    def argument(flag):
        try:return argv[argv.index(flag)+1]
        except (ValueError,IndexError):return None
    root=argument('--workspace') or argument('--root')
    if root is None:return _cli_inner(function,filename)
    if os.environ.get('HARNESS_DIAGNOSTICS')=='0':return _cli_inner(function,filename)  # 关闭诊断时守卫也不读写
    override=os.environ.get('HARNESS_RETRY_REASON','').strip()
    holder={'key':command_key(filename,argv),'previous':None,'blocked':False,'checked':False}

    def check(key):
        holder.update(key=key,checked=True)
        try:previous=_guard_io(Path(root)).get(key) if (Path(root)/ROOT_NAME/GUARD_NAME).is_file() else None
        except Exception:previous=None  # 守卫尽力而为：诊断目录不可用时不改变业务结果
        holder['previous']=previous
        if not previous or previous.get('count',0)<GUARD_LIMIT or _legacy_probe(key,previous):return
        reused=False
        if override and reason_valid(override):
            digest=override_digest(key,override)
            def remember(data):
                used=data.get('~used_overrides') if isinstance(data.get('~used_overrides'),list) else []
                holder['reused']=digest in used
                if not holder['reused']:data['~used_overrides']=used+[digest]
            try:_guard_io(Path(root),remember)
            except Exception:holder['reused']=False
            reused=holder.get('reused')
            if not reused:
                record_override(root,key,previous,override);return
        holder['blocked']=True
        message=stop_report(key,previous['count'],'上次同类报错见前两次输出（守卫只存类别指纹，不存原文）')
        if reused:
            message+=('\n这句 HARNESS_RETRY_REASON 已对这条命令放行过一次，不能重复放行（放行只对当时那一次有效）；'
                      '查明新的原因、向策略师或主控说明后写一句新的说明')
        elif override:
            message+=('\n本次 HARNESS_RETRY_REASON 不算说明：须写一句查到的原因（至少 8 个字，例如“已查明：版本号写错，已核对当前版本”），'
                      '放行理由会记入 '+OVERRIDES)
        print(message,file=sys.stderr)
        raise RetryBlocked(3)

    import argparse
    original=argparse.ArgumentParser.parse_args
    def patched(self,args=None,namespace=None):
        if holder['checked']:return original(self,args,namespace)
        try:ns=original(self,args,namespace)
        except SystemExit as exc:
            if exc.code not in (None,0):check(command_key(filename,argv))  # 用法错误也计数，第 3 次同样被拦
            raise
        check(parsed_key(filename,ns,root))
        return ns
    argparse.ArgumentParser.parse_args=patched
    sink=[];out,err=sys.stdout,sys.stderr;sys.stdout=_Tee(out,sink);sys.stderr=_Tee(err,sink)
    error=None;result=None
    try:
        result=_cli_inner(function,filename);return result
    except BaseException as exc:
        error=exc;raise
    finally:
        argparse.ArgumentParser.parse_args=original
        sys.stdout,sys.stderr=out,err
        cmd=holder['key'];previous=holder['previous']
        code=getattr(error,'code',None) if isinstance(error,SystemExit) else None
        state_report=isinstance(error,StateReport)
        failed=(error is not None and not state_report and not (isinstance(error,SystemExit) and code in (None,0))) or (type(result) is int and result!=0)
        message=(str(error) if error is not None and not isinstance(error,SystemExit) or isinstance(code,str) else '')+''.join(sink)[-1200:]
        def update(data):
            if state_report:
                if _legacy_probe(cmd,data.get(cmd)):data.pop(cmd,None)
                return
            if not failed:
                data.pop(cmd,None);return
            signature=error_signature(message);old=data.get(cmd) or {}
            count=old.get('count',0)+1 if old.get('signature')==signature else 1
            data[cmd]={'signature':signature,'count':count,'rule':2,'updated':datetime.now(timezone.utc).isoformat(),
                       **({'override':True} if override else {})}
        sealed=False
        try:sealed=json.loads((Path(root)/ROOT_NAME/'index.json').read_text()).get('sealed') is True
        except (OSError,ValueError,AttributeError):pass
        if state_report and not _legacy_probe(cmd,previous):pass  # F08：状态探测不计数、不清零
        elif not holder['blocked'] and (failed or previous is not None) and not sealed:  # 被拦那次不再计数；封顶即停采
            try:
                data=_guard_io(Path(root),update)
                if failed and data.get(cmd,{}).get('count',0)>=GUARD_LIMIT:
                    print(stop_report(cmd,data[cmd]['count'],message[-400:]),file=err)
            except Exception:pass


def _cli_inner(function,filename):
    argv=sys.argv[1:]
    def argument(flag):
        try:return argv[argv.index(flag)+1]
        except (ValueError,IndexError):return None
    root=argument('--workspace') or argument('--root')
    if root is None:return function()
    run=os.environ.get('HARNESS_RUN_ID','')
    automatic=not bool(re.fullmatch(r'[0-9a-f]{32}',run))
    run=uuid.uuid4().hex if automatic else run
    key=argument('--key')
    task=argument('--task-id') or argument('--task') or (key.split('::',1)[0] if key and '::' in key else None) or os.environ.get('HARNESS_TASK_ID')
    state={'automatic':automatic,'root':Path(root),'run_id':run,'invocation_id':uuid.uuid4().hex,'span_id':uuid.uuid4().hex,
     'task_id':opaque(task),'dispatch_id':opaque(argument('--dispatch-id')),
     'host':detect_host(),
     'enabled':os.environ.get('HARNESS_DIAGNOSTICS')!='0','gap':None,'attempts':{}}
    token=_state.set(state);start=time.monotonic();error=None;result=None
    module=Path(filename).stem
    operation={'phase2_review':'phase2','inspect_contract':'phase2','build_schedule':'phase2','export_gantt':'phase2','phase3':'phase3','phase5':'phase5',
       'deck_browser':'deck_browser','deck_export':'deck_export','knowledge_connectors':'knowledge_connectors'}.get(module,'command')
    try:
        note(operation,'start')
        result=function();return result
    except BaseException as exc:
        error=exc
        from workspace_lock import WorkspaceBusy
        if isinstance(exc,WorkspaceBusy):
            print(json.dumps({'status':'busy','reason_code':exc.reason_code,'message':str(exc),'written':False},ensure_ascii=False))
            raise SystemExit(1) from None
        raise
    finally:
        failed=bool(error) or type(result) is int and result!=0
        action=state.get('formal_action')
        if action:
            note(action['operation'],'error' if failed else 'end',duration_ms=(time.monotonic()-action['start'])*1000,
                 status='error' if failed else 'ok',reason_code=getattr(error,'reason_code','workflow_error') if failed else action['reason'],
                 version=action['version'],mode=action['mode'],span_id=action['span_id'],parent_span_id=action['parent'])
            state['span_id']=action['parent']
        note(operation,'error' if failed else 'end',duration_ms=(time.monotonic()-start)*1000,
             status='error' if failed else 'ok',reason_code=getattr(error,'reason_code','workflow_error') if failed else 'none')
        _state.reset(token)
        if state['gap']:print('运行记录不完整：'+state['gap']+'；业务结果不受诊断影响。',file=sys.stderr)
