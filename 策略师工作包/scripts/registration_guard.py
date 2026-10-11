"""Validate writes and preserve idempotent retries without inventing host identity."""
import inspect
from functools import wraps
from standalone_store import digest
from phase2_store import WorkflowError
from workspace_lock import serialized


def actor_check(actor):
    if (not isinstance(actor, str) or not actor.strip() or actor.strip() in
            {'未登记实例', 'unknown', 'placeholder', '待填写', '未登记'}):
        raise WorkflowError('写入前须提供实际执行者 actor；只读 inspect 不需要。缺真实实例时保留待登记状态')
    return actor


def dispatch_write(function):
    signature = inspect.signature(function)
    @wraps(function)
    def run(*args, request_id=None, **kwargs):
        bound=signature.bind(*args, **kwargs); bound.apply_defaults()
        root=bound.arguments['root'];actor_check(bound.arguments['actor'])
        # v1.7.5 G03：return 新增的 ledger_refs 没给时不进指纹，升级前登记的回传原样重试仍认得出（指纹与 v1.7.4 相同）
        values={k:v for k,v in bound.arguments.items() if k!='root' and not (k=='ledger_refs' and v is None)}
        request = values.get('request', {})
        explicit=request_id or request.get('request_id')
        if explicit is not None and (not isinstance(explicit,str) or not explicit.strip()):
            raise WorkflowError('request_id 必须是非空文字')
        fingerprint=digest({'action':function.__name__,'arguments':values})
        default=f"{function.__name__}:{values.get('dispatch_id', fingerprint)}:{values['actor']}"
        with serialized(root):
            from agent_dispatch import read_log
            records=read_log(root)
            if function.__name__ in {'budget_reached','extend'} and not explicit:
                # 预算可多轮：每追加一次开启新一段预算（budget_reached 按已追加次数分段），每次到预算后可再追加一次
                # （extend 按已登记超限次数分段）。默认键不含记录者：换会话、换记录者重复同一句“继续”也只算一次；
                # extend 另按原话区分（同段里策略师另说一句追加算新的一次）。同一段内重试原样返回，不重复登记、不重复追加。
                event='budget_extended' if function.__name__=='budget_reached' else 'budget_reached'
                window=sum(1 for x in records if x.get('dispatch_id')==values.get('dispatch_id') and x.get('event')==event)
                import re,unicodedata
                said=re.sub(r'[\W_]+','',unicodedata.normalize('NFKC',str(values.get('words') or ''))).casefold()
                default=f"{function.__name__}:{values.get('dispatch_id')}:w{window}"+(':'+digest(said)[:16] if function.__name__=='extend' else '')
                fingerprint=digest({'action':function.__name__,'arguments':{k:v for k,v in values.items() if k not in {'actor','words'}},'words':said})
            key=explicit or default
            previous=next((x for x in records if x.get('request_key')==key),None)
            if function.__name__=='prepare' and not explicit:
                # 候选 r3 #5（v1.7.4 起就有）：同一份分派请求上一次准备的那条分派已取消（如宿主没派成、被新版取代）时，
                # 再准备新建一条分派，不把取消掉的那条当成“已准备”原样返回；仍在进行中的（或已有其他结局的）照旧复用。
                # 新建的那条用“原请求键 + 被取消分派编号”作键：同一次再准备原样重试仍认得出，再取消、再准备时依次往后接。
                # 候选 r4 #5：对所有角色（作者分派同样）都这样接，只在上一条是“已取消”时生效。
                from agent_dispatch import replay
                states=replay(records)
                while previous and states.get(previous.get('dispatch_id'),{}).get('status')=='cancelled':
                    key=f"{key}:after:{previous['dispatch_id']}"
                    previous=next((x for x in records if x.get('request_key')==key),None)
            if previous:
                if previous.get('request_hash')!=fingerprint:
                    if function.__name__=='extend' and not explicit:
                        raise WorkflowError('这一段预算已按同一句原话追加过，但追加量不同；回读原记录（agent_dispatch.py progress），确需改动请策略师另说一句')
                    raise WorkflowError('同一请求编号内容不同；请回读原记录或使用新的 request_id')
                from yunxing_rizhi import note,bind
                bind(previous.get('task_id') or next((x.get('task_id') for x in records if x.get('dispatch_id')==previous.get('dispatch_id') and x.get('event')=='prepared'),None),previous.get('dispatch_id'))
                note('agent_dispatch.'+('return_candidate' if function.__name__=='return_candidate' else function.__name__),'retry',reason_code='idempotent_request')
                return previous
            # Scope metadata is passed to append only within this transaction.
            from contextvars import ContextVar
            token=_request.set({'request_key':key,'request_hash':fingerprint})
            try: return function(*args, **kwargs)
            finally: _request.reset(token)
    return run

from contextvars import ContextVar
_request=ContextVar('dispatch_write_request', default=None)

def request_metadata():
    return _request.get() or {}
