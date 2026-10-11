#!/usr/bin/env python3
"""PPTX 与对外 HTML 最小核对（v1.7.5 G13）：页数一致、每页文字一致（忽略空白与换行，不比图片），差异列出页和字。

- 读 PPTX 只用 Python 标准库：PPTX 是压缩包，页序按 ppt/presentation.xml 的页列表（sldIdLst）经 ppt/_rels/presentation.xml.rels
  找到 ppt/slides/slideN.xml，读每页 a:t 文字（讲者备注不读）；隐藏页（show="0"）不算页，另行列出。
- HTML 页 = 第几个顶层 <section>（与扫描、自检、截图、导出同一定义），文字取画面文字（html_huamian.screen，与内部用语扫描同口径）。
- 比较前统一做 NFKC 并去掉零宽字符等不显示的字符与全部空白（neibu_yongyu.fold）。一页文字一致 = 两边字符逐个相同（按多重集合，
  不看先后：PPTX 文本框顺序与 HTML 排版顺序常不同）；不一致时列出该页 PPTX 少了 / 多了的段落与字。
- PPTX 打不开、不是压缩包、缺页列表或页文件、XML 坏了、压缩数据损坏 → 状态 unchecked（未核对），不冒充一致；解析时出任何
  别的错也在 compare 外层兜底成未核对（批次三返修 r2 #2：deflate 数据损坏抛 zlib.error 等）。
- 页码、日期字段（a:fld type="slidenum" / "datetime…"）不算页面文字（网页版通常没有）；页里有图表或 SmartArt 时，它们的文字
  在另外的部件里、没有读，这页网页版多出的字不报“少了”，注明“这页的图表文字没有核对”（批次三返修 r2 #10）。
- 只核对文字与页数；字体、版式、图片、动画不比。"""
import argparse
import json
import re
import zipfile
import zlib
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from phase2_store import WorkflowError, local

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
SLIDE_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
MAX_BYTES = 200 * 1024 * 1024  # 单个 PPTX 解压后读取上限：超过按“未核对”
APPENDIX = "## 技术附录（给 Agent）"


class Unreadable(Exception):
    """PPTX 打不开或结构异常（调用方报“未核对”）。"""


def _xml(archive, name, budget):
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise Unreadable(f"缺少 {name}") from None
    budget[0] -= info.file_size
    if budget[0] < 0:
        raise Unreadable("解压后体积过大")
    try:
        return ElementTree.fromstring(archive.read(name))
    except (ElementTree.ParseError, zipfile.BadZipFile, OSError, RuntimeError, zlib.error, NotImplementedError, EOFError,
            ValueError) as exc:
        raise Unreadable(f"{name} 读不出来（{type(exc).__name__}）") from None


def slides(path):
    """[(第几页, 文字段落列表)]，隐藏页列入 hidden。打不开或结构异常抛 Unreadable。"""
    out, hidden, charts, _ = _read(path)
    return out, hidden, charts


def _read(path):
    """slides 的完整版：另返回 {第几页: 页码、日期字段里的字}（批次四开工补修 2：这些字“可有可无”）。"""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, zlib.error, NotImplementedError, EOFError, ValueError) as exc:
        raise Unreadable(f"不是能打开的 PPTX（{type(exc).__name__}）") from None
    with archive:
        budget = [MAX_BYTES]
        presentation = _xml(archive, "ppt/presentation.xml", budget)
        rels = _xml(archive, "ppt/_rels/presentation.xml.rels", budget)
        targets = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", NS) if r.get("Type") == SLIDE_TYPE}
        listed = presentation.find("p:sldIdLst", NS)
        if listed is None:
            raise Unreadable("缺少页列表")
        order = [s.get(f"{{{NS['r']}}}id") for s in listed.findall("p:sldId", NS)]
        if not order:
            raise Unreadable("页列表是空的")
        out, hidden, charts, fields = [], [], [], {}
        for number, rid in enumerate(order, 1):
            target = targets.get(rid)
            if not target:
                raise Unreadable(f"第 {number} 页找不到页文件")
            name = target.lstrip("/") if target.startswith("/") else "ppt/" + re.sub(r"^\./", "", target)
            name = re.sub(r"[^/]+/\.\./", "", name)
            slide = _xml(archive, name, budget)
            if slide.get("show") == "0":
                hidden.append(number); continue
            paragraphs, optional = [], []
            for para in slide.iter(f"{{{NS['a']}}}p"):
                text = "".join(_para_text(para))
                if text.strip():
                    paragraphs.append(text)
                optional.extend(_field_text(para))
            if optional:
                fields[number] = "".join(optional)
            if any(_unread_frame(g.get("uri") or "") for g in slide.iter(f"{{{NS['a']}}}graphicData")):
                charts.append(number)
            out.append((number, paragraphs))
        return out, hidden, charts, fields


