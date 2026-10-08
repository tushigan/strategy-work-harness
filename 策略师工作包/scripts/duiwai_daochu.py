"""独立任务/提案阶段演示稿的对外导出（U08，经 deck_export.py external 调用）。

以浏览器实际渲染为准（deck_external.mjs，不联网、不运行页面脚本）：按屏幕与打印两种媒体计算每段文字、每个元素是否
可见，两种都可见才写进对外 HTML；讲者稿/工具栏/脚本/aside 等约定的非画面元素、页面之外的内容、注释、元数据、
非白名单属性（data-*/aria-*/alt/title 等）、表单控件、SVG 标题/描述一律去掉，<title> 改中性标题，样式按最终页面重建
（去注释、去不用的规则）。图片按对外尺寸压缩、字体与样式表里的资源内嵌，再离线打印 PDF。

核对与删除无关：浏览器另行载入写好的对外 HTML，不判断可见性，取出全部文字（文本节点、属性文字、样式 content 字符串），
连同 PDF 文字，必须都是源稿“可见文字”的子集；不得有禁出现节点/属性、本机路径（含 file: 地址）或未内嵌引用；源稿页数、
对外 HTML 页数、PDF 页数三者一致。反向核对（M01）：源稿上看得见的字不得在对外版中缺失——①要删的文字逐段按实际画面
截图比较（隐去前后两种媒体画面都会变的就是看得见），②源稿每页两种媒体都可见的文字逐页必须出现在对外 HTML 同一页；
讲者稿/工具栏/脚本/元数据/表单控件/页面之外按约定删去，不在此列。任何一项不符即拒绝并指明页与文字，临时目录整体删除，
不留文件、不登记。缺 Playwright/Chrome 直接拒绝，不降级到源码解析。扫描与核对前文字统一做 NFKC 并去零宽/软连字符（fold）。
每次导出放进新的序号目录，从不覆盖已登记的对外版本。内部用语扫描覆盖全部对外文字（只提示，U05）。
不代替检核、不代表客户批准；发客户仍须过 deliver 关口。"""
import base64
import hashlib
import html as html_lib
import io
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from phase2_store import WorkflowError, local

EXTERNAL_EDGE = 1920
ORIGIN = "http://deck.invalid"
NEUTRAL_TITLE = "对外演示稿"
IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif"}
FONT = {".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf"}
# 本机路径：file: 地址一律算；网址（http(s)://主机/…、//主机/…）里的 /home/ 等不算
FILE_URL = re.compile(r"\bfile:[^\s\"'<>()]*", re.I)
LOCAL_PATH = re.compile(r"(?<![\w.:/-])(?:/Users/|/home/|/private/|/tmp/|/var/folders/)|(?<![\w])[A-Za-z]:\\|(?<![\w/])~/")
URL_LIKE = re.compile(r"(?:\bhttps?:)?//[^\s/\"'<>()][^\s\"'<>()]*", re.I)
# PDF 取字时部分字形映射到 CJK 部首补充区（如“页”取成“⻚”），这类字符按分隔处理
SEGMENT = re.compile(r"[\u2e80-\u2eff\s，。；：、,.;:!?！？()（）\[\]{}\"'<>/\\|=+*#&_`~·•…—–-]+")
CJK = re.compile(r"[㐀-鿿]")


def compress(raw, edge=EXTERNAL_EDGE):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(raw)) as image:
        image = ImageOps.exif_transpose(image)
        if max(image.size) > edge:
            scale = edge / max(image.size)
            image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
        out = io.BytesIO()
        if image.mode in ("RGBA", "LA", "P") and ("transparency" in image.info or image.mode != "P"):
            image.save(out, "PNG", optimize=True); return "image/png", out.getvalue()
        image.convert("RGB").save(out, "JPEG", quality=80, optimize=True, progressive=True)
        return "image/jpeg", out.getvalue()


