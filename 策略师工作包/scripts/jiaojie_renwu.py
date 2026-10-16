#!/usr/bin/env python3
"""任务交接打包（v1.7.8 J03，N4）：只交一个任务——把这个任务的记录、成品、来源、检核资料挑出来打成一个包；先给看（preview），回“好”才打包（pack）。

入口仍是 jiaojie_dabao.py（--kind task --task <任务编号>）；本模块放“挑什么”的逻辑，接手一侧在 jiaojie_jieshou.py。

这一版交什么：
  独立任务（project/records/standalone-tasks.jsonl 里的任务）。正式流程里的某一项连着整份合同和排期，对方没有同一版就接不上：
  一律只给白话说明并建议改用全量交接（用户 2026-10-16 拍板第 1 点，及“同意，照你的建議做”：不再提供“连同合同和排期一起交”）。
带什么（按记录里写到的路径带，不按目录猜）：
  任务记录全部版本、策略师原话、检核记录与报告原件、最近一份检核基线（及它引用的副本）、已做完的检核员分派记录与证据、发客户 / 交给策略师 / 对外版本这类“事实”、
  外部动作与外部文档快照、台账与回执中属于这个任务的行、成品、来源、对外导出、依赖的前序成果（只读参考副本，路径不变）、客户项目身份、项目记忆。
不带什么：
  “决定”类：豁免（不用检核直接发）、最终版、不用再查、已看到提醒、内部确认与客户确认、手改接纳——只放进“备查”，接手人要用须自己再说一次；
  过程文件、暂存、重复副本、没被这个任务记录用到的文件；没做完的分派（接手人的实例不同，带过去只会让他那边的检查报错）；更早的检核基线；
  凭据与登录状态类文件；本机的交出记录。
记录怎么放：每个记录文件里属于这个任务的行原样（逐字）放进 交接包/任务记录/<记录文件名>，决定类放进 交接包/备查/，任务清单在 交接包/任务交接.json。
清单与指纹沿用全量交接的结构（handoff_transfer.build_manifest + verify_package）。

给其他阶段复用的函数接口（N5 接手引导）：
  list_tasks(root) -> {"standalone": [...], "formal": [...], "unregistered": [...]}   只读
  collect(root, task_id) -> dict   挑出这个任务的记录行、文件（含成品页面用到的图片和样式）、依赖、身份、项目记忆；只读
  preview(root, task_id=None, include=()) -> dict   只读
  pack(root, task_id, words, actor, to=None, accept_problems=None, include=()) -> dict   回“好”之后才调用
  register_task(root, folder, title, words, actor) -> dict   把没登记的成果补登记成一个独立任务（策略师同意后才调用，原话记录）
"""
import hashlib
import json
import os
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from phase2_store import WorkflowError

TASK_MANIFEST = "交接包/任务交接.json"
EXCERPT_DIR = "交接包/任务记录"
KEEP_DIR = "交接包/备查"
TASK_FORMAT = 1
BASIS = "J03 任务交接（用户 2026-10-16 原话：“1 接受你的建議；2 根據你的建議”）"
RECORDS = "project/records/"
# 逐个记录文件的取法：(记录文件, 怎么认这个任务的行)
JOURNAL = "project/records/standalone-tasks.jsonl"
FACT_EVENTS = {"delivered", "handover", "external_version"}          # 发过什么、交过什么：事实，随包重放
DECISION_WORDS = {"waiver": "不检查直接发的书面说明", "final_version": "最终版标记", "recheck_dont": "不用再查", "reminder_ack": "已看到提醒",
                  "confirmation": "内部确认或客户确认", "adopted": "手改接纳"}
ACTIVE = {"prepared", "assigned", "returned_for_readback"}
STORE_JSON = {"agent-dispatch-index.json", "phase2-artifacts.json", "phase3-artifacts.json", "phase3-sources.json", "phase5-artifacts.json", "control-artifacts.json",
              "design-submissions.json", "phase4-html.json", "dependencies.json", "revision-counts.json", "import-template.json", "state.json"}
SKIP_RECORD_DIRS = {"handoffs", "handoff-imports", "交接说明"}
PATH_IN_TEXT = re.compile(r"project/[^\s，。；、,;：:\"'（()）【】<>]+")
OTHER_ROWS = ("off-package-actions.jsonl", "external-docs.jsonl", "task-receipts.jsonl", "fact-ledger.jsonl")     # 属于这个任务的行：事实，原样重放
KEEP_ONLY_ROWS = ("standalone-decisions.jsonl", "archive.jsonl")
MEMORY_ROLE = "项目记忆引用的资料"                                              # 只放备查


def _d():
    import jiaojie_dabao as module
    return module


def _human(size):
    return _d()._human(size)


def _clean(name):
    return _d()._clean(name)


# ---------------- 读记录 ----------------

def jsonl(root, rel):
    """[(原文行, 对象)]；读不出来就报错（不替人猜）。"""
    path = Path(root) / rel
    if not path.is_file():
        return []
    out = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise WorkflowError(f"{rel} 第 {number} 行读不出来，没法挑出这个任务的记录；先把这份记录恢复好再交接") from None
        if not isinstance(obj, dict):
            raise WorkflowError(f"{rel} 第 {number} 行不是一条记录；先把这份记录恢复好再交接")
        out.append((line, obj))
    return out


def _identity(root):
    return _d()._identity(root)


def _tasks_state(root):
    from standalone_store import history
    events = history(root)[0]
    latest = {}
    for e in events:
        latest[e["task_id"]] = e
    return events, latest


