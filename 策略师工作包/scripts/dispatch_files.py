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
        raise WorkflowError(f"{label}指纹与当前文件不符")
    # macOS may expose one temporary directory through two equivalent aliases.
    # Compare and store the canonical path so a valid local file is not rejected
    # merely because the workspace spelling differs.
    return {"path": path.resolve().relative_to(root.resolve()).as_posix(), "sha256": actual}


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


def reference_issues(root,refs):
    errors=[]
    from guidang import archived_index
    archived=archived_index(root)
    for label,value in refs:
        try:file_ref(root,value,label)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            # U13：已按授权归档到工作区外的历史文件判“已归档”，不是缺失。
            if isinstance(value,dict) and (value.get('path'),value.get('sha256')) in archived:continue
            path=value.get('path','未提供') if isinstance(value,dict) else str(value)
            errors.append(f'{label} {path}：{exc}')
    return errors


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
