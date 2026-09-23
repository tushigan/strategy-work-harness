"""Versioned controller summaries and externally executed task deliveries."""
import argparse
import re
from pathlib import Path

from phase2_store import WorkflowError, atomic_json, fingerprint, local, read_json, sha
from phase3_store import plan_ref, task
from phase5_common import atomic_bytes, json_bytes, now
from phase6_events import actor_check, append, archive, decision_valid, events, run_cli
from phase6_targets import (CONTROL, catalog, control_ref, control_registry, identity,
                            resolve, sources)
from workspace_lock import serialized

KINDS = {"project-summary", "closure-report", "external-deliverable"}


def key_for(data):
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        raise WorkflowError("总控产出类别不合法，不允许改名清零轮次")
    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", task_id):
        raise WorkflowError("任务编号不合法")
    if data["kind"] != "external-deliverable" and task_id != "project":
        raise WorkflowError("项目总控文件固定使用 project 编号")
    return f"{task_id}::{data['kind']}"


def latest(root, key):
    values = control_registry(root)["artifacts"].get(key, [])
    if not values:
        raise WorkflowError("尚未登记总控产出")
    return values[-1]


def publish(root, data, author, *, dependencies=(), source_paths=(), revision=None):
    with serialized(root):
        return _publish_unlocked(root, data, author, dependencies=dependencies,
                                 source_paths=source_paths, revision=revision)


def _publish_unlocked(root, data, author, *, dependencies=(), source_paths=(), revision=None):
    from review_gate import gate
    if not isinstance(author, str) or not author.strip():
        raise WorkflowError("需记录实际作者执行实例")
    key = key_for(data)
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise WorkflowError("缺标题")
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        raise WorkflowError("须提供完整正文 sections")
    if any(not isinstance(s, dict) or any(not isinstance(s.get(k), str) or not s[k].strip()
            for k in ("id", "title", "text")) for s in sections):
        raise WorkflowError("每章须有 id/title/text")
    if len({s["id"] for s in sections}) != len(sections):
        raise WorkflowError("章节编号不能重复")
    if set(data) - {"kind", "task_id", "title", "sections", "checklist_sha256"}:
        raise WorkflowError("总控正文包含未知字段")
    state = read_json(local(root, "project/state.json"))
    if not state.get("project_id") or state["status"] == "not_initialized":
        raise WorkflowError("先初始化项目")
    deps = [plan_ref(root), *dependencies]
    if data["kind"] == "external-deliverable":
        task(root, data["task_id"])
        brief = catalog(root).get(f"phase3/{data['task_id']}::brief")
        if not brief:
            raise WorkflowError("外部交付执行前须先登记并检核任务简报")
        if not any(identity(d) == identity(brief["target"]) for d in deps):
            deps.append(brief["target"])
    if len({identity(d) for d in deps}) != len(deps):
        raise WorkflowError("上游引用重复；计划由脚本自动绑定")
    for dep in deps:
        if identity(dep) == f"control/{key}":
            raise WorkflowError("不能依赖自身历史以制造循环")
        gate(root, dep)
    if data["kind"] == "external-deliverable":
        if not source_paths:
            raise WorkflowError("外部执行任务须归档实际交付文件，不能仅凭摘要完成")
    reg = control_registry(root)
    history = reg["artifacts"].setdefault(key, [])
    source_files = [archive(root, local(root, p)) for p in source_paths]
    digest = fingerprint({"data": data, "dependencies": deps, "source_files": source_files})
    if history and history[-1]["content_hash"] == digest:
        return resolve(root, control_ref(history[-1]))
    previous = history[-1] if history else None
    if previous:
        if not isinstance(revision, dict) or revision.get("base") != control_ref(previous) or (
                not isinstance(revision.get("reason"), str) or not revision["reason"].strip()) or (
                not isinstance(revision.get("changes"), list) or not revision["changes"] or any(
                    not isinstance(x, str) or not x.strip() for x in revision["changes"])):
            raise WorkflowError("修订须绑定上一版、修改原因和具体 changes")
        if previous.get("revision"):
            from control_review import revision_review
            revision_review(root, previous)
        if len(history) - 1 >= 2:
            allowed = [e for e in events(root, "control_allow_more")
                       if e["record_id"] == revision.get("allowance_id")
                       and e.get("target") == control_ref(previous) and decision_valid(root, e)]
            if not allowed:
                raise WorkflowError("已达两轮自动修订，须策略师明确授权再加一轮；历史不清零")
            source_files.append(allowed[-1]["evidence"])
    elif revision is not None:
        raise WorkflowError("初稿不能携带修订记录")
    folder = f"project/outputs/control/{key.replace('::', '/')}"
    used = [int(p.stem[1:]) for p in local(root, folder).glob("v*.json") if p.stem[1:].isdigit()]
    version = max([x["version"] for x in history] + used + [0]) + 1
    paths = [f"{folder}/v{version:04d}.{ext}" for ext in ("json", "md")]
    raw = json_bytes(data)
    markdown = "# " + data["title"] + "\n\n" + "\n\n".join(
        f"## {s['title']}\n\n{s['text']}" for s in sections) + "\n"
    for relative, content in zip(paths, (raw, markdown.encode())):
        if local(root, relative).exists():
            raise WorkflowError("版本文件已存在，不能覆盖历史")
        atomic_bytes(local(root, relative), content)
    meta = {"key": key, "task_id": data["task_id"], "kind": data["kind"], "version": version,
            "sha256": sha(local(root, paths[0])), "author_instance": author, "created_at": now(),
            "dependencies": deps, "source_files": source_files, "revision": revision,
            "content_hash": digest, "files": [{"path": p, "sha256": sha(local(root, p))} for p in paths]}
    history.append(meta)
    atomic_json(local(root, CONTROL), reg)
    append(root, "control_published", {"target": control_ref(meta), "author_instance": author,
                                     "revision": revision})
    return meta


def allow_more(root, key, evidence, actor, simulation=False):
    actor_check(root, actor, simulation)
    meta = latest(root, key)
    return append(root, "control_allow_more", {"target": control_ref(meta), "actor": actor,
        "simulation": simulation, "evidence": archive(root, evidence), "additional_rounds": 1}, unique=True)


def review_sources(root, key):
    meta = latest(root, key)
    target = control_ref(meta)
    resolve(root, target)
    data = read_json(local(root, meta["files"][0]["path"]))
    return {"target": target, "scope_required": [x["id"] for x in data["sections"]],
            "checked_sources_required": sources(root, target),
            "author_instances": catalog(root)[identity(target)]["authors"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("publish", "sources", "review", "allow-more"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--actor", default="")
    parser.add_argument("--simulation", action="store_true")
    args = parser.parse_args()
    if args.action == "publish":
        request = read_json(args.request)
        return publish(args.workspace, request["data"], args.actor,
            dependencies=request.get("dependencies", []), source_paths=request.get("source_paths", []),
            revision=request.get("revision"))
    if not args.key:
        raise WorkflowError("缺 --key")
    if args.action == "sources":
        return review_sources(args.workspace, args.key)
    if not args.evidence:
        raise WorkflowError("缺 --evidence")
    if args.action == "review":
        from control_review import record_review
        return record_review(args.workspace, args.key, args.evidence)
    return allow_more(args.workspace, args.key, args.evidence, args.actor, args.simulation)


if __name__ == "__main__":
    run_cli(main)
