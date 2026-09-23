"""Render an approved script's explicit page plan into a version-bound deck."""
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re

import deck_style
import deck_plan
import deck_page_render
import phase5_common as common
from phase2_store import WorkflowError
from phase3_review import gate as upstream_gate
from phase3_sources import identifier

UPSTREAM = ("brand-house", "derivation", "brief")
CHECKS = ("display", "navigation", "editing", "version_binding")
ASSESSMENTS = ("task_fulfilment", "evidence_reasoning", "counterevidence", "role_boundary", "uncertainties")


def _key(task_id):
    identifier(task_id)
    return f"{task_id}::html-deck"


def _document(root, meta):
    files = [f for f in meta["current_files"] if f["path"].endswith(".json")]
    if len(files) != 1 or files[0]["sha256"] != meta["sha256"]:
        raise WorkflowError("当前结构化稿不唯一或与版本指纹不一致")
    return common.read_json(common.local(root, files[0]["path"]))


def _sources(root, task_id, request):
    required = {*UPSTREAM, "script"}
    if (not isinstance(request, dict) or not required.issubset(request)
            or set(request) - required - {"revision", "visual_style", "page_plan"}):
        raise WorkflowError("请求必须提供 script、brand-house、derivation、brief 四个完整版本引用")
    for name in (*UPSTREAM, "script"):
        reference = request[name]
        space, kind = ("phase5", "proposal-script") if name == "script" else ("phase3", name)
        if (not isinstance(reference, dict) or set(reference) != {"space", "key", "version", "sha256"}
                or reference.get("space") != space or not isinstance(reference.get("key"), str)
                or not reference["key"].endswith("::" + kind)
                or type(reference.get("version")) is not int or reference["version"] < 1
                or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))):
            raise WorkflowError(f"{name} 必须提供正确类别、版本和完整指纹")
    from proposal_script import gate as script_gate
    script = script_gate(root, f"{task_id}::proposal-script", human=True)
    if common.ref(script) != request["script"] or script.get("task_id") != task_id:
        raise WorkflowError("逐字稿不是当前任务已确认的确切版本")
    for name in UPSTREAM:
        meta = upstream_gate(root, request[name]["key"])
        if common.phase3_ref(meta) != request[name] or meta.get("kind") != name:
            raise WorkflowError(f"{name} 已更新或类别不符，不能沿用旧引用")
        if request[name] not in script.get("dependencies", []):
            raise WorkflowError(f"{name} 不属于此逐字稿绑定的上游版本")
    return script


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"逐字稿缺少有效的 {label}")
    return value.strip()


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace(
        ">", "\\u003e").replace("&", "\\u0026")


def _render(payload, target, template, visual_css=None, assets=None):
    slides, options = [], []
    for page in payload["pages"]:
        number, kind = page["page"], page["kind"]
        title, body = escape(page["title"]), escape(page["text"])
        label = "提案" if kind == "cover" else f"{page['section_index'] + 1:02d}"
        content = f'<p class="audience" data-edit="{number}-text">{body}</p>'
        if payload.get("schema_version") == "2.0":
            content = deck_page_render.render(page, assets or {})
        elif kind == "content":
            split = re.search(r"[。！？!?；;\n]", page["text"])
            cut = split.end() if split and split.end() >= 12 else len(page["text"])
            lead, rest = escape(page["text"][:cut]), escape(page["text"][cut:])
            content = (f'<div class="argument"><p class="lead" data-edit="{number}-lead">{lead}</p>'
                       f'<p class="support" data-edit="{number}-rest">{rest}</p></div>')
        elif kind == "chapter":
            content = (f'<div class="chapter-line" aria-hidden="true"></div>'
                       f'<p class="chapter-note" data-edit="{number}-note">{body}</p>')
        slides.append(f'<section class="slide {kind}" id="page-{number}" data-page="{number}">'
                      f'<p class="eyebrow">{label}</p><h2 data-edit="{number}-title">{title}</h2>'
                      f'{content}<p class="folio">{number} / {len(payload["pages"])}</p></section>')
        options.append(f'<option value="{number - 1}">{number}. {title}</option>')
    data = {**payload, "deck_ref": target, "draft": {"revision": 0, "edits": {}}}
    versions = "".join(f'<li>{escape(name)}：{escape(ref["key"])} · v{ref["version"]}'
                       f'<code>{ref["sha256"]}</code></li>' for name, ref in
                       [("PPT", target), ("逐字稿", payload["script_ref"]), *payload["upstream_refs"].items()])
    values = {"TITLE": escape(payload["title"]), "SLIDES": "\n".join(slides),
              "OPTIONS": "\n".join(options), "VERSIONS": versions, "DATA": _encode(data)}
    html = re.sub(r"@@(TITLE|SLIDES|OPTIONS|VERSIONS|DATA)@@", lambda m: values[m[1]], template)
    if visual_css is not None:
        html = deck_style.embed(html, visual_css)
    return html.encode("utf-8")


