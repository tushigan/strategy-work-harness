"""Create versioned draft XLSX files; never upload or auto-approve them."""
import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from check_export import readback
from compare_import_template import binding
from gantt_model import ADAPTER_VERSION
from phase2_review import gate
from phase2_store import (WorkflowError, append, atomic_json, events, local, output_json, publish_file,
                         current, ref, registry, sha, ensure_revision_allowed)
from template_profile import SHEET, DATE_FIELDS
from validate_gantt import validate_current
from workbook_validations import restore_validations


def write_workbook(spec_path, output):
    from phase2_store import read_json
    spec = read_json(spec_path)
    run_node("write", spec_path, output)
    restore_validations(spec["template"], output, spec["sheet"], len(spec["rows"]))


def run_node(*args):
    node = os.environ.get("HARNESS_NODE", "node")
    script = Path(__file__).with_name("workbook_io.mjs")
    try:
        result = subprocess.run([node, str(script), *map(str, args)], check=True,
                                text=True, capture_output=True, timeout=300)
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise WorkflowError(f"Excel 制作/显示检查失败，未批准导出: {detail[-2500:]}") from exc


def export(root, actor):
    root = Path(root).resolve()
    gate(root, "contract")
    plan = gate(root, "plan")
    check = validate_current(root)
    if not check["valid"]:
        raise WorkflowError("仍是草稿，不能生成上传候选:\n" + "\n".join(check["errors"]))
    template = binding(root)
    history = registry(root)["artifacts"].get("gantt", [])
    if history:
        last = history[-1]
        if last["dependencies"] == [ref(plan)] and last["source_files"] == [template["source"]]:
            meta = current(root, "gantt")
            actual = readback(local(root, meta["path"]), template["profile"], check["rows"])
            if actual["valid"]:
                return render_checks(root, meta, plan, template, actual, len(check["rows"]))
            raise WorkflowError("相同计划对应的历史 Excel 不一致，保留原文件并调查原因")
    ensure_revision_allowed(root, "gantt")
    source = local(root, template["source"]["path"])
    profile = template["profile"]
    n = len(check["rows"])
    with tempfile.TemporaryDirectory(prefix="harness-export-") as folder:
        folder = Path(folder)
        spec = {"template": str(source), "sheet": SHEET, "headers": profile["headers"],
                "rows": [[r.get(h, "") for h in profile["headers"]] for r in check["rows"]],
                "dateFields": DATE_FIELDS, "validations": profile["validations"],
                "oldDataRows": profile["old_data_rows"]}
        atomic_json(folder / "spec.json", spec)
        workbook = folder / "draft.xlsx"
        write_workbook(folder / "spec.json", workbook)
        actual = readback(workbook, profile, check["rows"])
        if not actual["valid"]:
            raise WorkflowError("导出回读不一致:\n" + "\n".join(actual["errors"]))
        meta = publish_file(root, "gantt", workbook, actor, dependencies=[ref(plan)],
                            source_files=[template["source"]])
    return render_checks(root, meta, plan, template, actual, n)


def render_checks(root, meta, plan, template, actual, n):
    profile = template["profile"]
    preview_dir = local(root, f"project/outputs/versions/gantt/v{meta['version']:04d}-preview")
    specs = [{"sheetName": SHEET, "range": f"{left}1:{right}{min(n + 1, 15)}"}
             for left, right in (("A", "M"), ("N", "AA"), ("AB", "AI"))]
    specs.extend([{"sheetName": "数据字典", "range": "A1:G9"},
                  {"sheetName": "填写说明", "range": "A1:F19"},
                  {"sheetName": "填写说明", "range": "A21:F39"},
                  {"sheetName": "填写说明", "range": "A40:F56"}])
    # More than 14 rows must also have visual evidence, not just a top-of-sheet sample.
    for start in range(16, n + 2, 14):
        specs.extend({"sheetName": SHEET, "range": f"{a}{start}:{b}{min(start + 13, n + 1)}"}
                     for a, b in (("A", "M"), ("N", "AA"), ("AB", "AI")))
    with tempfile.TemporaryDirectory(prefix="harness-render-") as folder:
        spec_path = Path(folder) / "ranges.json"
        atomic_json(spec_path, specs)
        run_node("render", local(root, meta["path"]), preview_dir, spec_path)
    previews = [{"path": str(p.relative_to(root)).replace("\\", "/"), "sha256": sha(p)}
                for p in sorted(preview_dir.glob("*.png"))]
    if len(previews) != len(specs):
        raise WorkflowError("显示效果检查图不完整，Excel 保持草稿")
    append(root, "export-checks", {"record_type": "file_check", "target": ref(meta),
           "template_sha256": profile["sha256"], "plan_ref": ref(plan),
           "adapter_version": ADAPTER_VERSION, "readback": actual, "previews": previews,
           "visual_review": "pending_independent_review"})
    if not any(e.get("target") == ref(meta) for e in events(root, "system-imports")):
        append(root, "system-imports", {"record_type": "system_import", "target": ref(meta),
               "status": "not_uploaded", "template_sha256": profile["sha256"], "plan_ref": ref(plan),
               "adapter_version": ADAPTER_VERSION})
    from workflow_status import import_state
    return {"artifact": meta, "status": "draft_pending_file_review",
            "previews": previews, "system_import": import_state(root, meta)["status"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--resume-checks", action="store_true", help="对未完成显示检查的当前 Excel 重做检查")
    args = parser.parse_args()
    if args.resume_checks:
        root = args.workspace.resolve()
        plan = gate(root, "plan")
        meta, template = current(root, "gantt"), binding(root)
        if not any(s["sha256"] == template["source"]["sha256"] for s in meta["source_files"]):
            raise WorkflowError("模板已更新，请保留当前草稿并重新生成")
        check = validate_current(root)
        actual = readback(local(root, meta["path"]), template["profile"], check["rows"])
        if not check["valid"] or not actual["valid"]:
            raise WorkflowError("计划或文件回读未通过，不能只补显示检查")
        output_json(render_checks(root, meta, plan, template, actual, len(check["rows"])))
    else:
        output_json(export(args.workspace, args.actor))


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
