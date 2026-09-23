#!/usr/bin/env python3
"""One entry for native gates, explicit control reviews and version-bound decisions."""
import argparse
from pathlib import Path

from phase2_store import WorkflowError, local, read_json
from phase6_events import (actor_check, append, archive, decision_valid, events,
                           run_cli, valid_file)
from phase6_targets import (catalog, identity, last_review, resolve, review_stamp)
from task_validation import read_check


@read_check
def gate(root, target, *, human=False, client=False):
    human = human or client
    meta = resolve(root, target)
    space, key = target["space"], target["key"]
    if space == "source":
        if human or client:
            raise WorkflowError("来源资料不是可批准的正式产出")
        return meta
    if space == "phase2":
        from phase2_review import gate as native_gate
        native_gate(root, key, human=human)
    elif space in {"phase3", "phase5"}:
        if space == "phase3":
            from phase3_review import gate as native_gate
        else:
            from phase5_review import gate as native_gate
        native_gate(root, key, human=human)
    elif space == "html":
        from brand_house_review import gate as native_gate
        native_gate(root, key)
    elif space == "design-submission":
        from design_expression import gate as native_gate
        native_gate(root, key)
    elif space == "standalone":
        from standalone_review import gate as native_gate
        native_gate(root, key)
    else:
        from control_review import gate as native_gate
        native_gate(root, key)
    report = last_review(root, target)
    reviewer = report.get("reviewer_instance", report.get("actor"))
    if reviewer in catalog(root)[identity(target)]["authors"]:
        raise WorkflowError("参与此产出的任一作者实例不能独立检核")
    if not valid_file(root, report.get("evidence")):
        raise WorkflowError("独立检核原件失效")
    if human and space in {"html", "design-submission", "control", "standalone"}:
        confirmation(root, target, "strategist")
    if client:
        confirmation(root, target, "client")
    return meta


def confirmation(root, target, role):
    matches = [x for x in events(root, "confirmation")
               if x.get("target") == target and x.get("role") == role]
    value = matches[-1] if matches else {}
    if not decision_valid(root, value) or value.get("status") != "confirmed" or (
            value.get("review") != review_stamp(root, target)):
        raise WorkflowError(f"当前版本没有绑定最新检核的有效 {role} 确认")
    return value


def confirm(root, target, evidence, actor, *, role="strategist",
            status="confirmed", simulation=False):
    actor_check(root, actor, simulation)
    if role not in {"strategist", "client"} or status not in {"confirmed", "rejected"}:
        raise WorkflowError("确认角色/状态不合法")
    if not Path(evidence).read_text(encoding="utf-8").strip():
        raise WorkflowError("需要实际确认原文")
    space, key = target["space"], target["key"]
    resolve(root, target)
    if status == "confirmed":
        gate(root, target, human=role == "client")
    if role == "strategist" and space in {"phase2", "phase3", "phase5"}:
        if space == "phase2":
            from phase2_review import confirm as native_confirm
        elif space == "phase3":
            from phase3_review import confirm as native_confirm
        else:
            from phase5_review import confirm as native_confirm
        return native_confirm(root, key, evidence, actor, status=status, simulation=simulation)
    stamp = review_stamp(root, target)
    return append(root, "confirmation", {"target": target, "role": role, "status": status,
        "actor": actor, "simulation": simulation, "review": stamp,
        "evidence": archive(root, evidence)}, unique=True)


def inspect(root):
    result = []
    for marker, item in catalog(root).items():
        target = item["target"]
        row = {"target": target, "author_instances": item["authors"],
               "dependencies": item["dependencies"], "current": False,
               "review": "not_reviewed", "strategist_confirmed": False,
               "client_confirmed": False}
        try:
            resolve(root, target)
            row["current"] = True
            if target["space"] == "source":
                row["review"] = "source_not_deliverable"
            else:
                gate(root, target)
                row["review"] = last_review(root, target)["status"]
                for role, flag in (("strategist", "strategist_confirmed"), ("client", "client_confirmed")):
                    try:
                        gate(root, target, human=role == "strategist", client=role == "client")
                        row[flag] = True
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        row[f"{role}_reason"] = str(exc)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            row["reason"] = str(exc)
            row["review"] = "blocked"
        result.append(row)
    return {"artifacts": result, "independence": "requires_actual_fresh_instance",
            "notice": "独立检核、内部确认、客户确认分开；文件身份字段不构成外部身份认证。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "gate", "confirm", "reject"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--actor", default="")
    parser.add_argument("--role", choices=("strategist", "client"), default="strategist")
    parser.add_argument("--human", action="store_true")
    parser.add_argument("--client", action="store_true")
    parser.add_argument("--simulation", action="store_true")
    args = parser.parse_args()
    if args.action == "inspect":
        return inspect(args.workspace)
    if not args.target:
        raise WorkflowError("缺 --target 完整引用JSON")
    target = read_json(args.target)
    if args.action == "gate":
        return gate(args.workspace, target, human=args.human, client=args.client)
    if not args.evidence:
        raise WorkflowError("缺 --evidence 确认原文")
    return confirm(args.workspace, target, args.evidence, args.actor, role=args.role,
                   status="confirmed" if args.action == "confirm" else "rejected",
                   simulation=args.simulation)


if __name__ == "__main__":
    run_cli(main)
