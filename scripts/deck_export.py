"""Local, derived PDFs of confirmed native decks; never grants an approval."""
import argparse
from io import BytesIO
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import unicodedata
import uuid

import html_deck
import phase5_common as c
from phase3_sources import identifier
from workspace_lock import serialized

PROTOCOL = "harness-deck-pdf-v1"
RUNNER = "scripts/deck_export.mjs"
CONTROLLER = "scripts/deck_export.py"
# Chrome can encode whole simplified characters as radical glyphs in PDF text.
# Finite visual mappings from Unicode 17.0, not traditional/simplified conversion:
# https://www.unicode.org/Public/17.0.0/ucd/EquivalentUnifiedIdeograph.txt
PDF_GLYPHS = dict(zip(
    (0x2EB0, 0x2EC5, 0x2EC6, 0x2EC8, 0x2EC9, 0x2ECB, 0x2ED0, 0x2ED3,
     0x2ED4, 0x2ED9, 0x2EDA, 0x2EDB, 0x2EDC, 0x2EE0, 0x2EE2, 0x2EE5,
     0x2EE6, 0x2EE7, 0x2EE8, 0x2EE9, 0x2EEA, 0x2EEC, 0x2EEE, 0x2EF0, 0x2EF3),
    (0x7E9F, 0x89C1, 0x89D2, 0x8BA0, 0x8D1D, 0x8F66, 0x9485, 0x957F,
     0x95E8, 0x97E6, 0x9875, 0x98CE, 0x98DE, 0x9963, 0x9A6C, 0x9C7C,
     0x9E1F, 0x5364, 0x9EA6, 0x9EC4, 0x9EFE, 0x9F50, 0x9F7F, 0x9F99, 0x9F9F),
    strict=True))
NOTICE = "仅为已确认 HTML 的派生导出；PDF 仍须实际回读视觉，不代表客户批准。"
MESSAGES = {
    "invalid_key": "请提供本任务的完整编号 <task>::html-deck。",
    "source_invalid": "正式 HTML、上游来源、独立检核或策略师确认无效，请先检查当前版本。",
    "source_changed": "导出期间来源或确认记录发生变化，本次不登记成功。",
    "executor_changed": "导出执行器文件已改变或与当前运行脚本不一致，请对齐工作包后重新导出。",
    "dependency_missing": "缺少本地 Node、Playwright、pdf-lib 或 pypdf；请配置已安装依赖，不会联网安装。",
    "environment_unsupported": "仅支持 Mac 上已安装的 Google Chrome。",
    "browser_failed": "本地 Chrome 无法启动或完成导出，请检查已安装的浏览器。",
    "network_request": "页面尝试读取外部资源；只允许当前 HTML 内嵌资源。",
    "console_error": "页面存在脚本、控制台或资源加载错误。",
    "page_mismatch": "页面导航、正文或 PDF 页数与正式 HTML 不一致。",
    "overflow": "页面内容溢出、被隐藏或无法完整排版，不能裁剪后视为成功。",
    "pdf_invalid": "PDF 内容、尺寸、页数或文件指纹不符，不能沿用本次导出。",
    "invalid_manifest": "导出清单缺失、损坏或不属于当前任务的导出目录。",
    "timeout": "本地导出超时，失败文件已保留，可检查页面后重新导出。",
    "io_error": "无法读取或写入本地文件，请检查文件和目录权限。",
}


def fail(code):
    raise c.WorkflowError(f"{code}: {MESSAGES[code]}")


def _task(key):
    if not isinstance(key, str) or not key.endswith("::html-deck"):
        fail("invalid_key")
    try:
        return identifier(key.removesuffix("::html-deck"))
    except c.WorkflowError:
        fail("invalid_key")


def _base(key):
    return f"project/outputs/proposal/{_task(key)}/html-deck"


