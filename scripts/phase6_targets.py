"""Read native registries; do not rewrite earlier phases or their review history."""
import phase2_store as p2
import phase3_store as p3
import phase5_common as p5
from phase6_events import valid_file

CONTROL = "project/records/control-artifacts.json"


def control_registry(root):
    path = p2.local(root, CONTROL)
    return p2.read_json(path) if path.exists() else {"schema_version": "1.0", "artifacts": {}}


def control_ref(meta):
    return {"space": "control", **{k: meta[k] for k in ("key", "version", "sha256")}}


def identity(target):
    if not isinstance(target, dict) or not isinstance(target.get("key"), str):
        raise p2.WorkflowError("产出引用须为完整版本对象")
    if target.get("space") not in {"phase2", "phase3", "phase5", "source", "html",
                                  "design-submission", "control", "standalone"}:
        raise p2.WorkflowError("未知产出空间")
    return f"{target['space']}/{target['key']}"


def catalog(root):
    from phase3_sources import catalog as source_catalog, source_ref
    from brand_house_guard import registry as html_registry
    from brand_house_store import target as html_ref
    result = {}

    def add(target, meta, dependencies, files, authors=()):
        result[identity(target)] = {"target": target, "meta": meta,
            "dependencies": dependencies, "files": files,
            "authors": list(authors), "task_id": meta.get("task_id")}

    for key, history in p2.registry(root)["artifacts"].items():
        meta = history[-1]
        add({"space": "phase2", "key": key, **p2.ref(meta)}, meta,
            [{"space": "phase2", "key": x["kind"], **x} for x in meta["dependencies"]],
            [{"path": meta[k], "sha256": meta["sha256"]} for k in ("path", "snapshot_path")] +
            meta["source_files"], [x["author_instance"] for x in history])
    for key, history in p3.registry(root)["artifacts"].items():
        meta = history[-1]
        files = [{"path": meta[k], "sha256": meta[h]} for k, h in (
            ("path", "sha256"), ("snapshot_path", "sha256"),
            ("markdown_path", "markdown_sha256"), ("markdown_snapshot", "markdown_sha256"))]
        add(p3.ref(meta), meta, meta["dependencies"], files + meta.get("source_files", []),
            [x["author_instance"] for x in history])
    for key, history in p5.registry(root)["artifacts"].items():
        meta = history[-1]
        add(p5.ref(meta), meta, meta["dependencies"], meta["current_files"] +
            meta["snapshot_files"] + meta.get("source_files", []),
            [x["author_instance"] for x in history])
    for key, history in source_catalog(root)["sources"].items():
        meta = history[-1]
        add(source_ref(meta), meta, [], [meta, meta["original"]])
    for key, meta in html_registry(root)["documents"].items():
        add(html_ref(meta), meta, [meta["synced_body"]],
            [{"path": meta["path"], "sha256": meta["observed_sha"]}],
            meta.get("author_instances", []))
    for key, meta in p5.submission_registry(root)["submissions"].items():
        add({"space": "design-submission", "key": key, "sha256": meta["sha256"]}, meta,
            [meta["brief_ref"]], [meta], [meta["author_instance"], meta["submitter_instance"]])
    for key, history in control_registry(root)["artifacts"].items():
        meta = history[-1]
        add(control_ref(meta), meta, meta["dependencies"],
            meta["files"] + meta["source_files"], [x["author_instance"] for x in history])
    from standalone_store import digest as standalone_digest, history as standalone_history
    standalone_events, _ = standalone_history(root)
    latest_standalone = {event["task_id"]: event for event in standalone_events}
    for task_id, event in latest_standalone.items():
        task = event["task"]
        if task["status"] == "archived":
            continue
        task_history = [item for item in standalone_events if item["task_id"] == task_id]
        signatures = []
        for item in task_history:
            value = item["task"]
            signature = standalone_digest({"sources": value["sources"],
                "deliverables": value["deliverables"],
                "completion_evidence": value["completion_evidence"]})
            if not signatures or signatures[-1] != signature:
                signatures.append(signature)
        target = {"space": "standalone", "key": task_id,
                  "version": len(signatures), "sha256": signatures[-1]}
        files = [item for item in task["sources"] + task["deliverables"] if "path" in item]
        authors = [item["actor"] for item in standalone_events if item["task_id"] == task_id]
        add(target, {**event, "key": task_id, "version": event["revision"],
                     "sha256": target["sha256"]}, [], files, authors)
    return result


def resolve(root, target, seen=()):
    marker = identity(target)
    if marker in seen:
        raise p2.WorkflowError("跨阶段依赖循环")
    item = catalog(root).get(marker)
    if not item or item["target"] != target:
        raise p2.WorkflowError(f"{marker} 版本已更新、缺失或引用不完整")
    space, key = target["space"], target["key"]
    if space == "phase2":
        return p2.current(root, key)
    if space == "phase3":
        return p3.current(root, key)
    if space == "phase5":
        return p5.current(root, key)
    if space == "source":
        from phase3_sources import source_current
        return source_current(root, key)
    if space == "html":
        from brand_house_store import live
        return live(root, key)[0]
    if space == "design-submission":
        from design_expression import _current
        return _current(root, key)[0]
    if space == "standalone":
        if any(not valid_file(root, f) for f in item["files"]):
            raise p2.WorkflowError("独立任务来源或交付文件改变/丢失，需登记新版")
        return item["meta"]
    if any(not valid_file(root, f) for f in item["files"]):
        raise p2.WorkflowError("总控文件或来源改变/丢失，需登记新版")
    for dep in item["dependencies"]:
        resolve(root, dep, seen + (marker,))
    return item["meta"]


def sources(root, target, seen=()):
    marker = identity(target)
    resolve(root, target, seen)
    item = catalog(root)[marker]
    files = list(item["files"])
    for dep in item["dependencies"]:
        files.extend(sources(root, dep, seen + (marker,)))
    return [{"path": p, "sha256": s} for p, s in sorted(
        {(f["path"], f["sha256"]) for f in files})]


def last_review(root, target):
    from phase3_events import events as events3
    from phase6_events import events
    space = target["space"]
    if space == "phase2":
        records = p2.events(root, "reviews")
        expected = {k: target[k] for k in ("kind", "version", "sha256")}
    elif space in {"phase3", "html"}:
        records = events3(root, "phase3-reviews" if space == "phase3" else "phase4-reviews")
        expected = target
    elif space in {"phase5", "design-submission"}:
        records = p5.events(root, "review" if space == "phase5" else "design_review")
        expected = target
    elif space == "standalone":
        records, expected = events(root, "standalone_review"), target
    else:
        records, expected = events(root, "control_review"), target
    matches = [x for x in records if x.get("target") == expected]
    if not matches:
        raise p2.WorkflowError("当前版本尚无独立检核")
    return matches[-1]


def review_stamp(root, target):
    value = last_review(root, target)
    return {"record_id": value["record_id"], "evidence": value["evidence"],
            "status": value["status"]}
