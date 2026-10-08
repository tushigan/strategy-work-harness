"""演示稿 HTML 的“画面 / 非画面”源码口径：内部用语扫描、作者自检、截图与对外导出的页码共用这一份定义。

按 HTML 规范近似建元素栈：空元素不入栈；非空元素的自闭合写法（<div hidden/>）按浏览器规则当开始标签；
p/li/dt/dd/tr/td/th/thead/tbody/tfoot/option/optgroup/rp/rt 等按规范隐式闭合；结束标签按最近的同名元素弹栈。
非画面元素（源码约定）：脚本/模板/aside/讲者稿与工具栏标记、hidden 或 display:none / visibility:hidden
（含 <style> 里简单 .类 / #id 选择器）、页面之外的页眉页脚导航。顶层 <section> 是页面：即使被隐藏也算一页。

这是无浏览器时的源码判断，只用于扫描与自检提示（输出注明“未经浏览器核对”）；对外导出以浏览器实际渲染为准
（duiwai_daochu + deck_external.mjs），这里只提供页码定义：第几页 = 第几个顶层页面在全部 <section> 里的序号。"""
import html as html_lib
import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
DROP_TAGS = {"script", "template", "noscript", "aside", "iframe", "object", "embed"}
MARKER_ATTRS = {"data-speaker-notes", "data-deck-toolbar"}
# 讲者稿/工具栏：类名与 id 按精确名单（不区分大小写）判断，不按“含 -note / speaker”之类的片段猜，
# 以免包自带模板的 .chapter-note、常见的 .side-note 等正文被删。对外导出（deck_external.mjs）用同一份名单。
NOTE_CLASSES = frozenset({"notes", "speaker-notes", "speaker-note", "presenter-notes", "slide-notes", "deck-notes",
                          "toolbar", "deck-toolbar", "tools", "edit-mode"})
NOTE_IDS = frozenset({"speaker", "speaker-notes", "speaker-text", "speaker-transition", "notes", "presenter-notes",
                      "toolbar", "deck-toolbar"})
NOTE_ATTR = re.compile(r"note|speaker|toolbar", re.I)
HIDE_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden|content-visibility\s*:\s*hidden)", re.I)
BLOCK = {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset", "figcaption", "figure", "footer", "form",
         "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul"}
TOOL_TAGS = {"header", "footer", "nav"}
# 浏览器把这些元素的内容当文字（或不进 DOM 查询），其中的 <section> 不计入序号
RAW_TEXT = {"textarea", "title", "iframe", "xmp", "noembed", "noframes", "template", "plaintext"}
FOREIGN = {"svg", "math"}
# 隐式闭合：开始标签 → (被它闭合的元素, 查找到这些元素即停止的边界)
SCOPE = {"html", "table", "td", "th", "caption", "template", "marquee", "object", "applet", "svg", "math"}
P_CLOSERS = BLOCK | {"li", "dd", "dt", "menu", "hgroup", "dialog", "search", "summary", "center", "dir", "listing", "plaintext", "xmp"}
TABLE_BOUND = {"html", "table", "template"}
_SECTION = {"thead", "tbody", "tfoot"}
_RUBY = {"ruby", "html", "template"}
IMPLIED = {  # 每步：(要闭合的最近元素, 查到这些元素即停止)；按规范把它及其上方未闭合的元素一起弹出
    "li": [({"li"}, SCOPE | {"ul", "ol"})],
    "dt": [({"dt", "dd"}, SCOPE | {"dl"})], "dd": [({"dt", "dd"}, SCOPE | {"dl"})],
    "tr": [({"tr"}, TABLE_BOUND | _SECTION)],
    "td": [({"td", "th"}, TABLE_BOUND | {"tr"})], "th": [({"td", "th"}, TABLE_BOUND | {"tr"})],
    "thead": [(_SECTION | {"caption", "colgroup"}, TABLE_BOUND)],
    "tbody": [(_SECTION | {"caption", "colgroup"}, TABLE_BOUND)],
    "tfoot": [(_SECTION | {"caption", "colgroup"}, TABLE_BOUND)],
    "caption": [({"caption", "colgroup"} | _SECTION, TABLE_BOUND)], "colgroup": [({"caption", "colgroup"} | _SECTION, TABLE_BOUND)],
    "option": [({"option"}, {"select", "datalist", "optgroup", "html", "template"})],
    "optgroup": [({"option"}, {"select", "datalist", "optgroup", "html", "template"}), ({"optgroup"}, {"select", "html", "template"})],
    "rp": [({"rp", "rt", "rb"}, _RUBY | {"rtc"})], "rt": [({"rp", "rt", "rb"}, _RUBY | {"rtc"})],
    "rb": [({"rb", "rp", "rt"}, _RUBY | {"rtc"}), ({"rtc"}, _RUBY)], "rtc": [({"rb", "rp", "rt"}, _RUBY | {"rtc"}), ({"rtc"}, _RUBY)],
}
TEXT_BREAK = BLOCK | {"li", "dt", "dd", "tr", "td", "th", "br", "caption", "option", "summary", "figcaption"}


