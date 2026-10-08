#!/usr/bin/env python3
"""演示稿作者自检（U07）与改动页截图（U16）。

check：逐页列出图片完整/方向/亮度与文字量问题，交作者自测与主控回读；只提示，不改稿。
shots：只截指定的改动页并存压缩 JPG；整套截图须写明理由（首次完整检核/对外交付前）。"""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from phase2_store import WorkflowError, local

TEXT_LIMIT = 220            # 每页画面文字建议上限（字），超出提示
PHOTO_KINDS = {"photo", "package", "shelf", "product"}
COVER = re.compile(r"object-fit\s*:\s*cover", re.I)
DARK = re.compile(r"filter\s*:[^;]*brightness\(\s*(0?\.\d+|[0-8]?\d%)\s*\)", re.I)


def parse(text):
    """逐页画面文字与图片：与内部用语扫描、对外导出共用 html_huamian 的“非画面元素”口径（讲者稿、隐藏、脚本不计）。"""
    from html_huamian import screen, styles
    found = screen(text); css = "\n".join(styles(text))
    pages = []
    for index, label in enumerate(found["pages"], 1):
        images = [{"src": a.get("src", ""), "kind": a.get("data-kind"), "crop_reason": a.get("data-crop-reason"),
                   "style": a.get("style", ""), "class": a.get("class", "")} for a in found["images"].get(index, [])]
        pages.append({"page": label, "text": found["text"].get(index, "").replace(" ", ""), "images": images, "css": css})
    return pages


def _img_rules(css, pattern):
    """返回作用于 img 的、匹配 pattern 的全局规则选择器（粗略解析）。"""
    hits = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"\bimg\b", selector) and pattern.search(body):
            hits.append(selector.strip())
    return hits


def check(root, html_path):
    root = Path(root).resolve(); html = local(root, html_path)
    pages = parse(html.read_text(encoding="utf-8")); issues = []
    if not pages:
        raise WorkflowError("没有找到 <section> 页面，无法逐页自检")
    css = pages[0]["css"]; cover_rules = _img_rules(css, COVER); dark_rules = _img_rules(css, DARK)
    from sucai_ku import library as asset_library, verify_asset
    library = asset_library(root)
    for page in pages:
        n = page["page"]
        if len(page["text"]) > TEXT_LIMIT:
            issues.append({"page": n, "check": "文字量", "issue": f"画面文字约 {len(page['text'])} 字，超过建议 {TEXT_LIMIT} 字"})
        for img in page["images"]:
            src = img["src"]; target = None
            if not src.startswith("data:") and "://" not in src:
                target = (html.parent / src).resolve()
                if not target.is_file():
                    issues.append({"page": n, "check": "图片完整", "issue": f"缺图 {src}"}); continue
            rel = target.relative_to(root).as_posix() if target and target.is_relative_to(root) else None
            problem = verify_asset(root, rel, library) if rel else None
            if problem:
                issues.append({"page": n, "check": "图片完整", "issue": problem}); continue
            kind = img["kind"] or (library.get(rel) or {}).get("kind")
            covered = COVER.search(img["style"]) or (cover_rules and not re.search(r"object-fit\s*:\s*contain", img["style"], re.I))
            if kind in PHOTO_KINDS and covered and not img["crop_reason"]:
                issues.append({"page": n, "check": "图片完整", "issue": f"{kind} 图按 cover 裁切却没有 data-crop-reason：{src[:80]}"})
            if kind in {"product", "package"} and (DARK.search(img["style"]) or dark_rules):
                issues.append({"page": n, "check": "亮度", "issue": f"{kind} 图被压暗（brightness 滤镜）：{src[:80]}"})
            if target and target.suffix.lower() in {".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
                from PIL import Image
                with Image.open(target) as image:
                    orientation = image.getexif().get(0x0112, 1)
                if orientation not in (None, 1):
                    issues.append({"page": n, "check": "方向", "issue": f"图片 EXIF 方向 {orientation} 未转正：{src}（用 sucai_ku.py add 入库会自动转正）"})
    return {"html": html_path, "pages": len(pages), "issues": issues, "status": "attention" if issues else "ok",
            "basis": "源码解析（未经浏览器核对）；页码与截图、对外导出同一定义",
            "checklist": ["图片完整（实拍/包装默认完整显示，裁切写理由）", "方向（EXIF 转正）", "亮度（产品图不压暗）", f"每页文字量（≤{TEXT_LIMIT} 字）"],
            "notice": "自检只提示，不改稿；拿不准的取舍在交回时写 --tradeoff 交策略师决定"}


def shots(root, html_path, pages, out, all_reason=None, quality=70):
    root = Path(root).resolve(); html = local(root, html_path)
    if not pages and not (all_reason and all_reason.strip()):
        raise WorkflowError("默认只截改动页：用 --pages 3,5；整套截图须用 --all-reason 写明（首次完整检核/对外交付前）")
    target = local(root, out); target.mkdir(parents=True, exist_ok=True)
    node = os.environ.get("HARNESS_NODE", "node")
    from html_huamian import screen
    found = screen(html.read_text(encoding="utf-8"))
    run = subprocess.run([node, str(Path(__file__).with_name("deck_shots.mjs")), str(html), str(target), ",".join(map(str, pages or [])), str(quality),
                          json.dumps(found["page_ordinals"]), str(found["sections"])],
                         capture_output=True, text=True, timeout=300)
    if run.returncode == 2 or run.returncode == 3:
        raise WorkflowError(run.stderr.strip()[-400:])
    if run.returncode:
        raise WorkflowError("截图失败（需本机 Chrome 与 HARNESS_NODE_MODULES 下的 playwright）：" + run.stderr[-400:])
    written = json.loads(run.stdout.strip().splitlines()[-1])
    files = sorted(Path(p).resolve().relative_to(root).as_posix() for p in written)  # 只列本次写出的，不混入目录里的旧截图
    return {"status": "ok", "files": files, "pages": pages or "all", "reason": all_reason, "format": f"jpeg q{quality}"}


def main():
    parser = argparse.ArgumentParser(description="演示稿作者自检与改动页截图")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check"); c.add_argument("--workspace", type=Path, required=True); c.add_argument("--html", required=True); c.add_argument("--json", action="store_true")
    s = sub.add_parser("shots"); s.add_argument("--workspace", type=Path, required=True); s.add_argument("--html", required=True)
    s.add_argument("--pages", default=""); s.add_argument("--all-reason"); s.add_argument("--out", required=True); s.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "check":
        result = check(args.workspace, args.html)
    else:
        pages = [int(x) for x in args.pages.split(",") if x.strip()]
        result = shots(args.workspace, args.html, pages, args.out, args.all_reason)
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
