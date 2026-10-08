"""Validate task-specific upstream inputs and chapter source pointers."""
import re

import phase5_common as common
from phase2_store import WorkflowError
from phase3_review import gate
from phase3_store import task


def upstream(root, request, task_id):
    values = request.get("phase3_refs")
    if not isinstance(values, list) or len(values) != 3:
        raise WorkflowError("逐字稿须明确绑定品牌屋、推导和本任务简报三个版本")
    planned = task(root, task_id)
    if planned["title"] != "内部提案":
        raise WorkflowError("逐字稿只能关联计划中的内部提案任务")
    from phase3_tasks import execution_hold
    if execution_hold(planned):
        raise WorkflowError(execution_hold(planned))
    metas = {}
    for reference in values:
        if not isinstance(reference, dict) or set(reference) != {"space", "key", "version", "sha256"} or (
                reference.get("space") != "phase3" or type(reference.get("version")) is not int or
                reference["version"] < 1 or not isinstance(reference.get("key"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))):
            raise WorkflowError("上游引用须包含正确类别、版本号及完整指纹")
        meta = common.resolve(root, reference)
        kind = meta["kind"]
        if kind not in {"brand-house", "derivation", "brief"} or kind in metas:
            raise WorkflowError("上游类别错误或重复")
        gate(root, meta["key"], human=kind != "brief")
        data = common.read_json(common.local(root, meta["path"]))
        if any(g["blocks_completion"] for g in data["gaps"]):
            raise WorkflowError("上游存在阻断性缺口，不能制作正式提案")
        metas[kind] = meta
    if metas["brief"]["task_id"] != task_id or metas["brand-house"]["task_id"] != metas["derivation"]["task_id"] or (
            metas["brand-house"]["task_id"] not in planned["dependencies"]):
        raise WorkflowError("品牌屋、推导或简报不属于本任务指定的上游")
    return values


def claims(root, sections, dependencies):
    originals = {}
    for reference in dependencies:
        meta = common.resolve(root, reference)
        data = common.read_json(common.local(root, meta["path"]))
        originals[meta["key"]] = {s["id"] for s in data["sections"]}
    for section in sections:
        for pointer in section["claim_refs"]:
            if not isinstance(pointer, dict) or pointer.get("key") not in originals or (
                    pointer.get("section_id") not in originals[pointer["key"]]):
                raise WorkflowError(f"章节 {section['id']} 的论断须指向实际上游章节")
