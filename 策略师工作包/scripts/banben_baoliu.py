#!/usr/bin/env python3
"""版本保留上限（W02）：每份成果的成品与截图只留最新 2 版，更早的自动移到“<项目目录名>-归档/”。

覆盖：独立任务（含提案阶段任务）的交付物与该版检核截图；正式流程 Phase 5 的逐字稿与 HTML 演示稿历版快照
（project/outputs/proposal/... /versions/ 下的文件）与该版浏览器检查截图目录（project/reviews/browser/<run>/）。
“一版” = 独立任务一次登记里交付物（路径+指纹）的组合（交付物不变的登记不算新版）；正式成果 = 登记表里的一个版本号。
新版本登记成功后自动执行，复用 guidang 的移动、指纹、记录与“已归档”核对，依据记为 W02 保留规则（用户 2026-10-06 确认），
不逐次询问、事后报告。例外不移：已发客户的版本（任何任务的发客户记录或客户决定里出现该版文件指纹）、当前有效检核基线
绑定的版本（最近一份通过的检核，以及最近一份带完整基线的检核）、正在使用的文件（guidang._current）。v1.7.2：旧版与最新 2 版
或例外版本共用的文件（同路径或同指纹）不移（F02）；dry-run 与执行共用同一套逐文件过滤（F03）；受保护的文字记录路径不算候选（F04）。一个版本的文件先
整体预检再移动；只移不删；文字记录（任务记录、指纹、检核报告等）全部留在项目里。每次执行的结果（移出、跳过及原因、
非预期错误）都写入 project/records/archive.jsonl（event=retention），resume 报最近一次。移出失败不影响新版本登记。"""
import argparse
import json
import traceback
import uuid
from pathlib import Path

from phase2_store import WorkflowError, now

KEEP = 2
ACTOR = "W02自动保留"
BASIS = "W02 保留规则"
WORDS = "W02 保留规则：成品与截图只留最新 2 版（用户 2026-10-06 确认原话：“自动移出，事后告诉你 (推荐)”）"
PASS = {"passed", "passed_with_yellow"}
LOG = "project/records/archive.jsonl"


# ---------- 版本与例外 ----------

def versions(events, task_id):
    out = []
    for event in events:
        if event["task_id"] != task_id:
            continue
        files = tuple(sorted((d["path"], d["sha256"]) for d in event["task"]["deliverables"] if "path" in d))
        if not files:
            continue
        if out and out[-1]["files"] == files:
            out[-1]["revisions"].append(event["revision"]); continue
        out.append({"number": len(out) + 1, "files": files, "revisions": [event["revision"]]})
    return out


