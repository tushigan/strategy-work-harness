#!/usr/bin/env python3
"""说人话扫描（v1.7.5 G15）：给策略师读的文件或一段话里，找出程序用语、命令名、英文字段，给白话替换建议。

只扫“给策略师”部分：遇到标题行“## 技术附录（给 Agent）”（1—3 个 #）即停，之后是给 Agent 看的，不受限；行内出现这几个字不停（返修 #10）。
三类命中：词表里的程序用语（templates/说人话词表.tsv，项目可在 project/inputs/说人话词表.txt 追加）；没附白话的编号（如单独写 G04、r13）；
白名单外的英文（白名单只写在词表文件的“白名单：”一行，程序只读这一行，提示文字也从它拼出；项目品牌名、产品名从项目身份与项目记忆读，
也可在项目词表里写“允许：名称”）。
代码块（``` 包起来的命令）、注释与网址不扫：要策略师复制的命令放代码块并在前面写一句是干什么的。只提示，不改原文；输出格式同 neibu_yongyu.py。
英文例外（v1.7.5 G15 修订，用户 2026-10-11 拍板）：网站名、产品名、品牌名，和画面上照原样显示的字（按钮、菜单、模型名、思考等级档位）
可以照原样写，加引号（「」或“”），第一次出现附一句白话。引号内的英文不算命中，另列在 quoted_english 里，quoted_notice 提示
“引号内英文 N 处（请确认是名称或画面上原样显示的字）”，status 给 attention——程序分不出引号里是不是名称，这一项靠自查；引号外照报，
编号在引号内也照报；引号里像参数（- 开头）、文件名（带后缀）、带 _ 的字段、带 / 的路径、版本号（v 加数字）、任务编号（如 ST-001）的
也照报（候选 r4 #6：整句或整条命令加引号不能躲过扫描）；候选 r5 #7 起按包住它的整段（连续的非空白英文字符）判断，
引号里以波浪号、斜杠或点加斜杠开头的单段路径、点开头的文件名、小写或多字母的任务编号（st-001、PROJ-12）也照报。
对话正文没法用程序拦截，主控发话前可以用 --text 自查一段话。输出始终是 JSON。
常见业务编号（季度、半年、H5 页面、纸张尺寸等）放行，放行表只写在词表文件的“允许：”行里，程序不另写一份。"""
import argparse
import json
import re
import sys
from pathlib import Path

from phase2_store import WorkflowError

PACKAGE_WORDS = Path(__file__).resolve().parents[1] / "templates/说人话词表.tsv"
PROJECT_WORDS = "project/inputs/说人话词表.txt"
APPENDIX_HEADING = re.compile(r"^\s{0,3}#{1,3}\s*技术附录（给 Agent）\s*$")
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
URL = re.compile(r"https?://[^\s<>\"'）)]+")
# 编号：大写字母 1—2 个 + 数字（G04、E2、M1、P0），或 r + 数字（r13 第 13 次修订）。后面紧跟全角括号里的白话就算已解释。
CODE = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Z]{1,2}\d{1,3}|r\d{1,4})(?![A-Za-z0-9_])")
EXPLAINED = re.compile(r"\s?（[^）]*[一-鿿][^）]*）")
# 英文只按 ASCII 识别（返修 #4）：“PPT已经好了”只取 PPT，不把后面的汉字算进英文词
ENGLISH = re.compile(r"--?[A-Za-z][A-Za-z0-9_\-]*|[A-Za-z][A-Za-z0-9_\-]*(?:[./][A-Za-z0-9_\-]+)*")
FILE_SUFFIX = re.compile(r"\.(?:py|json|jsonl|md|html?|js|mjs|css|txt|tsv|zip|sh|toml|yaml|yml)$", re.I)
# 英文例外的引号（G15 修订）：只认同一行里成对的「」与“”
QUOTED = re.compile(r"「[^「」\n]*」|“[^“”\n]*”")
ASCII_WORD = re.compile(r"[\x21-\x7e]+(?: [\x21-\x7e]+)*")
ASCII_RUN = re.compile(r"[\x21-\x7e]+")
# 任务编号（如 ST-001、PJ-12）：字母 + 连字符 + 数字（候选 r4 #6；候选 r5 #7 起大小写都算、字母可以多于 4 个，如 st-001、PROJ-12）
TASK_ID = re.compile(r"[A-Za-z]{1,10}-\d{1,6}")
DOTFILE = re.compile(r"\.[A-Za-z][A-Za-z0-9_\-.]*")
VERSION = re.compile(r"v\d+(?:\.\d+)*")


