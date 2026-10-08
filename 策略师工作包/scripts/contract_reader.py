"""Read full source evidence and conservative service-item candidates."""
from __future__ import annotations

import re
import zipfile
from datetime import date, datetime
from pathlib import Path

import openpyxl
from pypdf import PdfReader

from phase2_store import WorkflowError, sha

ALLOWED_KINDS = {"brand_house", "product_strategy", "brand_design", "packaging", "other"}
SERVICE = re.compile(r"品牌|产品|包装|设计|研究|调研|策略|开发|营销|服务|[Ll][Oo][Gg][Oo]|\bVI\b")


def scalar(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def kind(text):
    if any(word in text for word in ("品牌屋", "品牌考古", "品牌定位", "品牌梳理")):
        return "brand_house"
    if any(word in text for word in ("产品策略", "产品线", "产品矩阵", "产品规划")):
        return "product_strategy"
    if any(word in text for word in ("包装", "包材")):
        return "packaging"
    if any(word in text.lower() for word in ("logo", "vi", "视觉识别", "品牌调性", "视觉升级")):
        return "brand_design"
    return "other"


def xlsx_units(path):
    book = openpyxl.load_workbook(path, data_only=False)
    units, issues = [], []
    try:
        with zipfile.ZipFile(path) as archive:
            if any(name.startswith("xl/media/") for name in archive.namelist()):
                issues.append({"code": "embedded_images",
                               "message": "工作簿含嵌入图片；文字读取不覆盖图片内容，需逐张查看并补可核对文字"})
        for sheet in book:
            for row in sheet:
                cells = [{"cell": c.coordinate, "value": scalar(c.value), "type": c.data_type}
                         for c in row if c.value is not None]
                if not cells:
                    continue
                location = f"'{sheet.title}'!{cells[0]['cell']}:{cells[-1]['cell']}"
                units.append({"location": location, "cells": cells,
                              "text": " | ".join(str(c["value"]) for c in cells),
                              "sheet_state": sheet.sheet_state,
                              "hidden_row": bool(sheet.row_dimensions[row[0].row].hidden)})
                if any(c["type"] == "f" for c in cells):
                    issues.append({"code": "formula", "location": location,
                                   "message": "存在公式；保留公式原文，需核对计算结果，未执行公式"})
        return units, issues
    finally:
        book.close()


def pdf_units(path):
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise WorkflowError("PDF 已加密，请提供获授权的可读副本")
    units, issues, declared = [], [], []
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        location = f"第{number}页"
        units.append({"page": number, "location": location, "text": text})
        has_images = bool(page.images)
        meaningful = re.sub(r"第\s*\d+\s*页\s*[/／,，]?\s*共\s*\d+\s*页", "", text).strip()
        if len(meaningful) < 10 or text.count("\ufffd") > 2 or has_images and len(meaningful) < 80:
            issues.append({"code": "unreadable_page", "location": location,
                           "message": "本页文字缺失或乱码，可能为扫描件；需 OCR/人工转录资料，不猜填",
                           "hard_block": True})
        elif has_images:
            issues.append({"code": "image_content", "location": location,
                           "message": "本页含图片；需查看是否另有扫描文字，不能只凭提取文本判断完整"})
        declared.extend((int(a), int(b)) for a, b in
                        re.findall(r"第\s*(\d+)\s*页\s*[/／,，]?\s*共\s*(\d+)\s*页", text))
    if declared and (any(total != len(reader.pages) for _, total in declared)
                     or [num for num, _ in declared] != list(range(1, len(reader.pages) + 1))):
        issues.append({"code": "page_gap", "message": "印刷页码/总页数与 PDF 不一致，疑似缺页或重复页"})
    return units, issues


def read_contract(path: Path):
    path = Path(path)
    if not path.is_file():
        raise WorkflowError("合同文件不存在")
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        units, issues = xlsx_units(path)
        fmt = "xlsx"
    elif suffix == ".pdf":
        units, issues = pdf_units(path)
        fmt = "pdf"
    else:
        raise WorkflowError("目前只支持 XLSX/XLSM/PDF；旧 XLS 请另存为 XLSX")
    items, seen = [], {}
    for unit in units:
        lines = [unit["text"]] if fmt == "xlsx" else unit["text"].splitlines()
        for line_number, text in enumerate(lines, 1):
            if not SERVICE.search(text) or not text.strip():
                continue
            location = unit["location"] + (f" 第{line_number}行" if fmt == "pdf" else "")
            item_id = f"ITEM-{len(items) + 1:03d}"
            items.append({"item_id": item_id, "raw_text": text, "title": text.strip(),
                          "service_kind": kind(text), "source_location": location,
                          "disposition": "unresolved", "note": ""})
            key = re.sub(r"\s+", "", text)
            if key in seen:
                issues.append({"code": "duplicate_candidate", "location": location,
                               "message": f"疑似与 {seen[key]} 重复，未自动删除", "item_id": item_id})
            seen[key] = item_id
    if not items:
        issues.append({"code": "no_candidates", "message": "未找到可靠候选；需人工依据完整原文补列项"})
    issues.append({"code": "source_coverage", "message": "候选不是合同解释。需逐页/逐表核对完整性、标题误识别、跨行及组合列项"})
    for number, issue in enumerate(issues, 1):
        issue.update({"issue_id": f"ISSUE-{number:03d}", "resolved": False})
    return {"schema_version": "0.2", "source_name": path.name, "source_sha256": sha(path),
            "format": fmt, "units": units, "items": items, "issues": issues,
            "recognition_status": "draft", "source_review_complete": False}