def _delivered_fingerprints(root):
    """所有任务的发客户记录（jiaofu_guankou deliver）与正式流程交付/客户决定事件里出现的文件指纹。"""
    found = set()
    try:
        from jiaofu_guankou import events as gate_events
        for e in gate_events(root):
            if e.get("event") == "delivered":
                for item in e.get("deliverables", []):
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        found.add(item[1])
                for item in e.get("external_files", []) or []:
                    if isinstance(item, dict) and item.get("sha256"):
                        found.add(item["sha256"])
        from phase6_events import events as phase6
        for e in phase6(root, "customer_decision") + phase6(root, "task_delivery"):
            found.add(json.dumps(e, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, WorkflowError) as exc:
        raise WorkflowError(f"发客户记录无法读取（{exc}），为免移走已发版本，本次不自动移出") from None
    return found


def _is_delivered(digests, delivered):
    return any(d in delivered or any(isinstance(x, str) and len(x) > 64 and d in x for x in delivered) for d in digests)


def _reviews(root, task_id):
    from phase6_events import events
    return [r for r in events(root, "standalone_review") if r.get("target", {}).get("key") == task_id]


def exemptions(root, task_id, items):
    """返回 {版本号: 原因}。"""
    reasons = {}
    by_revision = {rev: v["number"] for v in items for rev in v["revisions"]}
    delivered = _delivered_fingerprints(root)
    for v in items:
        if _is_delivered([d for _, d in v["files"]], delivered):
            reasons[v["number"]] = "已发客户"
    reviews = _reviews(root, task_id)
    passed = [r for r in reviews if r.get("status") in PASS]
    if passed and passed[-1]["target"].get("version") in by_revision:
        reasons.setdefault(by_revision[passed[-1]["target"]["version"]], "当前检核基线")
    based = [r for r in reviews if r.get("baseline")]
    if based and based[-1]["target"].get("version") in by_revision:
        reasons.setdefault(by_revision[based[-1]["target"]["version"]], "当前检核基线")
    return reasons


def _screenshots(root, task_id, revisions):
    """该版的检核截图：默认 r<修订>/截图/ 目录，加上该版检核报告 visual_evidence 实际引用的截图文件。"""
    from phase2_store import local, read_json
    out = [f"project/reviews/{task_id}/r{rev}/截图" for rev in revisions]
    for record in _reviews(root, task_id):
        if record.get("target", {}).get("version") not in revisions or not isinstance(record.get("evidence"), dict):
            continue
        try:
            report = read_json(local(root, record["evidence"]["path"]))
        except (OSError, ValueError, WorkflowError):
            continue
        for entry in report.get("visual_evidence", []) if isinstance(report.get("visual_evidence"), list) else []:
            for page in entry.get("pages", []) if isinstance(entry, dict) and isinstance(entry.get("pages"), list) else []:
                ref = page.get("file") if isinstance(page, dict) else None
                if isinstance(ref, dict) and isinstance(ref.get("path"), str) and ref["path"].startswith("project/reviews/"):
                    if not any(ref["path"].startswith(d + "/") for d in out):
                        out.append(ref["path"])
    return out


def _present(root, rel):
    from phase2_store import local
    try:
        path = local(root, rel)
    except WorkflowError:
        return False
    return path.is_file() or (path.is_dir() and any(p.is_file() for p in path.rglob("*")))


def _summary(found, prefix):
    if not found["candidates"]:
        return f"{prefix}：共 {found['versions']} 版，没有需要移出的旧版本" + (f"（例外：{found['exempt']}）" if found.get("exempt") else "")
    return f"{prefix}：将移出第 {'、'.join(str(c['number']) for c in found['candidates'])} 版（保留最新 {KEEP} 版与例外）"


def _protected(rel):
    """v1.7.2 F04：文字记录（guidang.PROTECTED：记录、运行日志、状态文件、登记请求）按 W02 规定全部留在项目里，不算任何一版要移的文件。"""
    from guidang import PROTECTED, _fold
    folded = _fold(rel)
    return any(folded == _fold(p) or folded.startswith(_fold(p) + "/") for p in PROTECTED)


def _keep_set(pairs):
    """v1.7.2 F02：保留集合 = 最新 2 版 ∪ 全部例外版本的文件，路径与指纹都算（同路径或同内容的旧版文件都不移）。"""
    from guidang import _fold
    return {_fold(p) for p, _ in pairs}, {d for _, d in pairs if d}


def _kept(rel, digest, keep):
    from guidang import _fold
    return _fold(rel) in keep[0] or (digest is not None and digest in keep[1])


def plan(root, task_id):
    from standalone_store import history
    events, _ = history(root)
    items = versions(events, task_id)
    if len(items) <= KEEP:
        out = {"scope": "standalone", "task_id": task_id, "versions": len(items), "candidates": [], "kept": [v["number"] for v in items], "exempt": {},
               "kept_protected": 0}
        return {**out, "summary": _summary(out, task_id)}
    exempt = exemptions(root, task_id, items)
    keep = _keep_set([f for v in items if v in items[-KEEP:] or v["number"] in exempt for f in v["files"]])
    candidates, protected = [], set()
    for v in items[:-KEEP]:
        if v["number"] in exempt:
            continue
        protected.update(p for p, _ in v["files"] if _protected(p))
        files = [p for p, d in v["files"] if not _kept(p, d, keep) and not _protected(p)]
        shots = [rel for rel in _screenshots(root, task_id, v["revisions"]) if not _protected(rel)]
        if not any(_present(root, rel) for rel in files + shots):
            continue  # 已移走（或已缺失），dry-run 与执行都不再列出
        candidates.append({"number": v["number"], "revisions": v["revisions"], "files": files, "screenshots": shots,
                           "fingerprints": {p: d for p, d in v["files"]}})
    out = {"scope": "standalone", "task_id": task_id, "versions": len(items), "candidates": candidates,
           "exempt": {str(k): r for k, r in sorted(exempt.items())}, "kept": [v["number"] for v in items[-KEEP:]],
           "kept_protected": len(protected)}
    return {**out, "summary": _summary(out, task_id)}


def formal_plan(root, key):
    """正式流程 Phase 5（逐字稿、HTML 演示稿）：登记表里的历版快照文件与该版浏览器检查截图目录。"""
    from phase5_common import registry, events, ref as target_ref
    history = registry(root).get("artifacts", {}).get(key, [])
    if len(history) <= KEEP:
        out = {"scope": "phase5", "key": key, "versions": len(history), "candidates": [], "kept": [m["version"] for m in history], "exempt": {},
               "kept_protected": 0}
        return {**out, "summary": _summary(out, key)}
    delivered = _delivered_fingerprints(root)
    reviews = [r for r in events(root, "review") if r.get("target", {}).get("key") == key]
    passed = [r for r in reviews if r.get("status") in PASS]
    based = [r for r in reviews if r.get("baseline")]
    base_versions = {x[-1]["target"].get("version") for x in (passed, based) if x}
    exempt, candidates, protected = {}, [], set()
    for meta in history[:-KEEP]:
        digests = [f["sha256"] for f in meta.get("snapshot_files", [])] + [meta.get("sha256")]
        if _is_delivered(digests, delivered):
            exempt[meta["version"]] = "已发客户"
        elif meta["version"] in base_versions:
            exempt[meta["version"]] = "当前检核基线"
    # F02：最新 2 版与例外版本的快照及当前文件（路径与指纹）都保留
    keep = _keep_set([(f["path"], f.get("sha256")) for m in history if m in history[-KEEP:] or m["version"] in exempt
                      for f in m.get("snapshot_files", []) + m.get("current_files", []) if isinstance(f, dict) and f.get("path")])
    for meta in history[:-KEEP]:
        if meta["version"] in exempt:
            continue
        protected.update(f["path"] for f in meta.get("snapshot_files", []) if _protected(f["path"]))
        files = [f["path"] for f in meta.get("snapshot_files", []) if not _kept(f["path"], f.get("sha256"), keep) and not _protected(f["path"])]
        shots = []
        for record in reviews:
            if record.get("target") == target_ref(meta):
                try:
                    from phase5_common import local, read_json
                    report = read_json(local(root, record["evidence"]["path"]))
                    run = (report.get("browser_checks") or {}).get("evidence", {}).get("path", "")
                except (OSError, ValueError, KeyError, TypeError, WorkflowError, AttributeError):
                    continue
                folder = str(Path(run).parent.as_posix())
                if folder.startswith("project/reviews/browser/") and folder not in shots and not _protected(folder):
                    shots.append(folder)
        if not any(_present(root, rel) for rel in files + shots):
            continue
        candidates.append({"number": meta["version"], "revisions": [meta["version"]], "files": files, "screenshots": shots,
                           "fingerprints": {f["path"]: f["sha256"] for f in meta.get("snapshot_files", [])}})
    out = {"scope": "phase5", "key": key, "versions": len(history), "candidates": candidates,
           "exempt": {str(k): r for k, r in sorted(exempt.items())}, "kept": [m["version"] for m in history[-KEEP:]],
           "kept_protected": len(protected)}
    return {**out, "summary": _summary(out, key)}


# ---------- 执行与报告 ----------

def _log(root, row):
    """把本次保留结果（含跳过原因与非预期错误）写入归档记录；写不进去不影响登记。"""
    from phase2_store import local
    try:
        path = local(root, LOG); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"schema_version": 1, "event": "retention", "created_at": now(), **row}, ensure_ascii=False) + "\n")
    except (OSError, ValueError, WorkflowError):
        pass


