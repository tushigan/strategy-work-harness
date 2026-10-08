#!/usr/bin/env python3
"""演示稿视觉增量检核（W01）：逐页像素指纹、必看页计算、继承核对与 HTML/PDF 同版文字核对。

“没变” = 同一渲染环境下逐页无损截图解码后像素逐个相同，且该页各状态文字相同，不设容差。HTML 每页截：运行脚本时的
屏幕、打印、展开隐藏元素、390 宽窄屏、深色，以及不运行脚本时的屏幕与打印（shijue_zhiwen.mjs）；截图范围含溢出到页框
外的内容。PDF 用 pypdfium2 逐页渲染。各渲染两次，两次不同、页内有运行中的动画、动图、视频或内嵌页面即判“不稳定”
（按变化页看）。字体或任何资源加载失败、被拦截 → 渲染不完整（全页）。指纹记录只存哈希与必要文字，按内容存一份、从不覆盖。

比对对象是该成果最近一份通过的独立检核所绑定的指纹记录（不是上一个文件版本）；中间未检核的版本不作基线，其后
未通过的检核指出的页一律必看。没有基线、基线为旧格式、环境不同、渲染不完整、页面脚本或其依赖改变、页数对不上
（从第一处不一致起其后全部必看）、阶段首次检核（以派工记录的用途为准）、基线检核员后来参与创作或模拟状态不一致
→ 全页或从不一致处起全部必看。继承只能由程序比对得出：门禁登记新报告时重新渲染当前成品核对指纹，继承页须与
程序计算的结果完全一致，并绑定基线报告与两份指纹记录。"""
import argparse
import base64
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import url2pathname

from phase2_store import WorkflowError, local

RECORDS = "project/records/visual-fingerprints"
PLANS = "project/reviews/visual-plans"
RENDERER = Path(__file__).with_name("shijue_zhiwen.mjs")
SCHEMA = 3  # r3：数据块指纹（模板按页）、DOM 静止、伪元素文字、计数器；第 1、2 版记录按旧格式（全页）处理
PASS = {"passed", "passed_with_yellow"}
FONT_DIRS = (("System", "Library", "Fonts"), ("Library", "Fonts"))  # 系统与本机字体目录（自根目录）；另加用户字体目录
JS_TYPES = {"", "text/javascript", "application/javascript", "module", "text/ecmascript", "application/ecmascript",
            "text/jscript", "application/x-javascript", "text/x-javascript", "text/livescript"}
URL_ATTRS = {"href", "src", "action", "formaction", "xlink:href", "data"}
SCRIPT_REQUESTS = {"script", "fetch", "xhr", "eventsource", "websocket"}
# 程序自己的模板（templates/proposal-deck.html，html_deck 生成）数据块里每版必变的元字段：版本号、指纹与各种引用。
# 去掉它们后 pages[i] 计入第 i 页，其余字段计入整稿数据。
DECK_META = {"deck_ref", "script_ref", "upstream_refs", "page_plan", "visual_style", "template_sha256"}
# PDF 取字时部首补充区字符与对应汉字（只按此对照表放过，其余部首字照常算不一致）
RADICALS = {"⻅": "见", "⻆": "角", "⻉": "贝", "⻋": "车", "⻓": "长", "⻔": "门", "⻙": "韦", "⻚": "页", "⻛": "风",
            "⻜": "飞", "⻢": "马", "⻥": "鱼", "⻦": "鸟", "⻧": "卤", "⻨": "麦", "⻩": "黄", "⻪": "黾", "⻬": "齐",
            "⻮": "齿", "⻰": "龙", "⻳": "龟", "⻄": "西", "⻑": "長", "⻘": "青", "⻟": "食", "⻣": "骨", "⻤": "鬼",
            "⺟": "母", "⺠": "民", "⻗": "雨"}
_VERIFIED = {}


class Unavailable(WorkflowError):
    """本机缺渲染条件（浏览器/Playwright/pypdfium2）；调用方据此退回全页，不当成通过。"""


def _sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _sha_json(value):
    return _sha_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _file_sha(path):
    return _sha_bytes(Path(path).read_bytes())


def pixel_digest(image_path):
    """无损截图解码后的像素指纹：尺寸 + 模式 + 原始像素字节。"""
    from PIL import Image
    with Image.open(image_path) as image:
        image.load()
        rgba = image.convert("RGBA")
        h = hashlib.sha256(f"{rgba.width}x{rgba.height}:RGBA:".encode())
        h.update(rgba.tobytes())
        return h.hexdigest()


def font_inventory():
    """本机字体清单指纹（文件名与大小）；字体增删会改变环境指纹。"""
    rows = []
    for base in [Path(os.sep).joinpath(*parts) for parts in FONT_DIRS] + [Path.home() / "Library" / "Fonts"]:
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                try:
                    if path.is_file():
                        rows.append(f"{path.relative_to(base).as_posix()}:{path.stat().st_size}")
                except OSError:
                    continue
    return _sha_bytes("\n".join(rows).encode())


def normalize_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if not ch.isspace() and unicodedata.category(ch) != "Cf")


def _file_url_bytes(url):
    """file:/data: 地址的字节；其它地址返回 None。"""
    if url.startswith("data:"):
        head, _, body = url.partition(",")
        return base64.b64decode(body) if head.endswith(";base64") else unquote(body).encode()
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    return Path(url2pathname(parsed.path)).read_bytes()