class _Resources:
    """把浏览器给出的资源地址（工作区内）内嵌为 data:；外部网络引用、缺失、越界、符号链接、素材库指纹不符一律记为问题。"""

    def __init__(self, root):
        from sucai_ku import library
        self.root = root; self.problems = []; self.known = library(root)
        self.stats = {"images": 0, "fonts": 0, "stylesheets": 0, "original_bytes": 0, "embedded_bytes": 0}

    def target(self, url):
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != ORIGIN:
            self.problems.append(f"引用外部网络资源 {url[:80]}"); return None
        rel = unquote(parts.path).lstrip("/")
        raw = self.root / rel; target = raw.resolve()
        if not target.is_relative_to(self.root) or any(p.is_symlink() for p in [raw, *raw.parents] if p.is_relative_to(self.root)) \
                or not target.is_file():
            self.problems.append(f"缺失、越界或符号链接：{rel[:80]}"); return None
        from sucai_ku import verify_asset
        problem = verify_asset(self.root, target.relative_to(self.root).as_posix(), self.known)
        if problem:
            self.problems.append(problem); return None
        return target

    def data_uri(self, url):
        target = self.target(url)
        if target is None:
            return None
        suffix = target.suffix.lower(); raw = target.read_bytes()
        if suffix in IMAGE:
            try:
                mime, data = compress(raw)
            except Exception as exc:  # 损坏图片：如实报告，不静默
                self.problems.append(f"图片无法读取 {target.name}（{type(exc).__name__}）"); return None
            self.stats["images"] += 1
        elif suffix == ".svg":
            mime, data = "image/svg+xml", raw; self.stats["images"] += 1
        elif suffix in FONT:
            mime, data = FONT[suffix], raw; self.stats["fonts"] += 1
        else:
            self.problems.append(f"不支持内嵌的资源类型 {target.name}"); return None
        self.stats["original_bytes"] += len(raw); self.stats["embedded_bytes"] += len(data)
        return f"data:{mime};base64," + base64.b64encode(data).decode()

    def embed(self, html, urls):
        for url in sorted(set(urls), key=len, reverse=True):
            uri = self.data_uri(url)
            if uri is not None:
                html = html.replace(html_lib.escape(url, quote=True), uri).replace(url, uri)
        return html


