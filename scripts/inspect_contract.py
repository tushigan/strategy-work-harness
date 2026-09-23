"""Archive and read a contract, or save a reviewed interpretation as a new draft."""
import argparse
from pathlib import Path

from contract_reader import ALLOWED_KINDS, read_contract
from phase2_store import (WorkflowError, archive, current, local, output_json,
                         publish, read_json, ref)


def resolve(root, decision_path, actor):
    meta = current(root, "contract")
    payload = read_json(local(root, meta["snapshot_path"]))
    decisions = read_json(decision_path)
    if decisions.get("target") != ref(meta) or decisions.get("source_review_complete") is not True:
        raise WorkflowError("修订必须绑定当前合同版本，并确认已读全部原文")
    by_id = {item["item_id"]: item for item in decisions.get("items", [])}
    if len(by_id) != len(decisions.get("items", [])) or set(by_id) != {i["item_id"] for i in payload["items"]}:
        raise WorkflowError("所有候选必须逐一保留并解释；不能丢项或重复编号")
    for item in payload["items"]:
        choice = by_id[item["item_id"]]
        if choice.get("disposition") not in {"included", "not_service"} or not choice.get("note", "").strip():
            raise WorkflowError("每项需要纳入/非服务判断及依据")
        if choice.get("service_kind") not in ALLOWED_KINDS or not choice.get("title", "").strip():
            raise WorkflowError("每项需要有效类别和标题")
        for key in ("disposition", "service_kind", "title", "note"):
            item[key] = choice[key]
    for added in decisions.get("added_items", []):
        if (added.get("item_id") in by_id or not str(added.get("item_id", "")).startswith("MANUAL-")
                or added.get("service_kind") not in ALLOWED_KINDS):
            raise WorkflowError("补列项需唯一 MANUAL- 编号及有效类别")
        if not all(isinstance(added.get(k), str) and added[k].strip()
                   for k in ("raw_text", "source_location", "title", "note")):
            raise WorkflowError("补列项必须附原文、来源和解释")
        if not any(added["raw_text"] in unit["text"] and
                   added["source_location"].startswith(unit["location"]) for unit in payload["units"]):
            raise WorkflowError("补列项原文及位置无法在本次完整读取资料核对")
        payload["items"].append({**added, "disposition": "included"})
        by_id[added["item_id"]] = added
    answers = decisions.get("issue_resolutions", {})
    if set(answers) != {i["issue_id"] for i in payload["issues"]}:
        raise WorkflowError("需逐一说明所有疑点")
    for issue in payload["issues"]:
        if issue.get("hard_block"):
            raise WorkflowError("存在不可读页面，请提供可读副本重新识别，不能用确认绕过")
        answer = answers[issue["issue_id"]]
        if not isinstance(answer, str) or not answer.strip():
            raise WorkflowError("疑点说明不能为空")
        issue.update({"resolved": True, "resolution": answer})
    if not any(i["disposition"] == "included" for i in payload["items"]):
        raise WorkflowError("没有纳入任何合同服务，不能进入排期")
    payload.update({"source_review_complete": True, "recognition_status": "pending_review"})
    return publish(root, "contract", payload, actor,
                   source_files=[*meta["source_files"], archive(root, decision_path, "evidence")])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", type=Path)
    group.add_argument("--resolve", type=Path, help="逐项解释、疑点处理 JSON；不是人工批准")
    parser.add_argument("--actor", required=True)
    args = parser.parse_args()
    if args.resolve:
        result = resolve(args.workspace, args.resolve, args.actor)
    else:
        source = archive(args.workspace, args.input, "contracts")
        payload = read_contract(local(args.workspace, source["path"]))
        payload["source_name"] = source["name"]
        result = publish(args.workspace, "contract", payload, args.actor, source_files=[source])
    output_json({"artifact": result, "status": "draft", "next": "独立检核与策略师确认"})


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