class _ScriptScan(HTMLParser):
    """页面脚本与其依赖的静态部分：可执行的内联脚本（含属性）、外链脚本地址、事件属性、javascript: 地址。
    type 为 application/json 等不执行的数据块不算脚本：它们对画面的影响由运行脚本的截图反映。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.sources, self.current, self.data = [], [], None, []

    def _attrs(self, tag, attrs):
        for name, value in attrs:
            if name.startswith("on"):
                self.items.append(["event", tag, name, value or ""])
            elif name in URL_ATTRS and (value or "").strip().lower().startswith("javascript:"):
                self.items.append(["javascript-url", tag, name, value])

    def handle_starttag(self, tag, attrs):
        self._attrs(tag, attrs)
        if tag == "script":
            values = dict(attrs)
            kind = (values.get("type") or "").split(";")[0].strip().lower()
            self.current = {"attrs": sorted([k, v or ""] for k, v in attrs), "exec": kind in JS_TYPES, "body": [], "src": values.get("src")}

    def handle_startendtag(self, tag, attrs):
        self._attrs(tag, attrs)

    def handle_data(self, data):
        if self.current is not None:
            self.current["body"].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            item, self.current = self.current, None
            if item["exec"]:
                self.items.append(["script", item["attrs"], "".join(item["body"])])
                if item["src"]:
                    self.sources.append(item["src"])
            else:
                self.data.append((dict(item["attrs"]), "".join(item["body"])))


def scripts_digest(path, requests=()):
    """页面脚本指纹：内联脚本、本地外链脚本文件内容、事件属性、javascript: 地址，以及渲染时实际请求到的
    脚本/数据（fetch、xhr）文件内容。任何一项改变 → 全页（交互行为无法由截图证明没变）。返回 (指纹, 是否有脚本)。"""
    path = Path(path)
    scan = _ScriptScan()
    scan.feed(path.read_text(encoding="utf-8", errors="replace")); scan.close()
    base = path.resolve().as_uri()
    loaded = {}
    urls = {urljoin(base, s.strip()) for s in scan.sources} | {r["url"] for r in requests if r.get("type") in SCRIPT_REQUESTS}
    for url in sorted(urls):
        try:
            raw = _file_url_bytes(url)
            loaded[url.replace(base.rsplit("/", 1)[0], ".", 1)] = _sha_bytes(raw) if raw is not None else "远程地址（离线无法取得）"
        except OSError:
            loaded[url.replace(base.rsplit("/", 1)[0], ".", 1)] = "缺失或不可读"
    has = bool(scan.items or loaded)
    return _sha_json({"inline": scan.items, "loaded": loaded}), has


def data_digest(path, top_count):
    """数据块（不执行的 <script>，如 application/json）指纹，r3。返回 (整稿数据指纹, 逐页数据指纹列表或 None)。
    程序已知的模板（data-phase5-deck）去掉每版必变的元字段（DECK_META）后，pages[i] 计入第 i 个顶层页（含讲者稿），
    其余字段计入整稿数据；pages 条数与顶层页数对不上时 pages 整体计入整稿数据。其他数据块原文计入整稿数据。
    整稿数据改变且页面有脚本 → 全页（脚本读数据后可能延迟、懒加载或交互时才显示，截图证明不了没变）。"""
    scan = _ScriptScan()
    scan.feed(Path(path).read_text(encoding="utf-8", errors="replace")); scan.close()
    whole, per_page = [], None
    for attrs, body in scan.data:
        if "data-phase5-deck" in attrs and per_page is None:
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("pages"), list):
                rest = {k: v for k, v in data.items() if k not in DECK_META}
                pages = rest.pop("pages")
                if len(pages) == top_count:
                    per_page = [_sha_json(page) for page in pages]
                else:
                    rest["pages"] = pages
                whole.append(["deck", rest])
                continue
        whole.append(["block", sorted(attrs.items()), body])
    return (_sha_json(whole) if whole else None), per_page


_ANIMATED_SVG = re.compile(rb"<(animate|animateTransform|animateMotion|set)\b|@keyframes|animation\s*:", re.I)


def _animated(raw):
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as image:
            return bool(getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1)
    except Exception:  # noqa: BLE001 —— 不是位图（如 SVG）或无法解码，按文本再判断
        return bool(_ANIMATED_SVG.search(raw))


def media_problems(urls, cache):
    """页内图片：动图（GIF/APNG/动画 WebP/带动画的 SVG）、远程或读不到的图片 → 本页不稳定（必看）。"""
    found = []
    for url in urls:
        if url not in cache:
            try:
                raw = _file_url_bytes(url)
                cache[url] = ("远程图片无法离线确认" if raw is None else "页内有动图" if _animated(raw) else None)
            except (OSError, ValueError):
                cache[url] = "页内图片缺失或不可读"
        if cache[url]:
            found.append(cache[url])
    return found


def _node():
    return os.environ.get("HARNESS_NODE", "node")


def render_timeout(path):
    """单次渲染的超时上限（秒）：30 秒 + 每页 15 秒，最多 300 秒。页数按源码里 <section 标签估算（读不到按 1 页）。
    v1.7.1：原固定 600 秒，卡住时每次空等 10 分钟。"""
    try:
        count = len(re.findall(rb"<section[\s>/]", Path(path).read_bytes(), re.I))
    except OSError:
        count = 0
    return min(300, 30 + 15 * max(1, count))


def _start_render(path, wait_ms):
    folder = tempfile.TemporaryDirectory(prefix="harness-fp-")
    try:
        proc = subprocess.Popen([_node(), str(RENDERER), str(path), folder.name, str(wait_ms)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        folder.cleanup()
        raise Unavailable(f"无法启动浏览器渲染（{type(exc).__name__}）") from exc
    return proc, folder


def _finish_render(proc, folder, timeout):
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill(); proc.communicate()
            raise Unavailable(f"浏览器渲染超时（{timeout} 秒）") from exc
        if proc.returncode:
            raise Unavailable("浏览器渲染失败（需本机 Chrome 与 HARNESS_NODE_MODULES 下的 playwright）：" + (stderr or stdout)[-300:])
        data = json.loads((Path(folder.name) / "result.json").read_text(encoding="utf-8"))
        for section in data["sections"]:
            section["pixels"] = {state: pixel_digest(file) for state, file in section.pop("files").items()}
        return data
    finally:
        folder.cleanup()


def _render_html_once(path, wait_ms):
    return _finish_render(*_start_render(path, wait_ms), render_timeout(path))


def _render_html(path, runs):
    """同一页面渲染 runs 次（第 1 次不等待，其余各等 450 毫秒），用于判断稳定。
    v1.7.1：依序进行（一次跑完再启动下一次）；并行启动多个 Chrome 时本机曾出现截完图后进程不退出。"""
    return [_render_html_once(path, 0 if k == 0 else 450) for k in range(runs)]


def fingerprint_html(path, *, runs=2):
    path = Path(path)
    renders = _render_html(path, runs)
    first = renders[0]
    env = {"renderer": "chrome-playwright", "browser_version": first["browser_version"], "playwright": first["playwright"],
           "viewport": first["viewport"], "narrow_viewport": first["narrow_viewport"], "device_scale_factor": first["device_scale_factor"],
           "javascript": first["javascript"], "states": first["states"], "fonts_sha256": font_inventory(),
           "platform": platform.mac_ver()[0] or platform.platform(), "renderer_sha256": _file_sha(RENDERER)}
    problems = sorted({p for r in renders for p in r["load_problems"]})
    if any(len(r["sections"]) != len(first["sections"]) for r in renders):
        problems.append("两次载入的页数不同")
    requests = {(q["url"], q["type"]) for r in renders for q in r["requests"]}
    scripts, has_scripts = scripts_digest(path, [{"url": u, "type": t} for u, t in sorted(requests)])
    data_sha, page_data = data_digest(path, len([s for s in first["sections"] if s["top"]]))
    tops = iter(page_data or [])
    pages, cache = [], {}
    for k, section in enumerate(first["sections"]):
        combined = _sha_json(section["pixels"])
        texts = {state: normalize_text(value) for state, value in section["texts"].items()}
        reasons = []
        if section["animations"]:
            reasons.append("页内有运行中的动画")
        if section["live_media"]:
            reasons.append("页内有" + "、".join(section["live_media"]) + "（视频或内嵌页面，画面会变）")
        reasons += media_problems(section["media_urls"], cache)
        if any(s.get("counters") for s in [section] + [o["sections"][k] for o in renders[1:] if k < len(o["sections"])]):
            reasons.append("页内用了 CSS 计数器（counter()/counters()），编号受其他页影响，逐页截图证明不了没变")
        for r in renders:
            if k < len(r["sections"]):
                reasons += r["sections"][k].get("unsettled") or []
        for other in renders[1:]:
            twin = other["sections"][k] if k < len(other["sections"]) else None
            if twin is None or _sha_json(twin["pixels"]) != combined:
                reasons.append("同页两次渲染像素不同")
            elif {s: normalize_text(v) for s, v in twin["texts"].items()} != texts:
                reasons.append("同页两次渲染文字不同")
        pages.append({"index": section["index"], "label": section["id"] or str(section["index"]), "top": section["top"],
                      "pixel_sha256": combined, "states": section["pixels"], "texts": texts, "overflow": section["overflow"],
                      "stable": not reasons, "unstable_reason": "；".join(dict.fromkeys(reasons)) or None,
                      "text": normalize_text(section["print_text"]) if section["top"] else None,
                      "blocks": [t for t in map(normalize_text, (section["print_text"] or "").split("\n")) if t] if section["top"] else None,
                      "data_sha256": next(tops, None) if section["top"] and page_data else None})
    complete = not problems
    return {"kind": "html", "environment": env, "complete": complete,
            "incomplete_reason": None if complete else "；".join(problems[:5]),
            "scripts_sha256": scripts, "has_scripts": has_scripts, "data_sha256": data_sha, "pages": pages}


def _pdfium():
    if os.environ.get("HARNESS_PDF_RENDER", "").lower() in {"off", "0", "no"}:
        raise Unavailable("已设 HARNESS_PDF_RENDER=off，PDF 不做逐页像素指纹，按全页检核")
    try:
        import pypdfium2
        return pypdfium2
    except ImportError as exc:
        raise Unavailable("本机没有 pypdfium2，PDF 无法逐页渲染，PDF 按全页检核") from exc


def fingerprint_pdf(path, *, runs=2):
    pdfium = _pdfium()
    from pypdf import PdfReader

    def once():
        document = pdfium.PdfDocument(str(path))
        try:
            result = []
            for index in range(len(document)):
                bitmap = document[index].render(scale=1.0)
                image = bitmap.to_pil().convert("RGBA")
                h = hashlib.sha256(f"{image.width}x{image.height}:RGBA:".encode()); h.update(image.tobytes())
                result.append(h.hexdigest())
            return result
        finally:
            document.close()
    first = once(); others = [once() for _ in range(runs - 1)]
    texts = [normalize_text(page.extract_text() or "") for page in PdfReader(str(path)).pages]
    info = pdfium.version
    env = {"renderer": "pypdfium2", "pypdfium2": str(getattr(info, "PYPDFIUM_INFO", "unknown")),
           "pdfium": str(getattr(info, "PDFIUM_INFO", "unknown")), "scale": 1.0,
           "platform": platform.mac_ver()[0] or platform.platform(), "renderer_sha256": _file_sha(__file__)}
    pages = []
    for index, digest in enumerate(first):
        unstable = any(index >= len(o) or o[index] != digest for o in others)
        pages.append({"index": index + 1, "label": str(index + 1), "top": True, "pixel_sha256": digest,
                      "stable": not unstable, "unstable_reason": "同页两次渲染像素不同" if unstable else None,
                      "text": texts[index] if index < len(texts) else "", "texts": {"pdf": texts[index] if index < len(texts) else ""}})
    complete = len(texts) == len(first)
    return {"kind": "pdf", "environment": env, "complete": complete,
            "incomplete_reason": None if complete else "PDF 取字页数与渲染页数不同", "scripts_sha256": None, "pages": pages}


def pdf_text_record(path):
    """只取 PDF 逐页文字（pypdf），供同版核对；没有 pypdfium2 时 PDF 不做像素指纹。"""
    from pypdf import PdfReader
    return {"kind": "pdf", "pages": [{"index": k, "label": str(k), "top": True, "text": normalize_text(page.extract_text() or "")}
                                     for k, page in enumerate(PdfReader(str(path)).pages, 1)]}


def compute(root, rel, runs=2):
    path = local(root, rel)
    if not path.is_file():
        raise WorkflowError(f"{rel}：文件不存在")
    digest = _file_sha(path)
    suffix = path.suffix.lower()
    data = fingerprint_pdf(path, runs=runs) if suffix == ".pdf" else fingerprint_html(path, runs=runs)
    if _file_sha(path) != digest:
        raise WorkflowError(f"{rel}：渲染期间文件改变，指纹作废")
    env_sha = _sha_json(data["environment"])
    return {"schema_version": SCHEMA, "artifact": {"path": rel, "sha256": digest}, "environment_sha256": env_sha, **data}


def store(root, record):
    raw = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode()
    digest = _sha_bytes(raw)
    rel = f"{RECORDS}/{record['artifact']['sha256'][:2]}/{record['artifact']['sha256']}-{digest[:16]}.json"
    path = local(root, rel)
    if path.exists():
        if path.read_bytes() != raw:
            raise WorkflowError(f"已有同名指纹记录内容不同，不覆盖：{rel}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".writing")
        temporary.write_bytes(raw); os.replace(temporary, path)
    return {"path": rel, "sha256": digest}


def fingerprint(root, rel):
    """渲染并保存当前成品的指纹记录，返回引用 {path, sha256}。"""
    root = Path(root).resolve()
    record = compute(root, rel)
    ref = store(root, record)
    _VERIFIED[(str(root), ref["path"], ref["sha256"])] = record
    return ref, record


def read_record(root, ref, artifact=None):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str):
        raise WorkflowError("指纹记录引用须为路径与指纹对象")
    root = Path(root).resolve()
    folder = (root / RECORDS).resolve()
    path = (root / ref["path"]).resolve()
    if not path.is_relative_to(folder) or ref["path"] != path.relative_to(root).as_posix():
        raise WorkflowError("指纹记录须是程序生成的 " + RECORDS + "/ 下的文件（路径先解析再判断，不接受 ../ 等写法）")
    if not path.is_file() or _file_sha(path) != ref["sha256"]:
        raise WorkflowError("指纹记录缺失或已改变")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema_version") != SCHEMA or not isinstance(record.get("pages"), list):
        raise WorkflowError("指纹记录格式无效或为旧格式（r1 记录不含多状态与文字，按全页处理）")
    if artifact is not None and record.get("artifact") != {"path": artifact["path"], "sha256": artifact["sha256"]}:
        raise WorkflowError("指纹记录不是当前成品的")
    return record


def verify(root, ref, artifact):
    """登记新报告时重新渲染当前成品（与准备时同样两次）：环境、完整性、脚本、页标识与文字、每个声称稳定的页的
    稳定性与像素都必须与记录一致（防止手写或改动指纹冒充继承）。"""
    root = Path(root).resolve()
    key = (str(root), ref.get("path") if isinstance(ref, dict) else None, ref.get("sha256") if isinstance(ref, dict) else None)
    record = read_record(root, ref, artifact)
    if key in _VERIFIED:
        return record
    fresh = compute(root, artifact["path"], runs=2)
    if fresh["environment_sha256"] != record.get("environment_sha256") or fresh["artifact"] != record["artifact"]:
        raise WorkflowError("当前渲染环境与指纹记录不同，不能继承；重新准备检核（jianhe_zhunbei）或全页检核")
    if record.get("complete") and not fresh["complete"]:
        raise WorkflowError("指纹记录称渲染完整，重新渲染不完整：" + (fresh.get("incomplete_reason") or "未知"))
    if (len(fresh["pages"]) != len(record["pages"]) or fresh.get("scripts_sha256") != record.get("scripts_sha256")
            or fresh.get("data_sha256") != record.get("data_sha256") or fresh.get("has_scripts") != record.get("has_scripts")):
        raise WorkflowError("指纹记录的页数、脚本指纹或数据块指纹与当前成品不符")
    for old, new in zip(record["pages"], fresh["pages"]):
        if any(old.get(f) != new.get(f) for f in ("index", "label", "top", "text", "texts", "blocks", "data_sha256")):
            raise WorkflowError(f"指纹记录第 {new['index']} 页的页标识、层级或文字与当前成品不符")
        if old.get("stable") and (not new["stable"] or old.get("pixel_sha256") != new["pixel_sha256"] or old.get("states") != new.get("states")):
            raise WorkflowError(f"指纹记录第 {new['index']} 页的稳定性或像素指纹与重新渲染结果不符，不能作为继承依据")
    _VERIFIED[key] = record
    return record


def labels(record, top_only=False):
    pages = [p for p in record["pages"] if p.get("top") or not top_only]
    if top_only:
        return [str(k) for k in range(1, len(pages) + 1)], pages
    return [p["label"] for p in pages], pages


def _flag_names(names, pages, flagged, top_only):
    """把退回报告指出的页（第 n 页 / 页标识）对到当前页名；第 n 页按顶层页面计数。"""
    tops = [n for n, p in zip(names, pages) if p.get("top")] if not top_only else names
    out, unknown = set(), []
    for token in flagged:
        if isinstance(token, int):
            if 1 <= token <= len(tops):
                out.add(tops[token - 1])
            else:
                unknown.append(f"第 {token} 页（当前共 {len(tops)} 页）")
        elif token in names:
            out.add(token)
        else:
            unknown.append(f"“{token}”")
    return out, unknown


def compare(base, current, *, top_only=False, reason_full=None, flagged=()):
    """必看页 / 继承页清单。base 为 None 或任一退回条件成立即全页。继承须像素与各状态文字都与基线相同，
    且不在此前未通过的检核所指出的页（flagged）里。"""
    names, pages = labels(current, top_only)

    def full(reason):
        return {"mode": "full", "full_reason": reason, "must_see": [{"page": n, "reason": reason} for n in names], "inherited": []}
    if reason_full:
        return full(reason_full)
    if base is None:
        return full("没有可用的视觉基线（无通过检核或旧格式报告无像素指纹）")
    if base.get("kind") != current.get("kind"):
        return full("基线与当前成品类型不同")
    if base.get("environment_sha256") != current.get("environment_sha256"):
        return full("渲染环境与基线不同（浏览器版本、视口、媒体、字体或渲染程序改变）")
    if not base.get("complete") or not current.get("complete"):
        return full("基线或当前渲染不完整：" + (current.get("incomplete_reason") or base.get("incomplete_reason") or "未知"))
    if base.get("scripts_sha256") != current.get("scripts_sha256"):
        return full("页面脚本或脚本依赖（内联脚本、外链脚本文件、事件属性、脚本读取的数据文件）改变")
    if base.get("data_sha256") != current.get("data_sha256") and (base.get("has_scripts") or current.get("has_scripts")):
        return full("页面数据块（application/json 等）改变且页面有脚本：脚本读数据后可能延迟、懒加载或交互时才显示，截图证明不了没变")
    _, old_pages = labels(base, top_only)
    marked, unknown = _flag_names(names, pages, flagged, top_only)
    if unknown:
        return full("此前未通过的检核指出的页对不上当前存在的页：" + "、".join(sorted(unknown)[:5]) + "；认不出就全页")
    must, inherited, broken = [], [], None
    for k, (name, page) in enumerate(zip(names, pages)):
        old = old_pages[k] if k < len(old_pages) else None
        if broken is not None:
            must.append({"page": name, "reason": f"页数变化：第 {broken + 1} 页起无法一一对应"}); continue
        if old is None:
            broken = k; must.append({"page": name, "reason": "新增页"}); continue
        changed = (page["pixel_sha256"] != old["pixel_sha256"] or page.get("texts") != old.get("texts")
                   or page.get("data_sha256") != old.get("data_sha256"))
        if changed and len(old_pages) != len(pages):
            broken = k; must.append({"page": name, "reason": f"页数变化：第 {k + 1} 页起无法一一对应"}); continue
        if not page["stable"] or not old.get("stable"):
            must.append({"page": name, "reason": "渲染不稳定：" + (page.get("unstable_reason") or old.get("unstable_reason") or "基线页不稳定")})
        elif page["pixel_sha256"] != old["pixel_sha256"]:
            must.append({"page": name, "reason": "像素有变化"})
        elif page.get("texts") != old.get("texts"):
            must.append({"page": name, "reason": "页面文字有变化（含溢出页框、被裁切、伪元素或只在某种状态出现的文字）"})
        elif page.get("data_sha256") != old.get("data_sha256"):
            must.append({"page": name, "reason": "本页数据（模板数据块里本页的内容，含讲者稿）有变化"})
        elif name in marked:
            must.append({"page": name, "reason": "此前未通过的检核指出过本页问题"})
        else:
            inherited.append({"page": name, "pixel_sha256": page["pixel_sha256"]})
    return {"mode": "incremental" if inherited else "full", "full_reason": None if inherited else "没有可继承的页",
            "must_see": must, "inherited": inherited}


def map_radicals(text):
    return "".join(RADICALS.get(ch, ch) for ch in text)


def _blocks_reordered(blocks, text):
    """text 能否由 blocks（HTML 的文本块：打印时的行/块）各用一次、按某种先后拼成：只放过块级先后不同。"""
    blocks = [b for b in blocks if b]
    if len(blocks) < 2 or sorted("".join(blocks)) != sorted(text):
        return False
    budget = [20000]

    def walk(pos, left):
        if pos == len(text):
            return not left
        budget[0] -= 1
        if budget[0] < 0:
            return False
        tried = set()
        for i, block in enumerate(left):
            if block in tried or not text.startswith(block, pos):
                continue
            tried.add(block)
            if walk(pos + len(block), left[:i] + left[i + 1:]):
                return True
        return False
    return walk(0, sorted(blocks, key=len, reverse=True))


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*%?")


def numbers_in_order(text):
    """按出现顺序抽出数字串（含小数、千分位、百分号；单位前的数字也算）。"""
    return _NUMBER.findall(text)


def text_mismatch(html_record, pdf_record):
    """HTML 与 PDF 是否同一版：逐页比对文字（HTML 取不运行脚本时打印媒体下顶层页面文字，PDF 取页面文字；去空白与不显示
    字符，部首补充区字符按对照表换成对应汉字）。返回问题说明列表；空列表表示一致。只有整块（HTML 打印文字的一行/一个块）
    先后不同可以放过，且放过时两边按出现顺序抽出的数字串必须完全相同（r3：标签与数值分行写时，块级换序会把数字对调）；
    块内任何字的位置互换（如两个数字对调）都算不一致。"""
    html_pages = [p for p in html_record["pages"] if p.get("top")]
    pdf_pages = pdf_record["pages"]
    if len(html_pages) != len(pdf_pages):
        return [f"页数不一致：HTML {len(html_pages)} 页，PDF {len(pdf_pages)} 页"]
    problems = []
    for k, (a, b) in enumerate(zip(html_pages, pdf_pages), 1):
        x, y = map_radicals(a.get("text") or ""), map_radicals(b.get("text") or "")
        if x == y:
            continue
        if _blocks_reordered([map_radicals(t) for t in a.get("blocks") or []], y):
            if numbers_in_order(x) == numbers_in_order(y):
                continue
            problems.append(f"第 {k} 页文字块先后不同且数字先后不同：HTML {numbers_in_order(x)[:8]} / PDF {numbers_in_order(y)[:8]}")
            continue
        start = next((i for i, (c, d) in enumerate(zip(x, y)) if c != d), min(len(x), len(y)))
        problems.append(f"第 {k} 页文字不一致：HTML“{x[max(0, start - 8):start + 16]}” / PDF“{y[max(0, start - 8):start + 16]}”")
    return problems


def pair_documents(entries):
    """同名（去扩展名）的 HTML 与 PDF 配对；只有一份 HTML 和一份 PDF 时直接配对。返回 (配对, 配不上的文件)。"""
    htmls = [e for e in entries if Path(e["path"]).suffix.lower() in {".html", ".htm"}]
    pdfs = [e for e in entries if Path(e["path"]).suffix.lower() == ".pdf"]
    pairs = [(h, p) for h in htmls for p in pdfs if Path(h["path"]).stem == Path(p["path"]).stem]
    if not pairs and len(htmls) == 1 and len(pdfs) == 1:
        pairs = [(htmls[0], pdfs[0])]
    used = {e["path"] for pair in pairs for e in pair}
    unpaired = [e["path"] for e in htmls + pdfs if e["path"] not in used] if htmls and pdfs else []
    return pairs, unpaired


# ---------- 基线：最近一份通过检核所绑定的指纹记录 ----------

def _failed(record):
    return (record.get("status") not in PASS or record.get("uncovered")
            or any(f.get("level") == "red" for f in record.get("findings", []) if isinstance(f, dict)))


_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(?:\d+|[零〇一二两三四五六七八九十百]+)"
_DASH = r"\s*[-–—~～至到]\s*"
_SEP = r"\s*[、,，/和及与;；]\s*"
# 第 3 页、第三页、第3-5页、第 3、4 页、第3至第5页
_DI = re.compile(rf"第\s*({_NUM}(?:(?:{_DASH}|{_SEP})(?:第\s*)?{_NUM})*)\s*页")
# p3、P3-P5、page 3、p3–p5
_PX = re.compile(rf"(?<![A-Za-z])(?:[Pp]age|[Pp])\s*(\d+(?:(?:{_DASH}|{_SEP})(?:(?:[Pp]age|[Pp])\s*)?\d+)*)(?![A-Za-z0-9])")
_PAGE_WORDS = ("封面", "封底", "尾页", "末页", "最后一页", "首页", "目录页", "扉页")
_BARE = re.compile(rf"\d+(?:(?:{_DASH}|{_SEP})\d+)*")


def _cn_number(text):
    if text.isdigit():
        return int(text)
    if "百" in text or not text or any(ch not in _CN and ch != "十" for ch in text):
        raise ValueError(text)
    if "十" in text:
        tens, _, ones = text.partition("十")
        if "十" in ones:
            raise ValueError(text)
        return (_CN[tens] if tens else 1) * 10 + (_CN[ones] if ones else 0)
    if len(text) != 1:
        raise ValueError(text)
    return _CN[text]


def _expand(group):
    """“3-5”“3、4”“三至五”展开成页码；区间须完整展开，反向或写不清 → ValueError。"""
    out = []
    for part in re.split(_SEP, group):
        ends = [re.sub(r"^(?:第|[Pp]age|[Pp])\s*", "", e) for e in re.split(_DASH, part)]
        if len(ends) == 1:
            out.append(_cn_number(ends[0]))
        elif len(ends) == 2:
            a, b = _cn_number(ends[0]), _cn_number(ends[1])
            if b < a or b - a > 200:
                raise ValueError(part)
            out.extend(range(a, b + 1))
        else:
            raise ValueError(part)
    return out


def _page_field(value):
    """page 字段须整个写成页：整数、页码写法（可多个，用、，和 等分隔）或单个页标识。剩下认不出的字 → None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return {value}
    if isinstance(value, list):
        out = set()
        for item in value:
            got = _page_field(item)
            if got is None:
                return None
            out |= got
        return out
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    found, rest = set(), text
    try:
        for pattern in (_DI, _PX):
            for m in pattern.finditer(text):
                found |= set(_expand(m.group(1)))
            rest = pattern.sub(" ", rest)
        if not found and _BARE.fullmatch(text):
            return set(_expand(text))
    except (ValueError, KeyError):
        return None
    if found:
        return found if not re.sub(_SEP, "", rest).strip() else None
    return {text}  # 页标识（如 p5、page-4 或 section id），对不上当前页时由 compare 退回全页