class _Metadata(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.parts, self.active, self.count = [], False, 0
        self.feed(text)
        self.close()
        if self.count != 1 or self.active:
            raise WorkflowError("HTML 的 data-phase5-deck 元数据缺失、重复或不完整")

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "data-phase5-deck" in values:
            if tag != "script" or values.get("type") != "application/json":
                raise WorkflowError("PPT 元数据容器格式错误")
            self.count += 1
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, text):
        if self.active:
            self.parts.append(text)


def generate(root, task_id, author_instance, request):
    key = _key(task_id)
    style_ref, style_raw = deck_style.load(root, task_id, request["visual_style"]) if (
        isinstance(request, dict) and "visual_style" in request) else (None, None)
    script = _sources(root, task_id, request)
    document = _document(root, script)
    pages, assets = deck_plan.build(root, document, request["script"], request.get("page_plan"))
    template = Path(__file__).resolve().parent.parent.joinpath("templates/proposal-deck.html").read_text(encoding="utf-8")
    payload = {"schema_version": "2.0", "task_id": task_id, "kind": "html-deck",
               "title": _text(document.get("title"), "title"),
               "audience": document["audience"], "purpose": document["purpose"], "pages": pages,
               "script_ref": request["script"], "upstream_refs": {n: request[n] for n in UPSTREAM},
               "page_plan": request["page_plan"],
               "template_sha256": common.sha_bytes(template.encode("utf-8"))}
    if style_ref is not None:
        payload["visual_style"] = style_ref
    base = f"project/outputs/proposal/{task_id}/html-deck"

    def files(version, content, markdown):
        saved = json.loads(content)
        target = {"space": "phase5", "key": key, "version": version, "sha256": common.sha_bytes(content)}
        rendered = _render(saved, target, template, style_raw.decode("utf-8") if style_raw is not None else None, assets)
        result = [("data", ".json", content), ("html", ".html", rendered)]
        if style_raw is not None:
            result.append(("style", ".css", style_raw))
        result.extend((role, suffix, raw) for role, (suffix, raw, _) in assets.items())
        return result

    def current_path(role, suffix):
        # Content-addressed CSS can be removed/re-added without overwriting an old style.
        if role == "style":
            return f"{base}/style-{style_ref['sha256']}.css"
        if role.startswith("asset-"):
            return f"{base}/assets/{role}{suffix}"
        return f"{base}/{'presentation' if role == 'html' else 'current'}{suffix}"

    revision = request.get("revision")
    if revision is not None and not common.registry(root).get("artifacts", {}).get(key):
        raise WorkflowError("首次生成不能伪装成历史修订")
    meta = common.publish(root, key=key, kind="html-deck", task_id=task_id, author=author_instance,
        payload=payload, markdown="", dependencies=[request[n] for n in ("script", *UPSTREAM)],
        file_layout={"folder": f"{base}/versions", "files": files,
                     "snapshot": lambda version, role, suffix: f"{base}/versions/v{version:04d}" + (
                         f"-{role}" if role.startswith("asset-") else "") + suffix,
                     "current": current_path},
        revision=revision)
    return {"artifact": meta, "target": common.ref(meta), "path": f"{base}/presentation.html",
            "pages": len(pages), "status": "generated_pending_deck_review"}