def _node(mode, root, payload, *extra):
    node = os.environ.get("HARNESS_NODE", "node")
    try:
        run = subprocess.run([node, str(Path(__file__).with_name("deck_external.mjs")), mode, str(root), *map(str, extra)],
                             input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise WorkflowError("对外导出需要本机 Node、Chrome 与 HARNESS_NODE_MODULES 下的 playwright；找不到 node，未导出（不降级到源码解析）") from None
    except subprocess.TimeoutExpired:
        raise WorkflowError("浏览器处理超时，未导出") from None
    try:
        result = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise WorkflowError("浏览器处理失败（需本机 Chrome 与 HARNESS_NODE_MODULES 下的 playwright），未导出：" + (run.stderr or run.stdout)[-400:]) from None
    if result.get("fatal") == "no_playwright":
        raise WorkflowError("对外导出需要本机浏览器：HARNESS_NODE_MODULES 下找不到 playwright，未导出（不降级到源码解析）")
    if result.get("error"):
        raise WorkflowError("页面结构解析不一致，未导出：" + result.get("message", ""))
    return result


def _prune(root, rel, text, found):
    """浏览器按实际渲染删去不可见内容；返回对外 HTML（资源为绝对地址）与源稿可见文字。"""
    from html_huamian import note_names
    return _node("prune", root, {"path": rel, "html": text, "ordinals": found["page_ordinals"], "sections": found["sections"],
                                 "note_names": note_names()})


def _verify(root, html, pdf):
    """与删除无关的核对：载入对外 HTML，取全部文字与禁出现项，并打印 PDF。"""
    return _node("verify", root, {"path": "对外演示稿.html", "html": html}, pdf)


def _norm(text):
    from neibu_yongyu import fold
    return re.sub(r"\s+", "", fold(text)).casefold()


def _segments(text):
    from neibu_yongyu import fold
    for seg in SEGMENT.split(fold(text).casefold()):
        if seg and (CJK.search(seg) or len(seg) > 3):  # 不含中文且 ≤3 个字符的片段（编号、计数器）不核
            yield seg


def _covered(seg, source, piece):
    """seg 能否由源稿可见文字里的片段依次拼出（每段至少 piece 个字符，末段可更短但须出现过）。"""
    if seg in source:
        return True
    i = 0
    while i < len(seg):
        j = len(seg)
        while j > i and seg[i:j] not in source:
            j -= 1
        if j == i or (j - i < piece and j < len(seg)):
            return False
        i = j
    return True


def not_in_source(texts, source_text, piece=None):
    """对外文字里不属于源稿可见文字的片段。HTML/属性/样式文字按整段核；PDF 文字允许按 ≥2 字的片段拼（分栏换行会打乱）。"""
    source = _norm(source_text); missing = []
    for text in texts:
        for seg in _segments(text):
            if not (seg in source if piece is None else _covered(seg, source, piece)):
                missing.append(seg)
    return list(dict.fromkeys(missing))


def local_paths(html):
    without_data = re.sub(r"data:[^\"')\s]+", "", html)
    m = FILE_URL.search(without_data) or LOCAL_PATH.search(URL_LIKE.sub("", without_data))
    return m.group(0)[:80] if m else None


def lost_in_output(source_pages, output_pages):
    """反向核对（文字）：源稿每页两种媒体都可见的文字片段，逐页必须出现在对外 HTML 的同一页。返回 [(页, 片段)]。"""
    lost = []
    for number, text in enumerate(source_pages, 1):
        out = _norm(output_pages[number] if number < len(output_pages) else "")
        lost += [(number, seg) for seg in dict.fromkeys(_segments(text)) if seg not in out]
    return lost


def _lost_message(items):
    shown = "；".join(f"第{page}页「{text[:24]}」" for page, text in items[:8])
    more = f" 等 {len(items)} 处" if len(items) > 8 else ""
    return shown + more


def _current_html(root, task_id, html):
    from standalone_store import history
    own = [e for e in history(root)[0] if e["task_id"] == task_id]
    if not own:
        raise WorkflowError("找不到任务：" + task_id)
    event = own[-1]
    pages = [d for d in event["task"]["deliverables"] if Path(d.get("path", "")).suffix.lower() in {".html", ".htm"}]
    if html:
        pages = [d for d in pages if d["path"] == html]
    if len(pages) != 1:
        raise WorkflowError("只能从任务当前登记的一份 HTML 交付物导出；用 --html 指定，或先用 iterate/save 登记要导出的版本")
    ref = pages[0]; path = local(root, ref["path"])
    raw = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    if raw is None or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise WorkflowError("当前 HTML 与登记指纹不一致：先核对并登记新版，再导出")
    return event, ref, raw  # 只读一次：指纹与浏览器处理用的是同一份字节


def _parent(root, task_id):
    parent = local(root, f"project/exports/对外/{task_id}")
    parent.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in [parent, *parent.parents] if p.is_relative_to(root)):
        raise WorkflowError("对外导出目录不能是符号链接")
    return parent


def _publish(root, parent, temp, revision):
    """核对全部通过后才把临时目录改名为 r{修订号}-{序号}；同一版本再导出放新目录，已登记的对外版本从不覆盖。"""
    for n in range(1, 1000):
        folder = parent / f"r{revision}-{n:02d}"
        if folder.exists():
            continue
        os.rename(temp, folder)
        return folder, folder.relative_to(root).as_posix()
    raise WorkflowError("对外导出目录序号用尽")


