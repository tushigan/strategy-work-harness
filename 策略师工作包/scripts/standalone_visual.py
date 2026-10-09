"""Passing v2 standalone visual reports must cover current HTML/PDF independently.

W01：必看页实际观察 + 其余页“继承视觉覆盖”。继承页只能是程序比对得出的像素未变页，绑定基线报告、
基线指纹记录与当前指纹记录；登记新报告时重新渲染当前成品核对指纹。阶段首次检核仍全页。
同一任务里同名（或唯一一对）HTML 与 PDF 由程序逐页比对文字，不一致即拒绝。"""
from html.parser import HTMLParser
from pathlib import Path
from phase2_store import WorkflowError,local
from phase5_common import sha,test_mode
from phase5_visual import image_evidence

class Sections(HTMLParser):
    """页 = 文档里每个 <section>（文档顺序），与渲染程序一致：<template>、<noscript> 里的不算（浏览器运行脚本时不生成）。"""
    def __init__(self):super().__init__();self.pages=[];self.inert=0
    def handle_starttag(self,tag,attrs):
        if tag in ('template','noscript'):self.inert+=1
        elif tag=='section' and not self.inert:self.pages.append(dict(attrs).get('id') or str(len(self.pages)+1))
    def handle_endtag(self,tag):
        if tag in ('template','noscript') and self.inert:self.inert-=1


def required_pages(path):
    if path.suffix.lower()=='.pdf':
        from pypdf import PdfReader
        return [str(i) for i in range(1,len(PdfReader(path).pages)+1)]
    parsed=Sections();parsed.feed(path.read_text(encoding='utf-8'))
    pages=parsed.pages or ['document']
    if len(set(pages))!=len(pages):raise WorkflowError('页面标识不唯一，须建立稳定映射或全页重查')
    return pages


ENTRY_FIELDS={'artifact','method','pages'}
INCREMENT_FIELDS={'fingerprint','base_review','inherited_pages'}


def _record(root,entry,artifact,verify_render):
    import shijue_zengliang as z
    ref=entry.get('fingerprint')
    if ref is None:return None
    try:
        return z.verify(root,ref,artifact) if verify_render else z.read_record(root,ref,artifact)
    except z.Unavailable as exc:
        raise WorkflowError(f'无法重新渲染核对像素指纹（{exc}）；去掉 fingerprint/inherited_pages 按全页登记') from None


def _inherited(root,target,report,entry,artifact,record):
    import shijue_zengliang as z
    claimed=entry.get('inherited_pages',[])
    if not isinstance(claimed,list):raise WorkflowError('inherited_pages 必须为列表')
    if not claimed:
        if entry.get('base_review') is not None:raise WorkflowError('没有继承页时不能填写 base_review')
        return set()
    if record is None:raise WorkflowError('继承页须绑定程序生成的当前指纹记录（fingerprint），不能自报')
    if report.get('report_schema_version')!=2 or target is None:raise WorkflowError('视觉继承只用于第2版报告')
    purpose=z.dispatched_purpose(root,target,report.get('reviewer_instance'))
    if purpose is None:raise WorkflowError('找不到本次检核的派工记录（agent_dispatch 分派或 jianhe_zhunbei 请求），用途无法核实，不能继承；按全页登记')
    if purpose=='stage':raise WorkflowError('阶段首次检核须全页视觉检查，不能继承（以派工记录的用途为准）')
    kind='pdf' if artifact['path'].lower().endswith('.pdf') else 'html'
    base_review,base_record,reason,flagged=z.standalone_base(root,target,artifact['path'],kind,purpose,report.get('simulation'))
    reason=reason or z.page_mismatch(record,required_pages(local(root,artifact['path'])))
    if reason:raise WorkflowError(f'不能继承视觉结论：{reason}')
    if entry.get('base_review')!=base_review:
        raise WorkflowError('继承须绑定程序认定的基线报告与基线指纹（最近一份通过检核），引用不符')
    allowed={(x['page'],x['pixel_sha256']) for x in z.compare(base_record,record,flagged=flagged)['inherited']}
    pages=set()
    for item in claimed:
        if not isinstance(item,dict) or set(item)!={'page','pixel_sha256'} or (str(item['page']),item['pixel_sha256']) not in allowed:
            raise WorkflowError(f'继承页 {item.get("page") if isinstance(item,dict) else item} 不是程序比对得出的像素未变页')
        if str(item['page']) in pages:raise WorkflowError('继承页重复')
        pages.add(str(item['page']))
    from phase2_store import read_json
    base=read_json(local(root,base_review['evidence']['path']))
    pending=[f for f in base.get('findings',[]) if isinstance(f,dict) and f.get('level')=='yellow']
    if any(f not in report.get('findings',[]) for f in pending):
        raise WorkflowError('继承视觉结论不能丢弃基线未解决的黄灯意见；须保留意见或全页实看')
    return pages


def _manual_same_version(root,entries,html,pdf,reason):
    """A11：没有本机浏览器渲染时，HTML 与 PDF 同版由检核员逐页人工确认并在报告该 HTML 条目里记录。"""
    entry=next((e for e in entries if isinstance(e,dict) and e.get('artifact')=={'path':html['path'],'sha256':html['sha256']}),{})
    manual=entry.get('same_version_manual')
    pages=required_pages(local(root,html['path']))
    if (not isinstance(manual,dict) or set(manual)!={'pdf','reason','pages','observation'} or manual['pdf']!=pdf['path']
            or not isinstance(manual['reason'],str) or '程序比对不可用' not in manual['reason']
            or not isinstance(manual['pages'],list) or [str(x) for x in manual['pages']]!=pages
            or not isinstance(manual['observation'],str) or not manual['observation'].strip()
            or manual['observation'].strip().startswith('填写')):  # r3：模板占位原文不算人工确认
        raise WorkflowError(f'无法程序核对 {html["path"]} 与 {pdf["path"]} 是否同一版（{reason}）；检核员逐页人工确认后在该 HTML 条目写 '
                            'same_version_manual：{pdf, reason:"程序比对不可用…", pages:[全部页], observation}')


