#!/usr/bin/env python3
"""导出前字符映射预检（v1.7.5 G12）：稿子里有会在 PDF 文字里变成别的字的字，且字体会落到出问题的系统字体时提示。

背景（在途项目使用中发现，10-09）：系统宋体导出 PDF 时把“口”写成部首“⼜”（规范化后是“又”），对外导出的同版文字核对因此拒绝。
本机实测（2026-10-10，Chrome 154，见 验收/v1.7.5/原始数据/G12-字体实测.json）：238 个有部首对应写法的字逐个渲染导出 PDF，
PDF 文字层大多写成部首码位（各字体都如此，规范化或部首补充区对照表能还原，不影响核对）；还原后仍是别的字、会被对外导出核对
拒绝的只有：系统宋体（Songti SC，含没写字体时落到 serif）的“口”→“又”，黑体-简（Heiti SC）的“广”→“干”。苹方、冬青黑体
（Hiragino Sans GB）为 0。表只写实测会出错的字（RISK），不按猜测扩大；换机或换 Chrome 版本后应重测。
判断字体栈：取文档里每一处 font-family（样式表、style 属性、font 简写），从左到右第一个认得出的中文字体决定：系统宋体 / 黑体-简
→ 有风险；苹方、冬青黑体、微软雅黑、思源黑体、sans-serif、system-ui 等 → 没风险；@font-face 内嵌的字体 → 没风险（随稿带字体）；
认不出的名字（西文字体或本机没装）跳过看下一个；serif 按系统宋体算；整串都认不出按浏览器默认（实测为苹方）。
这是源码层面的近似：只要文档里有一处字体栈落到有风险的字体、某页又有表里的字，就提示该页（偏保守，可能多报）。只提示，不改稿、不拦导出。"""
import argparse
import json
import re
from pathlib import Path

from phase2_store import WorkflowError, local

# 实测会在 PDF 文字层变成别的字的字：{字体: {字: 变成的字}}（2026-10-10 本机实测，见模块说明）
RISK = {"songti": {"口": "又"}, "heiti": {"广": "干"}}
FONT_LABEL = {"songti": "系统宋体", "heiti": "系统黑体（黑体-简）"}
SUGGESTED = '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif'
SONGTI = {"songti sc", "songti tc", "songti", "stsong", "st song", "华文宋体", "宋体", "宋体-简", "宋体-繁", "simsun", "nsimsun", "serif"}
HEITI = {"heiti sc", "heiti tc", "stheiti", "黑体-简", "黑体-繁", "华文黑体"}
SAFE = {"pingfang sc", "pingfang tc", "pingfang hk", "苹方", "苹方-简", "hiragino sans gb", "冬青黑体", "冬青黑体简体中文",
        "microsoft yahei", "微软雅黑", "noto sans cjk sc", "noto sans sc", "source han sans sc", "source han sans cn", "思源黑体",
        "sans-serif", "system-ui", "-apple-system", "blinkmacsystemfont", "ui-sans-serif"}
APPENDIX = "## 技术附录（给 Agent）"


def _names(value):
    out = []
    for part in value.split(","):
        name = part.strip().strip("'\"").strip().lower()
        if name:
            out.append(name)
    return out


def stacks(html):
    """文档里全部字体栈（样式表与 style 属性；font 简写取字号之后的部分）与 @font-face 内嵌的字体名。"""
    from html_huamian import styles
    blocks = list(styles(html)) + re.findall(r"""style\s*=\s*("[^"]*"|'[^']*')""", html, re.I)
    text = re.sub(r"/\*.*?\*/", "", "\n".join(b.strip("'\"") if b[:1] in "'\"" else b for b in blocks), flags=re.S)
    embedded = set()
    for face in re.findall(r"@font-face\s*{([^}]*)}", text, re.I):
        for value in re.findall(r"font-family\s*:\s*([^;}]+)", face, re.I):
            embedded.update(_names(value))
    text = re.sub(r"@font-face\s*{[^}]*}", "", text, flags=re.I)
    found = [_names(v) for v in re.findall(r"font-family\s*:\s*([^;}]+)", text, re.I)]
    for value in re.findall(r"(?<![-\w])font\s*:\s*([^;}]+)", text, re.I):
        m = re.search(r"(?:\d+(?:\.\d+)?(?:px|pt|em|rem|%|vw|vh)|xx-small|x-small|small|medium|large|x-large|xx-large|smaller|larger)"
                      r"(?:\s*/\s*\S+)?\s+(.+)$", value.strip(), re.I)
        if m:
            found.append(_names(m.group(1)))
    return [s for s in found if s and s != ["inherit"]], embedded


def falls_to(stack, embedded):
    """字体栈实际落到的中文字体类别：songti / heiti / safe。"""
    for name in stack:
        if name in embedded or name in SAFE:
            return "safe"
        if name in SONGTI:
            return "songti"
        if name in HEITI:
            return "heiti"
    return "safe"  # 整串认不出：浏览器默认（本机实测为苹方）


def check(html):
    """返回预检结果：hits [{page, chars:[{字, 变成, 字体}]}]、risky_stacks、status ok / attention、给策略师的白话 text。"""
    from html_huamian import screen
    found_stacks, embedded = stacks(html)
    risky = {}
    for stack in found_stacks:
        kind = falls_to(stack, embedded)
        if kind != "safe":
            risky.setdefault(kind, []).append(", ".join(stack))
    page_text = screen(html)["text"]
    hits = []
    for number in sorted(k for k in page_text if k):
        chars = [{"char": ch, "becomes": RISK[kind][ch], "font": kind} for kind in risky for ch in RISK[kind] if ch in page_text[number]]
        if chars:
            hits.append({"page": number, "chars": chars})
    result = {"status": "attention" if hits else "ok", "hits": hits, "risky_stacks": {k: sorted(set(v))[:5] for k, v in risky.items()},
              "suggested_stack": SUGGESTED, "basis": "源码层面近似（文档里有一处字体栈落到有风险的字体即按整份算），实测表见模块说明"}
    result["text"] = plain(result)
    return result


def plain(result):
    if not result["hits"]:
        return ""
    pages = "；".join(f"第 {h['page']} 页的" + "、".join(f"“{c['char']}”" for c in h["chars"]) for h in result["hits"][:8])
    fonts = "、".join(FONT_LABEL[k] for k in result["risky_stacks"])
    sample = result["hits"][0]["chars"][0]
    return "\n".join([
        f"发生了什么：导出前检查发现，{pages}字，用{fonts}导出 PDF 时会变成另一个字（比如“{sample['char']}”会变成“{sample['becomes']}”）。",
        "对你有什么影响：画面上看不出来，但 PDF 里的文字是错的，客户复制或搜索会看到错字；对外导出的核对也会因此拒绝。",
        "要你做什么：不用做，我把这几处的字体换成苹方后再导出。",
        "", APPENDIX,
        "落到有风险字体的字体栈：" + json.dumps(result["risky_stacks"], ensure_ascii=False) + f"；建议字体栈改为 {SUGGESTED}"
        "（要宋体效果请用 @font-face 随稿内嵌字体，不要用系统宋体）"])


def main():
    parser = argparse.ArgumentParser(description="导出前字符映射预检（只提示）")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check(local(args.workspace.resolve(), args.file).read_text(encoding="utf-8"))
    print(json.dumps({"file": args.file, **result}, ensure_ascii=False, indent=2) if args.json or not result["text"] else result["text"])
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
