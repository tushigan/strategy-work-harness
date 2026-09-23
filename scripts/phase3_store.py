"""Task-scoped business versions; reuse Phase 2 without changing its registry."""
import re

from phase2_store import (WorkflowError, current as phase2_current, fingerprint,
                         json_bytes, local, now, read_json, ref as phase2_ref, sha)
from phase3_document import CHAIN, SECTIONS, render, validate
from phase3_io import PENDING, check_pointers, digest, registry, write_transaction
from phase3_sources import file_valid, identifier, source_current, source_ref
from workspace_lock import serialized


def key_for(task_id, kind):
    identifier(task_id)
    if kind not in SECTIONS:
        raise WorkflowError("未知业务产出类别，不能改名新建以清零轮次")
    return f"{task_id}::{kind}"


def ref(meta):
    return {"space": "phase3", "key": meta["key"],
            "version": meta["version"], "sha256": meta["sha256"]}


def plan_ref(root):
    return {"space": "phase2", "key": "plan", **phase2_ref(phase2_current(root, "plan"))}


def task(root, task_id):
    plan = phase2_current(root, "plan")
    data = read_json(local(root, plan["path"]))
    found = [t for t in data["tasks"] if t["task_id"] == task_id]
    if len(found) != 1:
        raise WorkflowError("任务必须唯一存在于当前任务计划，不另建任务")
    return found[0]


def latest(root, key):
    history = registry(root)["artifacts"].get(key, [])
    if not history:
        raise WorkflowError(f"缺少业务产出 {key}")
    return history[-1]


def resolve(root, reference, seen=None):
    space, key = reference.get("space"), reference.get("key")
    if space == "phase3":
        meta = current(root, key, seen)
        actual = ref(meta)
    elif space == "source":
        meta = source_current(root, key)
        actual = source_ref(meta)
    elif space == "phase2" and key == "plan":
        meta = phase2_current(root, "plan")
        actual = plan_ref(root)
    else:
        raise WorkflowError("未知版本引用")
    if actual != reference:
        raise WorkflowError(f"上游 {key} 已更新，当前稿需对齐")
    return meta


def current(root, key, seen=None):
    if local(root, PENDING).exists():
        raise WorkflowError("上次写入未结束，先恢复再读取当前成果")
    seen = set(seen or ())
    if key in seen:
        raise WorkflowError("业务产出依赖存在循环")
    seen.add(key)
    meta = latest(root, key)
    check_pointers(root, meta)
    for path_field, hash_field in (("snapshot_path", "sha256"), ("markdown_snapshot", "markdown_sha256")):
        if not file_valid(root, {"path": meta[path_field], "sha256": meta[hash_field]}):
            raise WorkflowError("历史快照丢失或被修改")
    for dep in meta["dependencies"]:
        resolve(root, dep, seen)
    if any(not file_valid(root, f) for f in meta.get("source_files", [])):
        raise WorkflowError("修订依据的反馈或授权原文改变/丢失")
    from brand_house_guard import guard
    guard(root, meta)
    return meta


def pool(root, dependencies):
    result, visited = {}, set()

    def collect(dep):
        marker = (dep["space"], dep["key"])
        if marker in visited:
            return
        visited.add(marker)
        meta = resolve(root, dep)
        if dep["space"] != "phase3":
            return
        data = read_json(local(root, meta["snapshot_path"]))
        for item in data.get("entries", []):
            if item["id"] in result and result[item["id"]] != item:
                raise WorkflowError("不同证据稿使用了同一编号，先消除歧义")
            result[item["id"]] = item
        for upstream in meta["dependencies"]:
            collect(upstream)
    for dep in dependencies:
        collect(dep)
    return result


def check_chain(root, key, data, dependencies):
    direct = [resolve(root, dep, {key}) for dep in dependencies]
    kinds = {m.get("kind") for m in direct}
    if data["kind"] != "brief" and not any(
            m.get("kind") == "brief" and m.get("task_id") == data["task_id"] for m in direct):
        raise WorkflowError("任务执行前必须关联本任务简报版本")
    required = CHAIN.get(data["kind"])
    if required and required not in kinds:
        raise WorkflowError(f"{data['kind']} 缺少直接上游 {required}")
    if required and data["kind"] != "summary":
        allowed_tasks = (task(root, data["task_id"])["dependencies"] if
                         data["kind"] == "brand-house" else [data["task_id"]])
        if not any(m.get("kind") == required and m.get("task_id") in allowed_tasks for m in direct):
            raise WorkflowError("直接上游不属于本任务或计划指定的前置任务")
    return direct