NOTICES=[]  # 本次登记给主控的提示（standalone_review 输出 notices）


def _same_version(root,visuals,records,verify_render,entries):
    """HTML 与 PDF 是否同一版由程序逐页比对文字（登记新报告时执行）；缺渲染条件时改为检核员逐页人工确认（记录在报告里）。"""
    if not verify_render:return
    import shijue_zengliang as z
    pairs,unpaired=z.pair_documents(visuals)
    if unpaired:  # A13（r3）：登记时配不上对也提示，不静默跳过
        NOTICES.append('这些 HTML/PDF 配不上对（不同名且不止一对），程序没有核对它们是否同一版：'+'、'.join(unpaired)+'；改成同名或由检核员人工确认')
    for html,pdf in pairs:
        found={}
        try:
            for item in (html,pdf):
                record=records.get(item['path'])
                if record is None:
                    try:record=z.fingerprint(root,item['path'])[1]
                    except z.Unavailable:
                        if item is pdf:record=z.pdf_text_record(local(root,item['path']))
                        else:raise
                found[item['path']]=record
        except z.Unavailable as exc:
            _manual_same_version(root,entries,html,pdf,exc);continue
        problems=z.text_mismatch(found[html['path']],found[pdf['path']])
        if problems:
            raise WorkflowError(f'{html["path"]} 与 {pdf["path"]} 不是同一版：'+'；'.join(problems[:5]))


def _purpose_matches(root,target,report):
    """A6：报告用途须与派工记录一致（有记录时）。"""
    if target is None or report.get('report_schema_version')!=2:return
    import shijue_zengliang as z
    recorded=z.dispatched_purpose(root,target,report.get('reviewer_instance'))
    if recorded is not None and report.get('review_purpose')!=recorded:
        note='，阶段首次检核须全页' if 'stage' in (recorded,report.get('review_purpose')) else ''
        raise WorkflowError(f'报告用途（{report.get("review_purpose")}）与派工记录（{recorded}）不一致{note}，不能登记')


def _visuals(meta):
    return [x for x in meta['task']['deliverables'] if 'path' in x and Path(x['path']).suffix.lower() in {'.html','.htm','.pdf'}]


def _bound_entry(root,report,artifact):
    """字段检查（不读成品、不渲染）：该 HTML/PDF 恰有一条绑定当前成品指纹的视觉证据、方法有效、有逐页记录。
    validate 与 resume 的静态判断（static_problem）共用这一处，不另写规则。"""
    entries=report.get('visual_evidence',[])
    matches=[e for e in entries if isinstance(e,dict) and e.get('artifact')=={'path':artifact['path'],'sha256':artifact['sha256']}] if isinstance(entries,list) else []
    if len(matches)!=1:raise WorkflowError('HTML/PDF 通过须绑定当前成品的逐页实际视觉证据')
    entry=matches[0];method=entry.get('method')
    if not isinstance(method,str) or not method.strip() or ('synthetic' in method.lower() and not (report['simulation'] and test_mode(root))):
        raise WorkflowError('视觉方法无效，合成截图不得用于真实通过')
    if not isinstance(entry.get('pages'),list) or not (entry['pages'] or entry.get('inherited_pages')):
        raise WorkflowError('缺逐页视觉观察')
    return entry


def static_problem(root,meta,report):
    """F07（v1.7.3）：resume 轻量核对用。按 gate 的当前规则做静态判断——报告格式版本与每个 HTML/PDF 的视觉证据字段，
    不渲染、不读来源与成品原件。能过返回 None，否则返回原因（与 gate 拒绝时的原文相同）。"""
    from incremental_review import schema
    try:
        schema(report)
        for artifact in _visuals(meta):_bound_entry(root,report,artifact)
    except WorkflowError as exc:
        return str(exc)
    return None


def validate(root,meta,report,*,target=None,verify_render=False):
    _purpose_matches(root,target,report)
    visuals=_visuals(meta)
    entries=report.get('visual_evidence',[])
    records={}
    for artifact in visuals:
        entry=_bound_entry(root,report,artifact)
        expected=set(required_pages(local(root,artifact['path'])));seen=set();digests=set()
        record=_record(root,entry,artifact,verify_render)
        if record is not None:records[artifact['path']]=record
        inherited=_inherited(root,target,report,entry,artifact,record)
        for page in entry['pages']:
            if not isinstance(page,dict) or set(page)!={'page','file','observation'} or str(page['page']) not in expected or not isinstance(page['observation'],str) or not page['observation'].strip():
                raise WorkflowError('逐页视觉记录字段或范围无效')
            image_evidence(root,page['file']);digest=page['file']['sha256']
            if digest in digests or str(page['page']) in seen:raise WorkflowError('不能重复截图冒充不同页面')
            digests.add(digest);seen.add(str(page['page']))
        if seen&inherited:raise WorkflowError('继承页不能同时写成本轮已看：'+'、'.join(sorted(seen&inherited)))
        if seen|inherited!=expected:
            missing=sorted(expected-seen-inherited)
            raise WorkflowError('新增或变动页面未覆盖；必看页须实际观察，其余页只能由程序比对继承'+(f'（缺 {"、".join(missing[:8])}）' if missing else ''))
    _same_version(root,visuals,records,verify_render,entries)
