"""Run the bundled browser probe before accepting its local evidence."""
import os
from pathlib import Path
import platform
import subprocess
import uuid

import phase5_common as c

PROTOCOL = "harness-deck-browser-v1"
RUNNER = "scripts/deck_browser.mjs"
CONTROLLER = "scripts/deck_browser.py"


def file_ref(root, relative):
    return {"path": relative, "sha256": c.sha(c.local(root, relative))}


def _bound_files(root, meta):
    html = next(item for item in meta["current_files"] if item["role"] == "html")
    return {"input_html": file_ref(root, html["path"]),
            "runner": file_ref(root, RUNNER), "controller": file_ref(root, CONTROLLER)}


def _text_output(value, root, limit=4000):
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).replace(str(root), "<workspace>").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[输出已截断]"


def _failure_kind(stdout, stderr, exit_code=None):
    text = f"{stdout}\n{stderr}".lower()
    if "module_not_found" in text or "cannot find module" in text:
        return "dependency_missing"
    if "unsupported browser" in text or ("protocol" in text and "unsupported" in text):
        return "configuration_error"
    if "timeout" in text or exit_code == -9:
        return "timeout"
    return "browser_process_failed"


def _write_failure(root, folder, *, stdout="", stderr="", exit_code=None,
                   reason="", error_type=""):
    clean_stdout = _text_output(stdout, root)
    clean_stderr = _text_output(stderr, root)
    kind = _failure_kind(clean_stdout, clean_stderr, exit_code)
    detail = _text_output(reason or clean_stderr or clean_stdout, root)
    c.atomic_json(folder / "failure.json", {
        "status": "failed",
        "failure_kind": kind,
        "exit_code": exit_code,
        "error_type": error_type or None,
        "reason": detail or kind,
        "stdout": clean_stdout,
        "stderr": clean_stderr,
    })
    return kind


def check_result(root, meta, result, run_id, bound):
    """Check the process outputs, not a claim supplied in a review report."""
    from phase5_visual import BROWSER_CHECKS, image_evidence, payload
    if not isinstance(result, dict) or any(result.get(k) != v for k, v in {
            "protocol": PROTOCOL, "run_id": run_id, "target": c.ref(meta),
            "mode": "deck", "simulation": False, "offline_context": True, **bound}.items()):
        raise c.WorkflowError("浏览器运行结果与本次执行或当前HTML不符")
    environment = result.get("environment", {})
    if (not isinstance(environment, dict) or environment.get("platform") != "darwin" or not environment.get("browser_version")
            or not environment.get("playwright_version")):
        raise c.WorkflowError("缺少实际Mac Chrome运行环境")
    if (result.get("checks") != dict.fromkeys(BROWSER_CHECKS, True)
            or result.get("errors") != [] or result.get("external") != []):
        raise c.WorkflowError("浏览器运行未完成全部检查")
    steps = result.get("steps")
    if (not isinstance(steps, list) or any(not isinstance(x, dict) for x in steps)
            or [x.get("name") for x in steps] != list(BROWSER_CHECKS)
            or any(x.get("passed") is not True or not x.get("observed") for x in steps)):
        raise c.WorkflowError("缺少实际交互步骤结果")
    expected = {(page, width) for page in range(1, len(payload(root, meta)["pages"]) + 1)
                for width in (1280, 390)}
    screenshots = result.get("evidence")
    if (not isinstance(screenshots, list) or len(screenshots) != len(expected)
            or any(not isinstance(item, dict) or not isinstance(item.get("viewport"), dict)
                   for item in screenshots) or {
            (item.get("page"), item.get("viewport", {}).get("width")) for item in screenshots} != expected):
        raise c.WorkflowError("浏览器运行未覆盖全部桌面及窄屏页面")
    folder = f"project/reviews/browser/{run_id}/"
    files = [result.get("exported"), result.get("reopened_screenshot"),
             *[item.get("file") for item in screenshots]]
    for item in files:
        if (not isinstance(item, dict) or not str(item.get("path", "")).startswith(folder)
                or not c.valid_event_file(root, item)):
            raise c.WorkflowError("浏览器运行副本或截图丢失、改变或不属于本次执行")
    for item in screenshots:
        if item.get("rendered") is not True:
            raise c.WorkflowError("存在未渲染页面")
        image_evidence(root, item["file"])
    image_evidence(root, result["reopened_screenshot"])
    from html_deck import _Metadata
    import json
    exported = c.local(root, result["exported"]["path"]).read_text(encoding="utf-8")
    data = json.loads("".join(_Metadata(exported).parts))
    if data.get("deck_ref") != c.ref(meta) or data.get("draft", {}).get("revision", 0) < 1:
        raise c.WorkflowError("导出副本未保留本次编辑及上游版本")


