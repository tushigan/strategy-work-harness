"""Inline local shell, style and ordered IIFEs into an unassigned single file."""
import argparse
from pathlib import Path
import uuid

from brand_house_data import make_data
from brand_house_html import embed, extract
from phase2_store import WorkflowError, fingerprint, local, output_json
from phase3_document import SECTIONS
from phase3_io import atomic_bytes

SCRIPTS = ("brand_house_validate.js", "brand_house_core.js", "brand_house_controls.js",
           "brand_house_ui.js", "brand_house_file.js", "brand_house_cache.js", "brand_house_app.js")
BLANK_TITLES = {"category": "品类定位", "audience": "目标人群", "differentiated_value": "差异化价值",
                "positioning": "定位身份", "mission": "使命", "vision": "愿景", "values": "价值观",
                "rtb": "支持理由", "personality": "品牌人格", "proposition": "内部主张", "slogan": "品牌口号"}


def blank_data():
    document = {"task_id": "UNINITIALIZED", "kind": "brand-house", "title": "",
                "mode": "hypothesis", "sections": [
                    {"id": key, "title": BLANK_TITLES[key], "text": "", "evidence": [], "counterevidence": [],
                     "assumptions": [], "verification": []} for key in SECTIONS["brand-house"]],
                "gaps": [], "questions": [],
                "template_notice": "通用空白模板，尚未绑定项目；须由 Agent 从有效正文生成正式文件。"}
    meta = {"task_id": "UNINITIALIZED", "key": "UNINITIALIZED::brand-house", "version": 1,
            "sha256": fingerprint(document), "dependencies": []}
    data = make_data("UNINITIALIZED", meta, document)
    data["document_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "harness:brand-house-template:1"))
    data["current"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "harness:brand-house-template:initial:1"))
    data["history"][0].update(id=data["current"], created_at="2026-09-17T00:00:00+00:00",
                              note="通用空白模板，尚未绑定项目，不是业务产出")
    return data


def build(root):
    shell = local(root, "templates/brand-house-shell.html").read_text(encoding="utf-8")
    for marker in ("<!--BH_STYLES-->", "<!--BH_SCRIPTS-->"):
        if shell.count(marker) != 1:
            raise WorkflowError("模板壳必须各含一个样式和脚本占位符")
    css = local(root, "templates/brand-house.css").read_text(encoding="utf-8")
    if "</style" in css.lower():
        raise WorkflowError("样式源包含结束标签，不能安全内联")
    scripts = []
    for name in SCRIPTS:
        path = local(root, f"scripts/{name}")
        if not path.is_file():
            raise WorkflowError(f"缺少品牌屋必需脚本：{name}；未更新模板")
        source = path.read_text(encoding="utf-8")
        if "</script" in source.lower():
            raise WorkflowError(f"{name} 包含原始结束标签，需在源中转义后再构建")
        scripts.append(f'<script id="{path.stem}">\n{source}\n</script>')
    html = shell.replace("<!--BH_STYLES-->", f'<style id="brand-house-style">\n{css}\n</style>')
    html = html.replace("<!--BH_SCRIPTS-->", "\n".join(scripts))
    data = blank_data()
    raw = embed(html, data)
    if extract(raw) != data:
        raise WorkflowError("通用模板回读不一致")
    target = local(root, "templates/brand-house-model.html")
    atomic_bytes(target, raw)
    return {"path": "templates/brand-house-model.html", "status": "uninitialized_template",
            "scripts": [Path(n).stem for n in SCRIPTS if local(root, f"scripts/{n}").exists()],
            "bytes": len(raw), "notice": "构建和数据回读成功不代表实际浏览器验收"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        output_json(build(args.workspace.resolve()))
    except (OSError, WorkflowError) as exc:
        parser.exit(1, f"错误：{exc}\n")