FIELD_TYPES = re.compile(r"^(?:slidenum|datetime[0-9A-Za-z]*)$", re.I)  # 页码、日期（datetime1…13、datetimeFigureOut）


def _field_text(para):
    """一段里页码、日期字段的字（网页版有没有都算一致）。"""
    for child in para:
        if child.tag.rsplit("}", 1)[-1] == "fld" and FIELD_TYPES.match(child.get("type") or ""):
            yield from (t.text or "" for t in child.iter(f"{{{NS['a']}}}t"))


def _para_text(para):
    """一段的文字：文字块（a:r）与字段（a:fld）里的 a:t；页码、日期字段跳过（网页版通常没有，算进去会误判）。"""
    for child in para:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "fld" and FIELD_TYPES.match(child.get("type") or ""):
            continue
        if tag in {"r", "fld"}:
            yield from (t.text or "" for t in child.iter(f"{{{NS['a']}}}t"))


def _unread_frame(uri):
    """图表（chart / chartex）与 SmartArt（diagram）：文字在另外的部件里，本核对不读。"""
    return "chart" in uri.lower() or "diagram" in uri.lower()


def html_pages(path):
    """[(第几页, 文字段落列表)]：顶层 <section> 的画面文字，按行分段。"""
    from html_huamian import ScreenText
    raw = Path(path).read_text(encoding="utf-8")
    parser = ScreenText(raw); parser.feed(raw); parser.close()
    count = len(parser.pages)
    return [(n, [re.sub(r"\s+", " ", x).strip() for x in parser.page_text.get(n, []) if x.strip()]) for n in range(1, count + 1)]


def _norm(text):
    from neibu_yongyu import fold
    return re.sub(r"\s+", "", fold(text))


def compare(pptx_path, html_path):
    """返回核对结果：status = same / different / unchecked。"""
    try:
        deck, hidden, charts, fields = _read(pptx_path)
    except Unreadable as exc:
        return {"status": "unchecked", "reason": f"PPTX {exc}", "pptx": str(pptx_path), "html": str(html_path)}
    except Exception as exc:  # noqa: BLE001 —— 兜底：解析时出任何别的错都按打不开记“未核对”，不让调用方抛出
        return {"status": "unchecked", "reason": f"PPTX 读不出来（{type(exc).__name__}）", "pptx": str(pptx_path), "html": str(html_path)}
    try:
        pages = html_pages(html_path)
    except (OSError, ValueError, UnicodeError, WorkflowError) as exc:
        return {"status": "unchecked", "reason": f"HTML 读不出来（{type(exc).__name__}）", "pptx": str(pptx_path), "html": str(html_path)}
    differences, unread = [], []
    chart_slides = set(charts)
    if len(deck) != len(pages):
        differences.append({"page": None, "kind": "page_count", "pptx_pages": len(deck), "html_pages": len(pages)})
    for k in range(min(len(deck), len(pages))):
        p_no, p_paras = deck[k]
        h_no, h_paras = pages[k]
        a, b = _norm("".join(h_paras)), _norm("".join(p_paras))
        if p_no in chart_slides:  # 批次四开工补修 3：有图表的页一致时也注明“图表里的字没有核对”
            unread.append(h_no)
        if Counter(a) == Counter(b):
            continue
        optional = Counter(_norm(fields.get(p_no, "")))  # 批次四开工补修 2：网页版多出的字恰好是页码、日期字段的字，不算少了
        lost_count = Counter(a) - Counter(b) - optional
        missing = [x for x in h_paras if _norm(x) and _norm(x) not in b and Counter(_norm(x)) - optional]
        extra = [x for x in p_paras if _norm(x) and _norm(x) not in a]
        lost = "".join(lost_count.elements())
        added = "".join((Counter(b) - Counter(a)).elements())
        if not lost and not added:
            continue
        if p_no in chart_slides:  # 图表 / SmartArt 的文字没读：网页版多出的字可能就在图表里，不报“少了”；PPTX 多出的字照报
            if not added:
                continue
            missing, lost = [], ""
        differences.append({"page": h_no, "pptx_slide": p_no, "kind": "text", "missing_in_pptx": missing[:8], "extra_in_pptx": extra[:8],
                            "missing_chars": _ordered(lost, a), "extra_chars": _ordered(added, b)})
    for k in range(len(pages), len(deck)):
        differences.append({"page": None, "pptx_slide": deck[k][0], "kind": "extra_slide", "extra_in_pptx": deck[k][1][:8]})
    for k in range(len(deck), len(pages)):
        differences.append({"page": pages[k][0], "kind": "missing_slide", "missing_in_pptx": pages[k][1][:8]})
    return {"status": "different" if differences else "same", "pptx": str(pptx_path), "html": str(html_path),
            "pptx_pages": len(deck), "html_pages": len(pages), "hidden_slides": hidden, "differences": differences,
            **({"chart_unchecked_pages": unread} if unread else {})}


