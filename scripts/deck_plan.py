"""Validate page plans; semantic fidelity remains an independent review duty."""
from decimal import Decimal, InvalidOperation
from io import BytesIO
import math
from pathlib import Path
import re

import phase5_common as c

NUMBERS = re.compile(r"[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?")
KINDS = {"cover", "points", "table", "chart", "image"}


def text(value, label, limit=240):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise c.WorkflowError(f"页级方案 {label} 须为非空文字，最多 {limit} 字")
    return value


def fields(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required).issubset(value)
            or set(value) - set(required) - set(optional)):
        raise c.WorkflowError("页级方案字段缺失或含不支持字段")


def citations(document, entries):
    if not isinstance(entries, list) or not entries or len(entries) > 24:
        raise c.WorkflowError("每页须有可核对的逐字稿 citations")
    sections = {s["id"]: s for s in document["sections"]}
    for entry in entries:
        fields(entry, ("section_id", "field", "quote"))
        section = entry["section_id"]
        if section is not None and not isinstance(section, str):
            raise c.WorkflowError("页面引用章节须为文字编号或封面的 null")
        source = document if section is None else sections.get(section)
        allowed = ("title", "purpose") if section is None else ("title", "spoken_text", "transition")
        quote = text(entry["quote"], "引用原文", 20000)
        if (not source or not isinstance(entry["field"], str) or entry["field"] not in allowed
                or not isinstance(source.get(entry["field"]), str)
                or quote not in source[entry["field"]]):
            raise c.WorkflowError("页面引用不属于绑定逐字稿的实际章节原文")
    return entries


def _numbers(value):
    try:
        return {Decimal(m[0].replace(",", "")) for m in NUMBERS.finditer(value)}
    except InvalidOperation as exc:
        raise c.WorkflowError("页级方案数值格式超出可核对范围") from exc


def _table(table):
    fields(table, ("columns", "rows"))
    columns, rows = table["columns"], table["rows"]
    if (not isinstance(columns, list) or not 2 <= len(columns) <= 4
            or not isinstance(rows, list) or not 1 <= len(rows) <= 6
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)):
        raise c.WorkflowError("每页表格须 2-4 列、1-6 行且行列一致；更多内容请拆页")
    return [text(v, "表格文字", 100) for v in [*columns, *(v for row in rows for v in row)]]


def _chart(chart, refs):
    fields(chart, ("unit", "series"))
    unit = text(chart["unit"], "图表单位", 24)
    series = chart["series"]
    if not isinstance(series, list) or not 1 <= len(series) <= 8:
        raise c.WorkflowError("每页图表须包含 1-8 个非负数据项")
    labels, output = set(), [unit]
    for item in series:
        fields(item, ("label", "value", "citation"))
        label = text(item["label"], "图表标签", 48)
        value, index = item["value"], item["citation"]
        if (label in labels or type(value) not in (int, float) or not 0 <= value <= 1e12
                or not math.isfinite(value) or type(index) is not int or not 0 <= index < len(refs)
                or Decimal(str(value)) not in _numbers(refs[index]["quote"])):
            raise c.WorkflowError("图表数据必须为有限非负数且存在于指定引用，标签不能重复")
        labels.add(label)
        output.extend((label, str(value)))
    return output


def _image(root, reference):
    from PIL import Image
    fields(reference, ("path", "sha256", "alt", "caption"))
    text(reference["alt"], "图片替代文字", 160)
    text(reference["caption"], "图片说明", 160)
    relative = reference["path"]
    if (not isinstance(relative, str) or not relative.startswith("project/")
            or ".." in Path(relative).parts or not re.fullmatch(r"[0-9a-f]{64}", str(reference["sha256"]))):
        raise c.WorkflowError("图片须为本项目内的相对路径和完整指纹")
    path = c.local(root, relative)
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise c.WorkflowError("图片缺失或超过 10 MiB")
    raw = path.read_bytes()
    if c.sha_bytes(raw) != reference["sha256"]:
        raise c.WorkflowError("图片指纹不符，不能登记改变后的素材")
    try:
        with Image.open(BytesIO(raw)) as image:
            formats = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"), "WEBP": (".webp", "image/webp")}
            if image.format not in formats or image.width * image.height > 40000000 or getattr(image, "n_frames", 1) != 1:
                raise c.WorkflowError("只支持尺寸受限的静态 PNG/JPEG/WebP 图片")
            suffix, mime = formats[image.format]
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise c.WorkflowError("图片无法完整解码") from exc
    return suffix, raw, mime