def located_pages(record):
    """未通过检核的报告指出问题的页（r3 起严格）：findings 的 page 字段须整个能读成页（整数、“第 n 页”“第三页”“第3-5页”
    “第 3、4 页”“P3-P5”或单个页标识）；location 里写的页码写法都取出（区间完整展开）。page 字段有认不出的字、写法写不清，
    或 page 与 location 都没写页 → None（全页）。读出的页对不上当前存在的页（超出页数、页标识不存在）由 compare 退回全页。"""
    findings = record.get("findings") or []
    if record.get("uncovered") or not findings:
        return None
    tokens = set()
    for finding in findings:
        if not isinstance(finding, dict):
            return None
        found = set()
        if finding.get("page") not in (None, ""):
            got = _page_field(finding["page"])
            if got is None:
                return None
            found |= got
        location = finding.get("location")
        if isinstance(location, str):
            if any(word in location for word in _PAGE_WORDS):
                return None  # 用页名指页（封面、尾页等），按序号对不上，全页
            try:
                for pattern in (_DI, _PX):
                    for m in pattern.finditer(location):
                        found |= set(_expand(m.group(1)))
            except (ValueError, KeyError):
                return None
        if not found:
            return None
        tokens |= found
    return tokens


def dispatched_purpose(root, target, reviewer=None):
    """本次检核的用途以派工记录为准：agent_dispatch 的检核分派（review_plan），其次是程序写出的检核分派请求或视觉计划。
    返回用途；找不到记录返回 None。多份记录中有任一为 stage 即 stage。"""
    from agent_dispatch import read_log, replay
    items = [i for i in replay(read_log(root)).values() if i.get("review_target") == target and isinstance(i.get("review_plan"), dict)]
    mine = [i for i in items if reviewer and i.get("instance") == reviewer]
    purposes = {i["review_plan"].get("review_purpose") for i in (mine or items)}
    if not purposes:
        for rel in ([f"project/reviews/{target['key']}/r{target['version']}/检核分派请求.json"] if target.get("space") == "standalone" else []) + [_plan_path(target)]:
            try:
                data = json.loads(local(root, rel).read_text(encoding="utf-8"))
            except (OSError, ValueError, WorkflowError):
                continue
            if data.get("review_target", data.get("target")) == target and data.get("review_purpose") in {"stage", "revision", "final"}:
                purposes.add(data["review_purpose"])
    if not purposes:
        return None
    return "stage" if "stage" in purposes else sorted(purposes)[0] if len(purposes) == 1 else "stage"


