"""Require actual full-page browser evidence for a passing HTML presentation."""
from phase2_store import WorkflowError

BROWSER_CHECKS = ("display", "navigation", "editing", "export_reopen", "offline")


def payload(root, meta):
    from phase5_common import local, read_json
    item = next(x for x in meta["current_files"] if x["path"].endswith(".json"))
    return read_json(local(root, item["path"]))


def required_scope(root, meta):
    data = payload(root, meta)
    if meta["kind"] == "proposal-script":
        return [s["id"] for s in data["sections"]]
    return ["cover", "display", "navigation", "editing", "version_binding",
            *dict.fromkeys(p["section_id"] for p in data["pages"][1:]),
            *(["content_fidelity"] if data.get("schema_version") == "2.0" else [])]


def image_evidence(root, reference):
    from phase5_common import local, sha
    from PIL import Image
    if not isinstance(reference, dict):
        raise WorkflowError("截图引用须为路径与指纹对象")
    path = local(root, reference.get("path", ""))
    if not path.is_file() or sha(path) != reference.get("sha256"):
        raise WorkflowError("页面截图丢失或改变")
    try:
        with Image.open(path) as image:
            image.load()
            if image.width < 100 or image.height < 100 or all(a == b for a, b in image.convert("RGB").getextrema()):
                raise WorkflowError("截图为空白或尺寸不足")
    except (OSError, ValueError) as exc:
        raise WorkflowError("视觉证据不是可解码图片") from exc


def evidence_identity(reference):
    if not isinstance(reference, dict):
        raise WorkflowError("截图引用须为路径与指纹对象")
    path, digest = reference.get("path"), reference.get("sha256")
    if not isinstance(path, str) or not path.strip() or not isinstance(digest, str) or not digest.strip():
        raise WorkflowError("截图引用须包含路径与指纹")
    return digest


def _browser_result(root, meta, report, expected_pages):
    from phase5_common import local, read_json, ref, test_mode, valid_event_file

    browser = report.get("browser_checks")
    if not isinstance(browser, dict) or any(browser.get(name) is not True for name in BROWSER_CHECKS):
        raise WorkflowError("演示稿须实际验证显示、翻页、编辑、导出重开及离线资源")
    evidence = browser.get("evidence")
    if not isinstance(evidence, dict) or not valid_event_file(root, evidence) or not isinstance(
            evidence.get("path"), str) or not evidence["path"].lower().endswith(".json"):
        raise WorkflowError("浏览器功能检查必须绑定结构化 JSON 结果文件")
    try:
        result = read_json(local(root, evidence["path"]))
    except (OSError, ValueError, TypeError) as exc:
        raise WorkflowError("浏览器功能检查结果不是可读取的 JSON") from exc
    if not isinstance(result, dict) or result.get("mode") != "deck":
        raise WorkflowError("浏览器功能检查结果缺少 deck 模式")
    if result.get("target") != ref(meta):
        raise WorkflowError("浏览器功能检查结果未绑定当前演示稿版本")
    browser_name = result.get("browser")
    if not isinstance(browser_name, str) or not browser_name.startswith("Chrome headless on Mac"):
        raise WorkflowError("浏览器功能检查结果未说明实际 Mac Chrome 环境")
    if "synthetic" in browser_name.lower() and not test_mode(root):
        raise WorkflowError("合成浏览器结果不能用于非测试项目")
    if result.get("offline_context") is not True:
        raise WorkflowError("浏览器功能检查结果未证明使用离线上下文")
    checks = result.get("checks")
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in BROWSER_CHECKS):
        raise WorkflowError("浏览器结果中的五项检查必须全部为 true")
    if any(browser.get(name) != checks.get(name) for name in BROWSER_CHECKS):
        raise WorkflowError("报告中的浏览器检查与结构化结果不一致")
    if result.get("errors") != [] or result.get("external") != []:
        raise WorkflowError("浏览器检查存在页面错误或外部资源请求")
    pages = result.get("evidence")
    if not isinstance(pages, list):
        raise WorkflowError("浏览器结果缺少逐页截图证据")
    seen_pages = set()
    seen_digests = {}
    for entry in pages:
        if not isinstance(entry, dict) or type(entry.get("page")) is not int or (
                entry["page"] not in expected_pages or entry.get("rendered") is not True):
            raise WorkflowError("浏览器结果中的逐页证据无效")
        image_evidence(root, entry.get("file"))
        digest = evidence_identity(entry["file"])
        owner = seen_digests.get(digest)
        if owner is not None and owner != entry["page"]:
            raise WorkflowError("浏览器结果不能用同一张截图覆盖不同页面")
        seen_digests[digest] = entry["page"]
        seen_pages.add(entry["page"])
    if seen_pages != expected_pages:
        raise WorkflowError("浏览器结果未覆盖全部页面")
    if report.get("simulation") is True and result.get("simulation") is True and test_mode(root):
        return result
    from deck_browser import verify_recorded_run
    verify_recorded_run(root, meta, result, evidence)
    captured = {(item["page"], item["file"]["path"], item["file"]["sha256"]) for item in pages}
    if any((item["page"], item["file"]["path"], item["file"]["sha256"]) not in captured
           for item in report["visual_evidence"]):
        raise WorkflowError("独立报告的逐页观察未绑定这次真实浏览器截图")
    return result


def validate_deck(root, meta, report):
    data = payload(root, meta)
    expected = set(range(1, len(data["pages"]) + 1))
    visual = report.get("visual_evidence")
    if not isinstance(visual, list):
        raise WorkflowError("演示稿通过报告须有逐页画面证据")
    seen = set()
    evidence_owners = {}
    for entry in visual:
        if not isinstance(entry, dict) or type(entry.get("page")) is not int or entry["page"] not in expected or (
                entry.get("rendered") is not True or not isinstance(entry.get("observation"), str) or
                not entry["observation"].strip() or not isinstance(entry.get("method"), str) or
                not entry["method"].strip()):
            raise WorkflowError("演示稿画面证据须定位页面、说明查看方法并记录实际观察")
        if data.get("schema_version") == "2.0":
            for name in ("content_fidelity", "media_observation"):
                if not isinstance(entry.get(name), str) or not entry[name].strip():
                    raise WorkflowError("页级方案须逐页说明提炼是否忠实、图表或图片是否准确；无媒体也须注明")
        image_evidence(root, entry.get("file"))
        identity = evidence_identity(entry["file"])
        owner = evidence_owners.get(identity)
        if owner is not None and owner != entry["page"]:
            raise WorkflowError("不同页面不能复用同一张截图证据")
        evidence_owners[identity] = entry["page"]
        seen.add(entry["page"])
    if seen != expected:
        raise WorkflowError("演示稿未覆盖全部页面，不能通过")
    _browser_result(root, meta, report, expected)