def _source(root, key):
    try:
        meta = html_deck.gate(root, key, human=True)
        c.check_files(root, meta)
        files = [f for f in meta["current_files"] if f.get("role") == "html"]
        if (meta["key"] != key or meta["task_id"] != _task(key) or meta["kind"] != "html-deck"
                or len(files) != 1 or files[0]["path"] != f"{_base(key)}/presentation.html"):
            fail("source_invalid")
        document = html_deck._document(root, meta)
        pages = document["pages"]
        if not pages or [p["page"] for p in pages] != list(range(1, len(pages) + 1)):
            fail("source_invalid")
        review = c.last_review(root, c.ref(meta))
        decision = [e for e in c.events(root, "confirmation") if e.get("target") == c.ref(meta)][-1]
        return {"ref": c.ref(meta), "html": {k: files[0][k] for k in ("path", "sha256")},
                "meta_sha256": c.sha_bytes(c.json_bytes(meta)), "page_count": len(pages),
                "review_id": review["record_id"], "confirmation_id": decision["record_id"],
                "simulation": bool(review.get("simulation") or decision.get("simulation"))}, pages
    except (c.WorkflowError, OSError, ValueError, KeyError, TypeError, IndexError):
        fail("source_invalid")


def _executors(root):
    try:
        bound = {}
        for name, relative in (("runner", RUNNER), ("controller", CONTROLLER)):
            bound[name] = {"path": relative, "sha256": c.sha(c.local(root, relative))}
            if bound[name]["sha256"] != c.sha(Path(__file__).with_name(Path(relative).name)):
                fail("executor_changed")
        return bound
    except (c.WorkflowError, OSError, ValueError):
        fail("executor_changed")


def _unchanged(root, key, source, bound):
    try:
        same = _source(root, key)[0] == source
    except c.WorkflowError:
        same = False
    if not same:
        fail("source_changed")
    if _executors(root) != bound:
        fail("executor_changed")


