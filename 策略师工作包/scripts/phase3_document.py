"""Structured content validation and readable Markdown, not a strategy generator."""
from phase2_store import WorkflowError
from phase3_sources import identifier, verify_entry

SECTIONS = {
    "brief": ["problem", "objectives", "audience", "scenario", "confirmed_strategy",
              "facts", "judgments", "assumptions", "key_questions", "deliverables",
              "constraints", "exclusions", "acceptance", "people_dates", "missing_inputs"],
    "research-plan": ["research_objective", "executive_interview", "enterprise_materials",
                      "factory_supply_chain", "terminal", "dealer", "desk_research",
                      "reuse", "gaps"],
    "evidence": ["coverage", "source_tiers", "conflicts", "limits", "gaps"],
    "analysis": ["problem", "findings", "contradictions", "alternatives", "recommendation", "gaps"],
    "brand-house": ["category", "audience", "differentiated_value", "positioning",
                    "mission", "vision", "values", "rtb", "personality", "proposition", "slogan"],
    "derivation": ["positioning_triangle", "identity", "mvv", "value_to_rtb",
                   "personality_proposition", "slogan_translation", "counterevidence", "validation"],
    "summary": ["completed", "decisions", "risks", "next_actions"],
}
DESIGN_FIELDS = ("consumer_benefit", "differentiated_claim", "claim_evidence",
                 "information_priority", "mandatory_information", "red_lines",
                 "misreading_risks", "creative_freedom", "observable_checks")
DESIGN_TYPES = {"brand_tone_upgrade", "packaging", "terminal_material", "poster"}
CHAIN = {"research-plan": "brief", "evidence": "brief", "analysis": "evidence",
         "brand-house": "analysis", "derivation": "brand-house", "summary": "brief"}


def validate(root, data, evidence_pool):
    if not isinstance(data, dict) or data.get("kind") not in SECTIONS:
        raise WorkflowError("未知业务产出类别")
    identifier(data.get("task_id"))
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise WorkflowError("缺产出标题")
    if data.get("mode") not in {"draft", "hypothesis"}:
        raise WorkflowError("产出须明确 draft 或 hypothesis；不能自行声明已确认")
    if "design" in data and not isinstance(data["design"], dict):
        raise WorkflowError("设计策略转换清单必须是对象")
    sections = data.get("sections")
    if not isinstance(sections, list) or any(not isinstance(s, dict) for s in sections):
        raise WorkflowError("正文需要结构化章节")
    ids = [s.get("id") for s in sections]
    if ids != SECTIONS[data["kind"]]:
        raise WorkflowError(f"章节缺失、重复或顺序错误：{SECTIONS[data['kind']]}")
    for section in sections:
        if any(not isinstance(section.get(k), str) or not section[k].strip() for k in ("title", "text")):
            raise WorkflowError("每章需标题和实际正文，不能仅有字段")
        for key in ("evidence", "counterevidence", "assumptions", "verification"):
            if not isinstance(section.get(key), list) or any(
                    not isinstance(v, str) or not v.strip() for v in section[key]):
                raise WorkflowError(f"章节 {section['id']} 缺 {key} 列表")
        cited = section["evidence"] + section["counterevidence"]
        if any(i not in evidence_pool for i in cited):
            raise WorkflowError("正文引用的证据编号不存在于依赖的证据稿")
        if data["kind"] in {"analysis", "brand-house", "derivation"} and not (
                cited or section["assumptions"] and section["verification"]):
            raise WorkflowError("关键推导必须引用证据，或同时写明假设及验证方法")
        if data["mode"] == "draft" and section["assumptions"] and not section["verification"]:
            raise WorkflowError("假设必须有待验证安排")
    gaps = data.get("gaps")
    if not isinstance(gaps, list) or any(not isinstance(g, dict) or
            not all(g.get(k) for k in ("question", "cannot_do", "can_do")) or
            not isinstance(g.get("blocks_completion"), bool) for g in gaps):
        raise WorkflowError("缺口需说明问题、不能做/仍可做及是否阻断完成")
    questions = data.get("questions")
    if not isinstance(questions, list) or len(questions) > 5 or any(
            not isinstance(q, str) or not q.strip() for q in questions):
        raise WorkflowError("启发问题须为最多五条非空问题")
    if data["kind"] == "brief":
        brief_type = data.get("brief_type")
        if brief_type not in DESIGN_TYPES | {"strategy", "copywriting", "research"}:
            raise WorkflowError("简报类型无效")
        if brief_type in DESIGN_TYPES:
            design = data.get("design", {})
            if any(not design.get(k) for k in DESIGN_FIELDS):
                raise WorkflowError("设计简报缺消费者利益、依据、优先级、红线或创意自由等转换要求")
            checks = design["observable_checks"]
            if not isinstance(checks, list) or any(not isinstance(c, dict) or
                    not c.get("question") or not c.get("basis") or
                    c.get("level") not in {"red", "yellow"} for c in checks):
                raise WorkflowError("转换清单须包含可观察问题、依据及红黄等级")
    if data["kind"] == "evidence":
        entries = data.get("entries")
        if not isinstance(entries, list) or not entries or any(not isinstance(e, dict) for e in entries):
            raise WorkflowError("证据稿须为非空的证据对象列表")
        entry_ids = [e.get("id") for e in entries]
        if any(not isinstance(i, str) for i in entry_ids):
            raise WorkflowError("证据编号必须是文字")
        if len(set(entry_ids)) != len(entry_ids):
            raise WorkflowError("证据编号重复")
        for entry in entries:
            verify_entry(root, entry)
        conflicts = data.get("conflicts", [])
        if not isinstance(conflicts, list) or any(not isinstance(c, dict) for c in conflicts):
            raise WorkflowError("矛盾记录须为对象列表")
        for conflict in conflicts:
            ids = conflict.get("evidence_ids")
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise WorkflowError("矛盾记录的 evidence_ids 须为证据编号列表")
            if len(set(ids)) < 2 or any(
                    i not in entry_ids for i in conflict["evidence_ids"]) or not conflict.get("analysis"):
                raise WorkflowError("矛盾须并列至少两项有效证据，并说明判断与未决部分")