def _moved_in_batch(root, batch, number):
    """中途失败时，按 archive.jsonl 本批次记录如实列出该版已移走的文件；number 为 None 时按版本分组列出本批次全部。"""
    path = Path(root) / LOG
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("event") == "archived" and row.get("batch") == batch and (number is None or row.get("version") == number):
                rows.append((row.get("version"), row["path"]))
    if number is not None:
        return [path for _, path in rows]
    grouped = {}
    for version, path in rows:
        grouped.setdefault(version, []).append(path)
    return grouped


def retain(root, task_id=None, *, key=None):
    """登记新版本后调用；任何失败只报告并记录，不抛出（不影响登记）。"""
    root = Path(root).resolve()
    owner = {"key": key} if key else {"task_id": task_id}
    batch = uuid.uuid4().hex[:12]
    try:
        result = _retain(root, task_id, key, batch)
    except Exception as exc:  # noqa: BLE001 —— 归档失败不得影响已成功的登记；非预期错误记入运行日志与归档记录
        expected = isinstance(exc, (WorkflowError, OSError, ValueError))
        try:
            from yunxing_rizhi import note
            note("command", "error", status="error", reason_code="unknown")
        except Exception:  # noqa: BLE001
            pass
        detail = f"{type(exc).__name__}: {exc}"
        # r3：中途出错时按 archive.jsonl 本批次记录如实列出已移走的文件（可能已有整版或部分文件移走）
        done = _moved_in_batch(root, batch, None)
        partial = [{"version": v, "moved_files": files, "reason": detail} for v, files in sorted(done.items())]
        summary = (f"本次自动移出中途出错：{exc}；" + "；".join(f"第 {p['version']} 版已移 {len(p['moved_files'])} 个文件" for p in partial)
                   + f"，按 {LOG} 本批次（{batch}）可搬回") if partial else f"本次自动移出未执行：{exc}"
        result = {"status": "partial" if partial else "skipped", **owner, "batch": batch if partial else None, "reason": summary,
                  "moved_versions": [], "partial": partial, "bytes": 0, "skipped": [], "summary": summary,
                  "error": None if expected else detail}
        _log(root, {**owner, "status": result["status"], "batch": result["batch"], "partial": partial, "summary": summary,
                    **({"error": detail, "trace": traceback.format_exc()[-1500:]} if not expected else {})})
    return result


