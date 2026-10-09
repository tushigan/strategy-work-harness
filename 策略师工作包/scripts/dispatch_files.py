"""Local file fingerprints and readable evidence for Agent dispatches."""
from pathlib import Path
import re
import unicodedata

from phase2_store import WorkflowError, sha

# Default_Ignorable_Code_Point letters in Unicode DerivedCoreProperties.
IGNORABLE_LETTERS = frozenset("\u115f\u1160\u3164\uffa0")


def local(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or relative.startswith(("/", "~"))
            or "\\" in relative or re.match(r"^[A-Za-z]:", relative)):
        raise WorkflowError("分派引用必须是工作包内相对路径")
    if Path(relative).parts[:2] == ("project", "运行日志"):
        raise WorkflowError("诊断记录不能作为业务分派来源")
    raw = root / relative
    resolved = raw.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve()):
        raise WorkflowError("分派引用越界")
    current = raw
    while current != root:
        if current.is_symlink():
            raise WorkflowError("分派引用不能使用符号链接")
        current = current.parent
    return raw


def file_ref(root: Path, value: object, label: str) -> dict[str, str]:
    if isinstance(value, str):
        path_value, expected = value, None
    elif isinstance(value, dict):
        path_value, expected = value.get("path"), value.get("sha256")
    else:
        raise WorkflowError(f"{label}必须是路径或路径指纹对象")
    path = local(root, path_value)
    if path.is_symlink() or not path.is_file():
        raise WorkflowError(f"{label}必须是工作包内真实文件，不能是符号链接或目录")
    actual = sha(path)
    if expected is not None and expected != actual:
        raise WorkflowError(f"{label}指纹与当前文件不符" + records_hint(root, value))
    # macOS may expose one temporary directory through two equivalent aliases.
    # Compare and store the canonical path so a valid local file is not rejected
    # merely because the workspace spelling differs.
    return {"path": path.resolve().relative_to(root.resolve()).as_posix(), "sha256": actual}


def records_hint(root: Path, value: dict) -> str:
    """F09（v1.7.4 r2）：只追加的记录（如项目记忆）指纹不符时，说清是追加还是改写、该怎么办。"""
    from task_validation import is_records_path
    if not is_records_path(value.get("path")):
        return ""
    try:
        added = appended(root, value)
    except OSError:
        return ""
    if added:
        return (f"（{value['path']} 自登记后已追加 {added} 条、登记部分未变：检核派工会自动按前缀接受；"
                "其他分派把这条引用改成当前指纹或只写路径后重新准备分派，不必改任务记录）")
    return (f"（{value['path']} 是只追加的记录，登记那部分已被改写、删行或截断，不是单纯追加：先核对原因；"
            "确需采用新版，用 standalone_tasks.py save 显式 accept_changed_references 登记后再准备分派）")


def input_ref(root: Path, value: object) -> dict[str, str]:
    """F09（v1.7.4 r2）：分派输入文件里 project/records/*.jsonl 的登记指纹是当前文件按行边界的前缀（之后只追加）也收，
    按当前指纹登记（与 APPENDABLE 含“输入文件”同一口径：之后的核对照样按前缀判定）；其余照 file_ref 整文件核对。"""
    try:
        return file_ref(root, value, "输入文件")
    except WorkflowError:
        if isinstance(value, dict) and appended(root, value) is not None:
            return file_ref(root, value["path"], "输入文件")
        raise


def same_inputs(root: Path, refs: list, planned: list) -> bool:
    """检核派工的输入须精确对应累计计划；计划给的是登记指纹，records 输入按当前指纹登记，登记指纹是当前文件前缀即同一份。"""
    return len(refs) == len(planned) and all(
        r == p or (isinstance(p, dict) and r.get("path") == p.get("path") and appended(root, p) is not None)
        for r, p in zip(refs, planned))