def render(data, context):
    lines = [f"# {data['title']}", "", f"任务：{data['task_id']} | 类别：{data['kind']}",
             f"状态：{'假设稿' if data['mode'] == 'hypothesis' else '待独立检核稿'}，不代表内部或客户确认。",
             "", "## 任务与上游", "", context, ""]
    for section in data["sections"]:
        lines += [f"## {section['title']}", "", section["text"], ""]
        for field, label in (("evidence", "支持证据"), ("counterevidence", "反证"),
                             ("assumptions", "假设"), ("verification", "待验证")):
            if section[field]:
                lines += [f"{label}："] + [f"- {v}" for v in section[field]] + [""]
    if data.get("design"):
        lines += ["## 设计策略转换清单", ""]
        for name, value in data["design"].items():
            lines += [f"### {name}", "", pretty(value), ""]
    if data.get("entries"):
        lines += ["## 完整证据表", ""]
        for entry in data["entries"]:
            lines += [f"### {entry['id']} · {entry['classification']}", "",
                      entry["statement"], "", f"来源：{entry['source']}；{entry['location']}",
                      "", "原文：", entry["quote"], "", f"适用限制：{entry['limits']}", ""]
    if data.get("conflicts"):
        lines += ["## 矛盾并列记录", "", pretty(data["conflicts"]), ""]
    if data["gaps"]:
        lines += ["## 未完成与可继续部分", "", pretty(data["gaps"]), ""]
    if data["questions"]:
        lines += ["## 下一步思考", ""] + [f"- {q}" for q in data["questions"]] + [""]
    if data.get("revision"):
        lines += ["## 修订对照与原因", "", pretty(data["revision"]), ""]
    return "\n".join(lines)


def pretty(value):
    if isinstance(value, list):
        return "\n".join(f"- {pretty(v)}" for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k}：{pretty(v)}" for k, v in value.items())
    return str(value)


def scaffold(task_id, kind, brief_type="strategy"):
    identifier(task_id)
    if kind not in SECTIONS:
        raise WorkflowError("未知产出类别")
    return {"task_id": task_id, "kind": kind, "title": "", "mode": "hypothesis",
            "brief_type": brief_type,
            "sections": [{"id": name, "title": "", "text": "", "evidence": [],
                          "counterevidence": [], "assumptions": [], "verification": []}
                         for name in SECTIONS[kind]],
            "gaps": [], "questions": [],
            "template_notice": "未填写的技术模板，不是正式业务产出；空正文不能登记。"}
