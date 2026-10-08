"""Lossless, explicit mapping from the Phase 3 brand-house schema."""
import copy
import uuid

from phase2_store import WorkflowError, now
from phase3_document import SECTIONS

SUPPORT = {"evidence": "支持证据", "counterevidence": "反证",
           "assumptions": "假设", "verification": "待验证"}
STYLE = {"fontFamily": "system", "fontSize": 18, "fontWeight": 400,
         "color": "#202424", "textAlign": "left", "lineHeight": 1.6}
LAYOUT = {"columns": 3, "width": 1200, "gap": 12, "padding": 16}


def text_at(document, path):
    value = document
    for part in path:
        value = value[part]
    if not isinstance(value, str):
        raise WorkflowError("展示映射必须指向正文中的真实文字")
    return value


def make_fields(document):
    """Return (field definitions, snapshot); fail on unhandled schema changes."""
    allowed = {"task_id", "kind", "title", "mode", "brief_type", "sections",
               "gaps", "questions", "revision", "template_notice"}
    if not isinstance(document, dict) or document.get("kind") != "brand-house":
        raise WorkflowError("HTML 只能从真实的品牌屋正文生成")
    if set(document) - allowed:
        raise WorkflowError("正文出现未映射字段，先明确完整映射，不能默默漏段")
    sections = document.get("sections")
    if not isinstance(sections, list) or any(not isinstance(s, dict) for s in sections):
        raise WorkflowError("品牌屋章节格式无效")
    if [s.get("id") for s in sections] != SECTIONS["brand-house"]:
        raise WorkflowError("品牌屋必须完整包含约定的11章，且顺序一致")
    definitions, values = [], {}

    def add(key, group, role, label, path=None, text=None):
        content = text_at(document, path) if path is not None else text
        if not isinstance(content, str):
            raise WorkflowError("字段文字无效")
        style = {**STYLE}
        if role == "title":
            style.update(fontSize=28 if group == "header" else 22, fontWeight=700)
        elif role == "label":
            style.update(fontSize=14, fontWeight=600)
        elif role == "support":
            style.update(fontSize=14)
        definitions.append({"key": key, "group": group, "role": role,
                            "label": label, "path": path})
        values[key] = {"text": content, "style": style}

    add("model_label", "header", "label", "模型名称", text="品牌屋")
    add("title", "header", "title", "标题", ["title"])
    for index, section in enumerate(sections):
        if set(section) != {"id", "title", "text", *SUPPORT}:
            raise WorkflowError("章节 schema 改变，必须先补齐明确映射")
        group = section["id"]
        for field, role, label in (("title", "title", "栏目标题"), ("text", "body", "正文")):
            add(f"{group}_{field}", group, role, label, ["sections", index, field])
        for name, label in SUPPORT.items():
            if not isinstance(section[name], list):
                raise WorkflowError("证据、反证、假设、验证必须是列表")
            add(f"{group}_{name}_label", group, "label", label, text=label)
            for item, _ in enumerate(section[name]):
                add(f"{group}_{name}_{item}", group, "support", label,
                    ["sections", index, name, item])
    if not isinstance(document.get("gaps"), list) or not isinstance(document.get("questions"), list):
        raise WorkflowError("缺口和思考问题必须完整提供")
    if document["gaps"]:
        add("gaps_label", "notes", "label", "缺口与边界", text="缺口与边界")
    for index, gap in enumerate(document["gaps"]):
        if not isinstance(gap, dict) or set(gap) != {"question", "cannot_do", "can_do", "blocks_completion"}:
            raise WorkflowError("缺口 schema 改变，必须先补齐明确映射")
        if not isinstance(gap["blocks_completion"], bool):
            raise WorkflowError("缺口必须明确是否阻断完成")
        for name, label in (("question", "缺口"), ("cannot_do", "不能做"), ("can_do", "仍可做")):
            add(f"gap_{index}_{name}_label", "notes", "label", label, text=label)
            add(f"gap_{index}_{name}", "notes", "body", label, ["gaps", index, name])
        add(f"gap_{index}_blocking_label", "notes", "label", "原始缺口状态",
            text="原始缺口：阻断完成" if gap["blocks_completion"] else "原始缺口：不阻断完成")
    if document["questions"]:
        add("questions_label", "notes", "label", "下一步思考", text="下一步思考")
    for index, _ in enumerate(document["questions"]):
        add(f"question_{index}", "notes", "body", "思考问题", ["questions", index])
    return definitions, {"fields": values, "layout": {**LAYOUT}}


def make_data(project_id, meta, document):
    from phase3_store import ref
    fields, snapshot = make_fields(document)
    revision = str(uuid.uuid4())
    return {"schema_version": "1.0", "document_id": str(uuid.uuid4()),
            "project_id": project_id, "task_id": meta["task_id"],
            "source": {"body": ref(meta), "dependencies": copy.deepcopy(meta["dependencies"]),
                       "document": copy.deepcopy(document)}, "fields": fields, "current": revision,
            "history": [{"id": revision, "version": 1, "parent": None, "created_at": now(),
                         "kind": "generated", "note": "依据第三阶段正文生成；尚未独立检核 HTML",
                         "restored_from": None, "snapshot": snapshot}], "events": [], "branches": []}


def content(snapshot):
    return {key: value["text"] for key, value in sorted(snapshot["fields"].items())}


def compare(before, after):
    keys = sorted(before["fields"])
    return {"content": [k for k in keys if before["fields"][k]["text"] != after["fields"][k]["text"]],
            "style": [k for k in keys if before["fields"][k]["style"] != after["fields"][k]["style"]],
            "layout": before["layout"] != after["layout"]}


def mapped_document(data):
    document = copy.deepcopy(data["source"]["document"])
    snapshot = data["history"][-1]["snapshot"]
    for field in data["fields"]:
        path = field["path"]
        if path is None:
            continue
        value = document
        for part in path[:-1]:
            value = value[part]
        value[path[-1]] = snapshot["fields"][field["key"]]["text"]
    return document