def list_tasks(root):
    """本项目的任务（独立任务 + 正式流程项，带状态），再加没登记成任务的大文件夹。只读。"""
    root = Path(root).resolve()
    events, latest = _tasks_state(root)
    delivered = {}
    for _, row in jsonl(root, "project/records/delivery-gate.jsonl"):
        if row.get("event") == "delivered" and row.get("task_id"):
            delivered.setdefault(row["task_id"], []).append(row.get("to") or "客户")
    reviews = {}
    for _, row in jsonl(root, "project/records/phase6-events.jsonl"):
        t = row.get("target") or {}
        if row.get("event") == "standalone_review" and t.get("space") == "standalone":
            reviews[t.get("key")] = reviews.get(t.get("key"), 0) + 1
    standalone = []
    for task_id, e in latest.items():
        task = e["task"]
        standalone.append({"task_id": task_id, "title": task["title"], "status": task["status"], "status_words": _d().STATUS_WORDS.get(task["status"], "状态待确认"),
                           "revision": e["revision"], "delivered_to": list(dict.fromkeys(delivered.get(task_id, []))), "reviews": reviews.get(task_id, 0)})
    formal, warnings = [], []
    plan = root / "project/tasks/task-plan.json"
    if plan.is_file():
        try:
            for item in json.loads(plan.read_text(encoding="utf-8")).get("tasks", []):
                if isinstance(item, dict):
                    tid = item.get("task_id") or item.get("id")
                    if tid:
                        status = item.get("status") or "not_started"
                        formal.append({"task_id": tid, "title": item.get("title") or item.get("name") or tid, "status": status,
                                       "status_words": _d().STATUS_WORDS.get(status, "状态待确认")})
        except (OSError, ValueError, AttributeError):
            warnings.append("正式流程的任务清单（project/tasks/task-plan.json）读不出来，所以下面看不到正式流程里的任务；先把它恢复好，别当成没有")
    guocheng = _d()._g()
    try:
        unregistered = [{"dir": d["dir"], "bytes": d["bytes"], "files": d["files"], "label": _clean(d["dir"].rsplit("/", 1)[-1]) or "一处名字是英文的文件夹"}
                        for d in guocheng.unregistered_dirs(root)]
    except Exception as exc:
        unregistered, warnings = [], warnings + [f"没挂在任何任务下的大文件夹没能检查（{str(exc)[:80]}），所以下面没有列它们"]
    return {"standalone": standalone, "formal": formal, "unregistered": unregistered, "warnings": warnings}


# ---------------- 挑出一个任务的东西 ----------------

def _store_file(rel):
    if not rel.startswith(RECORDS):
        return False
    sub = rel[len(RECORDS):]
    if "/" in sub:
        return sub.split("/")[0] in SKIP_RECORD_DIRS
    return sub.endswith(".jsonl") or sub.endswith(".head.json") or sub in STORE_JSON or sub.startswith(".")