def build(root, document, script_ref, plan):
    fields(plan, ("script_ref", "pages"))
    if plan["script_ref"] != script_ref:
        raise c.WorkflowError("页级方案不属于当前已确认的逐字稿版本")
    candidates = plan["pages"]
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 80:
        raise c.WorkflowError("页级方案须包含封面和内容页，共 2-80 页")
    sections = {s["id"]: (i, s) for i, s in enumerate(document["sections"])}
    pages, assets, covered = [], {}, set()
    for number, entry in enumerate(candidates, 1):
        fields(entry, ("kind", "title", "section_id", "bullets", "citations"), ("table", "chart", "image"))
        kind, section = entry["kind"], entry["section_id"]
        if not isinstance(kind, str) or kind not in KINDS or ((number == 1) != (kind == "cover")):
            raise c.WorkflowError("第一张必须是唯一封面，内容页类型不支持")
        if (kind == "cover" and (section is not None or entry["title"] != document["title"])) or (
                kind != "cover" and (not isinstance(section, str) or section not in sections)):
            raise c.WorkflowError("页面章节或封面标题与逐字稿不一致")
        extra = {k for k in ("table", "chart", "image") if k in entry}
        if extra != ({kind} if kind in {"table", "chart", "image"} else set()):
            raise c.WorkflowError("页面类型与表格、图表或图片结构不一致")
        title = text(entry["title"], "标题", 100)
        bullets = entry["bullets"]
        if not isinstance(bullets, list) or not 1 <= len(bullets) <= 4:
            raise c.WorkflowError("每页须 1-4 条画面要点，不把完整讲稿投屏")
        display = [text(v, "画面要点", 120) for v in bullets]
        refs = citations(document, entry["citations"])
        if section is not None and not any(r["section_id"] == section and r["field"] == "spoken_text" for r in refs):
            raise c.WorkflowError("内容页须引用本章节讲述原文，标题引用不能替代论据")
        if kind == "table":
            display.extend(_table(entry["table"]))
        elif kind == "chart":
            display.extend(_chart(entry["chart"], refs))
        elif kind == "image":
            asset = _image(root, entry["image"])
            role = "asset-" + entry["image"]["sha256"]
            assets[role] = asset
            display.append(entry["image"]["caption"])
        displayed_numbers = {value for field in [title, *display] for value in _numbers(field)}
        quoted_numbers = {value for reference in refs for value in _numbers(reference["quote"])}
        if kind == "cover":
            quoted_numbers.update(_numbers(document["title"]))
        if not displayed_numbers.issubset(quoted_numbers):
            raise c.WorkflowError("画面出现引用中不存在的数值；先核对逐字稿，不能补造数据")
        index, source = sections[section] if section is not None else (None, {})
        if section is not None:
            covered.add(section)
        pages.append({**entry, "page": number, "title": title, "bullets": display[:len(bullets)],
                      "display_texts": display, "text": "".join(display), "section_index": index,
                      "speaker_text": source.get("spoken_text", ""),
                      "speaker_transition": source.get("transition", ""), "source_field": "page_plan"})
    if covered != set(sections):
        raise c.WorkflowError("页级方案未覆盖全部逐字稿章节；不得默默删除论证")
    return pages, assets


def check_registered(meta, payload):
    expected = {"asset-" + p["image"]["sha256"]: p["image"]["sha256"]
                for p in payload["pages"] if p["kind"] == "image"}
    for group in ("current_files", "snapshot_files"):
        actual = {f["role"]: f["sha256"] for f in meta[group] if f["role"].startswith("asset-")}
        if actual != expected:
            raise c.WorkflowError("演示稿图片与登记的素材指纹不一致")