def export(root, task_id, actor, html=None):
    from registration_guard import actor_check
    from neibu_yongyu import scan_text
    from html_huamian import screen
    actor_check(actor); root = Path(root).resolve()
    event, ref, raw = _current_html(root, task_id, html)
    text = raw.decode("utf-8")
    found = screen(text)
    if not found["pages"]:
        raise WorkflowError("源稿没有顶层 <section> 页面，无法逐页导出与核对页数")
    pruned = _prune(root, ref["path"], text, found)
    if pruned["problems"]:
        raise WorkflowError("对外导出未写出：" + "；".join(pruned["problems"][:5]))
    if pruned["pages"] != len(found["pages"]):
        raise WorkflowError(f"浏览器页数 {pruned['pages']} 与源稿页数 {len(found['pages'])} 不一致，未写出")
    if pruned["lostVisible"]:
        raise WorkflowError("源稿画面上看得见的文字在对外版中缺失（反向核对按实际画面比较），未写出、未登记："
                            + _lost_message([(x["page"], x["text"]) for x in pruned["lostVisible"]])
                            + "。可见性判断可能误删：把这些字改到纯色底上或去掉干扰元素后再导出，或交主控人工处理")
    resources = _Resources(root)
    out_html = resources.embed(pruned["html"], pruned["resources"])
    if resources.problems:
        raise WorkflowError("对外导出资源问题，未写出：" + "；".join(dict.fromkeys(resources.problems[:5])))
    parent = _parent(root, task_id)
    temp = parent / f".导出中-{uuid.uuid4().hex[:12]}"
    temp.mkdir()
    folder = None
    try:
        external_html = temp / "对外演示稿.html"; external_html.write_text(out_html, encoding="utf-8")
        pdf = temp / "对外演示稿.pdf"
        written = external_html.read_text(encoding="utf-8")  # 按实际写出的文件字节核对
        checked = _verify(root, written, pdf)
        if not pdf.is_file():
            raise WorkflowError("PDF 未生成")
        from pypdf import PdfReader
        reader = PdfReader(pdf)
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        source = " ".join(pruned["sourceText"])
        issues = list(dict.fromkeys(checked["issues"]))
        where = local_paths(written)
        if where:
            issues.append("含本机路径：" + where)
        extra = not_in_source(checked["texts"], source)
        if extra:
            issues.append("对外 HTML 出现源稿画面上看不到的文字：" + "、".join(x[:24] for x in extra[:5]))
        extra_pdf = not_in_source([pdf_text], source, piece=2)
        if extra_pdf:
            issues.append("PDF 出现源稿画面上看不到的文字：" + "、".join(x[:24] for x in extra_pdf[:5]))
        lost = lost_in_output(pruned["sourcePages"], checked["pageTexts"])
        if lost:
            issues.append("源稿画面上看得见的文字在对外 HTML 中缺失：" + _lost_message(lost))
        counts = (len(found["pages"]), checked["pages"], len(reader.pages))
        if len(set(counts)) != 1:
            issues.append("页数不一致：源稿 %d 页、对外 HTML %d 页、PDF %d 页" % counts)
        if issues:
            raise WorkflowError("核对未通过，未写出、未登记：" + "；".join(issues[:8]))
        page_texts = checked["pageTexts"]
        hits = scan_text(root, [(i, t) for i, t in enumerate(page_texts) if t and t.strip()])
        scan_result = {"file": "对外演示稿.html", "hits": hits, "count": len(hits), "categories": sorted({h["category"] for h in hits}),
                       "status": "attention" if hits else "ok", "basis": "浏览器取出的全部对外文字（含伪元素、属性文字）"}
        base, folder = _publish(root, parent, temp, event["revision"])
        scan_result["file"] = f"{folder}/对外演示稿.html"
        files = [{"path": f"{folder}/{p}", "sha256": hashlib.sha256((base / p).read_bytes()).hexdigest(), "bytes": (base / p).stat().st_size}
                 for p in ("对外演示稿.html", "对外演示稿.pdf")]
        from jiaofu_guankou import register_external
        record = register_external(root, task_id, files, {"count": scan_result["count"], "categories": scan_result["categories"]}, actor,
                                   source=ref)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        if folder is not None:
            shutil.rmtree(root / folder, ignore_errors=True)
        raise
    return {"status": "exported", "source": ref, "files": files, "pages": len(found["pages"]), "images": resources.stats,
            "dropped_non_screen": pruned["dropped"], "dropped_text_chars": pruned["droppedChars"], "scan": scan_result,
            "lost_visible": [],  # 反向核对：源稿上看得见的字全部在对外版同一页（有缺失即已拒绝）
            "record_id": record["record_id"],
            "notice": ("对外版本已登记（新目录，不覆盖旧版）。可见性以浏览器屏幕与打印两种渲染为准；被去掉的元素见 dropped_non_screen，"
                       "dropped_text_chars>0 时回读确认没有误删画面内容。扫描只提示不删。PDF 仍需实际逐页回读，"
                       "发客户须过 jiaofu_guankou.py deliver 关口，不代表客户批准")}
