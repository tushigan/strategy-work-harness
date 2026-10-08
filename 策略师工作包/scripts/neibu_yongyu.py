#!/usr/bin/env python3
"""对外文本内部用语扫描（U05）：对画面层与对外讲稿给提示，交检核者/主控判断，不自动删改。

默认词表覆盖在途项目 v1.0 复盘中上屏的内部口径类别；项目可在 project/inputs/内部用语词表.txt 追加（每行一个，
以 re: 开头表示正则）。HTML 只扫观众可见的画面文字（与对外导出共用 html_huamian 的“非画面元素”口径），不扫讲者稿、脚本与样式。
扫描前先做 NFKC 并去掉零宽字符、软连字符等不显示的格式字符（fold），插在词中间的这类字符不会让词逃过扫描。"""
import argparse
import json
import re
import unicodedata
from pathlib import Path

from phase2_store import WorkflowError, local

DEFAULT = [
    ("口径约束", r"不相加|不可相加|不能相加"), ("待确认", r"待.{0,8}确认|需.{0,6}确认后"), ("未核实", r"未核实|待核实|核实中|待核(?!心)"),
    ("在售说明", r"不代表在售|历史陈列"), ("出处说明", r"出处[:：]|页脚出处|数据出处"), ("内部口径", r"内部口径|口径待|口径约束|讲者口径"),
    ("讲者备注", r"讲者备注|仅供讲者|讲者[:：]"), ("禁区", r"数字禁区|禁区"), ("不上屏", r"勿上屏|不上屏|不对外|仅内部"),
    ("推断标记", r"推断"), ("草稿占位", r"草稿占位|占位|TODO|TBD|XXX"), ("作者批注", r"作者注|批注[:：]|备注[:：]"),
    ("待补", r"待补|待定|待更新"), ("版本内部说明", r"未检核|待检核|检核中"), ("合成测试", r"合成甲方|合成来源"),
]
WORDS = "project/inputs/内部用语词表.txt"


def terms(root):
    result = [(label, re.compile(pattern)) for label, pattern in DEFAULT]
    path = Path(root) / WORDS
    if path.is_file():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    result.append(("项目词表", re.compile(line[3:] if line.startswith("re:") else re.escape(line))))
                except re.error as exc:
                    raise WorkflowError(f"{WORDS} 第 {number} 行正则无效（{exc}）：{line[:60]}；改正或去掉 re: 前缀按普通词处理") from None
    return result


# 零宽、软连字符、方向控制等格式字符（Unicode Cf 类）与变体选择符：插在词中间会让扫描与核对“看不见”这个词
_INVISIBLE = re.compile(r"[\u034f\u180b-\u180f\ufe00-\ufe0f\U000e0100-\U000e01ef]")


def fold(text):
    """扫描与对外文字核对前的统一归一：NFKC（全角/兼容字符）+ 去掉零宽字符、软连字符等不显示的格式字符。"""
    text = unicodedata.normalize("NFKC", text)
    return _INVISIBLE.sub("", "".join(c for c in text if unicodedata.category(c) != "Cf"))


def scan_text(root, pages):
    hits, patterns = [], terms(root)
    for number, text in pages:
        text = fold(text)
        for label, pattern in patterns:
            for match in pattern.finditer(text):
                start = max(0, match.start() - 12)
                hits.append({"page": number, "category": label, "term": match.group(0), "context": text[start:match.end() + 12]})
    return hits


def css_strings(found):
    """<style> 里 content 声明写出的字符串（伪元素文字）。"""
    from html_huamian import css_content
    return css_content(found)


def scan(root, path):
    root = Path(root).resolve(); target = local(root, path) if not Path(path).is_absolute() else Path(path)
    raw = target.read_text(encoding="utf-8")
    if target.suffix.lower() in {".html", ".htm"}:
        from html_huamian import screen
        found = screen(raw)
        pages = [(i, t) for i, t in sorted(found["text"].items()) if t]
        shown = " ".join(css_strings(found))
        if shown:
            pages.append((0, shown))  # 样式里 content 写出的文字也会上屏
    else:
        pages = [(1, raw)]
    hits = scan_text(root, pages)
    basis = {"basis": "源码解析（未经浏览器核对）：CSS 隐藏、伪元素位置等以对外导出的浏览器核对为准"} if target.suffix.lower() in {".html", ".htm"} else {}
    return {"file": path, "hits": hits, "count": len(hits), "categories": sorted({h["category"] for h in hits}),
            "status": "attention" if hits else "ok", **basis,
            "notice": "只提示，不自动删改；由检核者或主控判断是否属于需上屏披露"}


def main():
    parser = argparse.ArgumentParser(description="对外文本内部用语扫描（只提示）")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    print(json.dumps(scan(args.workspace, args.file), ensure_ascii=False, indent=2))
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
