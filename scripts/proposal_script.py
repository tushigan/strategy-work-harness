"""Phase 5 proposal-script versions and their review/confirmation gates."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

import phase5_common as common
from phase2_store import WorkflowError
from proposal_input import claims, upstream

KIND = "proposal-script"
MIN_SPOKEN_CHARS = 40

JsonObject = dict[str, object]


def _json_input(value: object, label: str) -> JsonObject:
    if isinstance(value, (str, Path)):
        try:
            value = json.loads(Path(value).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WorkflowError(f"{label}必须是可读取的 JSON 对象：{exc}") from exc
    if not isinstance(value, Mapping):
        raise WorkflowError(f"{label}必须是 JSON 对象")
    return {str(key): item for key, item in value.items()}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label}必须是非空文字")
    return value.strip()

def _display(value: object, label: str) -> str:
    if isinstance(value, str):
        return _text(value, label)
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return "、".join(item.strip() for item in value)
    raise WorkflowError(f"{label}必须是非空文字或文字列表")

def _task_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise WorkflowError("task_id 须为字母、数字、短横线或下划线，最多96位")
    return value

def _validate_sections(value: object) -> list[JsonObject]:
    if not isinstance(value, list) or not value:
        raise WorkflowError("sections 必须是非空章节列表")
    result, seen = [], set()
    for number, raw in enumerate(value, 1):
        if not isinstance(raw, Mapping):
            raise WorkflowError(f"第{number}个章节必须是对象")
        section: JsonObject = {str(key): item for key, item in raw.items()}
        section_id = _text(section.get("id"), f"第{number}个章节的 id")
        if section_id in seen:
            raise WorkflowError(f"章节 id 重复：{section_id}")
        seen.add(section_id)
        _text(section.get("title"), f"章节 {section_id} 的 title")
        claims = section.get("claim_refs")
        if not isinstance(claims, list) or not claims:
            raise WorkflowError(f"章节 {section_id} 必须有非空 claim_refs")
        for claim in claims:
            valid_object = isinstance(claim, Mapping) and all(
                isinstance(claim.get(field), str) and claim[field].strip()
                for field in ("key", "section_id")
            )
            if not valid_object:
                raise WorkflowError(
                    f"章节 {section_id} 的 claim_refs 必须是包含 key 和 section_id 的对象"
                )
        spoken = _text(section.get("spoken_text"), f"章节 {section_id} 的 spoken_text")
        compact = re.sub(r"\s+", "", spoken)
        if len(compact) < MIN_SPOKEN_CHARS:
            raise WorkflowError(
                f"章节 {section_id} 的 spoken_text 过短，不能只有提纲短句（至少{MIN_SPOKEN_CHARS}个非空白字符）"
            )
        _text(section.get("transition"), f"章节 {section_id} 的 transition")
        result.append(section)
    return result

def render_markdown(data: Mapping[str, object], version: object = None,
                    dependencies: object = None) -> str:
    """把完整逐字稿渲染成可回读的 Markdown，不省略 spoken_text 或 transition。"""
    if not isinstance(data, Mapping):
        raise WorkflowError("逐字稿内容必须是 JSON 对象")
    value = data.get("version", version)
    version_label = f"v{value:04d}" if isinstance(value, int) else str(value or "待登记")
    deps = dependencies if dependencies is not None else data.get("upstream_fingerprints", [])
    lines = ["# 提案逐字稿", "", f"- 版本：{version_label}",
             f"- 听众：{_display(data.get('audience'), 'audience')}",
             f"- 目的：{_text(data.get('purpose'), 'purpose')}",
             "- 状态：候选稿；提案稿不等于客户确认。", "", "## 上游版本指纹"]
    lines.extend(f"- {json.dumps(dep, ensure_ascii=False, sort_keys=True)}" for dep in deps)
    for number, section in enumerate(data.get("sections", []), 1):
        lines.extend([f"## {number}. {section['title']}（{section['id']}）", "",
                      "### 转场", section["transition"], "", "### 完整逐字稿",
                      section["spoken_text"], "", "### 论断来源"])
        lines.extend(f"- {json.dumps(claim, ensure_ascii=False, sort_keys=True) if isinstance(claim, Mapping) else claim}"
                     for claim in section["claim_refs"])
        lines.append("")
    if data.get("revision"):
        lines.extend(["## 修订 metadata", "```json",
                      json.dumps(data["revision"], ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines) + "\n"


def _layout(task_id: str) -> dict[str, object]:
    base = f"project/outputs/proposal/{task_id}"
    return {
        "folder": f"{base}/versions",
        "files": lambda _version, content, _markdown: [
            ("payload", "json", content),
            ("markdown", "md", render_markdown(json.loads(content.decode("utf-8")).copy()).encode("utf-8")),
        ],
        "snapshot": lambda version, _role, suffix: f"{base}/versions/v{version:04d}.{suffix}",
        "current": lambda _role, suffix: f"{base}/proposal-script.{suffix}",
    }


def _payload_path(root: Path, meta: Mapping[str, object]) -> Path:
    files = meta.get("current_files", [])
    item = next((item for item in files if item.get("role") == "payload"), None)
    item = item or next((item for item in files if str(item.get("path", "")).endswith(".json")), None)
    if not item:
        raise WorkflowError("逐字稿版本缺少结构化 JSON 文件")
    return common.local(root, item["path"])


def _payload(root: Path, meta: Mapping[str, object]) -> JsonObject:
    try:
        value = common.read_json(_payload_path(root, meta))
    except (OSError, ValueError, KeyError) as exc:
        raise WorkflowError(f"逐字稿结构化文件不可读：{exc}") from exc
    if not isinstance(value, dict) or value.get("kind") != KIND:
        raise WorkflowError("当前版本不是 proposal-script 结构")
    return value


def _without_runtime(value: Mapping[str, object]) -> JsonObject:
    result = dict(value)
    result.pop("version", None)
    result.pop("revision", None)
    return result


def publish(root: str | Path, input: object, request: Mapping[str, object],
            author: str) -> JsonObject:
    """登记一版候选逐字稿；不会写 project/state.json 或确认记录。"""
    root = Path(root).resolve()
    data = _json_input(input, "逐字稿输入")
    if not isinstance(request, Mapping):
        raise WorkflowError("请求必须是 JSON 对象")
    task_id = _task_id(data.get("task_id"))
    _display(data.get("audience"), "audience")
    _text(data.get("title"), "title")
    _text(data.get("purpose"), "purpose")
    sections = _validate_sections(data.get("sections"))
    dependencies = upstream(root, request, task_id)
    claims(root, sections, dependencies)
    key = f"{task_id}::{KIND}"
    payload = dict(data)
    payload.update({"kind": KIND, "task_id": task_id, "sections": sections,
                    "upstream_fingerprints": dependencies})
    for field in ("version", "revision", "dependencies", "phase3_refs"):
        payload.pop(field, None)
    history = common.registry(root).get("artifacts", {}).get(key, [])
    previous = history[-1] if history else None
    if previous:
        for group in ("current_files", "snapshot_files"):
            for item in previous.get(group, []):
                path = common.local(root, item["path"])
                if not path.is_file() or common.sha(path) != item.get("sha256"):
                    raise WorkflowError(f"旧逐字稿版本文件已缺失或被修改：{item.get('path')}")
        if _without_runtime(_payload(root, previous)) == payload and previous.get("dependencies") == dependencies:
            return dict(previous)
        payload["revision"] = request.get("revision")
    elif request.get("revision"):
        raise WorkflowError("首次生成不能伪装成历史修订")
    layout = _layout(task_id)
    return common.publish(root, key=key, kind=KIND, task_id=task_id, author=author,
                          payload=payload, markdown=render_markdown(payload),
                          dependencies=dependencies, file_layout=layout,
                          revision=payload.get("revision"))


def _current_and_scope(root: Path, key: str) -> tuple[JsonObject, list[str]]:
    meta = common.current(root, key)
    data = _payload(root, meta)
    sections = _validate_sections(data.get("sections"))
    return meta, [section["id"] for section in sections]


def review_sources(root: str | Path, key: str) -> JsonObject:
    """列出独立检核实例必须完整阅读的逐字稿章节和来源。"""
    root = Path(root).resolve()
    meta, scope = _current_and_scope(root, key)
    data = _payload(root, meta)
    return {
        "target": common.ref(meta), "task_id": data["task_id"], "kind": KIND,
        "audience": data["audience"], "purpose": data["purpose"],
        "scope_required": scope,
        "chapters_required": [{"id": item["id"], "title": item["title"],
                                "fields": ["spoken_text", "transition", "claim_refs"]}
                               for item in data["sections"]],
        "checked_sources_required": common.required_sources(root, meta),
        "notice": "这是独立检核输入清单，不是通过结论；提案稿不等于客户确认。",
    }


def _check_scope(root: Path, meta: Mapping[str, object]) -> None:
    _, required = _current_and_scope(root, meta["key"])
    review = common.last_review(root, common.ref(meta))
    if not common.valid_event_file(root, review.get("evidence")):
        raise WorkflowError("独立报告原件已失效")
    report = common.read_json(common.local(root, review["evidence"]["path"]))
    scope = report.get("scope") if isinstance(report, Mapping) else None
    if not isinstance(scope, list) or not set(required).issubset(scope):
        missing = [item for item in required if not isinstance(scope, list) or item not in scope]
        raise WorkflowError(f"独立报告未覆盖完整逐字稿章节：{', '.join(missing)}")


def record_review(root: str | Path, key: str, report_path: str | Path) -> JsonObject:
    root = Path(root).resolve()
    _, scope = _current_and_scope(root, key)
    return common.record_review(root, key, report_path, required_scope=scope)


def gate(root: str | Path, key: str, human: bool = False) -> JsonObject:
    root = Path(root).resolve()
    meta = common.gate(root, key, human=human)
    _check_scope(root, meta)
    return meta


def confirm(root: str | Path, key: str, evidence: str | Path, actor: str,
            simulation: bool = False) -> JsonObject:
    if not isinstance(actor, str) or not actor.strip():
        raise WorkflowError("确认需要实际策略师身份")
    if not isinstance(simulation, bool):
        raise WorkflowError("simulation 必须是布尔值")
    gate(root, key)
    return common.confirm(Path(root).resolve(), key, evidence, actor, simulation)


def status(root: str | Path, key: str) -> JsonObject:
    result = common.status_for(Path(root).resolve(), key)
    result["notice"] = "提案稿不等于客户确认；正式 PPT 和客户确认不由本模块代替。"
    return result