def shaped(word):
    """像程序用的东西（候选 r4 #6）：参数（- 开头）、文件名（带后缀）、带 _ 的字段、带 / 的路径、版本号（v 加数字）、任务编号。
    这些在引号里也照报——英文例外只放名称与画面上照原样显示的字。"""
    word = word.rstrip(".,;:!?)]}'\"")  # 候选 r5 #7：句末标点不算词的一部分
    return bool(word.startswith(("-", "~")) or "/" in word or "_" in word or FILE_SUFFIX.search(word) or DOTFILE.fullmatch(word)
                or VERSION.fullmatch(word) or TASK_ID.fullmatch(word))


def _words(line, head):
    return [w for w in re.split(r"[\s,，、]+", line[len(head):].strip()) if w]


def _entries(lines, source):
    terms, allowed, whitelist = [], set(), []
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("允许：", "允许:")):
            allowed.update(w.casefold() for w in _words(line, "允许："))
            continue
        if line.startswith(("白名单：", "白名单:")):  # 英文白名单（r3 #9：只在词表这一行）
            whitelist += _words(line, "白名单：")
            continue
        word, _, plain = line.partition("\t")
        word, plain = word.strip(), plain.strip() or "换成白话"
        try:
            if word.startswith("re:"):
                pattern = re.compile(word[3:])
            elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", word):
                pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", re.I)  # 连字符拼的词（如 delivery-gate）也算
            else:
                pattern = re.compile(re.escape(word))
        except re.error as exc:
            raise WorkflowError(f"{source} 第 {number} 行写法有误（{exc}）：{line[:60]}") from None
        terms.append((word, pattern, plain))
    return terms, allowed, whitelist


def vocabulary(root=None):
    """(词条, 放行词, 白名单原文列表)。白名单只取包内词表的“白名单：”行；项目词表里的“白名单：”按“允许：”放行。"""
    terms, allowed, whitelist = _entries(PACKAGE_WORDS.read_text(encoding="utf-8").splitlines(), "templates/说人话词表.tsv")
    if root is not None and (Path(root) / PROJECT_WORDS).is_file():
        extra, more, listed = _entries((Path(root) / PROJECT_WORDS).read_text(encoding="utf-8").splitlines(), PROJECT_WORDS)
        terms, allowed = extra + terms, allowed | more | {w.casefold() for w in listed}  # 项目词条优先
    return terms, allowed, whitelist


BRAND_TYPES = re.compile(r"brand|product|company|client|competitor|partner|品牌|产品|公司|客户|竞品|合作", re.I)
# 品牌英文名只取首字母大写或全大写的词（Cookio、MAXI），普通小写词与这几个常见虚词不放行（返修 #12）
BRAND_WORD = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9&'\-]*")
COMMON_WORDS = {"and", "or", "the", "a", "an", "it", "of", "to", "in", "on", "for", "with", "by", "is", "new", "plus"}


def brand_words(root):
    """项目里的品牌名、产品名（英文部分）：项目身份 + 项目记忆里品牌/产品/公司类条目的实体名与当前值。读不到就如实说明。"""
    if root is None:
        return set(), "没有给工作区，只按白名单"
    found, sources, broken = set(), [], False
    try:
        state = json.loads((Path(root) / "project/state.json").read_text(encoding="utf-8"))
        texts = [state.get(k) for k in ("brand_name", "client_name", "project_name") if isinstance(state.get(k), str)]
        if texts:
            sources.append("项目身份")
    except (OSError, ValueError):
        texts = []
    try:
        from project_memory_store import inspect as memory
        data = memory(root)
        if data.get("status") == "blocked":  # 记忆损坏或读不了：inspect 不抛错而是报 blocked
            raise ValueError("project memory blocked")
        entries = data["current_facts"] + data["needs_confirmation"] + data["needs_attention"]
        picked = [e for e in entries if BRAND_TYPES.search(str(e.get("entity_type", "")))]
        for e in picked:
            texts += [str(e.get("entity_id", "")), str(e.get("current_value", ""))]
        if picked:
            sources.append("项目记忆")
    except Exception:  # 记忆读不了不影响扫描，只少了品牌名，并如实说明
        broken = True
    for text in texts:
        found.update(w.casefold() for w in BRAND_WORD.findall(text or "") if w.casefold() not in COMMON_WORDS)
    if broken:
        note = "项目记忆读不了，品牌英文名可能被报出" + (("；已取" + "、".join(sources)) if found else "") + "；可在 " + PROJECT_WORDS + " 写“允许：名称”"
    elif found:
        note = "品牌名取自" + "、".join(sources)
    else:
        note = "未读到项目品牌名，只按白名单；品牌英文名会被报出，可在 " + PROJECT_WORDS + " 写“允许：名称”"
    return found, note


