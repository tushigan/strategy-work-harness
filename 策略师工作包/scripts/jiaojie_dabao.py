#!/usr/bin/env python3
"""交接打包（v1.7.8 J01、J02、J05）：策略师说“交接”，把整个工作区精简后打成一个压缩包；先给看（preview），她回“好”才打包（pack）。

全量交接（kind=full）在本文件；任务交接（kind=task，N4）的挑选与打包在 jiaojie_renwu.py，接手导入在 jiaojie_jieshou.py。
原工作区一个文件都不删、不改：打包只多两样东西——项目记录里的交接说明（project/records/交接说明/）和一条交出记录（project/records/handoff-out.jsonl）。

带什么、不带什么（沿用 guocheng_zhengli 的分类，保护规则一条不放宽）：
  带——工作包本身（规则、技能、脚本、模板，凡不在下面“不带”里的都带）、项目记录与状态、成品、来源资料、记录里用到的文件、拿不准的文件（默认带，列出来）、
       没挂在任何任务下的大文件夹里除暂存 / 重复副本 / 看名字像过程的以外的部分（默认带，列出来，可点名不带）。
  不带——过程文件夹里的东西、暂存、同内容的多余副本（每组留的那份照带）、看名字像过程的（默认不带，可点名要带）、运行日志、锁文件、
       缓存与运行环境（.venv、node_modules、__pycache__ 等）、凭据与登录状态类文件、符号链接、本机的交出记录、工作区外的备份与归档（本来就不在工作区里）。
  点名：--include（要带本来不带的：过程 / 暂存 / 重复副本 / 看名字像过程的），--exclude（不带本来要带的：成品 / 来源 / 拿不准 / 没挂任务的大文件夹）；
       记录里用到的文件不能点名不带，凭据与登录状态类永远不带。

打包前：续做状态与移交检查有阻断问题 → 说明（按类别逐条白话列出），要她当次原话同意（--accept-problems）才照原样打包，问题记进清单，接收核对只放过这些交出时就有的问题；
        **凭据类问题（疑似凭据）不接受“原样交”**：直接拒绝（blocked_credentials），接收核对也不放过它们；
        要带的每个文件都跑凭据扫描（mingan_xinxi；已登记的误报照 H02 放行）——项目记录、被改过的发行文件、工作包目录里的非发行文件、点名多带的，一个不豁免；
        只有“指纹和发行清单一致、没被改过的发行文件”不扫（发行清单已经管着它们）。命中就不打包并说是哪份文件（不回显原文）。
清单与指纹：沿用移交清单的结构（handoff_transfer.build_manifest），清单里另写 package：精简后带了什么 / 没带什么、每个文件的指纹、交出时已有的问题；
        交接说明放在 project/records/交接说明/（随包，也在清单里有指纹）。接收方解压后用现有的 project_handoff.py accept 核对。

给其他阶段复用的函数接口（N4 任务交接、N5 接手引导）：
  plan(root, include=(), exclude=()) -> dict   按上面的规则给每个文件定带 / 不带，含凭据扫描结果；只读
  preview(root, kind="full", include=(), exclude=(), full_list=False, task=None) -> dict   只读，白话 + 清单（kind="task" 时不给 task 就列任务让她选；任务交接不支持 exclude）
  pack(root, kind, words, actor, include=(), exclude=(), to=None, accept_problems=None, task=None) -> dict   回“好”之后才调用
  verify_package(zip_path) -> {"ok","errors","files","bytes","manifest_id"}   不解压核对压缩包（路径安全、每个文件的指纹、清单、历史记录）
  handed_over(root) -> [{"at","kind","recipient","package_name","for_strategist"}]   最新一笔在前；续做状态用它在顶部提醒
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from phase2_store import WorkflowError, now

LOG_OUT = "project/records/handoff-out.jsonl"
CAUTION = "提醒：交接包没有加密，也没法证明是谁发出来的，所以只接手认识的人发来的交接包；不认识的人发来的，先问清楚再说。"
TASK_NO_EXCLUDE = "任务交接不支持少带（--exclude）：任务包只带这个任务的记录用到的文件，已经没有可以再去掉的。要去掉被凭据扫描拦下的文件，请改掉文件里的内容，或者确认是误报后登记。"
NOTE_DIR = "project/records/交接说明"
MANIFEST_REL = "project/records/handoff-manifest.json"
EVENTS_REL = "project/records/handoff-events.jsonl"
HISTORY_DIR = "project/records/handoffs"
BASIS = "J01 交接打包（用户 2026-10-16 原话：“此時我只需跟 Agent 說‘交接’，它就會把需要交接的文件打包壓縮發給我”）"
PACKAGE_FORMAT = 1
ENV_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"}
LOCK_RE = re.compile(r"(\.lock|\.lck)$|^\.~lock|^~\$", re.I)
# 登录状态类：永远不带（不拦着不让打包）。窄名单，只认常见的登录信息、cookie、环境变量、私钥证书文件
LOGIN_RE = re.compile(r"^(\.env(\..*)?|\.netrc|\.git-credentials|auth\.json|\.credentials(\.json)?|cookies?(\..*)?|.*[-_.]cookies?\.(json|txt|sqlite)|"
                      r"storage[-_]state.*\.json|settings\.local\.json|\.mcp\.json|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|.*\.(pem|p12|pfx)|.*\.session|.*\.token)$", re.I)
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".tif", ".tiff", ".bmp", ".ico", ".mp4", ".mov", ".m4a", ".mp3", ".wav", ".pdf", ".pptx", ".docx",
                   ".xlsx", ".zip", ".psd", ".ai", ".ttf", ".otf", ".woff", ".woff2", ".sketch", ".fig"}
STORED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".mp4", ".mov", ".m4a", ".mp3", ".zip", ".pptx", ".docx", ".xlsx", ".pdf", ".woff", ".woff2"}
LABELS = {"package": "工作包本身", "records": "项目记录", "referenced": "记录里用到的文件", "product": "成品", "source": "来源资料", "unsure": "拿不准的文件",
          "unregistered": "没挂在任何任务下的大文件夹", "included": "你点名要带的", "process": "过程文件", "temp": "暂存", "duplicate": "重复副本",
          "suspect": "看名字像过程的", "logs": "操作记录", "cache": "缓存与运行环境", "lock": "锁文件", "login": "凭据与登录状态", "symlink": "符号链接",
          "outlog": "本机的交出记录", "excluded": "你点名不带的"}
CARRY_GROUPS = {"package", "records", "referenced", "product", "source", "unsure", "unregistered", "included"}
INCLUDABLE = {"process", "temp", "duplicate", "suspect"}
EXCLUDABLE = {"product", "source", "unsure", "unregistered", "included"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
LIST_LIMIT = 5000
STATUS_WORDS = {"in_progress": "进行中", "completed": "已完成", "paused": "暂停了", "cancelled": "已取消", "blocked": "卡住了", "not_started": "还没开始",
                "needs_attention": "有问题要先处理", "archived": "已归档", "draft": "草稿"}
STAGE_WORDS = {"project_setup": "项目刚设置", "contract_scope": "合同与范围", "research": "调研", "brand_house_draft": "品牌屋草稿", "internal_proposal": "内部提案",
               "client_confirmation": "等客户确认", "closure": "结项"}


def _mb(size):
    return round(size / 1024 ** 2, 1)


def _human(size):
    return f"{_mb(size)}MB" if size >= 1024 ** 2 else ("不到 1MB" if size else "0")


def _g():
    import guocheng_zhengli as module
    return module


def _clean(name):
    return _g()._clean(name)


# ---------------- 工作区检查 ----------------

def _require_workspace(root):
    root = Path(root).resolve()
    if not (root / "project/state.json").is_file() or not (root / "scripts").is_dir() or not (root / "AGENTS.md").is_file():
        raise WorkflowError("这里还不是装好工作包的工作区（找不到项目资料和工作包文件），没法交接。请先打开要交接的那个工作区文件夹再说一次。")
    return root


def _package_inventory(root):
    """发行清单里列的文件 {路径: 指纹}（工作包本身，已有发行清单管着；指纹没变的不重复扫凭据，变了的照扫）。"""
    path = Path(root) / "package-inventory.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {x["path"]: x.get("sha256") for x in data.get("inventory", []) if isinstance(x, dict) and x.get("kind") == "file"}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _package_names(root):
    return set(_package_inventory(root))


def _file_sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(root):
    try:
        state = json.loads((Path(root) / "project/state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return {k: state.get(k) for k in ("project_id", "project_name", "client_name", "brand_name")}


def _package_version(root):
    try:
        head = (Path(root) / "README.md").read_text(encoding="utf-8")[:2000]
        match = re.search(r"当前版本：\*{0,2}\s*(v[\d.]+[^（｜*\n]*)", head)
        return match.group(1).strip() if match else "未知"
    except OSError:
        return "未知"


# ---------------- 给每个文件定带不带 ----------------

def _walk(root):
    """整个工作区的普通文件：[(相对路径, 字节, 权限)]；符号链接、空文件夹另记。不进本地开发目录和 __pycache__。"""
    from runtime_structure import LOCAL_ONLY_ROOTS
    root = Path(root)
    files, links, empty = [], [], []
    for current, dirs, names in os.walk(root, followlinks=False):
        here = Path(current)
        rel_dir = "" if here == root else here.relative_to(root).as_posix()
        keep = []
        for d in sorted(dirs):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if (not rel_dir and d in LOCAL_ONLY_ROOTS) or d == "__pycache__":
                continue
            if os.path.islink(here / d):
                links.append(rel)
                continue
            keep.append(d)
        dirs[:] = keep
        if rel_dir and not keep and not names:
            empty.append(rel_dir)
        for name in sorted(names):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            path = here / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if os.path.islink(path):
                links.append(rel)
            elif os.path.isfile(path):
                files.append((rel, info.st_size, info.st_mode & 0o7777))
    return files, links, empty


def _group(rel, cat):
    parts = rel.split("/")
    name = parts[-1]
    if name in SKIP_NAMES or name.endswith(".pyc"):
        return None
    if rel in (MANIFEST_REL, EVENTS_REL) or rel.startswith(HISTORY_DIR + "/"):
        return "bookkeeping"
    if rel == LOG_OUT:
        return "outlog"
    if rel.startswith("project/运行日志/"):
        return "logs"
    if any(p in ENV_DIRS for p in parts[:-1]):
        return "cache"
    if LOCK_RE.search(name):
        return "lock"
    if LOGIN_RE.match(name):
        return "login"
    if parts[0] != "project":
        return "package"
    if rel == "project/state.json" or rel.startswith("project/records/"):
        return "records"
    return {"referenced": "referenced", "product": "product", "source": "source", "unsure": "unsure", "unregistered_dir": "unregistered", "process": "process",
            "temp": "temp", "duplicate": "duplicate", "suspect": "suspect"}.get(cat, "unsure")


def _under(rel, base):
    return _g()._is_under(rel, base)


def _released_by_false_alarm(root, rel, state):
    """已登记的误报照 H02 放行：这份文件现在被启动检查判出的每一处都已登记、文件没变、没有硬规则命中。文件名像凭据的不放。"""
    from mingan_xinxi import credential_name
    if credential_name(Path(rel).name):
        return False
    if "inspect" not in state:
        try:
            from wubao_dengji import inspect as false_alarms
            state["inspect"] = false_alarms(root)
        except Exception:
            state["inspect"] = {"hits": []}
    spots = [h for h in state["inspect"].get("hits", []) if h.get("path") == rel]
    if spots and all(h.get("registered") and not h.get("has_secret_style") for h in spots):
        return True
    try:                                              # N4 P2-1：只被交接扫描判出的文件，按“这份文件”登记过且没变的也放行
        from wubao_dengji import whole_file_registered
        return whole_file_registered(root, rel)
    except Exception:
        return False


def _scan(root, entries):
    """要带的文件里像凭据的：[相对路径]（不回显原文）。二进制文件只看文件名。
    全盘细查 P0-1：不再按“组”豁免——项目记录、被改过的发行文件、工作包目录里的非发行文件、点名多带的，一个都不漏；
    只有指纹和发行清单一致、没改过的发行文件不扫（发行清单管着）。"""
    from mingan_xinxi import credential_file, credential_name
    state, hits, released = {}, [], []
    for e in entries:
        if not e["carry"]:
            continue
        if e["group"] == "package" and e.get("package_unchanged"):
            continue
        rel = e["path"]
        path = Path(root) / rel
        if path.suffix.lower() in BINARY_SUFFIXES:
            why = "文件名看起来是登录凭据" if credential_name(path.name) else None
        else:
            why = credential_file(path)
        if not why:
            continue
        if _released_by_false_alarm(root, rel, state):
            released.append(rel)
        else:
            hits.append(rel)
    return sorted(hits), sorted(released)


def plan(root, include=(), exclude=()):
    """给工作区里每个文件定带 / 不带（只读）。include / exclude 是策略师点名的 project/ 开头的路径。"""
    root = _require_workspace(root)
    guocheng = _g()
    result = guocheng.classify(root)
    incomplete = bool(result["incomplete"])
    cat = {e["path"]: e["category"] for e in result["files"]}
    inventory = _package_inventory(root)
    raw, links, empty = _walk(root)
    entries, errors, warnings = [], [], list(result["warnings"])
    for rel, size, mode in raw:
        group = _group(rel, cat.get(rel))
        if group is None:
            continue
        carry = group in CARRY_GROUPS or (incomplete and group in INCLUDABLE)
        unchanged = False
        if group == "package" and rel in inventory:
            try:
                unchanged = size <= (64 << 20) and _file_sha(Path(root) / rel) == inventory[rel]
            except OSError:
                unchanged = False
        entries.append({"path": rel, "bytes": size, "mode": mode, "group": group, "carry": carry, "package_member": rel in inventory, "package_unchanged": unchanged})
    for rel in links:
        entries.append({"path": rel, "bytes": 0, "mode": 0, "group": "symlink", "carry": False, "package_member": False})
    if incomplete:
        warnings.append("判断不了哪些文件正在使用，所以过程文件、暂存这些也都先带上了")
    # ---- 点名
    inc = [guocheng._norm_include(x) for x in include]
    exc = [guocheng._norm_include(x) for x in exclude]
    include_report, exclude_report = {"applied": 0, "refused": []}, {"applied": 0, "refused": []}
    for target in inc:
        hits = [e for e in entries if _under(e["path"], target)]
        if not hits:
            errors.append(f"点名要带的「{target}」在工作区里找不到")
            continue
        for e in hits:
            if e["carry"] or e["group"] == "bookkeeping":
                continue
            if e["group"] in INCLUDABLE:
                e["carry"], e["group"] = True, "included"
                include_report["applied"] += 1
            else:
                include_report["refused"].append({"path": e["path"], "reason": LABELS.get(e["group"], e["group"]) + "，这一类永远不带"})
    for target in exc:
        hits = [e for e in entries if _under(e["path"], target)]
        if not hits:
            errors.append(f"点名不带的「{target}」在工作区里找不到")
            continue
        for e in hits:
            if not e["carry"]:
                continue
            if e["group"] in EXCLUDABLE:
                e["carry"], e["group"] = False, "excluded"
                exclude_report["applied"] += 1
            else:
                exclude_report["refused"].append({"path": e["path"], "reason": LABELS.get(e["group"], e["group"]) + "，不带的话对方接手会缺东西"})
    if exclude_report["refused"] and not exclude_report["applied"]:
        errors.append("点名不带的这些都是记录里用到的文件或工作包本身，不能不带（带上记录、缺了文件，对方接手就会出错）："
                      + "、".join(x["path"] for x in exclude_report["refused"][:5]))
    elif exclude_report["refused"]:
        warnings.append(f"点名不带的里面有 {len(exclude_report['refused'])} 个是记录里用到的文件，这些还是带上了")
    if include_report["refused"] and not include_report["applied"]:
        warnings.append("点名要带的这些属于永远不带的类别（凭据与登录状态、锁文件、缓存、运行日志等），没有带："
                        + "、".join(x["path"] for x in include_report["refused"][:5]))
    credential_hits, released = _scan(root, entries)
    # ---- 汇总
    carry = {"files": 0, "bytes": 0, "groups": {}}
    skip = {"files": 0, "bytes": 0, "groups": {}}
    for e in entries:
        if e["group"] == "bookkeeping":
            continue
        side = carry if e["carry"] else skip
        slot = side["groups"].setdefault(LABELS[e["group"]], {"files": 0, "bytes": 0})
        slot["files"] += 1; slot["bytes"] += e["bytes"]
        side["files"] += 1; side["bytes"] += e["bytes"]
    def largest(wanted):
        rows = sorted((e for e in entries if e["group"] != "bookkeeping" and e["carry"] is wanted), key=lambda e: -e["bytes"])[:5]
        return [{"path": e["path"], "mb": _mb(e["bytes"]), "group": LABELS[e["group"]]} for e in rows]
    unregistered = []
    for d in result["unregistered_dirs"]:
        inside = [e for e in entries if _under(e["path"], d["dir"])]
        unregistered.append({"dir": d["dir"], "bytes": d["bytes"], "files": d["files"], "carry_bytes": sum(e["bytes"] for e in inside if e["carry"]),
                             "skip_bytes": sum(e["bytes"] for e in inside if not e["carry"]), "subdirs": d["subdirs"]})
    skipped_files = [{"path": e["path"], "bytes": e["bytes"], "group": LABELS[e["group"]]} for e in entries if not e["carry"] and e["group"] != "bookkeeping"]
    page = [e for e in result["files"] if e.get("page_ref") and e.get("was") in ("process", "temp", "suspect")]
    page_referenced = {"files": len(page), "bytes": sum(e["bytes"] for e in page), "pages": sorted({Path(e["page_ref"]).name for e in page})[:5]}
    return {"root": str(root), "entries": entries, "empty_dirs": empty, "carry": carry, "skip": skip, "page_referenced": page_referenced, "largest_carried": largest(True), "largest_skipped": largest(False),
            "unregistered_dirs": unregistered, "credential_hits": credential_hits, "credential_released": released, "errors": errors, "warnings": warnings,
            "include": {**include_report, "requested": inc}, "exclude": {**exclude_report, "requested": exc}, "incomplete": incomplete,
            "skipped_files": skipped_files[:LIST_LIMIT], "skipped_files_truncated": len(skipped_files) > LIST_LIMIT}


# ---------------- 预览 ----------------

def _problems(root):
    """续做状态里的阻断问题（只读）。"""
    try:
        from workspace_resume import resume
        return sorted(set(resume(root).get("errors", [])))
    except Exception as exc:
        return [f"续做状态没能读出来：{str(exc)[:120]}"]


PROBLEM_KINDS = (("引用目标不存在", "记录里写到的文件找不到"), ("发行文件", "工作包本身的文件被改动、缺了或多了"), ("package-inventory", "工作包本身的文件被改动、缺了或多了"),
                 ("根目录", "工作区根目录里放着不属于工作包的文件"), ("宿主同步", "两个助手软件的入口文件对不上"), ("入口缺失", "两个助手软件的入口文件对不上"),
                 ("未登记的宿主副本", "两个助手软件的入口文件对不上"), ("读不出来", "有记录读不出来"), ("无法读取", "有记录读不出来"), ("损坏", "有记录损坏"),
                 ("分派", "有派出去的任务没有收尾"), ("身份", "项目身份对不上"))


def problem_kinds(problems):
    """把检查里没解决的问题按类别归并成白话：[{"label","count"}]（给策略师逐类列出，不只说“有 N 个问题”）。"""
    counts = {}
    for item in problems:
        label = next((words for key, words in PROBLEM_KINDS if key in str(item)), "其他检查提示")
        counts[label] = counts.get(label, 0) + 1
    return [{"label": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def kinds_words(problems):
    return "、".join(f"{x['label']}（{x['count']} 处）" for x in problem_kinds(problems))


def credential_problem_files(problems):
    """检查报出的“疑似凭据”问题里涉及的文件（不回显内容）。"""
    from mingan_xinxi import is_credential_problem
    out = set()
    for item in problems:
        if is_credential_problem(item):
            match = re.match(r"^(.*?)(?::\d+)? 中疑似凭据", str(item))
            out.add(match.group(1) if match and match.group(1) else str(item)[:120])
    return out


def split_problems(problems):
    """(凭据类, 其余)：凭据类不接受“原样交”。"""
    from mingan_xinxi import is_credential_problem
    return [x for x in problems if is_credential_problem(x)], [x for x in problems if not is_credential_problem(x)]


def _talk(root, p, problems):
    carry, skip = p["carry"], p["skip"]
    parts = [f"这份工作区整个交出去的话：会带走 {carry['files']} 个文件（{_human(carry['bytes'])}），不带 {skip['files']} 个文件（{_human(skip['bytes'])}）。"
             "带走的有：工作包本身（规则、技能、脚本）、项目记录、成品、客户给的资料和记录里用到的文件。"]
    groups = skip["groups"]
    left = [f"{name} {groups[name]['files']} 个（{_human(groups[name]['bytes'])}）" for name in
            ("过程文件", "暂存", "重复副本", "看名字像过程的", "操作记录", "缓存与运行环境", "锁文件", "凭据与登录状态", "符号链接", "本机的交出记录", "你点名不带的") if name in groups]
    if left:
        parts.append("不带的有：" + "、".join(left) + "。重复副本每一组都留了一份在包里。")
    else:
        parts.append("没有需要精简掉的东西，所有文件都会带上。")
    named = []
    for item in [x for x in p["largest_skipped"] if x["group"] in {"过程文件", "暂存", "重复副本", "看名字像过程的", "你点名不带的"}][:3]:
        folder = item["path"].rsplit("/", 1)[0]
        label = _g()._label(folder)
        named.append(f"{label or '一处名字是英文的文件夹'}（{item['group']}）{item['mb']}MB")
    if named:
        parts.append("不带的里面最大的几项是：" + "、".join(named) + "。")
    big = []
    for item in [x for x in p["largest_carried"] if x["group"] not in {"工作包本身", "项目记录"}][:3]:
        folder = item["path"].rsplit("/", 1)[0]
        label = _g()._label(folder)
        big.append(f"{label or '一处名字是英文的文件夹'}里的一个文件 {item['mb']}MB")
    if big:
        parts.append("带走的里面最大的是：" + "、".join(big) + "。")
    pr = p.get("page_referenced") or {}
    if pr.get("files"):
        who = "、".join(x for x in (_clean(n) for n in pr["pages"][:3]) if x) or "几个成品页面"
        parts.append(f"有 {pr['files']} 个文件（{_human(pr['bytes'])}）放在过程或暂存里，但被成品页面（{who}）用到了，所以照样带上，不在“不带”里。")
    unsure = carry["groups"].get("拿不准的文件")
    if unsure:
        parts.append(f"我拿不准的文件有 {unsure['files']} 个（{_human(unsure['bytes'])}），默认也带上了，免得对方缺东西；你觉得不用带的，告诉我文件夹名。")
    if p["unregistered_dirs"]:
        shown = []
        for d in p["unregistered_dirs"][:3]:
            name = _clean(d["dir"].rsplit("/", 1)[-1]) or "一处名字是英文的文件夹"
            shown.append(f"{name}（带走 {_human(d['carry_bytes'])}）")
        parts.append("有不属于任何任务的大文件夹：" + "、".join(shown) + "。我默认带上了其中不属于暂存、重复副本、过程的部分；不想带的话告诉我文件夹名。")
    if p["credential_hits"]:
        names = "、".join(_clean(Path(x).name) or "一份名字是英文的文件" for x in p["credential_hits"][:3])
        parts.append(f"注意：{names}里有像登录凭据的内容，没处理前我不会打包（你同意原样交也不行，凭据不能随包交出去）。你可以改掉那份文件里的密码或密钥，或者让我把它不带；如果你看过确认那不是密码或密钥（比如是飞书的文件代号），告诉我，我登记成“已确认不是凭据”，文件没变之前就不再拦。")
    if problems:
        parts.append(f"另外，工作区现在的检查有 {len(problems)} 个没解决的问题，大致是：{kinds_words(problems)}；照这样交出去，对方接手后会看到同样的问题。要先修，还是你同意原样交？原样交的话，请你当次明确说“同意照原样交”。")
    for message in p["errors"]:
        parts.append(message + "。")
    parts.append("想多带哪些（比如看名字像过程的某个文件夹），或少带哪些（比如那个大文件夹），告诉我文件夹名。你回“好”，我就打包；原来的工作区一个文件都不会删，只多一份交接说明和一条交出记录。")
    return "".join(parts)


def preview(root, kind="full", include=(), exclude=(), full_list=False, task=None):
    if kind == "task":
        if exclude:
            raise WorkflowError(TASK_NO_EXCLUDE)
        import jiaojie_renwu
        return jiaojie_renwu.preview(root, task, include)
    root = _require_workspace(root)
    p = plan(root, include, exclude)
    problems = _problems(root)
    cred_problems, problems_other = split_problems(problems)
    p["credential_hits"] = sorted(set(p["credential_hits"]) | credential_problem_files(cred_problems))
    listing = ({"skipped_files": p["skipped_files"], "skipped_files_truncated": p["skipped_files_truncated"]} if full_list else
               {"skipped_files_count": p["skip"]["files"], "skipped_files_note": "默认只给个数和最大几项；要把每个不带的文件都列出来，加 --list"})
    return {"status": "preview", "kind": "full", "workspace": root.name, "carry": p["carry"], "skip": p["skip"], "largest_carried": p["largest_carried"],
            "largest_skipped": p["largest_skipped"], "unregistered_dirs": p["unregistered_dirs"], "credential_hits": p["credential_hits"],
            "credential_released": p["credential_released"], "page_referenced": p["page_referenced"], "problems": problems, "problem_kinds": problem_kinds(problems_other), "errors": p["errors"], "warnings": p["warnings"], "include": p["include"],
            "exclude": p["exclude"], **listing,
            "for_strategist": _talk(root, p, problems_other),
            "technical": "规则见 scripts/jiaojie_dabao.py 顶部说明；打包：python3 scripts/jiaojie_dabao.py pack --workspace . --kind full --words \"<策略师当次回复的原话>\" --actor <实例> "
                         "[--to <接手人>] [--include <project/ 开头的路径>]... [--exclude <project/ 开头的路径>]... [--accept-problems \"<同意照原样交的原话>\"]"}


# ---------------- 交接说明 ----------------

NOTE_FALLBACK = """# 交接说明