def _plan_path(target):
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(target.get("key")))
    return f"{PLANS}/{target.get('space')}-{safe}-v{target.get('version')}.json"


def _excluded(root, target):
    """当前版本的作者与诊断者（含历次作者分派的实际实例）：基线检核员若在其中，基线不再独立。"""
    from incremental_review import excluded_instances
    from agent_dispatch import AUTHOR_ROLES, read_log, replay
    authors = set()
    if target["space"] == "standalone":
        from phase6_targets import catalog
        item = catalog(root).get(f"standalone/{target['key']}") or {}
        authors |= set(item.get("authors") or [])
        task_id = target["key"]
    else:
        from phase5_common import registry
        authors |= {m.get("author_instance") for m in registry(root).get("artifacts", {}).get(target["key"], [])}
        task_id = target["key"].split("::")[0]
    authors |= {d.get("instance") for d in replay(read_log(root)).values()
                if d.get("task_id") == task_id and d.get("role") in AUTHOR_ROLES and d.get("instance")}
    return excluded_instances(root, target, authors)


def _pick_base(root, target, records, purpose, simulation):
    """在该成果的检核记录里找视觉基线：跳过未通过的检核，回到最近一份通过的；其后（直到当前版本）未通过检核指出的页
    返回为 flagged。返回 (基线记录, flagged, 全页原因)。"""
    if purpose == "stage":
        return None, (), "阶段首次检核仍全页"
    upto = [r for r in records if type(r.get("target", {}).get("version")) is int and r["target"]["version"] <= target["version"]]
    earlier = [r for r in upto if r["target"]["version"] < target["version"]]
    if not earlier:
        return None, (), "该成果之前没有独立检核"
    passed = [k for k, r in enumerate(upto) if not _failed(r) and r["target"]["version"] < target["version"]]
    if not passed:
        return None, (), "之前没有通过的独立检核"
    latest = upto[passed[-1]]
    flagged = set()
    for later in upto[passed[-1] + 1:]:
        if _failed(later):
            pages = located_pages(later)
            if pages is None:
                return None, (), "此后有未通过的检核，其指出的问题无法定位到页（或证据不足/未覆盖），视觉结论不沿用"
            flagged |= pages
    if simulation is not None and latest.get("simulation") is not simulation:
        return None, (), "基线与当前模拟状态不一致"
    if latest.get("reviewer_instance") in _excluded(root, target):
        return None, (), "基线检核员后来参与了创作，不再独立"
    return latest, flagged, None


