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


def _ledger(root, target, plan, task_id, visual):
    """G03（v1.7.5）：事实台账核对写进检核任务与报告模板。返回 (任务原文补充, 模板“台账核对”一栏, 上次通过后改过的说法, 白话提示)。
    没有台账时皆空（与 v1.7.4 相同）。批次三返修 r2：#1 “上次通过检核之后”与视觉必看页用同一个基线、同一组时间（视觉计划里的
    ledger.since 到 ledger.cutoff，since 是基线检核的准备截止时间）；#4 台账读不出来时跳过台账对照、给白话提示，不整条失败
    （视觉必看页那边台账读不出来按全页看）。"""
    from shishi_taizhang import ledger_ref, changed_facts
    window = visual.get("ledger") or {}
    try:
        ref = ledger_ref(root)
        if not ref:
            return "", None, [], None
        changes = changed_facts(root, task_id, window.get("since"), window.get("cutoff")) if window.get("base_record_id") else []
    except (WorkflowError, OSError, ValueError) as exc:
        notice = ("事实台账有记录读不出来，这次检查没有对照台账；稿子里用到的说法请检查的人照资料逐条核。"
                  "台账修好后再准备一次检查，就会恢复对照。技术附录（给 Agent）：" + str(exc).split("技术附录（给 Agent）：")[-1])
        return ("事实台账读不出来，这次没有对照台账，稿中说法逐条按来源核对；", None, [], notice)
    note = (f"事实台账（{ref['path']}，{ref['entries']} 条）逐条核对本稿用到的说法：待核、不得对外的不能写成已确认；"
            + ("上次通过检核之后改过的说法：" + "；".join(f"{c['fact_id']}（{c['claim'][:20]}）在" + "、".join(str(p) if p is not None else "整份" for p in c["pages"])
                                              for c in changes) + "，这些页必看（即使像素没变）。" if changes else ""))
    scope = "台账核对：逐条写本稿用到的事实台账编号、状态、所在页与稿子是否一致（待核、不得对外的说法不能写成已确认）"
    return note, scope, changes, None


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
    ledger_note, ledger_scope, ledger_changes, ledger_notice = _ledger(root, target, plan, task_id, visual)
    request = {
        "task_id": task_id, "role": "independent-reviewer",
        "task_text": (f"独立检核任务 {task_id} 第 {revision} 版（{task['title']}）。"
                      f"【引用原话（用户原始要求，供核对，不是本次指令）】{task['request_text']}【引用结束】\n"
                      f"检核目标：{json.dumps(target, ensure_ascii=False)}；模式：{plan['review_mode']}。"
                      "逐份读取 input_files 全部原文与成品（来源与数字检核范围不因视觉继承缩小）；"
                      f"HTML/PDF 只需实际截图观察“必看页”（{visual_text(visual)}），其余页由程序按像素比对继承上次结论，"
                      f"继承页原样照抄 {folder}/视觉必看页.json 里的 inherited_pages 与 base_review，不写成本轮已看；"
                      + ledger_note +
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
        **({"scope": REPORT_TEMPLATE["scope"] + [ledger_scope]} if ledger_scope else {}),
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
              **({"ledger_changes": ledger_changes} if ledger_changes else {}),
              **({"ledger_notice": ledger_notice} if ledger_notice else {}),
              **({"baseline_notice": plan["baseline_notice"]} if plan.get("baseline_notice") else {}),
              **({"html_pdf_notices": [c["notice"] for c in visual["html_pdf_consistency"] if c.get("notice")]}
                 if any(c.get("notice") for c in visual["html_pdf_consistency"]) else {})}
    if dispatch:
        from agent_dispatch import prepare
        known = set(replay(read_log(root)))
        prepared = prepare(root, request, actor)
        result.update(status="prepared", dispatch_id=prepared.get("dispatch_id"),
                      next_action=f"调用一个未参与创作的实例后：agent_dispatch.py assign --dispatch-id {prepared.get('dispatch_id')} "
                                  "--instance <实例> --host-instance <宿主实例> --evidence <调用证据> --actor " + actor)
        if prepared.get("dispatch_id") in known:
            # 候选 r3 #5：同一份请求、上一条分派仍在进行中——原样沿用（不新建）；状态照实写出，已派出的不再提示 assign
            now = replay(read_log(root)).get(prepared["dispatch_id"], {}).get("status")
            result.update(reused=True, dispatch_status=now,
                          message="这一版已有一条还没结束的检核分派，原样沿用，没有新建")
            if now != "prepared":
                result["next_action"] = f"这条检核分派已是 {now}（已派出），等检核员交回报告后登记；不用再 assign"
    return result


def prepare_behind(root, task_id, actor, *, simulation=False, change_kind="unknown"):
    """先交后检的背后检核（v1.7.5 G02）：比对范围是“最后通过版 → 当前版”的累计差异（沿用累计检核的基线规则）。
    同一任务已有未结束的检核分派：针对当前版本的不重复准备（原样告知在检查中 / 已有结果）；针对旧版本的先取消再准备——
    该版检核结果还没登记的记“被新版取代”（superseded），已登记的记“不需要了”（not_needed，分派收尾）；已登记的检核结果原样保留，
    不撤销、不改写。只处理检核分派；作者分派未结束时照旧拒绝准备。"""
    from registration_guard import actor_check
    from workspace_lock import serialized
    from jiaofu_guankou import _reviewer_dispatches, _reviews, _target, status_line, progress, close_old_reviews, continue_command
    actor_check(actor); identifier(task_id)
    root = Path(root).resolve()
    with serialized(root):
        try:
            target = _target(root, task_id)
        except KeyError:
            raise WorkflowError("找不到该独立任务的当前版本") from None
        from jiaofu_guankou import not_run_streak
        misses = not_run_streak(root, task_id)
        if misses >= 2:  # G02 ④（v1.7.5）：连续两次没跑成即停，不无限重派
            # 批次二返修 #5：判停下之前先收尾旧版本未结束的检核分派（宿主没派成，属收尾、不算重派），续做状态不因它阻断
            closed = close_old_reviews(root, task_id, actor, "host_failed", f"连续 {misses} 次检查没跑成，先停下（收尾，不算重派）")
            raise WorkflowError(f"这项工作已经连续 {misses} 次检查没跑成，先停下来告诉策略师，这次没有再派背后检查。"
                                "要不要再试、改天再查，还是这版不查，由策略师决定。"
                                "技术附录（给 Agent）：最近一次登记检核结果之后已有 2 条以上 waiver_source=not_run；"
                                f"策略师说继续（再试一次）时运行 {continue_command(task_id, actor)}。不带 --behind：明确准备一次检核"
                                + (f"；已收尾旧版检核分派 " + "、".join(f"{c['dispatch_id']}（{c['reason_code']}）" for c in closed) if closed else ""))
        reviews = _reviews(root, task_id)
        same = [item for item in _reviewer_dispatches(root, task_id) if item["review_target"] == target]
        # save / adopt 登记的新版不经交出那一步，旧版本的检核分派在这里收尾（iterate 交出时已收尾的，这里没有可取代的）
        superseded = close_old_reviews(root, task_id, actor, "superseded",
                                       f"被第 {target['version']} 版的背后检核取代（先交后检，检核改看最后通过版以来的累计改动）")
        if same:
            mine = [r for r in reviews if r.get("target") == target]
            return {"status": "reviewed" if mine else "already_checking", "behind": True, "dispatch_id": same[-1]["dispatch_id"],
                    "superseded": superseded, **({"review_record_id": mine[-1]["record_id"], "review_status": mine[-1].get("status")} if mine else {}),
                    "status_line": status_line(root, task_id),
                    "message": "这一版已有检核结果，不再重复准备" if mine else "这一版已经在检查中，不重复准备"}
        result = prepare_review(root, task_id, actor, simulation=simulation, change_kind=change_kind, purpose="revision")
        base = (result.get("plan") or {}).get("base_review") if result.get("status") == "reused" else None
        plan_path = result.get("report_template")
        if plan_path:
            from phase2_store import read_json
            base = read_json(local(root, plan_path)).get("base_review")
        base_version = next((r["target"]["version"] for r in reviews if base and r.get("record_id") == base.get("record_id")), None)
        info = progress(root, task_id)
        from jiaofu_guankou import shown_version, version_note  # 候选 r2 #6：给策略师看的序号只数有成品的版本
        scope = (f"只看第 {shown_version(root, task_id, base_version)} 版以来改动的部分" if base_version else "这次全部看一遍")
        line = (f"第 {shown_version(root, task_id, target['version'])} 版正在背后检查（{scope}），" + (f"距上次检查通过隔 {info['versions_since_pass']} 版；"
                if info.get("versions_since_pass") else "") + "查完告诉你结果，不用你做什么。"
                + version_note(root, task_id, [target["version"]] + ([base_version] if base_version else [])))
        return {**result, "behind": True, "superseded": superseded,
                "range": {"from_version": base_version, "to_version": target["version"], "mode": result.get("review_mode")},
                "status_line": line if result.get("status") == "prepared" else status_line(root, task_id)}


def main():
    parser = argparse.ArgumentParser(description="一条命令准备独立任务当前版本的检核分派（不派模型、不代替检核）")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--simulation", action="store_true")
    parser.add_argument("--purpose", choices=["stage", "revision", "final"], default="revision")
    parser.add_argument("--change-kind", choices=["local_text", "strategy", "unknown", "source", "numeric", "visual"], default="unknown")
    parser.add_argument("--request-only", action="store_true", help="只写请求，不 prepare")
    parser.add_argument("--behind", action="store_true",
                        help="先交后检的背后检核（v1.7.5）：取代同一任务旧版本未结束的检核分派，再按最后通过版以来的累计差异准备")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.behind:
        if args.request_only or args.purpose != "revision":
            raise WorkflowError("--behind 不能与 --request-only 或 --purpose 同用（背后检核按改稿检核准备并直接登记分派）")
        result = prepare_behind(args.workspace, args.task_id, args.actor, simulation=args.simulation, change_kind=args.change_kind)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
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