def _live(root, key):
    meta = common.current(root, key)
    if meta.get("kind") != "html-deck" or key != _key(meta["task_id"]):
        raise WorkflowError("此接口只接受 HTML PPT 产出")
    payload = _document(root, meta)
    deck_style.check_registered(meta, payload)
    if payload.get("schema_version") == "2.0":
        deck_plan.check_registered(meta, payload)
    paths = [f for f in meta["current_files"] if f.get("role") == "html"]
    if len(paths) != 1:
        raise WorkflowError("PPT 当前 HTML 不唯一")
    parser = _Metadata(common.local(root, paths[0]["path"]).read_text(encoding="utf-8"))
    data = json.loads("".join(parser.parts))
    expected = {**payload, "deck_ref": common.ref(meta), "draft": {"revision": 0, "edits": {}}}
    if data != expected:
        raise WorkflowError("HTML 来源或正文已改动；编辑稿不能继承原版批准")
    _sources(root, meta["task_id"], {"script": payload["script_ref"], **payload["upstream_refs"]})
    return meta, payload, paths[0]["path"]


def review_sources(root, key):
    meta, payload, path = _live(root, key)
    return {"target": common.ref(meta), "path": path, "pages": payload["pages"],
            "scope_required": ["cover", *CHECKS, *dict.fromkeys(p["section_id"] for p in payload["pages"][1:]),
                               *(["content_fidelity"] if payload.get("schema_version") == "2.0" else [])],
            "assessment_required": list(ASSESSMENTS),
            "checked_sources_required": common.required_sources(root, meta),
            "notice": "须由不同执行实例读取全部来源并在本地浏览器检查每一页；清单不代表通过。"}


def record_review(root, key, report_path):
    sources = review_sources(root, key)
    report = common.read_json(report_path)
    if not isinstance(report, dict):
        raise WorkflowError("独立检核报告必须是 JSON 对象")
    scope = sources["scope_required"] if report.get("status") in common.PASS else ()
    return common.record_review(root, key, report_path, required_scope=scope)


def gate(root, key, *, human=False):
    sources = review_sources(root, key)
    meta = common.gate(root, key, human=human)
    event = common.last_review(root, common.ref(meta))
    report = common.read_json(common.local(root, event["evidence"]["path"]))
    common.review_checks(root, meta, report, required_scope=sources["scope_required"])
    return meta


def confirm(root, key, evidence, actor, simulation=False):
    gate(root, key)
    return common.confirm(root, key, evidence, actor, simulation)


def status(root, key):
    result = common.status_for(root, key)
    result["human_confirmed"] = False
    if result["status"] == "stale":
        return result
    try:
        _live(root, key)
    except (WorkflowError, OSError, ValueError, KeyError) as exc:
        return {**result, "status": "stale", "reason": str(exc)}
    try:
        gate(root, key)
    except (WorkflowError, OSError, ValueError, KeyError) as exc:
        return {**result, "review_effective": False, "reason": str(exc)}
    result["review_effective"] = True
    try:
        gate(root, key, human=True)
        result["human_confirmed"] = True
    except (WorkflowError, OSError, ValueError, KeyError):
        pass
    return result


def inspect(root, task_id):
    key = _key(task_id)
    result = status(root, key)
    meta = common.latest(root, key)
    return {**result, "files": meta["current_files"], "dependencies": meta["dependencies"],
            "browser_verified": False, "notice": "只核对磁盘文件与版本；不会读取浏览器缓存或代替显示检核。"}