def _ordered(chars, text):
    """差异的字按在原文里出现的先后排（去重前后保持次数）。"""
    left = Counter(chars)
    out = []
    for ch in text:
        if left[ch] > 0:
            out.append(ch); left[ch] -= 1
    return "".join(out)[:60]


def _q(text, n=30):
    text = text.strip()
    return "“" + (text if len(text) <= n else text[:n] + "…") + "”"


def plain(result, label="可编辑的 PPT"):
    """给策略师的三段说明（核对结果）。"""
    unread = result.get("chart_unchecked_pages") or []
    chart_note = ("；".join(f"第 {n} 页有图表，这页的图表文字没有核对" for n in unread) + "。") if unread else ""
    if result["status"] == "same":
        what = f"{label}和网页版对过了：{result['pptx_pages']} 页，每页文字一样" + ("（图表里的字除外）" if unread else "") + "。"
        return "\n".join([f"发生了什么：{what}" + chart_note,
                          "对你有什么影响：客户拿到的两份文字一致（版式、图片没有比）。" + ("图表里的字要你自己看一眼。" if unread else ""),
                          "要你做什么：" + ("发之前看一眼这几页图表里的字和网页版是否一样。" if unread else "不用做。")])
    if result["status"] == "unchecked":
        return "\n".join(["发生了什么：" + f"{label}没能和网页版对照（文件打不开或结构不对），这次算“没核对”，不算一致。",
                          "对你有什么影响：如果要把它发给客户，里面的文字可能和网页版不一样，现在还不知道。",
                          "要你做什么：如果要发，请把它重新另存一次再给我，或者告诉我这份不用对照。",
                          "", APPENDIX, "原因：" + result.get("reason", "")])
    lines = []
    for d in result["differences"]:
        if d["kind"] == "page_count":
            lines.append(f"页数不一样：{label} {d['pptx_pages']} 页，网页版 {d['html_pages']} 页。")
        elif d["kind"] == "extra_slide":
            lines.append(f"{label}多出一页（第 {d['pptx_slide']} 页）" + (f"，上面写着{_q(d['extra_in_pptx'][0])}" if d.get("extra_in_pptx") else "") + "。")
        elif d["kind"] == "missing_slide":
            lines.append(f"网页版第 {d['page']} 页在{label}里没有" + (f"（这页写着{_q(d['missing_in_pptx'][0])}）" if d.get("missing_in_pptx") else "") + "。")
        else:
            parts = [f"第 {d['page']} 页文字不一样"]
            if d["missing_in_pptx"]:
                parts.append(f"{label}里少了" + "、".join(_q(x) for x in d["missing_in_pptx"][:3]))
            if d["extra_in_pptx"]:
                parts.append(f"{label}里多了" + "、".join(_q(x) for x in d["extra_in_pptx"][:3]))
            if not d["missing_in_pptx"] and not d["extra_in_pptx"]:
                parts.append(f"少了这些字：{d['missing_chars'] or '无'}；多了这些字：{d['extra_chars'] or '无'}")
            lines.append("：".join(parts[:1]) + "：" + "；".join(parts[1:]) + "。")
    if chart_note:
        lines.append(chart_note)
    return "\n".join([f"发生了什么：{label}和网页版对照，有 {len(result['differences'])} 处不一样：", *[f"- {x}" for x in lines[:12]],
                      "对你有什么影响：客户如果拿这份 PPT 去改，看到的是另一份稿。",
                      "要你做什么：告诉我是改这份 PPT 跟网页版一致，还是这些不一样本来就是你要的（说一句原话，我记下再登记发出）。"])


def pair(deliverables):
    """交付物里的 PPTX 与 HTML 配对：同名（去后缀）优先；各只有一份时直接配。返回 ([(pptx, html)], [配不上的 PPTX])。"""
    pptx = [d for d in deliverables if str(d).lower().endswith(".pptx")]
    html = [d for d in deliverables if str(d).lower().endswith((".html", ".htm"))]
    pairs, lone = [], []
    for p in pptx:
        same = [h for h in html if Path(h).stem == Path(p).stem]
        if len(same) == 1:
            pairs.append((p, same[0]))
        elif len(pptx) == 1 and len(html) == 1:
            pairs.append((p, html[0]))
        else:
            lone.append(p)
    return pairs, lone


def main():
    parser = argparse.ArgumentParser(description="PPTX 与对外 HTML 最小核对：页数、每页文字（只用标准库读 PPTX）")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--pptx", required=True)
    parser.add_argument("--html", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    result = compare(local(root, args.pptx), local(root, args.html))
    result.update(pptx=args.pptx, html=args.html, text=plain(result))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["text"])
    return 0


if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main

    def main():
        return cli(_business_main, __file__)

    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