def text_ref(root: Path, evidence: object) -> dict[str, str]:
    proof = file_ref(root, evidence, "主控文字证据")
    try:
        text = local(root, proof["path"]).read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise WorkflowError("主控证据须为可读、非空 UTF-8 文字记录") from exc
    controls = any(unicodedata.category(char) == "Cc" and char not in "\t\r\n" for char in text)
    meaningful = any(char not in IGNORABLE_LETTERS and unicodedata.category(char)[0] in "LN"
                     for char in text)
    if controls or not meaningful:
        raise WorkflowError("主控证据须为可读、非空 UTF-8 文字记录")
    return proof


def verify_refs(root: Path, item: dict) -> None:
    errors=reference_issues(root,required_refs(item))
    if errors:raise WorkflowError('；'.join(errors))


def required_refs(item):
    """Every supported dispatch proof, labelled before current/history filtering."""
    refs=[(label,r) for key,label in (('input_files','输入文件'),('candidates','回传候选'),
          ('takeover_refs','接手证据')) for r in item.get(key,[])]
    invocation=item.get('invocation_evidence')
    if invocation is not None:refs.append(('调用凭证',invocation))
    elif item.get('instance'):refs.append(('调用凭证',None))
    terminal=item.get('return_invocation_evidence')
    if terminal is not None:refs.append(('返回调用凭证',terminal))
    proof=item.get('readback_evidence')
    if proof is not None:
        if isinstance(proof,dict) and proof.get('kind')=='readback_summary':
            refs.extend(('回读原件',r) for r in proof.get('references',[]))
            if not proof.get('references'):refs.append(('回读原件',None))
        else:refs.append(('回读凭证',proof))
    elif item.get('readback_verified'):refs.append(('回读凭证',None))
    return refs


# F06（v1.7.3 r2）：前缀宽容只对输入 / 来源类引用；调用、返回调用、回读凭证与回传候选照旧整文件核对。
APPENDABLE={'输入文件','接手证据','回读原件'}


def reference_issues(root,refs):
    errors=[]
    from guidang import archived_index
    archived=archived_index(root)
    for label,value in refs:
        try:file_ref(root,value,label)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            # U13：已按授权归档到工作区外的历史文件判“已归档”，不是缺失。
            if isinstance(value,dict) and (value.get('path'),value.get('sha256')) in archived:continue
            # F06：只追加的 project/records/*.jsonl，登记部分原样、之后只是追加，不算指纹不符（限 APPENDABLE）。
            try:
                if label in APPENDABLE and isinstance(value,dict) and appended(root,value) is not None:continue
            except OSError as again:
                exc=again  # 读记录出错：报读文件的原因，不报成指纹不符
            path=value.get('path','未提供') if isinstance(value,dict) else str(value)
            errors.append(f'{label} {path}：{exc}')
    return errors


def appended(root,value):
    """F06：引用的是 project/records/*.jsonl 且登记指纹是当前文件按行边界的某个前缀 → 返回之后追加的条数，否则 None。
    读文件出错（OSError）不吞，交呼叫方按各自的 OSError 分支报原因（v1.7.4 r2）；引用结构不对仍返回 None。"""
    from task_validation import appended_records
    try:
        path=local(root,value.get('path'))
    except (ValueError,KeyError,TypeError,AttributeError):
        return None
    if path.is_symlink() or not path.is_file():return None
    try:
        return appended_records(path,value['path'],value.get('sha256'))
    except (KeyError,TypeError):
        return None


def readback_ref(root, evidence, candidates):
    """Optional inline readback; retain original text-file evidence compatibility."""
    if not isinstance(evidence,dict) or 'summary' not in evidence:
        return text_ref(root,evidence)
    if set(evidence)!={'summary','references'} or not isinstance(evidence['summary'],str) or not evidence['summary'].strip():
        raise WorkflowError('简短回读须有非空summary和references原件引用')
    if not isinstance(evidence['references'],list) or not evidence['references']:
        raise WorkflowError('简短回读须引用实际核对的原件')
    refs=[file_ref(root,value,'回读原件') for value in evidence['references']]
    actual={(x['path'],x['sha256']) for x in refs}
    if not {(x['path'],x['sha256']) for x in candidates}<=actual:
        raise WorkflowError('简短回读必须覆盖全部回传候选原件')
    return {'kind':'readback_summary','summary':evidence['summary'],'references':refs}