def run(root, key):
    from html_deck import _live
    if platform.system() != "Darwin":
        raise c.WorkflowError("浏览器验收目前仅支持Mac")
    root = Path(root).resolve()
    meta, _, _ = _live(root, key)
    bound = _bound_files(root, meta)
    # Only this fixed executable is dispatched; callers cannot register JSON as a run.
    for name, relative in (("runner", RUNNER), ("controller", CONTROLLER)):
        if bound[name]["sha256"] != c.sha(Path(__file__).with_name(Path(relative).name)):
            raise c.WorkflowError("工作包执行器与当前运行脚本不一致，请先对齐工作包")
    run_id = "deck-" + uuid.uuid4().hex
    folder = c.local(root, f"project/reviews/browser/{run_id}")
    folder.mkdir(parents=True, exist_ok=False)
    request = {"protocol": PROTOCOL, "run_id": run_id, "target": c.ref(meta), **bound}
    c.atomic_json(folder / "request.json", request)
    node = os.environ.get("HARNESS_NODE", "node")
    try:
        process = subprocess.run(
            [node, str(c.local(root, RUNNER)), str(root),
             (folder / "request.json").relative_to(root).as_posix()],
            capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        kind = _write_failure(root, folder, reason=str(exc) or type(exc).__name__,
                              error_type=type(exc).__name__)
        raise c.WorkflowError(f"浏览器未完成运行（{kind}）；已保留失败记录，没有登记成功") from exc
    if process.returncode:
        kind = _write_failure(root, folder, stdout=process.stdout, stderr=process.stderr,
                              exit_code=process.returncode)
        raise c.WorkflowError(f"浏览器检查失败（{kind}）；请查看本次运行目录，没有登记成功")
    try:
        evidence = file_ref(root, (folder / "browser-result.json").relative_to(root).as_posix())
        result = c.read_json(c.local(root, evidence["path"]))
        check_result(root, meta, result, run_id, bound)
        latest, _, _ = _live(root, key)
        if c.ref(latest) != c.ref(meta) or _bound_files(root, latest) != bound:
            raise c.WorkflowError("浏览器运行期间文件或上游已改变，须重新检查")
    except (OSError, ValueError, KeyError, TypeError, c.WorkflowError) as exc:
        kind = _write_failure(root, folder, reason=str(exc) or type(exc).__name__,
                              error_type=type(exc).__name__)
        raise c.WorkflowError(f"浏览器输出不完整或执行期间版本改变（{kind}），没有登记成功") from exc
    c.append_event(root, "browser_completed", {"protocol": PROTOCOL, "run_id": run_id,
        "target": c.ref(meta), "result": evidence, **bound})
    return {"run_id": run_id, "result": evidence, "pages": len(result["evidence"]) // 2,
            "checks": result["checks"]}


def verify_recorded_run(root, meta, result, evidence):
    runs = [event for event in c.events(root, "browser_completed")
            if event.get("run_id") == result.get("run_id") and event.get("result") == evidence]
    if len(runs) != 1:
        raise c.WorkflowError("缺少工作包实际执行记录；须先运行 deck-browser-check")
    event = runs[0]
    bound = _bound_files(root, meta)
    if (event.get("protocol") != PROTOCOL or event.get("target") != c.ref(meta)
            or any(event.get(k) != v for k, v in bound.items())):
        raise c.WorkflowError("浏览器执行记录对应的HTML、执行器或版本已改变")
    check_result(root, meta, result, event["run_id"], bound)
