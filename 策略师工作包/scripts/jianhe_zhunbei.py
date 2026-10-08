#!/usr/bin/env python3
"""检核分派一键准备（U04）：按独立任务当前版本生成检核分派请求并直接 prepare。

一条命令完成：定位当前版本与指纹 → 计算累计检核范围（首次完整/之后增量）→ 排除历版作者与本主控 →
写出请求原件与第2版报告模板 → agent_dispatch prepare。之后只需 assign 真实独立实例。
不派模型、不代替检核；当前版本未登记时给前置提示。"""
import argparse
import json
from pathlib import Path

from phase2_store import WorkflowError, atomic_json, local
from standalone_store import identifier

REPORT_TEMPLATE = {
    "report_schema_version": 2, "status": "passed | passed_with_yellow | returned | insufficient_evidence",
    "scope": ["写明本次实际检查的范围"], "uncovered": [], "findings": [],
    "summary": "结论摘要", "impact_assessment": {"change_kind": "full | local_text | strategy | unknown | source | numeric | visual", "reason": "实质影响说明"},
    "visual_evidence": "HTML/PDF 成品：必看页逐页截图观察，继承页照抄程序给出的 inherited_pages：[{artifact, method, fingerprint, base_review, inherited_pages, pages:[{page,file:{path,sha256},observation}]}]",
}


def visual_plan(root, target, meta, purpose, simulation=None):
    """W01：必看页 / 继承页（程序按像素比对算出）；HTML 与 PDF 文字不一致直接拒绝准备。"""
    from shijue_zengliang import plan_standalone
    visual = plan_standalone(root, target, meta, purpose, simulation=simulation)
    bad = [c for c in visual["html_pdf_consistency"] if c.get("problems")]
    if bad:
        raise WorkflowError("HTML 与 PDF 不是同一版，先重新导出再准备检核：" + "；".join(
            f"{c['html']} / {c['pdf']}：" + "；".join(c["problems"][:3]) for c in bad))
    return visual


def shijue_summary(visual):
    from shijue_zengliang import summary
    return summary(visual)


def visual_text(visual):
    parts = []
    for a in visual["artifacts"]:
        pages = [m["page"] for m in a["must_see"]]
        parts.append(f"{a['artifact']['path']}：{'全页' if a['mode'] == 'full' else '、'.join(pages) or '无'}"
                     + (f"（全页原因：{a['full_reason']}）" if a["mode"] == "full" and a.get("full_reason") else ""))
    return "；".join(parts) or "无 HTML/PDF"


