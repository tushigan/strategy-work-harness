"""Full, portable source records. Source tier is not a truth score."""
from datetime import date
from pathlib import Path
import re

from phase2_store import (WorkflowError, archive, atomic_json, fingerprint,
                         local, now, read_json, sha)
from workspace_lock import serialized

INDEX = "project/records/phase3-sources.json"
CLASSES = {"fact", "opinion", "inference", "hypothesis", "future_capability"}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise WorkflowError("编号须为字母、数字、短横线或下划线，最多96位")
    return value


def catalog(root):
    path = local(root, INDEX)
    return read_json(path) if path.exists() else {"schema_version": "0.3", "sources": {}}


def source_ref(meta):
    return {"space": "source", "key": meta["source_id"],
            "version": meta["version"], "sha256": meta["sha256"]}


def file_valid(root, item):
    if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
        return False
    path = local(root, item["path"])
    return path.is_file() and sha(path) == item.get("sha256")


def source_current(root, source_id):
    identifier(source_id)
    history = catalog(root)["sources"].get(source_id, [])
    if not history:
        raise WorkflowError(f"缺少来源 {source_id}")
    meta = history[-1]
    if not file_valid(root, meta) or not file_valid(root, meta["original"]):
        raise WorkflowError(f"来源 {source_id} 原件或全量提取记录改变/丢失")
    data = read_json(local(root, meta["path"]))
    if data.get("original") != meta["original"] or data.get("metadata", {}).get("source_id") != source_id:
        raise WorkflowError("来源登记与提取记录的原件或编号不符")
    if "verified_ranges" in data:
        from source_verification import validate_verifications
        validate_verifications(root, meta, data, history)
    return meta


def read_units(path):
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".srt", ".vtt", ".csv"}:
        text = path.read_text(encoding="utf-8-sig")
        units = []
        for number, line in enumerate(text.splitlines(keepends=True), 1):
            timestamp = re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\b", line)
            units.append({"location": f"第{number}行", "text": line,
                          "timestamp": timestamp.group() if timestamp else None})
        return units, [] if text.strip() else [{"code": "empty", "hard_block": True}]
    if suffix in {".pdf", ".xlsx", ".xlsm"}:
        from contract_reader import pdf_units, xlsx_units
        return pdf_units(path) if suffix == ".pdf" else xlsx_units(path)
    raise WorkflowError("研究原文支持 UTF-8 TXT/MD/SRT/VTT/CSV、文字 PDF、XLSX/XLSM；音频需先可靠转写")


def register_source(root, source_path, metadata, actor):
    with serialized(root):
        return _register_source_unlocked(root, source_path, metadata, actor)


def _register_source_unlocked(root, source_path, metadata, actor):
    if not isinstance(metadata, dict):
        raise WorkflowError("来源元数据必须是 JSON 对象")
    source_id = identifier(metadata.get("source_id"))
    state = read_json(local(root, "project/state.json"))
    if not state.get("project_id") or not actor.strip():
        raise WorkflowError("先初始化项目并提供采集登记者")
    required = ("title", "owner", "collected_at", "origin", "scope")
    if any(not isinstance(metadata.get(k), str) or not metadata[k].strip() for k in required):
        raise WorkflowError("来源缺标题、归属、时间、出处或适用范围")
    date.fromisoformat(metadata["collected_at"])
    if metadata.get("tier") not in (1, 2, 3):
        raise WorkflowError("来源层级只允许 1/2/3，不表示自动可信")
    relation = metadata.get("relation", "current_project")
    if relation not in {"current_project", "method_reference"}:
        raise WorkflowError("资料关系只能是本项目或跨项目方法参考")
    if relation == "current_project" and metadata.get("project_id") != state["project_id"]:
        raise WorkflowError("不能把其他项目资料登记为当前项目事实")
    locator = metadata.get("external_locator")
    if metadata["tier"] == 3 and (not isinstance(locator, str) or not locator.strip()):
        raise WorkflowError("公开资料须提供可回查网址或报告名称/出版信息")
    original = archive(root, source_path, "evidence")
    units, issues = read_units(local(root, original["path"]))
    data = {"schema_version": "0.3", "metadata": {**metadata, "relation": relation},
            "original": original, "units": units, "issues": issues,
            "extraction_complete": not bool(issues), "unit_count": len(units)}
    reg = catalog(root)
    history = reg["sources"].setdefault(source_id, [])
    if history and file_valid(root, history[-1]):
        previous = read_json(local(root, history[-1]["path"]))
        if {k: v for k, v in previous.items() if k != "verified_ranges"} == data:
            return source_current(root, source_id)
    return _save_source(root, data, actor, reg)


def _save_source(root, data, actor, reg):
    """Caller holds the workspace lock; snapshots are never overwritten."""
    source_id = data["metadata"]["source_id"]
    history = reg["sources"].setdefault(source_id, [])
    digest = fingerprint(data)
    if history and history[-1]["sha256"] == digest:
        return source_current(root, source_id)
    folder = local(root, f"project/research/sources/{source_id}")
    used = [int(p.stem[1:]) for p in folder.glob("v*.json") if re.fullmatch(r"v\d+", p.stem)]
    version = max([m["version"] for m in history] + used + [0]) + 1
    relative = f"project/research/sources/{source_id}/v{version:04d}.json"
    atomic_json(local(root, relative), data)
    meta = {"source_id": source_id, "version": version, "path": relative, "sha256": digest,
            "original": data["original"], "registered_at": now(), "actor": actor}
    history.append(meta)
    atomic_json(local(root, INDEX), reg)
    return meta


def verify_entry(root, entry):
    required = ("id", "statement", "location", "quote", "limits")
    if any(not isinstance(entry.get(k), str) or not entry[k].strip() for k in required):
        raise WorkflowError("证据需编号、判断、原文定位、逐字引用和适用限制")
    identifier(entry["id"])
    if entry.get("classification") not in CLASSES:
        raise WorkflowError("证据须区分事实、意见、推断、假设和待建设能力")
    supplied = entry.get("source", {})
    if not isinstance(supplied, dict):
        raise WorkflowError("证据的来源引用必须是对象")
    meta = source_current(root, supplied.get("key"))
    if type(supplied.get("version")) is not int or supplied != source_ref(meta):
        raise WorkflowError("证据引用不是当前来源版本")
    data = read_json(local(root, meta["path"]))
    if data["metadata"]["relation"] == "method_reference":
        raise WorkflowError("跨项目方法不能作为本项目证据条目")
    if "verified_ranges" in data:
        unit = next((u for u in data["verified_ranges"] if u["location"] == entry["location"]), None)
        if unit is None:
            raise WorkflowError("引用范围未核验；只可使用 source-status 列出的完整核验定位")
    else:
        if not data["extraction_complete"]:
            raise WorkflowError("原文提取不完整；用 source-verify 按页或单元格核验转录后再用于正式证据")
        unit = next((u for u in data["units"] if u["location"] == entry["location"]), None)
    if unit is None or entry["quote"] not in unit["text"]:
        raise WorkflowError("证据引用与原文位置/核验转录的逐字引文不符")
    return source_ref(meta)


def source_files(root, reference):
    meta = source_current(root, reference["key"])
    if type(reference.get("version")) is not int or reference != source_ref(meta):
        raise WorkflowError("来源版本已更新，需对齐")
    from source_verification import verification_files
    files = [{"path": meta["path"], "sha256": meta["sha256"]}, meta["original"],
             *verification_files(read_json(local(root, meta["path"])))]
    return list({(f["path"], f["sha256"]): f for f in files}.values())