def _standalone_records(root, target):
    from incremental_review import _records
    return [r for r in _records(root, target) if r.get("target", {}).get("key") == target["key"]]


def _artifact_match(current_path, kind, candidates):
    """在基线报告的视觉条目里找对应成品：同路径 → 同文件名 → 同类型唯一一份；找不到或有歧义返回 None。"""
    same = [c for c in candidates if c["artifact"]["path"] == current_path]
    if len(same) == 1:
        return same[0]
    named = [c for c in candidates if Path(c["artifact"]["path"]).name == Path(current_path).name]
    if len(named) == 1:
        return named[0]
    typed = [c for c in candidates if c["kind"] == kind]
    return typed[0] if len(typed) == 1 else None


def standalone_base(root, target, current_path, kind, purpose, simulation=None):
    """返回 (base_review, base_record, reason, flagged)；reason 非空表示须全页。"""
    from phase6_events import valid_file
    from phase2_store import read_json
    latest, flagged, reason = _pick_base(root, target, _standalone_records(root, target), purpose, simulation)
    if reason:
        return None, None, reason, ()
    evidence = latest.get("evidence")
    if not valid_file(root, evidence):
        return None, None, "基线报告原件缺失或改变", ()
    report = read_json(local(root, evidence["path"]))
    candidates = []
    for entry in report.get("visual_evidence", []) if isinstance(report.get("visual_evidence"), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("fingerprint"), dict) and isinstance(entry.get("artifact"), dict):
            try:
                record = read_record(root, entry["fingerprint"], entry["artifact"])
            except (WorkflowError, OSError, ValueError):
                continue
            candidates.append({"artifact": entry["artifact"], "kind": record["kind"], "record": record, "ref": entry["fingerprint"]})
    if not candidates:
        return None, None, "基线报告为旧格式或没有像素指纹", ()
    match = _artifact_match(current_path, kind, candidates)
    if match is None:
        return None, None, "无法确定基线中对应的成品", ()
    return ({"record_id": latest["record_id"], "evidence": evidence, "fingerprint": match["ref"]}, match["record"], None,
            tuple(sorted(flagged, key=str)))