def _decide(root, found, rehearse=False):
    """v1.7.2 F03：候选 → 逐文件过滤 → 每版最终会移的文件与跳过原因；dry-run 与执行共用。
    过滤规则：正在使用（guidang._current）、符号链接、已不在工作区、内容已不是该版、截图目录内有正在使用的文件。
    rehearse=True（dry-run）时再用 guidang.preflight 预演整批核对（与 archive 移动前同一套），报错原文即实际执行会报的原因。"""
    from guidang import _current, _fold, preflight
    from phase2_store import local, sha
    busy = {_fold(p) for p in _current(root)}
    decisions = []
    for item in found["candidates"]:
        paths, notes = [], []
        for rel in item["files"]:
            path = local(root, rel)  # local 已解析符号链接，判断链接要看未解析的原路径
            if _fold(rel) in busy:
                notes.append(f"{rel} 正在使用，不移"); continue
            if _linked(root, rel):
                notes.append(f"{rel} 是符号链接，不移"); continue
            if not path.is_file():
                notes.append(f"{rel} 已不在工作区（手动归档过或缺失），未移"); continue
            if sha(path) != item["fingerprints"][rel]:
                notes.append(f"{rel} 内容已不是第 {item['number']} 版，不移"); continue
            paths.append(rel)
        for rel in item["screenshots"]:
            path = local(root, rel)
            if _linked(root, rel):
                notes.append(f"{rel} 是符号链接，不移"); continue
            if path.is_file():
                if _fold(rel) in busy:
                    notes.append(f"{rel} 正在使用，不移"); continue
                paths.append(rel); continue
            if path.is_dir() and any(p.is_file() for p in path.rglob("*")):
                inside = [p.relative_to(root).as_posix() for p in path.rglob("*") if p.is_file()]
                if any(_fold(p) in busy for p in inside):
                    notes.append(f"{rel} 有正在使用的文件，不移"); continue
                if not any(rel.startswith(d.rstrip("/") + "/") for d in paths):
                    paths = [p for p in paths if not p.startswith(rel + "/")] + [rel]
        error = None
        if rehearse and paths:
            try:
                preflight(root, paths)
            except (WorkflowError, OSError, ValueError) as exc:
                error = str(exc)
        decisions.append({"number": item["number"], "paths": paths, "notes": notes, "error": error})
    return decisions


