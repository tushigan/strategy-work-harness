"""Retain manual edits and restore the registered pointer only after an explicit decision."""
import argparse
from pathlib import Path

from phase2_store import (WorkflowError, append, archive, atomic_json, latest, local,
                         output_json, preserve_conflict, read_json, ref, sha, test_mode)


def resolve(root, kind, decision_path, actor):
    meta = latest(root, kind)
    pointer, snapshot = local(root, meta["path"]), local(root, meta["snapshot_path"])
    if not pointer.is_file() or not snapshot.is_file() or sha(snapshot) != meta["sha256"]:
        raise WorkflowError("当前文件或登记快照丢失/损坏；先依据历史记录人工恢复，不猜造")
    decision = read_json(decision_path)
    if (not actor.strip() or decision.get("target") != ref(meta)
            or decision.get("conflict_sha256") != sha(pointer)
            or decision.get("action") != "restore_registered_snapshot"
            or not str(decision.get("confirmation_text", "")).strip()
            or not str(decision.get("rationale", "")).strip()):
        raise WorkflowError("缺精确版本、人工分支指纹或策略师选择依据")
    if decision.get("simulation") and not test_mode(root):
        raise WorkflowError("正式项目不能采用模拟冲突决定")
    if sha(pointer) == meta["sha256"]:
        raise WorkflowError("当前无人工分支冲突，不需恢复")
    try:
        preserve_conflict(root, meta)
    except WorkflowError:
        pass
    conflict = f"project/outputs/conflicts/{kind}/{sha(pointer)}.json"
    if not local(root, conflict).is_file() or sha(local(root, conflict)) != sha(pointer):
        raise WorkflowError("人工分支未成功留档，不能恢复指针")
    evidence = archive(root, decision_path, "evidence")
    atomic_json(pointer, read_json(snapshot))
    event = append(root, "conflict-resolutions", {"actor": actor, "status": "pointer_restored",
                   "target": ref(meta), "conflict": {"path": conflict, "sha256": decision["conflict_sha256"]},
                   "evidence": evidence, "simulation": bool(decision.get("simulation"))})
    from workflow_status import refresh
    refresh(root)
    return event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["contract", "plan"])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--decision", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    args = parser.parse_args()
    output_json(resolve(args.workspace, args.kind, args.decision, args.actor))


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