def page_mismatch(record, expected):
    """程序页定义（required_pages）与渲染得到的页必须一一相同，否则不允许继承。"""
    got = [p["label"] for p in record["pages"]]
    return None if got == list(expected) else f"渲染得到的页（{len(got)} 页）与页面定义（{len(expected)} 页）不一致"


def plan_standalone(root, target, meta, purpose="revision", record_refs=None, simulation=None):
    """检核准备：为当前每份 HTML/PDF 生成（或读取）指纹并算出必看页；同时做 HTML/PDF 同版文字核对。"""
    from standalone_visual import required_pages
    root = Path(root).resolve()
    visuals = [x for x in meta["task"]["deliverables"] if "path" in x and Path(x["path"]).suffix.lower() in {".html", ".htm", ".pdf"}]
    plans, records = [], {}
    for artifact in visuals:
        kind = "pdf" if artifact["path"].lower().endswith(".pdf") else "html"
        item = {"artifact": {"path": artifact["path"], "sha256": artifact["sha256"]}}
        names = required_pages(local(root, artifact["path"]))
        try:
            if record_refs and artifact["path"] in record_refs:
                ref = record_refs[artifact["path"]]; record = verify(root, ref, artifact)
            else:
                ref, record = fingerprint(root, artifact["path"])
                if record["artifact"]["sha256"] != artifact["sha256"]:
                    raise WorkflowError(f"{artifact['path']} 与登记指纹不符")
        except Unavailable as exc:
            plans.append({**item, "fingerprint": None, "base_review": None, "mode": "full", "full_reason": str(exc),
                          "must_see": [{"page": n, "reason": str(exc)} for n in names], "inherited": []})
            continue
        records[artifact["path"]] = record
        base_review, base_record, reason, flagged = standalone_base(root, target, artifact["path"], kind, purpose, simulation)
        reason = reason or page_mismatch(record, names)
        result = compare(base_record, record, reason_full=reason, flagged=flagged)
        plans.append({**item, "fingerprint": ref, "base_review": base_review if result["inherited"] else None, **result})
    consistency = []
    pairs, unpaired = pair_documents(visuals)
    for html, pdf in pairs:
        if html["path"] in records and pdf["path"] in records:
            consistency.append({"html": html["path"], "pdf": pdf["path"],
                                "problems": text_mismatch(records[html["path"]], records[pdf["path"]])})
        elif html["path"] in records:
            consistency.append({"html": html["path"], "pdf": pdf["path"],
                                "problems": text_mismatch(records[html["path"]], pdf_text_record(local(root, pdf["path"])))})
        else:
            consistency.append({"html": html["path"], "pdf": pdf["path"], "problems": None, "manual_required": True,
                                "notice": "程序比对不可用（缺本机浏览器渲染）：检核员须逐页人工确认 HTML 与 PDF 为同一版，"
                                          "在报告该 HTML 条目写 same_version_manual"})
    if unpaired:
        consistency.append({"html": None, "pdf": None, "problems": None, "unpaired": unpaired,
                            "notice": "这些 HTML/PDF 配不上对（不同名且不止一对），程序没有核对它们是否同一版；改成同名或由检核员人工确认"})
    return {"artifacts": plans, "html_pdf_consistency": consistency}