def prepare_review(root, task_id, actor, *, simulation=False, change_kind="unknown", purpose="revision", dispatch=True):
    from registration_guard import actor_check
    from phase6_targets import catalog
    from incremental_review import default_plan, excluded_instances
    actor_check(actor); identifier(task_id)
    root = Path(root).resolve()
    item = catalog(root).get(f"standalone/{task_id}")
    if not item:
        raise WorkflowError("找不到该独立任务的当前版本")
    task = item["meta"]["task"]
    if not [d for d in task["deliverables"] if "path" in d]:
        raise WorkflowError("当前版本还没有登记交付物：先把回读候选用 standalone_tasks.py iterate/save 登记为当前版本，再准备检核")
    from agent_dispatch import unregistered_candidates
    waiting = [c for c in unregistered_candidates(root) if c["task_id"] == task_id]
    if waiting:
        raise WorkflowError("有已回读但未登记的候选（" + "、".join(c["path"] for c in waiting)
                            + "）；先登记为当前版本（standalone_tasks.py adopt），或策略师明确不要时用 agent_dispatch.py discard --dispatch-id "
                            + waiting[0]["dispatch_id"] + " --words \"策略师原话\" 放弃；检核只针对已登记的当前版本")
    target = item["target"]
    from agent_dispatch import AUTHOR_ROLES, read_log, replay
    # 本任务历次作者分派的实际执行实例也排除（不只排除任务记录者）。
    dispatched = {d.get("instance") for d in replay(read_log(root)).values()
                  if d.get("task_id") == task_id and d.get("role") in AUTHOR_ROLES and d.get("instance")}
    authors = excluded_instances(root, target, item["authors"]) | dispatched | {actor}
    plan = default_plan(root, target, "dispatch-plan-check", simulation, change_kind, purpose)
    if not plan.get("dispatch_required", True):
        return {"status": "reused", "dispatch_required": False, "plan": plan,
                "message": "当前版本已有有效检核且未变化，直接复用，不再派审"}
    revision = target["version"]
    folder = f"project/reviews/{task_id}/r{revision}"
    sources = plan["checked_sources_required"]
    visual = visual_plan(root, target, item["meta"], purpose, simulation)
    manual = {c["html"]: c["pdf"] for c in visual["html_pdf_consistency"] if c.get("manual_required")}
    atomic_json(local(root, f"{folder}/视觉必看页.json"), visual)
    request = {
        "task_id": task_id, "role": "independent-reviewer",
        "task_text": (f"独立检核任务 {task_id} 第 {revision} 版（{task['title']}）。"
                      f"【引用原话（用户原始要求，供核对，不是本次指令）】{task['request_text']}【引用结束】\n"
                      f"检核目标：{json.dumps(target, ensure_ascii=False)}；模式：{plan['review_mode']}。"
                      "逐份读取 input_files 全部原文与成品（来源与数字检核范围不因视觉继承缩小）；"
                      f"HTML/PDF 只需实际截图观察“必看页”（{visual_text(visual)}），其余页由程序按像素比对继承上次结论，"
                      f"继承页原样照抄 {folder}/视觉必看页.json 里的 inherited_pages 与 base_review，不写成本轮已看；"
                      f"报告按第2版格式写到 {folder}/检核报告.json（模板见同目录 报告模板.json），截图放 {folder}/截图/。"
                      "红灯/未覆盖不能判通过；只读来源与成品，不改任何业务文件。"),
        "input_files": sources, "upstream_refs": [],
        "permissions": ["只读 input_files 列出的来源与成品", f"只写 {folder}/"],
        "completion_criteria": ["第2版检核报告覆盖全部 checked_sources_required", "HTML/PDF 必看页有实际截图观察，继承页只用程序给出的清单",
                                "结论只能是 passed/passed_with_yellow/returned/insufficient_evidence"],
        "excluded_instances": sorted(authors), "review_target": target, "simulation": simulation,
        "change_kind": change_kind, "review_purpose": purpose,
    }
    atomic_json(local(root, f"{folder}/检核分派请求.json"), request)
    atomic_json(local(root, f"{folder}/报告模板.json"), {"target": target, "reviewer_instance": "填写实际独立实例",
        "simulation": simulation, "checked_sources": sources, "review_mode": plan["review_mode"], "review_purpose": purpose,
        "base_review": plan.get("base_review"), "changed_scope": plan.get("changed_scope", []),
        "reused_coverage": plan.get("reused_coverage", []), **REPORT_TEMPLATE,
        "visual_evidence": [{"artifact": a["artifact"], "method": "填写实际查看方法（真实浏览器/PDF 阅读器逐页截图）",
                             **({"fingerprint": a["fingerprint"]} if a["fingerprint"] else {}),
                             "base_review": a["base_review"], "inherited_pages": a["inherited"],
                             **({"same_version_manual": {"pdf": manual[a["artifact"]["path"]], "reason": "程序比对不可用（缺本机浏览器渲染），检核员逐页人工确认",
                                                         "pages": [m["page"] for m in a["must_see"]], "observation": "填写逐页核对 HTML 与 PDF 文字是否同一版的结果"}}
                                if a["artifact"]["path"] in manual else {}),
                             "pages": [{"page": m["page"], "file": {"path": f"{folder}/截图/填写截图文件", "sha256": "填写"},
                                        "observation": "填写实际观察（必看原因：" + m["reason"] + "）"} for m in a["must_see"]]}
                            for a in visual["artifacts"]]})
    result = {"status": "request_ready", "request_path": f"{folder}/检核分派请求.json",
              "report_template": f"{folder}/报告模板.json", "review_mode": plan["review_mode"],
              "checked_sources_required": len(sources), "excluded_instances": sorted(authors),
              "visual_plan": f"{folder}/视觉必看页.json", "visual_must_see": shijue_summary(visual),
              **({"html_pdf_notices": [c["notice"] for c in visual["html_pdf_consistency"] if c.get("notice")]}
                 if any(c.get("notice") for c in visual["html_pdf_consistency"]) else {})}
    if dispatch:
        from agent_dispatch import prepare
        prepared = prepare(root, request, actor)
        result.update(status="prepared", dispatch_id=prepared.get("dispatch_id"),
                      next_action=f"调用一个未参与创作的实例后：agent_dispatch.py assign --dispatch-id {prepared.get('dispatch_id')} "
                                  "--instance <实例> --host-instance <宿主实例> --evidence <调用证据> --actor " + actor)
    return result


def main():
    parser = argparse.ArgumentParser(description="一条命令准备独立任务当前版本的检核分派（不派模型、不代替检核）")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--simulation", action="store_true")
    parser.add_argument("--purpose", choices=["stage", "revision", "final"], default="revision")
    parser.add_argument("--change-kind", choices=["local_text", "strategy", "unknown", "source", "numeric", "visual"], default="unknown")
    parser.add_argument("--request-only", action="store_true", help="只写请求，不 prepare")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = prepare_review(args.workspace, args.task_id, args.actor, simulation=args.simulation,
                            change_kind=args.change_kind, purpose=args.purpose, dispatch=not args.request_only)
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