def _strip_media(css):
    """去掉 @media/@supports 块（打印专用的隐藏不算画面隐藏），保留普通规则。"""
    out, i = [], 0
    while i < len(css):
        m = re.compile(r"@(?:media|supports)[^{]*\{", re.I).search(css, i)
        if not m:
            out.append(css[i:]); break
        out.append(css[i:m.start()]); depth, j = 1, m.end()
        while j < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0); j += 1
        i = j
    return "".join(out)


def hidden_selectors(css_texts):
    """<style> 里把元素隐藏的简单选择器：.类、#id、标签.类（复杂选择器不判断，只按类名/ID 粗判）。"""
    classes, ids = set(), set()
    for css in css_texts:
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _strip_media(re.sub(r"/\*.*?\*/", "", css, flags=re.S))):
            if not HIDE_STYLE.search(";" + body):
                continue
            for part in selector.split(","):
                part = part.strip()
                if re.fullmatch(r"[\w-]*(?:\.[\w-]+)+", part):
                    classes.update(re.findall(r"\.([\w-]+)", part))
                elif re.fullmatch(r"[\w-]*#[\w-]+", part):
                    ids.add(part.split("#", 1)[1])
    return classes, ids


class _Styles(HTMLParser):
    def __init__(self):
        super().__init__(); self.css = []; self._in = False

    def handle_starttag(self, tag, attrs):
        self._in = tag == "style"

    def handle_endtag(self, tag):
        if tag == "style":
            self._in = False

    def handle_data(self, data):
        if self._in:
            self.css.append(data)


def note_names():
    """交给浏览器一侧的讲者稿/工具栏名单（与源码口径同一份）。"""
    return {"classes": sorted(NOTE_CLASSES), "ids": sorted(NOTE_IDS)}


def styles(text):
    p = _Styles(); p.feed(text); p.close(); return p.css


def nonvisual(tag, attrs, in_page, hidden=(set(), set()), top_section=False):
    """返回该元素不属于画面的原因；None 表示属于画面。top_section：顶层页面本身（隐藏也保留）。"""
    a = dict(attrs); classes = set((a.get("class") or "").split()); element_id = a.get("id") or ""
    if tag in DROP_TAGS:
        return "脚本/模板/aside/内嵌页"
    if MARKER_ATTRS & set(a):
        return "讲者稿/工具栏标记"
    if {c.lower() for c in classes} & NOTE_CLASSES or element_id.lower() in NOTE_IDS:
        return "讲者稿/工具栏类名"
    if tag in TOOL_TAGS and not in_page:
        return "页面之外的页眉/页脚/导航"
    if top_section:
        return None
    if "hidden" in a or HIDE_STYLE.search(";" + (a.get("style") or "")) or classes & hidden[0] or element_id in hidden[1]:
        return "隐藏元素"
    return None


class Huamian(HTMLParser):
    """元素栈解析。子类覆盖 start/end/text/comment/decl 钩子；visible 表示当前是否处在画面内。"""

    def __init__(self, text=None, hidden=None):
        super().__init__(convert_charrefs=False)
        self.hidden = hidden if hidden is not None else hidden_selectors(styles(text) if text else [])
        self.stack = []          # [tag, dropped(bool), page(bool)]
        self.pages = []          # 顶层页面的标识（data-page / id / 序号）
        self.dropped = []        # 被判为非画面的元素：(tag, 原因)
        self.sections = 0        # 浏览器可查询到的 <section> 总数（文档顺序）
        self.page_ordinals = []  # 每个顶层页面在全部 <section> 里的序号（0 起）

    # 状态
    @property
    def visible(self):
        return not any(item[1] for item in self.stack)

    @property
    def in_page(self):
        return any(item[2] for item in self.stack)

    @property
    def in_head(self):
        return any(item[0] in {"head", "title", "style"} for item in self.stack)

    @property
    def foreign(self):
        return any(item[0] in FOREIGN for item in self.stack)

    @property
    def raw_text(self):
        return any(item[0] in RAW_TEXT for item in self.stack)

    @property
    def page(self):
        return len(self.pages) if self.in_page else 0

    def _find(self, targets, boundary):
        for index in range(len(self.stack) - 1, -1, -1):
            name = self.stack[index][0]
            if name in targets:
                return index
            if name in boundary:
                return None
        return None

    def _close_implied(self, tag):
        if self.foreign:
            return
        if tag in P_CLOSERS:
            index = self._find({"p"}, SCOPE | {"button"})
            if index is not None:
                self._pop_to(index)
        for targets, boundary in IMPLIED.get(tag, ()):
            index = self._find(targets, boundary)
            if index is not None:
                self._pop_to(index)

    def _pop_to(self, index):
        while len(self.stack) > index:
            tag, dropped, page = self.stack.pop()
            self.end(tag, not dropped and self.visible, dropped)

    def handle_starttag(self, tag, attrs):
        self._close_implied(tag)
        was_visible = self.visible
        top = tag == "section" and was_visible and not self.in_page and not self.raw_text and not self.foreign
        reason = nonvisual(tag, attrs, self.in_page, self.hidden, top) if was_visible else None
        if reason:
            self.dropped.append((tag, reason)); top = False
        if tag == "section" and not self.raw_text:
            if top:
                self.page_ordinals.append(self.sections)
            self.sections += 1
        if top:
            a = dict(attrs); self.pages.append(a.get("data-page") or a.get("id") or str(len(self.pages) + 1))
        self.start(tag, attrs, was_visible and not reason, bool(reason))
        if tag not in VOID:
            self.stack.append([tag, bool(reason), top])

    def handle_startendtag(self, tag, attrs):
        """HTML 里非空元素的“/>”无效（浏览器当开始标签，后面的内容都在它里面）；只有 SVG/MathML 里才是空元素。"""
        foreign = self.foreign or tag in FOREIGN
        self.handle_starttag(tag, attrs)
        if tag not in VOID and foreign:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self._pop_to(index); return
        # 多余的结束标签：忽略

    def handle_data(self, data):
        self.text(data, self.visible and not self.in_head)

    def handle_entityref(self, name):
        self.text(f"&{name};", self.visible and not self.in_head, raw=True)

    def handle_charref(self, name):
        self.text(f"&#{name};", self.visible and not self.in_head, raw=True)

    def handle_comment(self, data):
        self.comment(data)

    def handle_decl(self, decl):
        self.decl(decl)

    def close(self):
        super().close(); self._pop_to(0)

    # 钩子
    def start(self, tag, attrs, visible, dropped):
        pass

    def end(self, tag, visible, dropped):
        pass

    def text(self, data, visible, raw=False):
        pass

    def comment(self, data):
        pass

    def decl(self, decl):
        pass