def _run(root, request):
    try:
        result = subprocess.run([os.environ.get("HARNESS_NODE", "node"),
            str(c.local(root, RUNNER)), str(root), request],
            capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        fail("dependency_missing")
    except subprocess.TimeoutExpired:
        fail("timeout")
    except OSError:
        fail("io_error")
    if result.returncode:
        # Only accept known codes. Browser stderr can contain URLs, secrets or HTML.
        try:
            code = json.loads(result.stderr.strip())["code"]
        except (ValueError, KeyError, TypeError):
            code = "browser_failed"
        fail(code if isinstance(code, str) and code in MESSAGES else "browser_failed")


def _text(value):
    return "".join(unicodedata.normalize("NFKC", value).translate(PDF_GLYPHS).split())


def _pdf(root, reference, expected_path, count):
    try:
        from pypdf import PdfReader
    except ImportError:
        fail("dependency_missing")
    if reference.get("path") != expected_path:
        fail("pdf_invalid")
    raw = c.local(root, expected_path).read_bytes()
    if c.sha_bytes(raw) != reference.get("sha256"):
        fail("pdf_invalid")
    reader = PdfReader(BytesIO(raw), strict=True)
    if reader.is_encrypted or len(reader.pages) != count:
        fail("pdf_invalid")
    return reader.pages


def _verify(root, folder, result, source, pages, bound):
    if (result["protocol"] != PROTOCOL or result["source"] != source
            or any(result.get(k) != v for k, v in bound.items())
            or result["page_count"] != len(pages) or len(result["pages"]) != len(pages)
            or result["checks"] != {k: True for k in ("offline", "console", "layout", "page_count")}
            or result["environment"]["platform"] != "darwin"
            or result["environment"]["channel"] != "chrome"
            or result["environment"]["media"] != "screen"):
        fail("pdf_invalid")
    for name in ("browser_version", "playwright_version", "pdf_lib_version", "node"):
        if not isinstance(result["environment"].get(name), str) or not result["environment"][name]:
            fail("pdf_invalid")
    prefix = folder.relative_to(root).as_posix()
    merged = _pdf(root, result["pdf"], f"{prefix}/presentation.pdf", len(pages))
    for i, (entry, expected, final) in enumerate(zip(result["pages"], pages, merged, strict=True), 1):
        if type(entry["page"]) is not int or entry["page"] != i:
            fail("pdf_invalid")
        single = _pdf(root, entry["file"], f"{prefix}/page-{i:03d}.pdf", 1)[0]
        for item in (single, final):
            for axis, measured in (("width", item.mediabox.width), ("height", item.mediabox.height)):
                px, pt = entry[f"{axis}_css_px"], entry[f"{axis}_pt"]
                if (type(px) not in (int, float) or type(pt) not in (int, float)
                        or not math.isfinite(px) or not math.isfinite(pt) or px <= 0
                        or abs(float(measured) - px * .75) > 1 or abs(float(measured) - pt) > .01):
                    fail("pdf_invalid")
            actual = _text(item.extract_text() or "")
            if (list(item.cropbox) != list(item.mediabox) or item.rotation
                    or any(_text(expected[k]) not in actual for k in ("title", "text"))):
                fail("pdf_invalid")


def export(root, key):
    root = Path(root).resolve()
    folder = c.local(root, f"{_base(key)}/exports/{uuid.uuid4().hex}")
    folder.mkdir(parents=True, exist_ok=False)
    stage = "source_invalid"
    try:
        bound = _executors(root)
        source, pages = _source(root, key)
        request = {"protocol": PROTOCOL, "run_id": folder.name, "source": source, **bound}
        c.atomic_json(folder / "request.json", request)
        stage = "browser_failed"
        _run(root, (folder / "request.json").relative_to(root).as_posix())
        stage = "pdf_invalid"
        result = c.read_json(folder / "render-result.json")
        if result["run_id"] != folder.name:
            fail("pdf_invalid")
        _verify(root, folder, result, source, pages, bound)
        manifest = {**result, "created_at": c.now(), "status": "exported_pending_visual_review",
                    "python": platform.python_version(), "notice": NOTICE}
        with serialized(root):
            _unchanged(root, key, source, bound)
            # No new approval, state database or success event is created.
            with (folder / "manifest.json").open("xb") as stream:
                stream.write(c.json_bytes(manifest))
        return {**manifest, "manifest": (folder / "manifest.json").relative_to(root).as_posix()}
    except Exception as exc:
        code = str(exc).split(":", 1)[0] if isinstance(exc, c.WorkflowError) else stage
        code = code if code in MESSAGES else stage
        (folder / "manifest.json").unlink(missing_ok=True)
        c.atomic_json(folder / "failure.json", {"status": "failed", "failure_kind": code,
                                               "reason": MESSAGES[code], "run_id": folder.name})
        fail(code)


def inspect(root, manifest):
    root = Path(root).resolve()
    try:
        path = c.local(root, manifest)
        value = c.read_json(path)
        key = value["source"]["ref"]["key"]
        folder = path.parent
        if (path.name != "manifest.json" or folder.parent != c.local(root, f"{_base(key)}/exports")
                or folder.name != value["run_id"] or len(folder.name) != 32
                or any(ch not in "0123456789abcdef" for ch in folder.name)
                or value["status"] != "exported_pending_visual_review"
                or (folder / "failure.json").exists()):
            fail("invalid_manifest")
        request = c.read_json(folder / "request.json")
        if request != {k: value[k] for k in ("protocol", "run_id", "source", "runner", "controller")}:
            fail("invalid_manifest")
        bound = _executors(root)
        if any(value.get(k) != v for k, v in bound.items()):
            fail("executor_changed")
        source, pages = _source(root, key)
        if source != value["source"]:
            fail("source_changed")
        _verify(root, folder, value, source, pages, bound)
        _unchanged(root, key, source, bound)
        if c.read_json(path) != value:
            fail("invalid_manifest")
        return {**value, "manifest": manifest, "inspection": "current_source_and_files", "notice": NOTICE}
    except c.WorkflowError:
        raise
    except Exception:
        fail("invalid_manifest")


def main():
    parser = argparse.ArgumentParser(description="从已确认的正式 HTML 本地导出 PDF；不替代视觉回读或客户批准")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, option in (("export", "key"), ("inspect", "manifest")):
        sub = commands.add_parser(name)
        sub.add_argument("--workspace", default=".")
        sub.add_argument(f"--{option}", required=True)
    args = parser.parse_args()
    try:
        result = export(args.workspace, args.key) if args.command == "export" else inspect(
            args.workspace, args.manifest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        code = str(exc).split(":", 1)[0] if isinstance(exc, c.WorkflowError) else "io_error"
        code = code if code in MESSAGES else "io_error"
        print(f"PDF 操作失败（{code}）：{MESSAGES[code]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