# ---------- 正式演示稿（phase5 html-deck）：页 = 第几个顶层 <section> ----------

def deck_html(meta):
    files = [f for f in meta.get("current_files", []) if f.get("role") == "html"]
    if len(files) != 1:
        raise WorkflowError("正式演示稿须有唯一一份当前 HTML")
    return {"path": files[0]["path"], "sha256": files[0]["sha256"]}


def deck_base(root, meta, purpose, simulation=None):
    """正式演示稿基线：同一套规则（跳过未通过检核回到最近一份通过的、退回指出的页必看、独立性与模拟一致），
    且基线报告里带程序生成的 visual_increment 指纹。返回 (base_review, base_record, reason, flagged)。"""
    from phase5_common import events, valid_event_file, read_json, ref as target_ref
    target = target_ref(meta)
    records = [r for r in events(root, "review") if r.get("target", {}).get("key") == meta["key"]]
    latest, flagged, reason = _pick_base(root, target, records, purpose, simulation)
    if reason:
        return None, None, reason.replace("该成果", "该演示稿"), ()
    if not valid_event_file(root, latest.get("evidence")):
        return None, None, "基线报告原件缺失或改变", ()
    report = read_json(local(root, latest["evidence"]["path"]))
    block = report.get("visual_increment")
    if not isinstance(block, dict) or not isinstance(block.get("fingerprint"), dict):
        return None, None, "基线报告为旧格式或没有像素指纹", ()
    try:
        record = read_record(root, block["fingerprint"])
    except (WorkflowError, OSError, ValueError):
        return None, None, "基线指纹记录缺失、改变或为旧格式", ()
    return ({"record_id": latest["record_id"], "evidence": latest["evidence"], "fingerprint": block["fingerprint"]}, record, None,
            tuple(sorted(flagged, key=str)))