def paths(task_id, kind, version):
    base = ("project/briefs" if kind == "brief" else "project/research" if
            kind in {"research-plan", "evidence", "analysis"} else "project/outputs/brand-house")
    folder = f"{base}/{task_id}/{kind}"
    return {"path": f"{folder}/current.json", "markdown_path": f"{folder}/{kind}.md",
            "snapshot_path": f"{folder}/versions/v{version:04d}.json",
            "markdown_snapshot": f"{folder}/versions/v{version:04d}.md"}


def context_text(task_data, dependencies):
    return "\n".join([
        f"合同列项：{', '.join(task_data['source_item_ids'])}",
        f"任务：{task_data['title']}；负责人：{task_data.get('executors', [])}",
        f"原排期：{task_data.get('start')} 至 {task_data.get('end')}（未改承诺日期）",
        f"任务交付物：{task_data.get('deliverable')}",
        f"验收：{task_data.get('acceptance')}",
        "上游版本："] + [f"- {d['space']} / {d['key']} / v{d['version']} / {d['sha256']}" for d in dependencies])


def publish(root, data, author, dependencies=(), revision=None):
    with serialized(root):
        return _publish_unlocked(root, data, author, dependencies, revision)


def _publish_unlocked(root, data, author, dependencies=(), revision=None):
    from phase2_review import gate as phase2_gate
    from phase3_decisions import revision_details
    if not isinstance(author, str) or not author.strip():
        raise WorkflowError("必须提供实际作者执行实例")
    if not isinstance(data, dict):
        raise WorkflowError("产出必须是 JSON 对象")
    phase2_gate(root, "plan")
    key = key_for(data.get("task_id"), data.get("kind"))
    task_data = task(root, data["task_id"])
    from phase3_tasks import execution_hold
    if execution_hold(task_data):
        raise WorkflowError(execution_hold(task_data))
    dependencies = list(dependencies)
    if any(not isinstance(d, dict) for d in dependencies):
        raise WorkflowError("上游须为版本引用对象列表")
    if any(d.get("space") == "phase2" for d in dependencies):
        raise WorkflowError("计划引用由脚本绑定，不从旧稿复制")
    dependencies.insert(0, plan_ref(root))
    if data["kind"] == "evidence":
        entries = data.get("entries")
        if not isinstance(entries, list) or not entries or any(not isinstance(e, dict) for e in entries):
            raise WorkflowError("证据稿须为非空的证据对象列表")
        for item in entries:
            if not isinstance(item.get("source"), dict):
                raise WorkflowError("每条证据须绑定来源版本对象")
            if item.get("source") not in dependencies:
                dependencies.append(item["source"])
    if len({(d["space"], d["key"]) for d in dependencies}) != len(dependencies):
        raise WorkflowError("同一上游不能同时绑定多个版本")
    check_chain(root, key, data, dependencies)
    validate(root, data, pool(root, dependencies))
    history = registry(root)["artifacts"].get(key, [])
    previous = history[-1] if history else None
    normalized = {k: v for k, v in data.items() if k != "revision"}
    content_hash = fingerprint(normalized)
    if previous:
        check_pointers(root, previous)
        if previous["content_hash"] == content_hash and previous["dependencies"] == dependencies:
            return current(root, key)
    detail = revision_details(root, key, previous, normalized, revision)
    if detail:
        normalized["revision"] = detail
    folder = local(root, paths(data["task_id"], data["kind"], 1)["snapshot_path"]).parent
    used = [int(p.stem[1:]) for p in folder.glob("v*") if re.fullmatch(r"v\d+", p.stem)]
    version = max([m["version"] for m in history] + used + [0]) + 1
    markdown = render(normalized, context_text(task_data, dependencies))
    meta = {"key": key, "task_id": data["task_id"], "kind": data["kind"],
            "version": version, "author_instance": author, "created_at": now(),
            "dependencies": dependencies, "sha256": digest(json_bytes(normalized)),
            "source_files": detail["source_files"] if detail else [],
            "markdown_sha256": digest(markdown.encode("utf-8")), "content_hash": content_hash,
            "revision": detail, **paths(data["task_id"], data["kind"], version)}
    return write_transaction(root, meta, normalized, markdown)