def _linked(root, rel):
    """路径上任何一级是符号链接（与 guidang 拒绝的口径一致）。"""
    current = Path(root)
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _report(found, label, moved, files, partial, skipped, total, root, prefix=""):
    mb = round(total / 1024 ** 2, 2)
    parts = []
    if moved:
        parts.append(f"{prefix}本次自动移出 {len(moved)} 个旧版本（{label} 第 {'、'.join(map(str, moved))} 版），共 {mb} MB，"
                     f"移到 {root.name}-归档/，按 {LOG} 可搬回")
    if partial:
        parts.append("部分移出：" + "；".join(f"第 {p['version']} 版已移 {len(p['moved_files'])} 个文件后中断（{p['reason']}）" for p in partial))
    if skipped:
        parts.append("跳过：" + "；".join(f"第 {s['version']} 版（{s['reason']}）" for s in skipped))
    line = "；".join(parts) or f"{prefix}本次没有移出旧版本"
    status = "archived" if moved and not partial else "partial" if partial else "skipped" if skipped else "nothing"
    return {"status": status, "moved_versions": moved, "files": files, "partial": partial, "bytes": total, "mb": mb,
            "kept_versions": found["kept"], "exempt": found["exempt"], "kept_protected": found.get("kept_protected", 0),
            "skipped": skipped, "summary": line}


def preview(root, task_id=None, key=None):
    """dry-run：与执行同一套候选与逐文件过滤，并预演整批核对；输出将移出的版本、每版文件清单与跳过原因（执行时的 I/O 错误除外）。"""
    root = Path(root).resolve()
    found = formal_plan(root, key) if key else plan(root, task_id)
    owner = {"key": key} if key else {"task_id": task_id}
    moved, files, skipped, total = [], {}, [], 0
    for d in _decide(root, found, rehearse=True):
        if not d["paths"]:
            if d["notes"]:
                skipped.append({"version": d["number"], "reason": "；".join(d["notes"])})
            continue
        if d["error"]:
            skipped.append({"version": d["number"], "reason": d["error"] + "（整版未移动）"}); continue
        moved.append(d["number"]); files[str(d["number"])] = d["paths"]
        total += sum(_size(root, rel) for rel in d["paths"])
        if d["notes"]:
            skipped.append({"version": d["number"], "reason": "；".join(d["notes"])})
    out = _report(found, key or task_id, moved, files, [], skipped, total, root, prefix="dry-run：")
    return {"dry_run": True, **owner, "scope": found["scope"], "versions": found["versions"], "candidates": found["candidates"], **out,
            "status": "dry_run", "would_status": out["status"]}