## 给接手人

**这是什么**：{{what}}

**现在到哪了**：{{where}}

**下一步**：{{next}}

**在等谁拍板**：{{waiting}}

**注意事项**：{{notes}}

**没带的东西**：{{missing}}

**平台账号要自己连**：{{accounts}}

## 技术附录（给 Agent）

{{appendix}}
"""


OLD_WORDS_SENTENCE = ("原来那位说过的“这版不用检查”“这就是最终版”这类话，对你不算数，你要用就自己再说一次；检查过且没改过的成品照样算数。")


def _date_words(iso=None):
    when = datetime.fromisoformat(iso).astimezone() if iso else datetime.now().astimezone()
    return f"{when.year} 年 {when.month} 月 {when.day} 日"


def _bullets(items, empty):
    return "\n" + "\n".join(f"- {x}" for x in items) if items else empty


VERSION_WORD = re.compile(r"(?<![A-Za-z0-9])[vV](\d+(?:\.\d+)*)(?![A-Za-z0-9.])")


def task_own_versions(task):
    """一个任务记录里自己写到的版本号写法（标题、摘要、成品名里的 v0.4 这类，统一小写）。"""
    task = task or {}
    blobs = [str(task.get("title") or ""), str(task.get("summary") or "")]
    for ref in (task.get("deliverables") or []):
        blobs += [str(ref.get("label") or ""), str(ref.get("path") or "")]
    return {m.group(0).lower() for blob in blobs for m in VERSION_WORD.finditer(blob)}


def task_versions(root):
    """各任务自己写在记录里的版本号写法（标题、摘要、成品名里的 v0.4 这类），收尾小修 r3：只有这些才换成“第 N 版”。读不出来返回空集合。"""
    out = set()
    try:
        from standalone_tasks import history
        events, _ = history(Path(root).resolve())
        latest = {e["task_id"]: e for e in events}
        for event in latest.values():
            out |= task_own_versions(event.get("task"))
    except Exception:
        return set()
    return out


def plainify(text, names=None, versions=None):
    """给接手人读的话里去掉任务编号和版本号写法：只替换本工作区真实存在的任务编号（换成任务名）和这些任务自己的版本号写法（v0.4 换成“第 4 版”）；
    GPT-5、SKU-001、v2.0、工作包版本号这类别的词原样保留。"""
    out = str(text or "")
    for tid, title in sorted((names or {}).items(), key=lambda kv: -len(kv[0])):
        out = re.sub(re.escape(tid) + r"(?:（[^）]*）)?", f"「{title}」", out)
    own = {str(x).lower() for x in (versions or ())}
    if own:
        out = VERSION_WORD.sub(lambda m: ("第 " + m.group(1).split(".")[-1] + " 版") if m.group(0).lower() in own else m.group(0), out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def plain_piece(text, names=None, fallback="", root=None, versions=None):
    """plainify 之后还被说人话扫描（按这个工作区的品牌名、产品名放行）报命中的，整段不用，换成 fallback。"""
    out = plainify(text, names, versions)
    if not out:
        return fallback
    try:
        from shuo_renhua import scan_text
        if scan_text(out, root)["hits"]:
            return fallback
    except Exception:
        pass
    return out


def delivered_history(root, names):
    """发给过客户的版本（所有历史版本，不只当前版）：{任务编号: [(第几版, 发给谁)]}。读不出来返回空。"""
    path = Path(root) / "project/records/delivery-gate.jsonl"
    out = {}
    try:
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return out
    for row in rows:
        if not isinstance(row, dict) or row.get("event") != "delivered" or not row.get("task_id"):
            continue
        number = row.get("version")
        try:
            from jiaofu_guankou import shown_version
            shown = shown_version(root, row["task_id"], number)
        except Exception:
            shown = number
        pair = (shown if shown is not None else number, row.get("to") or "客户")
        if pair not in out.setdefault(row["task_id"], []):
            out[row["task_id"]].append(pair)
    return out


def build_note(root, p, resume_state, report, manifest_id, zip_name, actor, to, problems, accept_words):
    template_path = Path(root) / "templates/交接说明.md"
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError:
        template = NOTE_FALLBACK
    ident = _identity(root)
    project = resume_state.get("project", {})
    name = ident.get("project_name") or root_name(root)
    client = ident.get("client_name")
    what = (f"这是「{name}」" + (f"（客户：{client}）" if client else "") + f"的整个工作区，{_date_words()}整份交出来了"
            + (f"，交给{to}" if to else "") + "。里面有工作包本身（规则、技能、脚本）和这个项目到目前为止的全部资料与记录，你解开、打开就能接着做，不用另外装工作包。")
    stage = STAGE_WORDS.get(project.get("current_stage"), project.get("current_stage") or "还没开始")
    names = {t["task_id"]: t["title"] for t in resume_state.get("current_tasks", []) if t.get("task_id") and t.get("title")}
    versions = task_versions(root)
    tasks = [f"「{t['title']}」：{STATUS_WORDS.get(t['status'], '状态待确认')}；下一步{plain_piece(t.get('next_action'), names, '待定', root, versions)}" for t in resume_state.get("current_tasks", [])]
    where = f"项目阶段：{stage}。" + ("手上的任务：" + _bullets(tasks, "") if tasks else "目前还没有登记的任务。")
    nxt = plain_piece(project.get("next_action"), names, "", root, versions) or "接手后先看一遍上面的任务，再决定先做哪一项。"
    waiting = []
    for t in resume_state.get("current_tasks", []):
        if t.get("strategist_pending"):
            waiting.append(f"「{t['title']}」有 {t['strategist_pending']} 项检查提醒等策略师看")
    for item in resume_state.get("strategist_reminders", []):
        waiting.append(f"任务「{item.get('task_id')}」有一条检查提醒还没被看过")
    pending = resume_state.get("memory", {}).get("needs_confirmation", [])
    if pending:
        waiting.append(f"项目记忆里有 {len(pending)} 条待确认（比如「{pending[0]['topic']}」）")
    notes = []
    history = delivered_history(root, names)
    for t in resume_state.get("current_tasks", []):
        sent = history.get(t.get("task_id")) or []
        if sent:
            notes.append(f"「{t['title']}」" + "；".join(f"第 {number} 版发给过{who}" for number, who in sent) + "（接着改之前先看清哪一版发出去了）")
        elif t.get("delivered"):
            notes.append(f"「{t['title']}」已经发给过：{'、'.join(t['delivered'])}（接着改之前先看清哪一版发出去了）")
    notes.append(OLD_WORDS_SENTENCE)
    if problems:
        notes.append(f"交出时检查还有 {len(problems)} 个没解决的问题，是交出的人同意原样交的；具体见技术附录")
    if p["include"]["applied"]:
        notes.append(f"按交出的人要求，多带了 {p['include']['applied']} 个本来不带的文件")
    skip_groups = p["skip"]["groups"]
    missing = []
    for label in ("过程文件", "暂存", "重复副本", "看名字像过程的", "你点名不带的", "操作记录", "缓存与运行环境", "锁文件", "凭据与登录状态", "符号链接"):
        if label in skip_groups:
            missing.append(f"{label} {skip_groups[label]['files']} 个（{_human(skip_groups[label]['bytes'])}）")
    missing.append("工作区外的备份和归档（本来就不在工作区里）")
    missing_text = _bullets(missing, "") + "\n\n要这些的话，找交出的人要。重复副本每一组都留了一份在包里，不会缺内容。"
    accounts = "飞书、云端大脑、产品线网站这类账号都没有随包带，也不应该带。需要用的话，用你自己的账号连一次；比如说“帮我连产品线网站”，我会一步一步带你做。"
    appendix = "\n".join([
        f"- 交接类型：full（整个工作区）；包名：{zip_name}；交出实例（actor）：{actor}" + (f"；交给：{to}" if to else ""),
        f"- 工作包版本：{_package_version(root)}",
        "- 任务编号对照（上半部分里的任务只写任务名，编号在这里）：" + ("；".join(f"{k}={v}" for k, v in names.items()) if names else "无"),
        f"- 清单编号 manifest_id：{manifest_id}；清单里 package 一节写了带走 {p['carry']['files']} 个文件 / {p['carry']['bytes']} 字节，以及每个没带的文件。",
        f"- 接手核对：解压到新文件夹后运行 `python3 scripts/project_handoff.py accept --workspace . --actor <实例> --manifest-id {manifest_id} --json`；也可以不解压，先运行 `python3 scripts/jiaojie_dabao.py verify --package <压缩包路径>`。",
        "- 没带的类别与个数：" + (json.dumps({k: v["files"] for k, v in skip_groups.items()}, ensure_ascii=False) if skip_groups else "无"),
        "- 交出时仍有的检查问题：" + (json.dumps(problems, ensure_ascii=False)[:1500] if problems else "无") + (f"；交出的人当次原话：{accept_words}" if problems else ""),
        f"- 说明文件位置：{NOTE_DIR}/ 下，随包，清单里有指纹；交出记录只在交出的人这边（{LOG_OUT}），不随包。",
    ])
    text = template
    for key, value in (("what", what), ("where", where), ("next", nxt), ("waiting", _bullets(waiting, "暂时没有人在等拍板。")),
                       ("notes", _bullets(notes, "暂时没有特别要提醒的。")), ("missing", missing_text), ("accounts", accounts), ("appendix", appendix)):
        text = text.replace("{{" + key + "}}", value)
    return text


def root_name(root):
    return Path(root).resolve().name


# ---------------- 打包 ----------------

def _safe_name(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", str(value)).strip(" .-")
    return value or "工作区"


def _unique(parent, base, suffix):
    for n in range(1, 1000):
        name = f"{base}{suffix}" if n == 1 else f"{base}-{n}{suffix}"
        if not (Path(parent) / name).exists():
            return name
    raise WorkflowError("同一天打的包太多了，旁边的同名压缩包已经有一千个")


def _zip_info(name, mode=0o644, compress=zipfile.ZIP_DEFLATED, mtime=None):
    when = time.localtime(mtime)[:6] if mtime else time.localtime()[:6]
    if when[0] < 1980:
        when = (1980, 1, 1, 0, 0, 0)
    info = zipfile.ZipInfo(name, date_time=when)
    info.compress_type = compress
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def _add_bytes(z, name, data, members=None, member=True):
    z.writestr(_zip_info(name), data)
    if members is not None and member:
        members.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})


def _add_disk_file(z, root, e):
    path = Path(root) / e["path"]
    info = _zip_info(e["path"], e["mode"] or 0o644, zipfile.ZIP_STORED if path.suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED, os.stat(path).st_mtime)
    info.file_size = e["bytes"]
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source, z.open(info, "w", force_zip64=e["bytes"] > (1 << 30)) as target:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk); target.write(chunk); size += len(chunk)
    return {"name": e["path"], "kind": "file", "size": size, "sha256": digest.hexdigest()}


def _read_events(root):
    path = Path(root) / EVENTS_REL
    if not path.is_file():
        return b"", []
    raw = path.read_bytes()
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return raw, rows


def pack(root, kind="full", words="", actor="", include=(), exclude=(), to=None, accept_problems=None, task=None):
    if kind == "task":
        if exclude:
            raise WorkflowError(TASK_NO_EXCLUDE)
        import jiaojie_renwu
        return jiaojie_renwu.pack(root, task, words, actor, to, accept_problems, include)
    from registration_guard import actor_check
    from workspace_lock import serialized
    actor_check(actor)
    if not isinstance(words, str) or not words.strip():
        raise WorkflowError("打包须有策略师当次回复的原话（--words，比如她回的“好”），没有就不打包")
    root = _require_workspace(root)
    with serialized(root):
        p = plan(root, include, exclude)
        if p["errors"]:
            raise WorkflowError("；".join(p["errors"]))
        if p["credential_hits"]:
            names = "、".join(Path(x).name for x in p["credential_hits"][:5])
            return {"status": "blocked_credentials", "credential_hits": p["credential_hits"],
                    "for_strategist": credential_talk(p["credential_hits"], names),
                    "technical": "命中的文件（不回显内容）：" + "、".join(p["credential_hits"]) + "；点名不带用 --exclude <路径>；确认是误报："
                                 "python3 scripts/wubao_dengji.py register --workspace . --path <路径> --field （按文本检查） --words \"<策略师原话>\" --actor <实例>"
                                 "（N4 起交接扫描判出的也能登记，放行范围是这份文件，文件变了重新拦，含密钥样式的长串仍拒）"}
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
        blocked = credential_block(problems)
        if blocked:
            return blocked
        if problems and not (isinstance(accept_problems, str) and accept_problems.strip()):
            return consent_needed(problems)
        manifest_id = uuid.uuid4().hex
        created = record_now()
        ident = _identity(root)
        base = f"{_safe_name(ident.get('project_name') or ident.get('client_name') or root.name)}-全量交接-{datetime.now().astimezone().strftime('%Y%m%d')}"
        zip_name = _unique(root.parent, base, ".zip")
        zip_path = root.parent / zip_name
        part = root.parent / f".{zip_name}.part"
        note_rel = f"{NOTE_DIR}/{_unique(Path(root) / NOTE_DIR, zip_name[:-4], '.md')}"      # 之前同名的交接说明（压缩包被删掉重打）不覆盖
        note_text = build_note(root, p, resume_state, report, manifest_id, zip_name, actor, to, problems, accept_problems)
        note_bytes = note_text.encode("utf-8")
        carried = sorted((e for e in p["entries"] if e["carry"]), key=lambda e: e["path"])
        members, inventory = [], []
        need = sum(e["bytes"] for e in carried)
        try:
            free = shutil.disk_usage(root.parent).free
        except OSError:
            free = None
        if free is not None and free < need + (64 << 20):
            raise WorkflowError(f"工作区旁边的磁盘空间不够：这次最多要 {_human(need)}，只剩 {_human(free)}。请先腾出空间，或者换个位置再交接。")
        try:
            try:
                z = zipfile.ZipFile(part, "x", allowZip64=True)
            except OSError as exc:
                raise WorkflowError(f"压缩包没做成：不能在工作区旁边新建文件（{exc.strerror or '没有写入权限'}）。请检查那个文件夹能不能写入，换个能写入的位置再交接。") from None
            with z:
                for e in carried:
                    entry = _add_disk_file(z, root, e)
                    members.append({k: entry[k] for k in ("name", "size", "sha256")})
                    from handoff_inventory import is_ignored
                    if not is_ignored(root, root / e["path"]):
                        inventory.append(entry)
                quiet = ENV_DIRS | _g().TEMP_DIRS | {_g().PROCESS_DIR}
                for folder in p["empty_dirs"]:
                    if not any(part_ in quiet for part_ in folder.split("/")) and not folder.startswith("project/运行日志"):
                        info = _zip_info(folder + "/", 0o755, zipfile.ZIP_STORED); info.external_attr = (0o40755 << 16) | 0x10
                        z.writestr(info, b"")
                _add_bytes(z, note_rel, note_bytes, members)
                inventory.append({"name": note_rel, "kind": "file", "size": len(note_bytes), "sha256": sha256_bytes(note_bytes)})
                inventory.sort(key=lambda item: item["name"])
                # ---- 交接历史：沿用移交清单的链（旧版本的清单与事件原样带，新清单与新事件追加）
                events_raw, events = _read_events(root)
                prepared = [x for x in events if x.get("event") == "handoff_prepared"]
                previous = prepared[-1].get("manifest_sha256") if prepared else None
                history = Path(root) / HISTORY_DIR
                have = set()
                if history.is_dir():
                    for old in sorted(history.iterdir()):
                        if old.is_file() and not old.is_symlink() and re.fullmatch(r"[0-9a-f]{32}\.json", old.name):
                            _add_bytes(z, f"{HISTORY_DIR}/{old.name}", old.read_bytes(), members); have.add(old.name)
                latest = Path(root) / MANIFEST_REL
                if latest.is_file() and not latest.is_symlink():
                    latest_bytes = latest.read_bytes()
                    try:
                        old_id = json.loads(latest_bytes)["manifest_id"]
                    except (ValueError, KeyError, TypeError):
                        old_id = None
                    if old_id and f"{old_id}.json" not in have and previous == sha256_bytes(latest_bytes):
                        _add_bytes(z, f"{HISTORY_DIR}/{old_id}.json", latest_bytes, members)
                skip_groups = p["skip"]["groups"]
                package = {
                    "format": PACKAGE_FORMAT, "kind": "full", "created_by_tool": "scripts/jiaojie_dabao.py", "recipient": to, "words": words,
                    "package_version": _package_version(root), "identity": ident, "note_path": note_rel,
                    "carried": {"files": len(carried) + 1, "bytes": sum(e["bytes"] for e in carried) + len(note_bytes)},
                    "not_carried": skip_groups, "not_carried_files": [{"name": x["path"], "bytes": x["bytes"], "group": x["group"]} for x in p["skipped_files"]],   # 键名不能叫 path：启动检查会把 path 当成要核对的引用
                    "not_carried_truncated": p["skipped_files_truncated"],
                    "members": members, "known_errors": problems, "known_errors_words": accept_problems if problems else None,
                    "include": p["include"]["requested"], "exclude": p["exclude"]["requested"],
                    "note": "members 列出每个随包文件的指纹（清单自己、它的历史副本和交接事件除外，那三样由 accept 与 verify 另行核对）",
                }
                manifest = build_manifest(root, report, actor, entries=inventory, package=package)
                manifest["manifest_id"] = manifest_id
                manifest["created_at"] = created
                manifest["manifest_history_path"] = f"{HISTORY_DIR}/{manifest_id}.json"
                manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                event = {"event": "handoff_prepared", "event_id": uuid.uuid4().hex, "at": created, "actor": actor, "manifest_id": manifest_id,
                         "manifest_sha256": sha256_bytes(manifest_bytes), "manifest_history_path": manifest["manifest_history_path"],
                         "previous_manifest_sha256": previous, "inventory_sha256": manifest["inventory_sha256"], "status": report["status"]}
                line = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                events_new = events_raw + (b"" if not events_raw or events_raw.endswith(b"\n") else b"\n") + line
                _add_bytes(z, manifest["manifest_history_path"], manifest_bytes, None, member=False)
                _add_bytes(z, EVENTS_REL, events_new, None, member=False)
                _add_bytes(z, MANIFEST_REL, manifest_bytes, None, member=False)
            check = verify_package(part)
            if not check["ok"]:
                raise WorkflowError("压缩包做好后自己核对没通过，没有留下这个包：" + "；".join(check["errors"][:3]))
            os.replace(part, zip_path)
        except BaseException:
            if part.exists():
                part.unlink()
            raise
        # ---- 成功之后才动工作区：交接说明和交出记录（只追加）
        note_file = Path(root) / note_rel
        note_file.parent.mkdir(parents=True, exist_ok=True)
        note_file.write_bytes(note_bytes)
        zip_sha = hashlib.sha256()
        with zip_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                zip_sha.update(chunk)
        row = {"schema_version": 1, "event": "handed_over", "handover_id": uuid.uuid4().hex, "kind": "full", "created_at": created, "actor": actor, "words": words,
               "recipient": to, "package_name": zip_name, "package_bytes": zip_path.stat().st_size, "package_sha256": zip_sha.hexdigest(), "manifest_id": manifest_id,
               "inventory_sha256": manifest["inventory_sha256"], "files": len(carried) + 1, "bytes": package["carried"]["bytes"],
               "not_carried": {k: v["files"] for k, v in skip_groups.items()}, "note_path": note_rel, "known_errors": len(problems), "basis": BASIS}
        out = Path(root) / LOG_OUT
        with out.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n"); stream.flush(); os.fsync(stream.fileno())
        size = zip_path.stat().st_size
        forward = forward_text(ident.get("project_name") or root.name, size)
        talk = (f"交接包做好了：{_human(size)}，放在你工作区文件夹的旁边（路径在下面，可以直接复制）。"
                f"带走了 {len(carried) + 1} 个文件，没带 {p['skip']['files']} 个（{_human(p['skip']['bytes'])}，主要是过程文件、暂存和重复副本）；"
                "打包后我已经把包里每个文件核对过一遍，没有损坏。原来的工作区一个文件都没删，只多了一份交接说明和一条交出记录。\n\n"
                f"```\n{zip_path}\n```\n\n把压缩包连同下面这段话一起发给对方：\n\n{forward}")
        return {"status": "packed", "kind": "full", "package_path": str(zip_path), "package_name": zip_name, "package_bytes": size, "package_sha256": row["package_sha256"],
                "manifest_id": manifest_id, "carried": package["carried"], "not_carried": skip_groups, "known_errors": problems, "note_path": note_rel,
                "forward_text": forward, "for_strategist": talk,
                "technical": f"交出记录 {LOG_OUT}；清单在包内 {MANIFEST_REL}；接收核对 project_handoff.py accept --manifest-id {manifest_id}"}


def credential_talk(files, names):
    """像凭据的内容拦下时对策略师说的话。命中的是只追加的记录（.jsonl）时，内容不能改，也不能登记成已作废，所以不叫她“删掉”：
    先作废那个密钥，再联系维护这个工作包的人（不新增登记机制）。"""
    append_only = bool(files) and all(str(f).replace("\\", "/").endswith(".jsonl") for f in files)
    if append_only:
        return (f"这次没有打包：{names}里有像登录凭据的内容，直接交出去不安全，你同意原样交也不行。这是一份只追加的记录，里面的内容不能改，我也没法替你删。"
                "如果里面真有密钥，请先把那个密钥作废（去原来的网站换一把新的）；然后请联系维护这个工作包的人来处理这份记录，处理好之前这份交接先不打包。")
    return (f"这次没有打包：{names}里有像登录凭据的内容，直接交出去不安全，你同意原样交也不行。你可以先把里面的密码或密钥删掉、换成说明文字；"
            "如果你确认那不是密码或密钥，告诉我，我登记成“已确认不是凭据”，然后我再打包。")


def credential_block(problems):
    """检查里有“疑似凭据”类问题：直接拒绝，不接受“原样交”。没有则返回 None。"""
    cred, _ = split_problems(problems)
    if not cred:
        return None
    files = sorted(credential_problem_files(cred))
    names = "、".join(_clean(Path(x).name) or "一份名字是英文的文件" for x in files[:3]) or "有的记录"
    talk = credential_talk(files, names)
    return {"status": "blocked_credentials", "credential_hits": files, "problems": cred,
            "for_strategist": talk,
            "technical": "命中的位置（不回显内容）：" + "；".join(cred[:5]) + "；确认是误报：python3 scripts/wubao_dengji.py register --workspace . --path <路径> --field <字段> "
                         "--words \"<策略师原话>\" --actor <实例>；凭据类问题不接受 --accept-problems"}


def consent_needed(problems, kind="整个工作区"):
    """有非凭据类的检查问题：要她当次明确同意原样交；白话逐类列出是什么问题。"""
    return {"status": "needs_consent", "problems": problems, "problem_kinds": problem_kinds(problems),
            "for_strategist": f"这次没有打包：工作区现在的检查有 {len(problems)} 个没解决的问题，大致是：{kinds_words(problems)}。照这样交出去，对方接手后会看到同样的问题。"
                              "你可以先让我修好再交；或者你同意原样交，请你当次明确说“同意照原样交”，我再打包，并把这些问题写进交接说明。",
            "technical": "问题清单见 problems；同意后加 --accept-problems \"<原话>\" 再运行 pack"}


def forward_text(project_name, size):
    name = _clean(project_name) or "这个项目"
    return (f"我把{name}的整个工作区打成了一个压缩包发给你（{_human(size)}），里面有工作包和到目前为止的全部资料与记录，另有一份“交接说明”写了做到哪了、下一步是什么。\n"
            "接手的做法：在你自己的「Codex」或「Claude」（两款 AI 助手软件）里，先打开你想放这份工作的文件夹，把压缩包拖进对话，对它说“接手这个交接包”。"
            "它会先检查压缩包有没有损坏，再一步一步带你。\n"
            "你也可以自己把压缩包解压到一个新文件夹，用「Codex」或「Claude」打开这个新文件夹，说一句“接手”。\n"
            "飞书、云端大脑、产品线网站这类账号没有放在包里，需要用的话用你自己的账号连一次。\n"
            + CAUTION)


# ---------------- 不解压核对 ----------------

def _safe_member(name):
    return bool(name) and not name.startswith("/") and "\\" not in name and ".." not in name.split("/") and not re.match(r"^[A-Za-z]:", name)


def verify_package(path):
    """不解压核对：路径安全、清单结构与指纹、每个文件的指纹、历史副本与交接事件。返回 {"ok","errors","files","bytes","manifest_id"}。"""
    from handoff_inventory import inventory_digest
    errors, files, total, manifest_id = [], 0, 0, None
    try:
        with zipfile.ZipFile(path) as z:
            listed = [i.filename for i in z.infolist() if not i.is_dir()]
            if len(listed) != len(set(listed)):
                return {"ok": False, "errors": ["压缩包里有重名的文件：" + "、".join(sorted({n for n in listed if listed.count(n) > 1})[:3])], "files": 0, "bytes": 0, "manifest_id": None}
            infos = {i.filename: i for i in z.infolist() if not i.is_dir()}
            bad = [n for n in list(infos) + [i.filename for i in z.infolist() if i.is_dir()] if not _safe_member(n)]
            if bad:
                return {"ok": False, "errors": ["压缩包里有不安全的文件路径（绝对路径或上一级）：" + "、".join(bad[:3])], "files": 0, "bytes": 0, "manifest_id": None}
            if MANIFEST_REL not in infos:
                return {"ok": False, "errors": ["压缩包里没有交接清单，不是用交接功能打的包"], "files": 0, "bytes": 0, "manifest_id": None}
            manifest_bytes = z.read(MANIFEST_REL)
            try:
                manifest = json.loads(manifest_bytes)
            except ValueError:
                return {"ok": False, "errors": ["交接清单读不出来"], "files": 0, "bytes": 0, "manifest_id": None}
            ok = (isinstance(manifest, dict) and manifest.get("schema_version") == 1 and manifest.get("kind") == "project_handoff"
                  and isinstance(manifest.get("manifest_id"), str) and re.fullmatch(r"[0-9a-f]{32}", manifest["manifest_id"])
                  and manifest.get("manifest_history_path") == f"{HISTORY_DIR}/{manifest['manifest_id']}.json"
                  and isinstance(manifest.get("inventory"), list) and inventory_digest(manifest["inventory"]) == manifest.get("inventory_sha256")
                  and isinstance(manifest.get("package"), dict) and isinstance(manifest["package"].get("members"), list))
            if not ok:
                return {"ok": False, "errors": ["交接清单结构、编号或指纹不对"], "files": 0, "bytes": 0, "manifest_id": None}
            manifest_id = manifest["manifest_id"]
            members = {m["name"]: m for m in manifest["package"]["members"]}
            inventory = {m["name"]: m for m in manifest["inventory"]}
            special = {MANIFEST_REL, EVENTS_REL, manifest["manifest_history_path"]}
            for name in sorted(set(infos) - set(members) - special):
                errors.append(f"包里有清单没列的文件：{name}")
            for name, item in sorted({**inventory, **members}.items()):
                if name in special:
                    continue
                if name not in infos:
                    errors.append(f"清单里有、包里缺的文件：{name}")
                    continue
                digest, size = hashlib.sha256(), 0
                with z.open(infos[name]) as stream:
                    for chunk in iter(lambda: stream.read(1 << 20), b""):
                        digest.update(chunk); size += len(chunk)
                files += 1; total += size
                if digest.hexdigest() != item["sha256"] or size != item["size"]:
                    errors.append(f"文件内容和清单对不上（被改过或损坏）：{name}")
                if name in inventory and name in members and inventory[name]["sha256"] != members[name]["sha256"]:
                    errors.append(f"清单两处写的指纹不一致：{name}")
            if manifest["manifest_history_path"] not in infos or z.read(manifest["manifest_history_path"]) != manifest_bytes:
                errors.append("清单的历史副本缺失或与清单不一致")
            events = []
            if EVENTS_REL in infos:
                for line in z.read(EVENTS_REL).decode("utf-8", errors="replace").splitlines():
                    if line.strip():
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            errors.append("交接事件记录读不出来")
            last = [e for e in events if isinstance(e, dict) and e.get("event") == "handoff_prepared" and e.get("manifest_id") == manifest_id]
            if not last or last[-1].get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
                errors.append("交接事件里没有和清单对得上的准备记录")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return {"ok": False, "errors": [f"压缩包打不开或已损坏：{str(exc)[:120]}"], "files": files, "bytes": total, "manifest_id": manifest_id}
    return {"ok": not errors, "errors": errors, "files": files, "bytes": total, "manifest_id": manifest_id}


# ---------------- 交出记录（续做状态用） ----------------

def handed_over(root):
    path = Path(root) / LOG_OUT
    if not path.is_file():
        return []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict) and row.get("event") == "handed_over":
                    rows.append(row)
    except (OSError, ValueError):
        return [{"at": None, "kind": "unreadable", "recipient": None, "package_name": None,
                 "for_strategist": "交出记录读不出来（project/records/handoff-out.jsonl 坏了），所以我没法告诉你这里有没有交出去过；先把这份记录恢复好，再判断要不要重新交接。"}]
    out = []
    for row in reversed(rows):
        try:
            when = datetime.fromisoformat(row["created_at"]).astimezone()
            date = f"{when.month} 月 {when.day} 日"
        except (KeyError, ValueError, TypeError):
            date = "之前"
        who = row.get("recipient") or "别人"
        if row.get("kind") == "task":
            title = row.get("task_title") or "一个任务"
            out.append({"at": row.get("created_at"), "kind": "task", "task_id": row.get("task_id"), "recipient": row.get("recipient"), "package_name": row.get("package_name"),
                        "for_strategist": f"任务「{title}」已经交出去了（{date}交给了{who}）：交出之后你在这里改的这个任务，对方看不到；要让对方拿到新进展，需要再交接一次这个任务。"})
            continue
        what = "整个工作区"
        out.append({"at": row.get("created_at"), "kind": row.get("kind"), "recipient": row.get("recipient"), "package_name": row.get("package_name"),
                    "for_strategist": f"这份已经交出去了（{date}把{what}交给了{who}）：交出之后你在这里改的东西，对方看不到；要让对方拿到新进展，需要再交接一次。"})
    return out


def main():
    parser = argparse.ArgumentParser(description="交接打包（v1.7.8）：preview 先给看，策略师回“好”之后 pack；verify 不解压核对压缩包")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "pack"):
        s = sub.add_parser(name)
        s.add_argument("--workspace", type=Path, required=True)
        s.add_argument("--kind", choices=["full", "task"], default="full")
        s.add_argument("--task", help="任务交接：要交的任务编号（不写就列出任务让她选）")
        s.add_argument("--include", action="append", default=[], metavar="路径", help="策略师点名要带的（project/ 开头，可重复）：过程、暂存、重复副本、看名字像过程的")
        s.add_argument("--exclude", action="append", default=[], metavar="路径", help="策略师点名不带的（project/ 开头，可重复）：成品、来源、拿不准的、没挂任务的大文件夹（只用于全量交接，任务交接不支持）")
        s.add_argument("--json", action="store_true")
        if name == "preview":
            s.add_argument("--list", action="store_true", help="把每个不带的文件都列出来（真实项目可能上千个，默认只给个数和最大几项）")
        if name == "pack":
            s.add_argument("--words", default="", help="策略师看过预览后当次回复的原话")
            s.add_argument("--actor", required=True)
            s.add_argument("--to", help="接手人（可不填）")
            s.add_argument("--accept-problems", help="工作区检查有没解决的问题时，策略师同意原样交的当次原话")
    rt = sub.add_parser("register-task", help="没登记成任务的成果，策略师同意后补登记成一个独立任务（原话记录）")
    rt.add_argument("--workspace", type=Path, required=True)
    rt.add_argument("--dir", required=True, help="project/ 开头的文件夹")
    rt.add_argument("--title", required=True)
    rt.add_argument("--words", required=True, help="策略师同意登记的当次原话")
    rt.add_argument("--actor", required=True)
    rt.add_argument("--json", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--package", type=Path, required=True)
    v.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "verify":
        out = verify_package(args.package)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["ok"] else 1
    if args.command == "register-task":
        import jiaojie_renwu
        print(json.dumps(jiaojie_renwu.register_task(args.workspace, args.dir, args.title, args.words, args.actor), ensure_ascii=False, indent=2))
        return 0
    if args.command == "preview":
        out = preview(args.workspace, args.kind, args.include, args.exclude, args.list, args.task)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    out = pack(args.workspace, args.kind, args.words, args.actor, args.include, args.exclude, args.to, args.accept_problems, args.task)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["status"] in {"packed", "formal_task", "choose_task", "unknown_task"} else 1


if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main

    def main():
        return cli(_business_main, __file__)

    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