def plan_deck(root, meta, purpose="revision", simulation=None, write_plan=True):
    from phase5_common import ref as target_ref
    from phase5_visual import payload
    root = Path(root).resolve()
    artifact = deck_html(meta)
    count = len(payload(root, meta)["pages"])
    target = target_ref(meta)
    try:
        ref, record = fingerprint(root, artifact["path"])
    except Unavailable as exc:
        out = {"artifact": artifact, "fingerprint": None, "base_review": None, "mode": "full", "full_reason": str(exc),
               "must_see": [{"page": str(n), "reason": str(exc)} for n in range(1, count + 1)], "inherited": []}
    else:
        base_review, base_record, reason, flagged = deck_base(root, meta, purpose, simulation)
        tops = len([p for p in record["pages"] if p.get("top")])
        reason = reason or (None if tops == count else f"HTML 顶层页面数（{tops}）与页级方案页数（{count}）不一致")
        result = compare(base_record, record, top_only=True, reason_full=reason, flagged=flagged)
        kept = base_review if result["inherited"] else None
        out = {"artifact": artifact, "fingerprint": ref, "base_review": kept, **result,
               "report_field": {"visual_increment": {"fingerprint": ref, "base_review": kept,
                                                     "inherited_pages": [{"page": int(x["page"]), "pixel_sha256": x["pixel_sha256"]} for x in result["inherited"]]}}}
    if write_plan:
        from phase2_store import atomic_json
        rel = _plan_path(target)
        atomic_json(local(root, rel), {"target": target, "review_purpose": purpose, "simulation": simulation,
                                       "must_see": [m["page"] for m in out["must_see"]], "mode": out["mode"]})
        out["plan_path"] = rel
    return out


def summary(plan):
    rows = []
    for item in plan["artifacts"]:
        rows.append({"path": item["artifact"]["path"], "mode": item["mode"], "must_see": [m["page"] for m in item["must_see"]],
                     "inherited": len(item["inherited"]), "full_reason": item.get("full_reason")})
    return rows


def main():
    parser = argparse.ArgumentParser(description="演示稿视觉增量：生成逐页像素指纹或比对两份指纹记录")
    sub = parser.add_subparsers(dest="command", required=True)
    fp = sub.add_parser("fingerprint"); fp.add_argument("--workspace", type=Path, required=True); fp.add_argument("--path", required=True)
    dp = sub.add_parser("deck-plan", help="正式演示稿当前版本的必看页（并输出报告里 visual_increment 字段原样）")
    dp.add_argument("--workspace", type=Path, required=True); dp.add_argument("--key", required=True)
    dp.add_argument("--purpose", choices=["stage", "revision", "final"], default="revision"); dp.add_argument("--json", action="store_true")
    dp.add_argument("--simulation", action="store_true", help="合成检核（test_mode）；基线须同为合成")
    cp = sub.add_parser("compare"); cp.add_argument("--workspace", type=Path, required=True)
    cp.add_argument("--base"); cp.add_argument("--current", required=True)
    for p in (fp, cp):
        p.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    if args.command == "fingerprint":
        ref, record = fingerprint(root, args.path)
        out = {"record": ref, "pages": len(record["pages"]), "unstable": [p["label"] for p in record["pages"] if not p["stable"]],
               "complete": record["complete"], "environment_sha256": record["environment_sha256"]}
    elif args.command == "deck-plan":
        from phase5_common import current
        out = plan_deck(root, current(root, args.key), args.purpose, simulation=args.simulation, write_plan=True)
    else:
        def load(path):
            return json.loads(local(root, path).read_text(encoding="utf-8"))
        out = compare(load(args.base) if args.base else None, load(args.current))
    print(json.dumps(out, ensure_ascii=False, indent=2))
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