def _size(root, rel):
    path = Path(root) / rel
    return path.stat().st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _retain(root, task_id, key, batch):
    from guidang import archive
    found = formal_plan(root, key) if key else plan(root, task_id)
    owner = {"key": key} if key else {"task_id": task_id}
    label = key or task_id
    if not found["candidates"]:
        return {"status": "nothing", **owner, "versions": found["versions"], "moved_versions": [], "files": {}, "bytes": 0,
                "exempt": found["exempt"], "kept_protected": found.get("kept_protected", 0), "skipped": [], "summary": found["summary"]}
    moved, files, skipped, partial, total = [], {}, [], [], 0
    for d in _decide(root, found):
        paths, notes = d["paths"], d["notes"]
        if not paths:
            if notes:
                skipped.append({"version": d["number"], "reason": "；".join(notes)})
            continue
        try:
            result = archive(root, paths, WORDS, ACTOR, basis=BASIS, extra={"batch": batch, **owner, "version": d["number"]})
        except (WorkflowError, OSError, ValueError) as exc:
            done = _moved_in_batch(root, batch, d["number"])
            if done:
                partial.append({"version": d["number"], "moved_files": done, "reason": str(exc)})
            skipped.append({"version": d["number"], "reason": str(exc) + (f"（已移走 {len(done)} 个文件，见 partial）" if done else "（整版未移动）")})
            continue
        moved.append(d["number"]); files[str(d["number"])] = paths; total += result["bytes"]
        if notes:
            skipped.append({"version": d["number"], "reason": "；".join(notes)})
    rep = _report(found, label, moved, files, partial, skipped, total, root)
    out = {**rep, "batch": batch if (moved or partial) else None, **owner}
    _log(root, {**owner, "status": out["status"], "batch": out["batch"], "moved_versions": moved, "partial": partial,
                "skipped": skipped, "bytes": total, "summary": out["summary"]})
    return out


def last_batch(root):
    """resume 用：最近一次自动保留的结果（移出、部分移出或跳过及原因；只读 archive.jsonl）。"""
    path = Path(root) / LOG
    if not path.is_file():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") == "retention":
            rows.append(row)
    if not rows:
        return None
    last = rows[-1]
    archived = [r for r in path.read_text(encoding="utf-8").splitlines() if last.get("batch") and f'"batch": "{last["batch"]}"' in r and '"event": "archived"' in r]
    return {"batch": last.get("batch"), "created_at": last.get("created_at"), "status": last.get("status"),
            "task_id": last.get("task_id"), "key": last.get("key"), "moved_versions": last.get("moved_versions", []),
            "partial": last.get("partial", []), "skipped": last.get("skipped", []), "files": len(archived),
            "bytes": last.get("bytes", 0), "error": last.get("error"),
            "summary": _resume_line(root, last)}


def _resume_line(root, last):
    moved, label = last.get("moved_versions") or [], last.get("key") or last.get("task_id")
    if moved:
        line = (f"最近一次自动移出 {len(moved)} 个旧版本（{label} 第 {'、'.join(map(str, moved))} 版），共 {round(last.get('bytes', 0) / 1024 ** 2, 2)} MB，"
                f"在项目同级“{Path(root).resolve().name}-归档/”，按 {LOG} 可搬回")
        rest = [x for x in (last.get("summary") or "").split("；") if x.startswith(("部分移出", "跳过"))]
        return "；".join([line] + rest)
    return "最近一次自动保留：" + (last.get("summary") or "")


def main():
    parser = argparse.ArgumentParser(description="查看或手动触发版本保留（只留最新 2 版，其余移到归档）")
    parser.add_argument("--workspace", type=Path, required=True)
    owner = parser.add_mutually_exclusive_group(required=True)
    owner.add_argument("--task-id", help="独立任务")
    owner.add_argument("--key", help="正式流程 Phase 5 成果（如 T1::html-deck）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    if args.dry_run:
        out = preview(root, args.task_id, key=args.key)
    else:
        from workspace_lock import serialized
        with serialized(root):
            out = retain(root, args.task_id, key=args.key)
    print(json.dumps(out, ensure_ascii=False, indent=2))
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