def plain_part(text):
    """取“给策略师”部分：到标题行“## 技术附录（给 Agent）”为止（代码块里的不算）；返回 (行列表, 停在第几行或 None, 全文行数)。"""
    lines, fenced = text.splitlines(), False
    for index, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and APPENDIX_HEADING.match(line):
            return lines[:index], index + 1, len(lines)
    return lines, None, len(lines)


def scan_text(text, root=None):
    terms, allowed, listed = vocabulary(root)
    whitelist = {w.casefold() for w in listed}
    brands, brand_note = brand_words(root)
    allowed |= brands
    # 注释（<!-- -->）不显示给读者，不扫；换成空白以保留行号
    text = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    lines, stopped, total = plain_part(text)
    hits, quoted, fenced = [], [], False
    for number, line in enumerate(lines, 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        taken = [m.span() for m in URL.finditer(line)] + [m.span() for m in re.finditer(r"技术附录（给 Agent）", line)]  # 固定栏目名不报

        def free(span):
            return not any(span[0] < b and a < span[1] for a, b in taken)

        quotes = [m.span() for m in QUOTED.finditer(line)]

        runs = [m.span() for m in ASCII_RUN.finditer(line)]

        def whole_of(span, word):
            """包住它的整段（连续的非空白英文字符，候选 r5 #7）：波浪号加斜杠开头的路径里的词按整段路径判断、点开头的文件名里的词按整个文件名判断。"""
            return next((line[a:b] for a, b in runs if a <= span[0] and span[1] <= b), word)

        def quoted_at(span):
            return any(a < span[0] and span[1] < b for a, b in quotes)

        def in_quotes(span, word):
            """整段在一对引号里、且是英文（ASCII）——按英文例外只列进提示，不算命中；
            像参数、文件名、字段、路径、版本号、任务编号的照报（候选 r4 #6；按包住它的整段判断，如「brief.json」里的 json）。"""
            return bool(ASCII_WORD.fullmatch(word)) and not shaped(word) and not shaped(whole_of(span, word)) and quoted_at(span)

        def keep(category, term, span):
            taken.append(span)
            start = max(0, span[0] - 12)
            quoted.append({"line": number, "term": term, "would_be": category, "context": line[start:span[1] + 12].strip()})

        def add(category, term, suggestion, span):
            taken.append(span)
            start = max(0, span[0] - 12)
            hits.append({"line": number, "category": category, "term": term, "suggestion": suggestion,
                         "context": line[start:span[1] + 12].strip()})

        for word, pattern, plain in terms:
            for m in pattern.finditer(line):
                if free(m.span()) and m.group(0).casefold() not in allowed | whitelist:  # 白名单里的词不当程序用语报
                    if in_quotes(m.span(), m.group(0)):
                        keep("程序用语", m.group(0), m.span())
                    else:
                        add("程序用语", m.group(0), plain, m.span())
        for m in CODE.finditer(line):
            if free(m.span()) and (EXPLAINED.match(line, m.end()) or m.group(0).casefold() in allowed):
                taken.append(m.span())  # 已附白话的编号不报，也不再按英文报
            elif free(m.span()):
                add("编号", m.group(0), "第 N 次修订" if m.group(0).startswith("r") else "后面用括号附一句白话，或直接说是哪件事", m.span())
        for m in ENGLISH.finditer(line):
            word = m.group(0)
            if len(word) < 2 or not free(m.span()) or word.casefold() in whitelist or word.casefold() in allowed:
                continue
            if all(part.casefold() in whitelist | allowed for part in re.split(r"[\s/._\-]+", word) if part):
                continue
            if in_quotes(m.span(), word):
                keep("英文", word, m.span())
                continue
            if quoted_at(m.span()) and not shaped(word):  # 候选 r5 #7：引号里因整段的形状照报的，按整段归类、报整段
                word = whole_of(m.span(), word).rstrip(".,;:!?)]}'\"")
            if VERSION.fullmatch(word):
                add("版本号", word, f"说成“第 {word[1:]} 版”", m.span())
            elif TASK_ID.fullmatch(word):  # 候选 r4 #6：任务编号（引号内外都照报，不因后面附了白话放行——与以前按英文照报同严）
                add("编号", word, "说是哪一项工作（如“那份新品提案”），编号放技术附录", m.span())
            elif word.startswith("-"):
                add("命令参数", word, "不写参数，说要做的事；要她执行的命令放代码块并说明用途", m.span())
            elif "/" in word:
                add("文件路径", word, "说文件的白话名称（如“第 7 版演示稿”），路径放技术附录", m.span())
            elif "_" in word or FILE_SUFFIX.search(word) or DOTFILE.fullmatch(word) or word.startswith("~"):
                add("程序名", word, "说这一步做什么（如“登记新版”），程序名放技术附录", m.span())
            else:
                add("英文", word, "换成中文；白名单只有 " + "、".join(listed), m.span())
    hits.sort(key=lambda h: (h["line"], h["context"]))
    terms_quoted = list(dict.fromkeys(q["term"] for q in quoted))
    return {"hits": hits, "count": len(hits), "categories": sorted({h["category"] for h in hits}),
            "quoted_english": quoted,
            "quoted_notice": (f"引号内英文 {len(quoted)} 处（请确认是名称或画面上原样显示的字）：" + "、".join(terms_quoted[:12])
                              + "。这一项靠自查：程序分不出引号里是不是网站名、产品名、品牌名或画面上的字，"
                                "不是的话（如程序名、参数）请改成白话；第一次出现要附一句白话。") if quoted else "",
            # 候选 r4 #6：有引号内英文（靠自查）时也给 attention，不说 ok
            "status": "attention" if hits or quoted else "ok", "scanned_until": f"第 {stopped} 行（技术附录）之前" if stopped else "全文",
            "brand_names": brand_note,
            "notice": "只提示，不改原文；技术附录之后与代码块不扫。客户稿里的品牌名、产品名不算，项目词表可用“允许：名称”放行"
                      + (f"。注意：技术附录从第 {stopped} 行开始，在全文前一半，给策略师的部分只扫了前 {stopped - 1} 行，请确认分界放对了位置"
                         if stopped and stopped - 1 < total / 2 else "")}


def inside(root, path):
    """候选 r2 #10：要扫的文件须在工作区里——照独立任务引用（standalone_store.reference_path）的做法逐段核对：
    相对路径不能跳出工作区（..）、路径上任何一段不能是软链接（防越界），解析后仍在工作区内。
    与 reference_path 不同处：不限于 project/（随包模板也要能扫），运行日志下的复盘报告也能扫。返回实际文件路径，不合规时抛 WorkflowError。"""
    from pathlib import PurePosixPath
    root = Path(root).resolve()
    raw = Path(path)
    if raw.is_absolute():
        import os
        real = Path(os.path.realpath(raw.parent)) / raw.name  # 只展开目录（系统临时目录本身可能是软链接），文件本身是软链接照样拒
        try:
            raw = real.relative_to(root)
        except ValueError:
            raise WorkflowError("要扫的文件不在这个工作区里，这次没有扫。要扫工作区外的文件请明确加参数。"
                                "技术附录（给 Agent）：scan 默认只扫 --workspace 内的文件；确需扫工作区外文件时加 --allow-outside") from None
    rel = PurePosixPath(raw.as_posix())
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise WorkflowError("要扫的文件不在这个工作区里，这次没有扫。要扫工作区外的文件请明确加参数。"
                            "技术附录（给 Agent）：相对路径不能用 .. 跳出工作区；确需扫工作区外文件时加 --allow-outside")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise WorkflowError("要扫的文件经过了快捷方式（软链接），可能指到工作区外，这次没有扫。"
                                "技术附录（给 Agent）：路径上有符号链接；确需扫工作区外文件时加 --allow-outside")
    if not current.resolve().is_relative_to(root):
        raise WorkflowError("要扫的文件不在这个工作区里，这次没有扫。技术附录（给 Agent）：解析后在 --workspace 之外")
    return current


def scan(root, path, allow_outside=False):
    root = Path(root).resolve() if root is not None else None
    if allow_outside or root is None:
        target = Path(path)
        if not target.is_absolute() and root is not None:
            target = root / target
    else:
        target = inside(root, path)
    result = scan_text(target.read_text(encoding="utf-8"), root)
    return {"file": str(path), **result}


def main():
    parser = argparse.ArgumentParser(description="说人话扫描：给策略师读的文字里找程序用语（只提示）")
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("scan", description="扫一个文件或一段话。文件默认只扫工作区（--workspace）里的；"
                                                  "路径不能用 .. 跳出工作区、不能经过软链接")
    sub.add_argument("file", nargs="?", help="工作区里的文件（相对 --workspace 或工作区内的绝对路径）")
    sub.add_argument("--allow-outside", action="store_true",
                     help="明确要扫工作区外的文件（或经过软链接的文件）时才加；默认不扫工作区外的文件")
    sub.add_argument("--text", help="直接扫一段话（发给策略师前自查）")
    sub.add_argument("--workspace", type=Path, default=Path("."))
    args = parser.parse_args()
    if bool(args.file) == bool(args.text):
        parser.error("给一个文件，或用 --text 给一段话（二选一）")
    result = scan(args.workspace, args.file, args.allow_outside) if args.file else scan_text(args.text, args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
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