def _walk_paths(value, out, structured=True):
    """JSON 里所有 project/ 开头的字符串；文字里提到的路径（只算文件）另记。"""
    if isinstance(value, str):
        if value.startswith("project/"):
            out["exact"].add(value)
        elif "project/" in value:
            out["mentioned"].update(PATH_IN_TEXT.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            _walk_paths(v, out, structured)
    elif isinstance(value, list):
        for v in value:
            _walk_paths(v, out, structured)


def _expand(root, rel, files_only=False):
    pp = PurePosixPath(rel)
    if pp.is_absolute() or ".." in pp.parts or "\\" in rel or not rel.startswith("project/") or rel.startswith("project/运行日志"):
        return []
    cur = Path(root)
    for part in pp.parts:
        cur = cur / part
        if cur.is_symlink():
            return []
    if cur.is_file():
        return [rel]
    if cur.is_dir() and not files_only:
        found = []
        for dp, dn, fn in os.walk(cur, followlinks=False):
            dn[:] = [d for d in dn if not os.path.islink(os.path.join(dp, d)) and d != "__pycache__"]
            for f in fn:
                p = Path(dp) / f
                if not p.is_symlink():
                    found.append(p.relative_to(root).as_posix())
        return found
    return []


def _refs_of_task(task):
    out = {"exact": set(), "mentioned": set()}
    for name in ("sources", "deliverables", "process_refs", "historical_refs", "decision_refs"):
        for ref in task.get(name, []) or []:
            if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                out["exact"].add(ref["path"])
    _walk_paths(task.get("artifact_versions", []), out)
    return out["exact"]


def collect(root, task_id):
    """挑出一个独立任务要带的东西（只读）。返回：
    events（补全后的任务各版）、lines（任务记录原文行）、rows（各记录文件里属于它的行，原样）、keep（决定类，只备查）、files（路径→角色）、
    dependencies、identity、memory_lines、unfinished_dispatches、baseline。"""
    from standalone_store import event_digest, history
    root = Path(root).resolve()
    events, _ = history(root)
    own = [e for e in events if e["task_id"] == task_id]
    if not own:
        raise WorkflowError(f"找不到任务「{task_id}」；先说“交接”看一下有哪些任务")
    lines = [(t, o) for t, o in jsonl(root, JOURNAL) if o.get("task_id") == task_id]
    if len(lines) != len(own):
        raise WorkflowError("任务记录的行数和读出来的版本数对不上，先核对任务记录再交接")
    rows, keep, files, stats, warnings = {}, {}, {}, {"phase6": 0}, []
    paths = {"exact": set(), "mentioned": set()}
    dirs_ok = set()
    # --- 任务本身引用的文件，按角色
    role_of = {}
    for e in own:
        for name, role in (("deliverables", "成品"), ("sources", "来源"), ("historical_refs", "来源"), ("process_refs", "记录里用到的其他文件"),
                           ("decision_refs", "记录里用到的其他文件")):
            for ref in e["task"].get(name, []) or []:
                if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                    cur = role_of.get(ref["path"])
                    if cur != "成品":
                        role_of[ref["path"]] = role
    for p in _refs_of_task({"artifact_versions": [a for e in own for a in e["task"].get("artifact_versions", [])]}):
        role_of.setdefault(p, "记录里用到的其他文件")
    # --- 检核记录（只带“独立检核”这一种；确认类是决定，只备查）
    p6 = []
    for text, row in jsonl(root, "project/records/phase6-events.jsonl"):
        target = row.get("target") or {}
        if target.get("space") != "standalone" or target.get("key") != task_id:
            continue
        (p6 if row.get("event") == "standalone_review" else keep.setdefault("phase6-events.jsonl", [])).append((text, row) if row.get("event") == "standalone_review" else text)
    rows["phase6-events.jsonl"] = [t for t, _ in p6]
    last_baseline = next((r["baseline"] for _, r in reversed(p6) if isinstance(r.get("baseline"), dict) and r["baseline"].get("path")), None)
    for _, row in p6:
        view = row if row.get("baseline") == last_baseline else {k: v for k, v in row.items() if k != "baseline"}
        _walk_paths(view, paths)
    baseline_blobs = []
    if last_baseline:
        try:
            data = json.loads((root / last_baseline["path"]).read_text(encoding="utf-8"))
            b = {"exact": set(), "mentioned": set()}
            _walk_paths(data, b)
            baseline_blobs = sorted(b["exact"])
            paths["exact"].update(b["exact"])
        except (OSError, ValueError):
            warnings.append(f"最近一份检核基线（{last_baseline['path']}）读不出来，所以它引用的原版副本没带；对方可能要重新检核")
    # --- 发客户等记录
    gate = []
    for text, row in jsonl(root, "project/records/delivery-gate.jsonl"):
        if row.get("task_id") != task_id:
            continue
        if row.get("event") in FACT_EVENTS:
            gate.append(text); _walk_paths(row, paths)
        else:
            keep.setdefault("delivery-gate.jsonl", []).append(text)
    rows["delivery-gate.jsonl"] = gate
    # --- 分派（只带做完的；没做完的不带）
    disp_all = jsonl(root, "project/records/agent-dispatch.jsonl")
    ids = {o["dispatch_id"] for _, o in disp_all if o.get("task_id") == task_id and o.get("dispatch_id")}
    latest = {}
    for _, o in disp_all:
        if o.get("dispatch_id") in ids:
            latest.setdefault(o["dispatch_id"], {}).update(o)
    unfinished = sorted(i for i, o in latest.items() if o.get("status") in ACTIVE)
    done = ids - set(unfinished)
    rows["agent-dispatch.jsonl"] = [t for t, o in disp_all if o.get("dispatch_id") in done]
    keep_disp = [t for t, o in disp_all if o.get("dispatch_id") in unfinished]
    if keep_disp:
        keep["agent-dispatch.jsonl"] = keep_disp
    for t, o in disp_all:
        if o.get("dispatch_id") in done:
            _walk_paths(o, paths)
    # --- 其他事实行
    for name in OTHER_ROWS:
        picked = [(t, o) for t, o in jsonl(root, RECORDS + name) if o.get("task_id") == task_id]
        if picked:
            rows[name] = [t for t, _ in picked]
            for _, o in picked:
                _walk_paths(o, paths)
    for name in KEEP_ONLY_ROWS:
        picked = [t for t, o in jsonl(root, RECORDS + name) if o.get("task_id") == task_id]
        if picked:
            keep.setdefault(name, []).extend(picked)
    # --- 文件：任务记录用到的 + 记录里写到的 + 约定位置
    for p in role_of:
        paths["exact"].add(p)
    exact_dirs = set()
    for rel in sorted(paths["exact"]):
        found = _expand(root, rel)
        if found and (root / rel).is_dir():
            exact_dirs.add(rel)
        for f in found:
            files.setdefault(f, role_of.get(f) or ("检核与记录资料" if f.startswith(RECORDS) or f.startswith("project/reviews/") else "记录里用到的其他文件"))
    for rel in sorted(paths["mentioned"]):
        for f in _expand(root, rel, files_only=True):
            files.setdefault(f, "检核与记录资料" if f.startswith(("project/reviews/", RECORDS)) else "记录里用到的其他文件")
    for fixed, role in ((f"project/reviews/{task_id}", "检核与记录资料"), (f"project/exports/对外/{task_id}", "对外导出"), (f"project/records/策略师原话/{task_id}.jsonl", "策略师原话")):
        for f in _expand(root, fixed):
            files.setdefault(f, role)
    inputs = root / "project/inputs/登记请求"
    if inputs.is_dir():
        for p in sorted(inputs.iterdir()):
            if p.is_file() and not p.is_symlink() and p.name.startswith(task_id + "-"):
                files.setdefault(p.relative_to(root).as_posix(), "登记请求")
    for blob in baseline_blobs + ([last_baseline["path"]] if last_baseline else []):
        if blob.startswith(RECORDS + "review-baselines/"):
            for f in _expand(root, blob):
                files[f] = "检查时留下的原版副本"
    for f in list(files):
        if _store_file(f):
            del files[f]
    for target, page in _d()._g().page_closure(root, list(files)).items():                      # 成品页面用到的图片、样式、字体一起带（顺着样式表继续找）
        if target not in files and not _store_file(target) and not _d().LOGIN_RE.match(Path(target).name):
            files[target] = "成品页面引用的文件"
    from mingan_xinxi import credential_name
    login = sorted(f for f in files if _d().LOGIN_RE.match(Path(f).name))
    for f in login:
        del files[f]
    # --- 依赖的前序成果：来源里写的是别的任务的成品
    other_out = {}
    for e in events:
        if e["task_id"] != task_id:
            for ref in e["task"].get("deliverables", []) or []:
                if isinstance(ref, dict) and ref.get("path"):
                    other_out[ref["path"]] = (e["task_id"], ref.get("sha256"))
    mine_out = {r["path"] for e in own for r in e["task"].get("deliverables", []) or [] if isinstance(r, dict) and r.get("path")}
    deps = []
    for path, role in sorted(role_of.items()):
        if role == "来源" and path in other_out and path not in mine_out and path in files:
            deps.append({"path": path, "from_task": other_out[path][0], "sha256": other_out[path][1]})
            files[path] = "依赖的前序成果（只读参考）"
    memory_rows = jsonl(root, "project/records/project-memory.jsonl")
    memory = [t for t, _ in memory_rows]
    for _, row in memory_rows:                     # 项目记忆引用的资料（接收方是空白工作区、重放记忆时用得上；同一客户已有记忆时不放）
        for ref in row.get("source_refs") or []:
            if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                for f in _expand(root, ref["path"], files_only=True):
                    if not _store_file(f):
                        files.setdefault(f, MEMORY_ROLE)
    last_id = None
    ident_rows = jsonl(root, "project/records/project-identity.jsonl")
    if ident_rows:
        last_id = ident_rows[-1][1].get("identity")
    return {"task_id": task_id, "events": own, "lines": [t for t, _ in lines], "digests": [event_digest(e) for e in own], "rows": {k: v for k, v in rows.items() if v},
            "keep": keep, "files": files, "dependencies": deps, "identity": _identity(root), "identity_row": last_id, "memory_lines": memory,
            "unfinished_dispatches": unfinished, "baseline": last_baseline, "login_skipped": login, "warnings": warnings}


# ---------------- 预览 ----------------

def _decisions_words(keep):
    out = []
    for name, lines in sorted(keep.items()):
        if name in ("agent-dispatch.jsonl",):
            continue
        counts = {}
        for item in lines:
            row = json.loads(item) if isinstance(item, str) else item
            label = DECISION_WORDS.get(row.get("event"), "其他决定") if name in ("delivery-gate.jsonl", "phase6-events.jsonl") else \
                ("手改接纳" if name == "standalone-decisions.jsonl" else "旧版本整理记录")
            counts[label] = counts.get(label, 0) + 1
        out.extend((label, n) for label, n in sorted(counts.items()))
    return out


def _entries(root, c, result, include):
    """给要带的文件配上 plan() 同款的描述，好复用凭据扫描。"""
    cat = {e["path"]: e for e in result["files"]}
    entries, errors = [], []
    carry = dict(c["files"])
    for target in include:
        norm = _d()._g()._norm_include(target)
        hits = [e["path"] for e in result["files"] if _d()._under(e["path"], norm) and e["category"] in ("process", "temp", "duplicate", "suspect", "unsure", "unregistered_dir")]
        if not hits:
            errors.append(f"点名要带的「{norm}」在工作区里找不到，或者它本来就在包里 / 永远不带")
        for h in hits:
            carry.setdefault(h, "你点名多带的")
    for rel, role in sorted(carry.items()):
        path = Path(root) / rel
        try:
            info = path.stat()
        except OSError:
            continue
        group = "records" if rel.startswith(RECORDS) or rel.startswith("project/reviews/") else ("product" if role == "成品" else "source" if "来源" in role else "referenced")
        entries.append({"path": rel, "bytes": info.st_size, "mode": info.st_mode & 0o7777, "group": group, "carry": True, "role": role, "package_member": False})
    return entries, errors


def _not_carried(root, c, result, carried):
    """这个任务所在文件夹里没带的东西，按类别数一数。"""
    folders = result.get("tasks", {}).get(c["task_id"], [])
    labels = {"process": "过程文件", "temp": "暂存", "duplicate": "重复副本", "suspect": "看名字像过程的"}
    out = {}
    for e in result["files"]:
        if e["path"] in carried or e["category"] not in labels:
            continue
        if e.get("task_id") != c["task_id"] and not any(_d()._under(e["path"], f) for f in folders):
            continue
        slot = out.setdefault(labels[e["category"]], {"files": 0, "bytes": 0})
        slot["files"] += 1; slot["bytes"] += e["bytes"]
    return out


def _scan_blobs(root, c):
    """任务包里的记录摘录行（含只备查的决定类行、项目记忆）也要扫凭据：命中的算到它们来自的记录文件上（误报照 H02 登记放行）。返回 (命中, 已放行)。"""
    from mingan_xinxi import credential_json, credential_text
    D = _d()
    state, hits, released = {}, [], []
    groups = {**c["rows"], "standalone-tasks.jsonl": c["lines"], "project-memory.jsonl": c["memory_lines"]}
    for name, lines in c["keep"].items():
        groups[name] = list(groups.get(name, [])) + [x if isinstance(x, str) else json.dumps(x[1], ensure_ascii=False) for x in lines]
    for name, lines in sorted(groups.items()):
        bad = any(credential_text(x) or credential_json(x) for x in lines)
        if not bad:
            continue
        rel = RECORDS + name
        (released if D._released_by_false_alarm(root, rel, state) else hits).append(rel)
    return hits, released


def _formal_talk(task):
    return (f"「{task['title']}」是照合同和排期做的正式流程里的一项。它的每一版都挂在整份合同和排期的某一版上，对方手里没有同一版的话就接不上，所以不能像临时交办的任务那样单独拎出来交。"
            "我建议你改用“全量交接”，把整个工作区连着合同和排期一起交过去。要现在做全量交接吗？")


def preview(root, task_id=None, include=()):
    root = _d()._require_workspace(root)
    listing = list_tasks(root)
    if not task_id:
        return {"status": "choose_task", "kind": "task", "tasks": listing, "warnings": listing["warnings"],
                "for_strategist": _choose_talk(listing),
                "technical": "选好后：python3 scripts/jiaojie_dabao.py preview --workspace . --kind task --task <任务编号> --json；没登记的成果先问她要不要登记成任务，同意后："
                             "python3 scripts/jiaojie_dabao.py register-task --workspace . --dir <project/ 开头的文件夹> --title <任务名> --words \"<她的原话>\" --actor <实例>"}
    formal = next((t for t in listing["formal"] if t["task_id"] == task_id), None)
    if formal and not any(t["task_id"] == task_id for t in listing["standalone"]):
        return {"status": "formal_task", "kind": "task", "task": formal, "for_strategist": _formal_talk(formal),
                "technical": "正式流程项不单独打包（需连同 tasks/task-plan.json、outputs/versions、phase2/phase3/phase5 各记录一起，等于整个项目）；一律建议 --kind full（用户 2026-10-16 拍板）"}
    if not any(t["task_id"] == task_id for t in listing["standalone"]):
        return {"status": "unknown_task", "kind": "task", "tasks": listing,
                "for_strategist": "我找不到你说的这个任务。" + _choose_talk(listing),
                "technical": f"任务编号 {task_id} 不在 standalone-tasks.jsonl，也不在 task-plan.json"}
    c = collect(root, task_id)
    result = _d()._g().classify(root)
    entries, errors = _entries(root, c, result, include)
    hits, released = _d()._scan(root, entries)
    blob_hits, blob_released = _scan_blobs(root, c)
    hits, released = sorted(set(hits) | set(blob_hits)), sorted(set(released) | set(blob_released))
    problems = _d()._problems(root)
    cred_problems, problems_other = _d().split_problems(problems)
    hits = sorted(set(hits) | _d().credential_problem_files(cred_problems))
    carried = {e["path"] for e in entries}
    skip = _not_carried(root, c, result, carried)
    groups = {}
    for e in entries:
        slot = groups.setdefault(e["role"], {"files": 0, "bytes": 0})
        slot["files"] += 1; slot["bytes"] += e["bytes"]
    total = sum(e["bytes"] for e in entries)
    largest = [{"path": e["path"], "mb": _d()._mb(e["bytes"]), "role": e["role"]} for e in sorted(entries, key=lambda x: -x["bytes"])[:5]]
    task = c["events"][-1]["task"]
    info = next(t for t in listing["standalone"] if t["task_id"] == task_id)
    out = {"status": "preview", "kind": "task", "task": info, "carry": {"files": len(entries), "bytes": total, "groups": groups}, "largest_carried": largest,
           "not_carried": skip, "decisions_not_carried": [{"what": a, "count": n} for a, n in _decisions_words(c["keep"])],
           "records": {k: len(v) for k, v in c["rows"].items()}, "dependencies": c["dependencies"], "unfinished_dispatches": len(c["unfinished_dispatches"]),
           "memory_rows": len(c["memory_lines"]), "credential_hits": hits, "credential_released": released, "problems": problems,
           "problem_kinds": _d().problem_kinds(problems_other), "errors": errors, "warnings": c["warnings"],
           "include": {"requested": [x for x in include]}, "identity": c["identity"], "login_skipped": c["login_skipped"],
           "for_strategist": _task_talk(info, task, entries, groups, total, skip, c, hits, problems_other, errors),
           "technical": "打包：python3 scripts/jiaojie_dabao.py pack --workspace . --kind task --task " + task_id
                        + " --words \"<策略师当次回复的原话>\" --actor <实例> [--to <接手人>] [--include <project/ 开头的路径>]... [--accept-problems \"<同意照原样交的原话>\"]"}
    return out


def _choose_talk(listing):
    parts = []
    if listing["standalone"]:
        parts.append("这个项目里临时交办的任务有：" + "；".join(f"「{t['title']}」（{t['status_words']}" + (f"，已发给{'、'.join(t['delivered_to'])}" if t["delivered_to"] else "") + "）"
                                                   for t in listing["standalone"]) + "。")
    if listing["formal"]:
        parts.append("照合同和排期做的正式流程项有：" + "、".join(f"「{t['title']}」（{t['status_words']}）" for t in listing["formal"]) + "。正式流程项不能单独交，要交的话建议整个工作区一起交。")
    if not listing["standalone"] and not listing["formal"]:
        parts.append("这个项目里还没有登记过任何任务。")
    if listing["unregistered"]:
        names = "、".join(f"{d['label']}（约 {_human(d['bytes'])}）" for d in listing["unregistered"][:3])
        parts.append(f"另外有不属于任何任务的大文件夹：{names}。如果你要交的是它们，要不要先把它登记成一个任务再交？你同意我才登记。")
    for w in listing.get("warnings", []):
        parts.append("提醒：" + w + "。")
    parts.append("你想交哪一个？")
    return "".join(parts)


def _task_talk(info, task, entries, groups, total, skip, c, hits, problems, errors):
    parts = [f"只交「{info['title']}」这一个任务的话：会带走 {len(entries)} 个文件（{_human(total)}）。"]
    mid = [f"{name} {g['files']} 个（{_human(g['bytes'])}）" for name, g in sorted(groups.items(), key=lambda kv: -kv[1]["bytes"])]
    parts.append("其中有：" + "、".join(mid) + "。")
    if c["dependencies"]:
        parts.append(f"它用到了别的任务的成果 {len(c['dependencies'])} 份，会作为只读参考一起带上，对方只能看、不当成他自己的任务。")
    if skip:
        parts.append("这个任务文件夹里不带的有：" + "、".join(f"{k} {v['files']} 个（{_human(v['bytes'])}）" for k, v in skip.items()) + "。")
    dec = _decisions_words(c["keep"])
    if dec:
        parts.append("你之前说过的 " + "、".join(f"{a} {n} 条" for a, n in dec) + " 这类话不会带过去，只留一份备查；对方要用，得自己再说一次。")
    else:
        parts.append("你之前说过的“这版不用检查直接发”“这就是最终版”这类话，就算有也不会带过去，对方要用得自己再说一次。")
    if c["unfinished_dispatches"]:
        parts.append(f"还有 {len(c['unfinished_dispatches'])} 件派出去还没做完的检查任务，没有带。")
    parts.append("检核没问题的成品，字节不变的话对方不用重新检核；但对方发给客户前，门槛和你完全一样。")
    if hits:
        names = "、".join(_clean(Path(x).name) or "一份名字是英文的文件" for x in hits[:3])
        parts.append(f"注意：{names}里有像登录凭据的内容，没处理前我不会打包（你同意原样交也不行，凭据不能随包交出去）。如果那是误报，你确认后告诉我，我登记；也可以把里面的密码或密钥改掉。")
    if problems:
        parts.append(f"另外，工作区现在的检查有 {len(problems)} 个没解决的问题，大致是：{_d().kinds_words(problems)}；要先修，还是你同意原样交？原样交的话，请你当次明确说“同意照原样交”。")
    for w in c.get("warnings", []):
        parts.append("提醒：" + w + "。")
    for m in errors:
        parts.append(m + "。")
    parts.append("你回“好”，我就打包；想多带哪些文件夹也可以告诉我。原来的工作区一个文件都不会删，只多一份交接说明和一条交出记录。")
    return "".join(parts)


# ---------------- 打包 ----------------

def _task_note(root, c, entries, skip, actor, to, problems, accept_words, manifest_id, zip_name):
    D = _d()
    template = Path(root) / "templates/交接说明.md"
    try:
        text = template.read_text(encoding="utf-8")
    except OSError:
        text = D.NOTE_FALLBACK
    ident = c["identity"]
    task = c["events"][-1]["task"]
    name = ident.get("project_name") or D.root_name(root)
    what = (f"这是「{name}」" + (f"（客户：{ident['client_name']}）" if ident.get("client_name") else "") + f"里的一个任务「{task['title']}」，{D._date_words()}交出来了"
            + (f"，交给{to}" if to else "") + "。包里只有这个任务的资料和记录，不是整个工作区；你要先有一个装好同版本工作包的工作区（同一个客户的，或者全新的空白工作区），再把它接进去。")
    names = {c["task_id"]: task["title"]}
    versions = D.task_own_versions(task)
    summary = D.plain_piece(task.get("summary"), names, "", root, versions)
    where = f"任务状态：{D.STATUS_WORDS.get(task['status'], '状态待确认')}；已经改到第 {c['events'][-1]['revision']} 版。" + (f"摘要：{summary}" if summary else "")
    nxt = D.plain_piece(task.get("next_action"), names, "", root, versions) or "接手后先看一遍成品和检核记录，再决定下一步。"
    delivered = [json.loads(t) for t in c["rows"].get("delivery-gate.jsonl", [])]
    notes = []
    for row in delivered:
        if row.get("event") == "delivered":
            notes.append(f"这个任务的第 {row.get('version', '?')} 版已经发给过{row.get('to') or '客户'}（接着改之前先看清哪一版发出去了）")
    notes.append("发给客户之前的门槛对你和原来的人完全一样：成品没变、原来的独立检核仍然有效就能直接用；原来的人说过的“不用检查”“最终版”不会带过来。")
    if c["dependencies"]:
        notes.append("它用到了别的任务的成果，作为只读参考一起带来了，只能看；那些任务本身不在你这里")
    if problems:
        notes.append(f"交出时检查还有 {len(problems)} 个没解决的问题，是交出的人同意原样交的；具体见技术附录")
    missing = [f"{k} {v['files']} 个（{D._human(v['bytes'])}）" for k, v in skip.items()]
    dec = _decisions_words(c["keep"])
    for a, n in dec:
        missing.append(f"原来的人说过的{a} {n} 条（只留备查，没有生效；你要用得自己再说一次）")
    if c["unfinished_dispatches"]:
        missing.append(f"没做完的检查任务 {len(c['unfinished_dispatches'])} 件")
    missing.append("项目里其他任务和其余资料；工作区外的备份和归档")
    missing_text = D._bullets(missing, "") + "\n\n要这些的话，找交出的人要。"
    waiting = []
    accounts = "飞书、云端大脑、产品线网站这类账号都没有随包带，也不应该带。需要用的话，用你自己的账号连一次；比如说“帮我连产品线网站”，我会一步一步带你做。"
    appendix = "\n".join([
        f"- 交接类型：task；任务编号：{c['task_id']}（{len(c['events'])} 个版本）；包名：{zip_name}；交出实例（actor）：{actor}" + (f"；交给：{to}" if to else ""),
        f"- 工作包版本：{D._package_version(root)}；客户项目身份：{json.dumps(ident, ensure_ascii=False)}",
        f"- 清单编号 manifest_id：{manifest_id}；任务清单 {TASK_MANIFEST}，记录行摘录在 {EXCERPT_DIR}/，没带的决定类行在 {KEEP_DIR}/（只备查）。",
        f"- 接手：python3 scripts/jiaojie_jieshou.py check --workspace . --package <压缩包> --json（只读）；确认后 apply --words \"<原话>\" --actor <实例>。",
        "- 没带的类别与个数：" + (json.dumps({k: v['files'] for k, v in skip.items()}, ensure_ascii=False) if skip else "无") + "；决定类：" + (json.dumps(dec, ensure_ascii=False) if dec else "无"),
        "- 交出时仍有的检查问题：" + (json.dumps(problems, ensure_ascii=False)[:1500] if problems else "无") + (f"；交出的人当次原话：{accept_words}" if problems else ""),
        f"- 依赖的前序成果（只读参考）：{json.dumps(c['dependencies'], ensure_ascii=False) if c['dependencies'] else '无'}",
    ])
    for key, value in (("what", what), ("where", where), ("next", nxt), ("waiting", D._bullets(waiting, "暂时没有人在等拍板。")), ("notes", D._bullets(notes, "暂时没有特别要提醒的。")),
                       ("missing", missing_text), ("accounts", accounts), ("appendix", appendix)):
        text = text.replace("{{" + key + "}}", value)
    return text


def forward_text(project_name, title, size):
    name = _clean(project_name) or "这个项目"
    return (f"我把{name}里的一个任务{_clean(title) or title}打成了压缩包发给你（{_human(size)}），里面有这个任务的成品、来源和检核记录，另有一份“交接说明”写了做到哪了、下一步是什么。\n"
            "接手的做法：在你自己的「Codex」或「Claude」（两款 AI 助手软件）里，先打开你要放这个任务的工作区文件夹（同一个客户的工作区，或者一个装好同版本工作包的空白工作区），"
            "把压缩包拖进对话，对它说“接手这个交接包”。它会先检查压缩包有没有损坏、客户对不对，再一步一步带你。\n"
            "请不要自己解压：压缩包里的文件要由 AI 一步一步放进去，自己解压到工作区会盖掉你那边已有的记录。\n"
            "飞书、云端大脑、产品线网站这类账号没有放在包里，需要用的话用你自己的账号连一次。\n" + _d().CAUTION)


def pack(root, task_id, words, actor, to=None, accept_problems=None, include=()):
    D = _d()
    from registration_guard import actor_check
    from workspace_lock import serialized
    actor_check(actor)
    if not isinstance(words, str) or not words.strip():
        raise WorkflowError("打包须有策略师当次回复的原话（--words，比如她回的“好”），没有就不打包")
    root = D._require_workspace(root)
    if not task_id:
        return preview(root)
    listing = list_tasks(root)
    if not any(t["task_id"] == task_id for t in listing["standalone"]):
        return preview(root, task_id)         # 正式流程项 / 找不到：给说明，不打包
    with serialized(root):
        c = collect(root, task_id)
        result = D._g().classify(root)
        entries, errors = _entries(root, c, result, include)
        if errors:
            raise WorkflowError("；".join(errors))
        hits, _ = D._scan(root, entries)
        blob_hits, _ = _scan_blobs(root, c)
        hits = sorted(set(hits) | set(blob_hits))
        if hits:
            names = "、".join(Path(x).name for x in hits[:5])
            return {"status": "blocked_credentials", "credential_hits": hits,
                    "for_strategist": D.credential_talk(hits, names),
                    "technical": "命中的文件（不回显内容）：" + "、".join(hits) + "；确认是误报：python3 scripts/wubao_dengji.py register --workspace . --path <路径> --field （按文本检查） --words \"<原话>\" --actor <实例>"}
        from handoff_inspection import inspect_workspace
        from handoff_inventory import inventory_digest, sha256_bytes
        from handoff_records import now as record_now
        from handoff_transfer import build_manifest
        from workspace_resume import resume
        report = inspect_workspace(root)
        if report.get("storage_blocked"):
            raise WorkflowError("交接存储目录或文件有问题（" + "；".join(report.get("errors", []))[:200] + "），先恢复再交接")
        resume_state = resume(root)
        problems = sorted(set(report.get("errors", [])) | set(resume_state.get("errors", [])))
        blocked = D.credential_block(problems)
        if blocked:
            return blocked
        if problems and not (isinstance(accept_problems, str) and accept_problems.strip()):
            return D.consent_needed(problems)
        manifest_id = uuid.uuid4().hex
        created = record_now()
        ident = c["identity"]
        title = c["events"][-1]["task"]["title"]
        base = f"{D._safe_name(ident.get('project_name') or ident.get('client_name') or root.name)}-任务交接-{D._safe_name(title)}-{datetime.now().astimezone().strftime('%Y%m%d')}"
        zip_name = D._unique(root.parent, base, ".zip")
        zip_path = root.parent / zip_name
        part = root.parent / f".{zip_name}.part"
        note_rel = f"{D.NOTE_DIR}/{D._unique(Path(root) / D.NOTE_DIR, zip_name[:-4], '.md')}"
        skip = _not_carried(root, c, result, {e["path"] for e in entries})
        note_bytes = _task_note(root, c, entries, skip, actor, to, problems, accept_problems, manifest_id, zip_name).encode("utf-8")
        need = sum(e["bytes"] for e in entries)
        import shutil
        try:
            free = shutil.disk_usage(root.parent).free
        except OSError:
            free = None
        if free is not None and free < need + (64 << 20):
            raise WorkflowError(f"工作区旁边的磁盘空间不够：这次最多要 {_human(need)}，只剩 {_human(free)}。请先腾出空间，或者换个位置再交接。")
        members, excerpts, kept = [], {}, {}
        try:
            try:
                z = zipfile.ZipFile(part, "x", allowZip64=True)
            except OSError as exc:
                raise WorkflowError(f"压缩包没做成：不能在工作区旁边新建文件（{exc.strerror or '没有写入权限'}）。请检查那个文件夹能不能写入，换个能写入的位置再交接。") from None
            with z:
                for e in entries:
                    got = D._add_disk_file(z, root, e)
                    members.append({k: got[k] for k in ("name", "size", "sha256")})
                for name, lines in sorted({**c["rows"], "standalone-tasks.jsonl": c["lines"], "project-memory.jsonl": c["memory_lines"]}.items()):
                    if lines:
                        data = ("\n".join(lines) + "\n").encode("utf-8")
                        D._add_bytes(z, f"{EXCERPT_DIR}/{name}", data, members)
                        excerpts[name] = {"lines": len(lines), "sha256": hashlib.sha256(data).hexdigest()}
                for name, lines in sorted(c["keep"].items()):
                    lines = [x if isinstance(x, str) else json.dumps(x[1], ensure_ascii=False) for x in lines]
                    data = ("\n".join(lines) + "\n").encode("utf-8")
                    D._add_bytes(z, f"{KEEP_DIR}/{name}", data, members)
                    kept[name] = {"lines": len(lines), "sha256": hashlib.sha256(data).hexdigest()}
                D._add_bytes(z, note_rel, note_bytes, members)
                task_manifest = {"format": TASK_FORMAT, "kind": "task", "created_by_tool": "scripts/jiaojie_dabao.py", "task_id": c["task_id"], "title": title,
                                 "status": c["events"][-1]["task"]["status"], "revisions": len(c["events"]), "event_digests": c["digests"], "workspace": root.name,
                                 "package_version": D._package_version(root), "identity": ident, "identity_row": c["identity_row"], "recipient": to, "actor": actor, "words": words,
                                 "created_at": created, "records": excerpts, "keep_only": kept, "decision_rows_not_carried": {k: len(v) for k, v in c["keep"].items() if k != "agent-dispatch.jsonl"},
                                 "dependencies": c["dependencies"], "unfinished_dispatches": c["unfinished_dispatches"],
                                 "files": [{"path": e["path"], "role": e["role"], "bytes": e["bytes"]} for e in entries], "note_path": note_rel,
                                 "not_carried": skip, "baseline": c["baseline"], "memory_rows": len(c["memory_lines"]),
                                 "append_only_files": [f"project/records/策略师原话/{c['task_id']}.jsonl"]}
                D._add_bytes(z, TASK_MANIFEST, (json.dumps(task_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), members)
                inventory = sorted(({"name": m["name"], "kind": "file", "size": m["size"], "sha256": m["sha256"]} for m in members), key=lambda x: x["name"])
                package = {"format": D.PACKAGE_FORMAT, "kind": "task", "task_id": c["task_id"], "created_by_tool": "scripts/jiaojie_dabao.py", "recipient": to, "words": words,
                           "package_version": D._package_version(root), "identity": ident, "note_path": note_rel,
                           "carried": {"files": len(members), "bytes": sum(m["size"] for m in members)}, "members": members, "known_errors": problems,
                           "known_errors_words": accept_problems if problems else None,
                           "note": "任务交接包：只列这个任务的文件与记录摘录；接手用 scripts/jiaojie_jieshou.py，不用整个工作区的接收核对"}
                manifest = build_manifest(root, report, actor, entries=inventory, package=package)
                manifest["manifest_id"] = manifest_id
                manifest["created_at"] = created
                manifest["manifest_history_path"] = f"{D.HISTORY_DIR}/{manifest_id}.json"
                manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                event = {"event": "handoff_prepared", "event_id": uuid.uuid4().hex, "at": created, "actor": actor, "manifest_id": manifest_id,
                         "manifest_sha256": sha256_bytes(manifest_bytes), "manifest_history_path": manifest["manifest_history_path"], "previous_manifest_sha256": None,
                         "inventory_sha256": manifest["inventory_sha256"], "status": report["status"]}
                line = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                D._add_bytes(z, manifest["manifest_history_path"], manifest_bytes, None, member=False)
                D._add_bytes(z, D.EVENTS_REL, line, None, member=False)
                D._add_bytes(z, D.MANIFEST_REL, manifest_bytes, None, member=False)
            check = D.verify_package(part)
            if not check["ok"]:
                raise WorkflowError("压缩包做好后自己核对没通过，没有留下这个包：" + "；".join(check["errors"][:3]))
            os.replace(part, zip_path)
        except BaseException:
            if part.exists():
                part.unlink()
            raise
        note_file = Path(root) / note_rel
        note_file.parent.mkdir(parents=True, exist_ok=True)
        note_file.write_bytes(note_bytes)
        zip_sha = hashlib.sha256()
        with zip_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                zip_sha.update(chunk)
        size = zip_path.stat().st_size
        row = {"schema_version": 1, "event": "handed_over", "handover_id": uuid.uuid4().hex, "kind": "task", "task_id": c["task_id"], "task_title": title, "created_at": created,
               "actor": actor, "words": words, "recipient": to, "package_name": zip_name, "package_bytes": size, "package_sha256": zip_sha.hexdigest(), "manifest_id": manifest_id,
               "inventory_sha256": manifest["inventory_sha256"], "files": len(members), "revisions": len(c["events"]), "revision_at_handover": c["events"][-1]["revision"],
               "decision_rows_not_carried": task_manifest["decision_rows_not_carried"], "note_path": note_rel, "known_errors": len(problems), "basis": BASIS}
        with (Path(root) / D.LOG_OUT).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n"); stream.flush(); os.fsync(stream.fileno())
        forward = forward_text(ident.get("project_name") or root.name, title, size)
        talk = (f"任务交接包做好了：{_human(size)}，放在你工作区文件夹的旁边（路径在下面，可以直接复制）。里面是「{title}」这一个任务的 {len(members)} 个文件和记录；"
                "打包后我已经把包里每个文件核对过一遍，没有损坏。原来的工作区一个文件都没删，只多了一份交接说明和一条交出记录。\n\n"
                f"```\n{zip_path}\n```\n\n把压缩包连同下面这段话一起发给对方：\n\n{forward}")
        return {"status": "packed", "kind": "task", "task_id": c["task_id"], "package_path": str(zip_path), "package_name": zip_name, "package_bytes": size,
                "package_sha256": row["package_sha256"], "manifest_id": manifest_id, "carried": package["carried"], "known_errors": problems, "note_path": note_rel,
                "decision_rows_not_carried": task_manifest["decision_rows_not_carried"], "forward_text": forward, "for_strategist": talk,
                "technical": f"交出记录 {D.LOG_OUT}（kind=task）；任务清单在包内 {TASK_MANIFEST}；接手 scripts/jiaojie_jieshou.py check / apply"}


# ---------------- 补登记成任务 ----------------

def register_task(root, folder, title, words, actor):
    """没登记成任务的成果，策略师同意后补登记成一个独立任务（走 standalone_tasks.save，原话记录）。成果 = 文件夹里不属于过程 / 暂存 / 重复的文件。"""
    from registration_guard import actor_check
    from standalone_tasks import save
    actor_check(actor)
    root = _d()._require_workspace(root)
    for value, name in ((title, "任务名"), (words, "策略师原话")):
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError(f"补登记要有{name}")
    rel = folder.strip().rstrip("/")
    found = _expand(root, rel)
    if not found or not rel.startswith("project/"):
        raise WorkflowError(f"找不到要登记的文件夹「{rel}」（要写成 project/ 开头的位置）")
    result = _d()._g().classify(root)
    ok = {e["path"] for e in result["files"] if e["category"] in ("product", "unsure", "unregistered_dir", "referenced", "source") and _d()._under(e["path"], rel)}
    deliverables = [{"label": Path(p).name, "path": p} for p in sorted(ok) if p in set(found)]
    if not deliverables:
        raise WorkflowError("这个文件夹里没有可以登记成成品的文件（只有过程、暂存或重复的）")
    events, _ = _tasks_state(root)
    taken = {e["task_id"] for e in events}
    stamp = datetime.now().astimezone().strftime("%Y%m%d")
    n = 1
    while f"JJ-{stamp}-{n:02d}" in taken:
        n += 1
    task_id = f"JJ-{stamp}-{n:02d}"
    ident = _identity(root)
    request = {"task_id": task_id, "request_id": f"{task_id}-r1", "expected_revision": 0, "reason": "交接前补登记：策略师同意把没登记的成果登记成任务（原话见 request_text）",
               "task": {"title": title, "project_label": ident.get("project_name") or "补登记", "request_text": "策略师原话：" + words, "goal": title, "status": "in_progress",
                        "summary": "为了交接，把原来没登记的成果补登记成一个任务；成品是这个文件夹里的文件", "next_action": "接手后核对成品", "owner": None, "due_date": None,
                        "sources": [], "deliverables": deliverables, "completion_evidence": ""}}
    event = save(root, request, actor)
    return {"status": "registered", "task_id": task_id, "title": title, "files": len(deliverables), "revision": event["revision"],
            "for_strategist": f"登记好了：「{title}」现在是项目里的一个任务（包含 {len(deliverables)} 个成品文件），可以交接了。原话已经记下。",
            "technical": f"standalone_tasks.save 写入 {task_id} 第 1 版；来源为空，成品为 {rel} 下 {len(deliverables)} 个文件；原话在 request_text 与 reason"}
