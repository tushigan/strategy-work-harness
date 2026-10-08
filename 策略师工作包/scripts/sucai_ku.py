#!/usr/bin/env python3
"""项目素材库（U07/U16）：素材按内容指纹存一份，各版本 HTML 引用、不复制。

入库时按 EXIF 转正、按显示需要压缩（16:9 全屏长边约 2560px）、去掉元数据；原图只留在 project/inputs/ 一份。
同一原图重复入库返回同一素材；不同原图压缩后字节相同也只存一份。"""
import argparse
import hashlib
import io
import json
from pathlib import Path

from phase2_store import WorkflowError, atomic_json, local, now
from workspace_lock import serialized

LIBRARY = "project/assets"
INDEX = LIBRARY + "/index.json"
KINDS = {"photo", "package", "product", "scene", "chart", "other"}


def _index(root):
    path = local(root, INDEX)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema_version": 1, "by_original": {}, "assets": {}}


def process(raw, max_edge=2560):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(raw)) as image:
        orientation = image.getexif().get(0x0112, 1)
        image = ImageOps.exif_transpose(image)
        original_size = image.size
        if max(image.size) > max_edge:
            scale = max_edge / max(image.size)
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        out = io.BytesIO()
        alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
        if alpha:
            image.save(out, "PNG", optimize=True); suffix = ".png"
        else:
            image.convert("RGB").save(out, "JPEG", quality=85, optimize=True, progressive=True); suffix = ".jpg"
        return out.getvalue(), suffix, {"orientation_fixed": orientation != 1, "exif_orientation": orientation,
                                         "original_size": list(original_size), "size": list(image.size)}


def add(root, files, actor, kind="other", max_edge=2560):
    from registration_guard import actor_check
    actor_check(actor)
    if kind not in KINDS:
        raise WorkflowError("素材类别须为 " + "/".join(sorted(KINDS)))
    root = Path(root).resolve(); results = []
    with serialized(root):
        index = _index(root)
        for value in files:
            if not isinstance(value, str) or not value.startswith("project/inputs/"):
                raise WorkflowError(f"{value}：原图须先归档在 project/inputs/（只留这一份），再入素材库")
            path = local(root, value)
            if not path.is_file():
                raise WorkflowError(f"{value}：原图不存在")
            raw = path.read_bytes(); original = hashlib.sha256(raw).hexdigest()
            if original in index["by_original"]:
                entry = index["assets"][index["by_original"][original]]
                notice = {} if kind in ("other", entry["kind"]) else {"kind_notice": f"该原图已按 {entry['kind']} 入库，本次 --kind {kind} 未改登记（其它版本也在引用）；需要按 {kind} 自检时在 img 上写 data-kind=\"{kind}\""}
                results.append({**entry, "source": value, "status": "existing", **notice}); continue
            data, suffix, info = process(raw, max_edge)
            digest = hashlib.sha256(data).hexdigest()
            rel = f"{LIBRARY}/{digest[:24]}{suffix}"
            target = local(root, rel)
            if target.exists() or target.is_symlink():
                if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise WorkflowError(f"{rel}：素材库已有同名文件但内容不符（或是符号链接），未覆盖")
            else:
                target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            entry = {"path": rel, "sha256": digest, "kind": kind, "bytes": len(data), "original": {"path": value, "sha256": original,
                     "bytes": len(raw)}, **info, "added_at": now(), "actor": actor}
            index["assets"].setdefault(digest, entry); index["by_original"][original] = digest
            results.append({**index["assets"][digest], "source": value, "status": "added"})
        atomic_json(local(root, INDEX), index)
    return {"status": "ok", "assets": results,
            "notice": "工作稿 HTML 引用素材库路径，不内嵌；只有对外导出才内嵌或生成 PDF"}


def library(root):
    """素材库路径 → 登记信息（含入库时的内容指纹）。"""
    index = _index(root)
    return {a["path"]: a for a in index["assets"].values()}


def verify_asset(root, rel, known=None):
    """引用素材库文件时按登记指纹核对内容：被替换/改动/缺失返回问题说明，否则 None（非素材库文件不管）。"""
    if not rel or not rel.startswith(LIBRARY + "/") or rel == INDEX:
        return None
    entry = (known if known is not None else library(root)).get(rel)
    path = Path(root) / rel
    if entry is None:
        return f"素材库文件 {rel} 没有入库登记（不在 index.json），无法核对指纹；用 sucai_ku.py add 从原图入库"
    if path.is_symlink() or not path.is_file():
        return f"素材库文件 {rel} 缺失或不是普通文件"
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        return f"素材库文件 {rel} 内容与入库指纹不符（被替换或改动）；旧版本引用会静默变图，先恢复原素材或重新入库为新文件"
    return None


def check(root, html_path):
    """工作稿图片引用检查：缺图、内嵌、引用素材库以外的图片。"""
    from deck_zijian import parse
    root = Path(root).resolve()
    html = local(root, html_path)
    pages = parse(html.read_text(encoding="utf-8"))
    issues, known = [], library(root)
    for page in pages:
        for img in page["images"]:
            src = img["src"]
            if src.startswith("data:"):
                issues.append({"page": page["page"], "level": "warning", "issue": f"工作稿内嵌图片约 {len(src)*3//4//1024}KB；改为引用 project/assets/ 素材"})
            elif "://" in src:
                issues.append({"page": page["page"], "level": "error", "issue": f"引用外部网络图片 {src}"})
            else:
                target = (html.parent / src).resolve()
                rel = target.relative_to(root).as_posix() if target.is_relative_to(root) else None
                problem = verify_asset(root, rel, known)
                if not target.is_file():
                    issues.append({"page": page["page"], "level": "error", "issue": f"缺图：{src}"})
                elif problem:
                    issues.append({"page": page["page"], "level": "error", "issue": problem})
                elif not target.is_relative_to((root / LIBRARY).resolve()):
                    issues.append({"page": page["page"], "level": "warning", "issue": f"图片不在素材库：{src}"})
    return {"html": html_path, "pages": len(pages), "issues": issues,
            "status": "blocked" if any(i["level"] == "error" for i in issues) else "attention" if issues else "ok"}


def main():
    parser = argparse.ArgumentParser(description="项目素材库：入库去重、EXIF 转正、按显示尺寸压缩；工作稿引用检查")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add"); a.add_argument("--workspace", type=Path, required=True); a.add_argument("--file", action="append", required=True)
    a.add_argument("--kind", default="other"); a.add_argument("--max-edge", type=int, default=2560); a.add_argument("--actor", required=True)
    a.add_argument("--json", action="store_true")
    c = sub.add_parser("check"); c.add_argument("--workspace", type=Path, required=True); c.add_argument("--html", required=True); c.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = add(args.workspace, args.file, args.actor, args.kind, args.max_edge) if args.command == "add" else check(args.workspace, args.html)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main

    def main():
        return cli(_business_main, __file__)

    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