class ScreenText(Huamian):
    """逐页收集画面文字与图片；同时收集非画面文字（讲者稿、隐藏、注释、脚本、讲者属性），供提示。
    文字按原样拼接（行内标签、实体字符不拆词），只在块级元素边界加空格。"""

    def __init__(self, text):
        super().__init__(text); self.page_text = {0: []}; self.images = {0: []}; self.hidden_text = []

    def _break(self, tag):
        if tag in TEXT_BREAK:
            self.page_text.setdefault(self.page, []).append(" ")

    def start(self, tag, attrs, visible, dropped):
        a = dict(attrs); page = self.page if not (tag == "section" and visible and not self.in_page) else len(self.pages)
        self.page_text.setdefault(page, []); self.images.setdefault(page, [])
        if visible and tag in TEXT_BREAK:
            self.page_text[page].append(" ")
        if visible and tag == "img":
            self.images[page].append(a)
            if a.get("alt"):
                self.page_text[page].append(" " + a["alt"] + " ")
        for k, v in attrs:
            if v and (NOTE_ATTR.search(k) or (not visible and k in {"alt", "title"})):
                self.hidden_text.append(v)

    def end(self, tag, visible, dropped):
        if visible:
            self._break(tag)

    def text(self, data, visible, raw=False):
        if raw:
            data = html_lib.unescape(data)
        if not data.strip():
            if visible and data:
                self.page_text.setdefault(self.page, []).append(" ")
            return
        if visible:
            self.page_text.setdefault(self.page, []).append(data)
        elif not self.in_head or any(item[1] for item in self.stack):
            self.hidden_text.append(data)

    def comment(self, data):
        self.hidden_text.append(data)


def screen(text):
    """返回 {pages: [页标识], text: {页序号: 文字}, images: {页序号: [属性]}, hidden_text: [非画面文字],
    page_ordinals: [顶层页面在全部 section 里的序号], sections: section 总数}；页序号 0 为页面之外。"""
    parser = ScreenText(text); parser.feed(text); parser.close()
    return {"pages": parser.pages, "text": {k: re.sub(r"\s+", " ", "".join(v)).strip() for k, v in parser.page_text.items()},
            "images": parser.images, "hidden_text": parser.hidden_text, "dropped": parser.dropped, "hidden": parser.hidden,
            "page_ordinals": parser.page_ordinals, "sections": parser.sections, "css": styles(text),
            "basis": "源码解析（未经浏览器核对）"}


def css_content(found_or_text):
    """<style> 里 content: 声明中的字符串（::before/::after 文字，会上屏），按 CSS 转义还原。"""
    css = styles(found_or_text) if isinstance(found_or_text, str) else found_or_text.get("css", [])
    result = []
    for block in css:
        block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
        for value in re.findall(r"content\s*:\s*([^;}]*)", block, re.I):
            for a, b in re.findall(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', value):
                text = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", lambda m: chr(int(m.group(1), 16)), a or b)
                result.append(re.sub(r"\\(.)", r"\1", text))
    return result
