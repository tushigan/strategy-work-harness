#!/usr/bin/env python3
"""接手引导（v1.7.8 J04，N5）：接手人在自己选的工作区里说“接手”，AI 一步一步带他，每一步说清在做什么、为什么停下、他要选什么。

三个命令（都输出 JSON，`for_strategist` 是要原样转述给接手人的白话）：
  find     --workspace . [--package <压缩包>]                         找交接包：先用他给的路径；没给就在当前文件夹与上一级找，多于一个列出来问；找不到请他把压缩包放进当前文件夹
  guide    --workspace . [--package <压缩包>]                         只读：核对压缩包、看清包的类型和当前文件夹的情况，告诉他现在是哪种情况、有哪几个选择（options）；**不写任何文件**
  receive  --workspace . --package <压缩包> --choice <选项> --words "<他的原话>" --actor <实例> [--name 称呼] [--from-who 谁交给他的]
                                                                       他选了之后才动手；做完用三句话报“现在到哪了 / 要你做什么 / 下一步”，记下谁、哪天、从谁那里接手，提醒平台账号用自己的连
  memory-choose --workspace . --import-id <导入编号> --numbers 1,3 --words "<他的原话>" --actor <实例>
                                                                       同一客户已有项目记忆时，接手结果逐条列了“包里这样说 / 你这里这样说”；他说“第 N 条用包里的”，
                                                                       就用现有的项目记忆登记追加一条（状态待确认、记他的原话），他原来的记忆一个字节不改

状况（guide 返回的 situation）与选项（choice）：
  找不到 / 多个            not_found / choose_package                  请他放进当前文件夹 / 点名是哪一个
  包坏了或被改过           package_damaged（refuse）                   拒绝：不解开、不写文件，请他找原来的人重要一份
  看不懂的包               unsupported_package（refuse）
  全量包
    当前文件夹是空的       full_empty        full_here（解到这里，推荐）/ full_beside（解到旁边）
    有别的东西（含别的交接压缩包）、不是工作区   full_other        full_here（在里面新建一个子文件夹放，原有的东西一个字节不动，推荐）/ full_beside（解到旁边）
    已经是工作区（有案子或空白） full_mixing    full_beside（解到旁边新文件夹，推荐）/ full_here（他坚持才按他说的做：放进工作区会盖掉已有的文件，绝不这么做，如实告诉他再解到旁边）
    已经解压在这里          full_unzipped_here accept_here（直接核对并接手）
  任务包
    当前文件夹没装工作包    install_first（info）                       先装同版本工作包，给“安装”那一句话；或请对方改发全量交接
    工作包版本比包旧        upgrade_first（info）                       先升级（给那一句话）再接；接收方比包新则说明后照常接
    同一客户 / 空白 / 旧版  ready             import（他说“接手”）
    比对不了是不是同一客户   identity_unknown  identity_same（他说就是这个客户）/ new_workspace / switch
    不同客户                different_client  new_workspace（在旁边新开那个客户的工作区，推荐）/ switch（换到别的工作区再接手）/ mix_here（他坚持放在这里，回原话；两个客户的资料会混在一起，项目记忆不合并）
    同编号任务两边各改过     task_diverged     fork_keep_mine（保留你这边，不导入，包留备查）/ fork_save_new（另存为新任务）
    已经接过 / 已有更新      already_imported / nothing_to_import
    其余要停下的（文件冲突、请求编号冲突、你这边本来就有没解决的问题）blocked
未选之前不写任何文件。每个写入的选项都要他当次的原话（--words）。

接好之后：核对（全量包 accept、任务包 apply 之后续做状态 errors 必须为空）→ 追加接手记录 project/records/handoff-received.jsonl（谁、哪天、从谁那里接手、原话、选项）→ 读交接说明 → 三句话。
压缩包如果就放在要用的工作区根目录，会被挪到文件夹旁边（不删）：它留在根目录会让续做状态报“根目录有未归档文件”，全量包也过不了 accept。
只复制工作包本身的新工作区（new_workspace）：按发行清单复制 project/ 以外的工作包文件，project/ 重建成空白（空白状态由 BLANK_STATE 给出，用例对照模板防漂移），不复制项目资料与记录。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from phase2_store import WorkflowError

RECEIVED_LOG = "project/records/handoff-received.jsonl"
BASIS = "J04 接手引导"
INSTALL_REPO = "https://github.com/tushigan/strategy-work-harness"
PLATFORM_REMINDER = "飞书、云端大脑、产品线网站这类账号都没有随交接包带过来，要用的话用你自己的账号连一次；比如要用产品线网站，对我说“帮我连产品线网站”，我会一步一步带你做。"
BLANK_STATE = {
    "schema_version": "0.1", "project_id": None, "project_name": None, "client_name": None, "brand_name": None, "status": "not_initialized",
    "current_stage": "project_setup", "next_action": "上传合同服务列项文件，并先确认识别结果",
    "task_status": {"status": "not_started", "current_task_id": None, "completed_task_ids": []},
    "artifact_status": {"status": "not_started", "current_artifact_id": None, "latest_artifact_version": None},
    "machine_review": {"status": "not_started", "latest_review_id": None},
    "human_confirmation": {"status": "not_requested", "latest_confirmation_id": None},
    "client_confirmation": {"status": "not_requested", "latest_confirmation_id": None},
    "system_import": {"status": "not_applicable", "latest_import_id": None},
    "integrations": {"feishu": {"status": "not_configured", "scope": "当前项目", "last_checked": None},
                     "cloud_brain": {"status": "not_configured", "scope": "当前项目硬连接；跨项目只取方法论软连接", "last_checked": None}},
    "references": [], "last_updated": None,
    "phase2": {"contract": {"status": "not_started"}, "plan": {"status": "not_started"}, "gantt": {"status": "not_started"}, "system_import": "not_applicable"},
}
BLANK_JSON = {"project/records/revision-counts.json": {"schema_version": "0.1", "automatic_revision_rounds": {}, "director_rework_rounds": {}},
              "project/records/dependencies.json": {"schema_version": "0.1", "dependencies": []}}


def _d():
    import jiaojie_dabao as module
    return module


def _j():
    import jiaojie_jieshou as module
    return module


def _human(size):
    return _d()._human(size)


def _clean(text):
    return _d()._clean(text)


def _now():
    return datetime.now().astimezone().isoformat()


def _date_words(iso=None):
    return _d()._date_words(iso)


def _is_handoff_zip(path):
    return path.is_file() and path.suffix.lower() == ".zip" and "交接" in path.name


def _fmt_time(path):
    when = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return f"{when.month} 月 {when.day} 日 {when:%H:%M}"


def _wrap(status, situation, talk, options=None, written=False, **extra):
    out = {"status": status, "situation": situation, "for_strategist": talk, "options": options or [], "written": written}
    out.update(extra)
    return out


def _opt(key, label, recommended=False, needs_words=True):
    return {"key": key, "label": label, "recommended": recommended, "needs_words": needs_words}


# ---------------- 找包 ----------------

def _received_ids(root):
    """这个工作区已经接过的包（清单编号）：导入事件 + 接手记录。"""
    out = set()
    for rel in (_j().IMPORT_LOG, RECEIVED_LOG):
        path = Path(root) / rel
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            pkg = row.get("package") or row.get("declined_package") or {}
            if isinstance(pkg, dict) and pkg.get("manifest_id"):
                out.add(pkg["manifest_id"])
    return out


def _manifest_id_of(path):
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read(_d().MANIFEST_REL)).get("manifest_id")
    except Exception:
        return None


def find_package(workspace, package=None):
    """找交接包。返回 {"status": found / choose_package / none, ...}；只读。"""
    workspace = Path(workspace).expanduser().resolve()
    stop = _d().version_stop(workspace)                       # J14：用新版脚本去跑旧版工作区（或反过来）先停下
    if stop:
        return stop
    if package:
        path = Path(package).expanduser()
        path = path if path.is_absolute() else Path.cwd() / path
        if path.is_file():
            return {"status": "found", "package": str(path.resolve()), "for_strategist": "", "written": False}
        return {"status": "none", "searched": [str(path)], "written": False,
                "for_strategist": "你给我的这个位置上我没找到压缩包。请你确认一下它放在哪；最省事的做法是把压缩包直接放进你现在打开的这个文件夹，然后再对我说一次“接手”。"}
    seen, found = set(), []
    for where, folder in (("这个文件夹", workspace), ("上一级文件夹", workspace.parent)):
        if not folder.is_dir():
            continue
        for p in sorted(folder.iterdir()):
            if _is_handoff_zip(p) and p.resolve() not in seen:
                seen.add(p.resolve())
                found.append((where, p))
    received = _received_ids(workspace) if (workspace / "project").is_dir() else set()
    items, same_ones = [], set()
    for where, p in sorted(found, key=lambda x: (x[0] != "这个文件夹", -x[1].stat().st_mtime)):
        mid = _manifest_id_of(p)
        if mid and (mid, p.stat().st_size) in same_ones:
            continue                                       # 同一个包放了两处（这个文件夹和上一级各一份）：只算一个，优先当前文件夹里的
        same_ones.add((mid, p.stat().st_size))
        done = mid in received if received else False
        items.append({"package": str(p.resolve()), "name": p.name, "bytes": p.stat().st_size, "where": where, "modified": _fmt_time(p), "already_received": bool(done)})
    fresh = [x for x in items if not x["already_received"]]
    pool = fresh or items
    if not pool:
        return {"status": "none", "searched": [str(workspace), str(workspace.parent)], "written": False,
                "for_strategist": "我在你现在打开的文件夹和它的上一级里没找到交接包（名字里带“交接”的压缩包）。请你把收到的压缩包放进现在这个文件夹里，然后再对我说一次“接手”；"
                                  "如果它放在别处，把它所在的位置告诉我也行。"}
    if len(pool) == 1:
        return {"status": "found", "package": pool[0]["package"], "packages": items, "for_strategist": "", "written": False}
    lines = "\n".join(f"- 「{Path(x['name']).stem}」，{_human(x['bytes'])}，{x['modified']}，在{x['where']}" + ("（这个已经接过了）" if x["already_received"] else "") for x in pool)
    return {"status": "choose_package", "packages": pool, "written": False,
            "for_strategist": "我找到了不止一个交接包，是哪一个？\n" + lines + "\n告诉我是哪一个（说名字或者“最新的那个”都行），我再继续；在你选之前我不动任何文件。"}


# ---------------- 看清包 ----------------

def inspect_package(package):
    """不解压核对压缩包，读出类型、版本、身份。返回 dict；ok=False 时 situation 是 package_damaged 或 unsupported_package。"""
    D, J = _d(), _j()
    package = Path(package)
    v = D.verify_package(package)
    if not v["ok"]:
        unsupported = any("不是用交接功能打的包" in e for e in v["errors"])
        return {"ok": False, "situation": "unsupported_package" if unsupported else "package_damaged", "errors": v["errors"]}
    with zipfile.ZipFile(package) as z:
        manifest = json.loads(z.read(D.MANIFEST_REL))
        names = set(z.namelist())
        pkg = manifest.get("package") or {}
        kind = pkg.get("kind")
        task = None
        if J._r().TASK_MANIFEST in names:
            try:
                task = json.loads(z.read(J._r().TASK_MANIFEST))
            except ValueError:
                task = None
            kind = "task"
        events = []
        if D.EVENTS_REL in names:
            for line in z.read(D.EVENTS_REL).decode("utf-8", errors="replace").splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        prepared = [e for e in events if isinstance(e, dict) and e.get("event") == "handoff_prepared" and e.get("manifest_id") == manifest["manifest_id"]]
    if kind not in ("full", "task"):
        return {"ok": False, "situation": "unsupported_package", "errors": ["交接清单里没有写明是全量还是任务"]}
    ident = (task or {}).get("identity") if task else pkg.get("identity")
    return {"ok": True, "kind": kind, "manifest_id": manifest["manifest_id"], "package_version": (task or {}).get("package_version") or pkg.get("package_version"),
            "identity": ident or {}, "recipient": pkg.get("recipient"), "from_actor": (prepared[-1].get("actor") if prepared else None) or (task or {}).get("actor"),
            "created_at": pkg.get("created_at") or (prepared[-1].get("at") if prepared else None), "note_path": pkg.get("note_path"),
            "task_id": (task or {}).get("task_id"), "task_title": (task or {}).get("title"), "bytes": package.stat().st_size, "files": v["files"], "name": package.name,
            "sha256": J._sha_file(package)}


def _project_label(info, fallback="这个项目"):
    """项目名（原样，用来给文件夹起名）。"""
    ident = info.get("identity") or {}
    return str(ident.get("project_name") or ident.get("client_name") or "").strip() or fallback


def _project_words(info):
    """项目名放进给接手人的话里（带引号；名字里有英文编号这类说人话扫描会报的就说“这个项目”）。"""
    return _clean(_project_label(info, "")) or "这个项目"


def classify_folder(root, package=None):
    """当前文件夹是什么情况：empty 空的 / other_folder 有别的东西但没装工作包 / workspace_blank 装好了还没开始 / workspace_project 装好了而且已经有案子。"""
    D, J = _d(), _j()
    root = Path(root).resolve()
    if not root.is_dir():
        return {"kind": "missing", "count": 0}
    own = Path(package).resolve() if package else None
    entries = [p for p in root.iterdir() if p.name not in {".DS_Store"} and not (own and p.resolve() == own)]       # 别的交接压缩包也算“有别的东西”（它会让接收核对报根目录有未归档文件）
    try:
        D._require_workspace(root)
        installed = True
    except WorkflowError:
        installed = False
    if not installed:
        return {"kind": "empty" if not entries else "other_folder", "count": len(entries)}
    try:
        from standalone_store import history
        events, _ = history(root)
    except Exception:
        events = [None]
    ident = D._identity(root)
    blank = not any(ident.values()) and not events
    return {"kind": "workspace_blank" if blank else "workspace_project", "count": len(entries), "identity": ident, "version": D._package_version(root)}


def _unzipped_here(root):
    """当前文件夹本身就是一份解压好的全量交接包（有交接清单、还没接过）。返回 manifest_id 或 None。"""
    D = _d()
    try:
        manifest = json.loads((Path(root) / D.MANIFEST_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pkg = manifest.get("package") if isinstance(manifest, dict) else None
    if not isinstance(pkg, dict) or pkg.get("kind") != "full" or not manifest.get("manifest_id"):
        return None
    return manifest["manifest_id"]


# ---------------- 引导（只读） ----------------

def _ver_words(version):
    match = re.search(r"v?(\d+\.\d+\.\d+)", str(version or ""))
    return f"第 {match.group(1)} 版" if match else ""


def install_text(version):
    ver = f"（交接包里的工作包是{_ver_words(version)}）" if _ver_words(version) else ""
    return (f"要先装好和交接包同一个版本的策略师工作包{ver}。把下面这一段发给你的 AI 助手，装到你想放这份工作的文件夹里：\n\n```\n"
            f"请从 {INSTALL_REPO} 安装或更新“策略师工作包”。\n"
            "先读取仓库根目录的《给Agent的安装更新提示词.md》和《版本清单.json》，再按其中流程执行。\n"
            "我要装到的目录：【写上你想放的文件夹路径】\n"
            "已有工作区先备份再更新，project/ 里的业务资料一个文件都不能改、删、移。装完告诉我当前版本和检查结果。\n```\n\n"
            "装好之后，再把交接包拖进对话，对它说一次“接手”。")


def guide(workspace, package=None):
    """只读：找包、核对、看清情况，返回状况和选项。不写任何文件。"""
    out = _guide(workspace, package)
    if out.get("options") and not out.get("technical"):
        keys = " / ".join(o["key"] for o in out["options"])
        out["technical"] = f"他选定后运行：python3 scripts/jiaojie_yindao.py receive --workspace . --package <压缩包> --choice <{keys}> --words \"<他当次原话>\" --actor <实例> [--name <称呼>] [--from-who <谁交给他的>] --json"
    return out


def _guide(workspace, package=None):
    D, J = _d(), _j()
    root = Path(workspace).expanduser().resolve()
    found = find_package(root, package)
    if found["status"] == "version_mismatch":
        return _wrap("refuse", "version_mismatch", found["for_strategist"], technical=found.get("technical"))
    unzipped = _unzipped_here(root)
    if found["status"] == "none" and unzipped:
        if unzipped in _received_ids(root):
            return _wrap("info", "already_imported", "这份交接包你已经接过了，不用再接一次。要看现在到哪了，我可以直接带你看进度。")
        return _guide_unzipped(root, unzipped, None)
    if found["status"] != "found":
        return _wrap("ask" if found["status"] == "choose_package" else "not_found", "choose_package" if found["status"] == "choose_package" else "not_found",
                     found["for_strategist"], packages=found.get("packages", []))
    path = found["package"]
    info = inspect_package(path)
    if not info["ok"]:
        if info["situation"] == "package_damaged":
            talk = "这个压缩包坏了，或者被人改动过，我不敢把它解开，也没有动你的任何文件。请你找当初交给你的人，让他重新发一份。"
        else:
            talk = "这个压缩包不是用交接功能做的，我看不懂，也没有动你的任何文件。请确认发给你的是交接包；不确定的话问一下交出的人。"
        return _wrap("refuse", info["situation"], talk, package=path, errors=info["errors"][:3])
    if unzipped and unzipped == info["manifest_id"]:
        if unzipped in _received_ids(root):
            return _wrap("info", "already_imported", "这份交接包你已经接过了，不用再接一次。要看现在到哪了，我可以直接带你看进度。", package=path)
        return _guide_unzipped(root, unzipped, info, path)
    folder = classify_folder(root, path)
    if info["kind"] == "full":
        return _guide_full(root, path, info, folder)
    return _guide_task(root, path, info, folder)


def _guide_unzipped(root, manifest_id, info, path=None):
    talk = ("我看到你现在打开的文件夹，本身就是一份解压好的交接包（一整间办公室）。我先核对每个文件有没有被改动，没问题就带你看现在到哪了。"
            "接手前问你一句：怎么称呼你？（不说就记“接手人”）这份是谁交给你的？（不说也行）确认的话，请你说“接手”。")
    return _wrap("ready", "full_unzipped_here", talk, [_opt("accept_here", "核对并接手", True)], package=path, manifest_id=manifest_id, ask_name=True)


def _guide_full(root, path, info, folder):
    name = _project_words(info)
    base = f"这是一个全量交接包：{name}的整间办公室（{_human(info['bytes'])}，{info['files']} 个文件），里面有工作包本身和这个项目到目前为止的资料与记录。"
    if folder["kind"] == "empty":
        talk = (base + "你现在打开的文件夹是空的，我可以把它解开放在这里，也可以解开放在这个文件夹旁边的一个新文件夹里。你想放哪里？"
                "（我会先核对压缩包没有损坏、解开后每个文件都对得上，再带你看现在到哪了。）")
        return _wrap("ask", "full_empty", talk, [_opt("full_here", "解到现在这个文件夹", True), _opt("full_beside", "解到旁边一个新文件夹")], package=path, ask_name=True)
    if folder["kind"] == "other_folder":
        talk = (base + f"你现在打开的文件夹里已经有 {folder['count']} 样别的东西（它还不是工作区）。整间办公室直接铺在这里，会和它们混在一起，所以有两个办法："
                "在这个文件夹里面新建一个子文件夹放，不动你原来的任何东西；或者解到这个文件夹旁边的一个新文件夹。想放哪里？解好之后，你以后就在放它的那个文件夹里打开对话。")
        return _wrap("ask", "full_other", talk, [_opt("full_here", "放在现在这个文件夹里（新建一个子文件夹）", True), _opt("full_beside", "解到旁边新文件夹")], package=path, ask_name=True)
    what = "你自己的案子" if folder["kind"] == "workspace_project" else "一个装好了工作包、还没开始的工作区"
    talk = (base + f"但你现在打开的文件夹里已经有{what}，把这一整间办公室放进来，会和你现在的东西混在一起。"
            "我建议解到旁边一个新文件夹，不动你现在的任何东西；解好之后，你以后就在那个新文件夹里打开对话。要这样吗？"
            "如果你坚持放在现在这个文件夹，也可以告诉我；不过放进工作区里会盖掉已有的文件，我不会这么做，会如实告诉你，再回到解到旁边。")
    return _wrap("ask", "full_mixing", talk, [_opt("full_beside", "解到旁边新文件夹（推荐）", True), _opt("full_here", "坚持放在现在这个文件夹")], package=path, ask_name=True)


def _guide_task(root, path, info, folder):
    J = _j()
    title = _clean(info.get("task_title") or "") or "一个任务"           # 带引号的名字，或“一个任务”
    base_ver = info.get("package_version")
    if folder["kind"] in ("empty", "other_folder", "missing"):
        talk = (f"这是一个任务交接包：只有{title}这一个任务的资料和记录，不是整个工作区。你现在打开的文件夹里还没有装策略师工作包，没法直接接。"
                "有两个办法：一是先装好同版本的工作包再接；二是请交出的人改发“全量交接”（整间办公室一起发，你解开就能用）。我先不动你的任何文件。\n\n" + install_text(base_ver))
        return _wrap("info", "install_first", talk, [_opt("install_first", "先装工作包再接", True, False), _opt("ask_full", "请对方改发全量交接", False, False)], package=path)
    result = J.check(root, path)
    sit = result["situation"]
    if sit == "version_differs":
        ver = (result.get("version") or {})
        talk = (f"这个任务交接包是用更新版本的工作包（{_ver_words(ver.get('package') or base_ver) or '较新的版本'}）做的，你这里的是旧一点的版本（{_ver_words(ver.get('receiver') or folder.get('version')) or '较旧的版本'}）。"
                "格式可能对不上，所以我先不接，也没有动你的任何文件。请先把你的工作包升级到同一个版本，再来接手。把下面这一段发给你的 AI 助手：\n\n```\n"
                f"请从 {INSTALL_REPO} 更新“策略师工作包”。\n"
                "先读取仓库根目录的《给Agent的安装更新提示词.md》和《版本清单.json》，再按其中流程执行；已有工作区先备份再更新，project/ 里的业务资料一个文件都不能改、删、移。\n"
                "我的工作区目录：【写上这个工作区的文件夹路径】\n```\n\n升级好之后，再对我说一次“接手”。")
        return _wrap("info", "upgrade_first", talk, [_opt("upgrade_first", "先升级再接", True, False)], package=path, version=ver)
    if sit in ("same_client", "blank", "blank_no_identity", "append_newer"):
        ver = (result.get("summary") or {}).get("version") or {}
        newer = ver.get("relation") == "receiver_newer"
        talk = result["for_strategist"].replace("确认要接手的话，请你说“接手”。", "")
        if newer:
            talk += (f"另外，你这里的工作包（{_ver_words(ver.get('receiver'))}）比交接包里的（{_ver_words(ver.get('package'))}）更新一点，记录格式对得上，我照常接；接完我会再检查一遍，有问题马上告诉你。")
        talk += "接手前问你一句：怎么称呼你？（不说就记“接手人”）这个包是谁交给你的？（不说也行）确认的话，请你说“接手”。"
        return _wrap("ready", sit, talk, [_opt("import", "接手", True)], package=path, summary=result.get("summary"), ask_name=True, receiver_newer=newer)
    if sit in ("already_imported", "nothing_to_import"):
        return _wrap("info", sit, result["for_strategist"], package=path)
    if sit == "different_client":
        theirs, mine = info["identity"], folder.get("identity") or {}
        a = _clean(theirs.get("client_name") or theirs.get("project_name") or "") or "另一个客户"
        b = _clean(mine.get("client_name") or mine.get("project_name") or "") or "现在这个客户"
        talk = (f"这个任务（{title}）属于{a}，而你现在打开的是{b}的工作区，两边对不上。我先不动任何文件，你选一个：\n"
                f"1. 在旁边新开一个{a}的工作区，把任务放进去（推荐）。我会用你已经装好的工作包复制一份新的，只复制工作包本身，不复制你现在这个项目的任何资料和记录；做好后你以后在那个新文件夹里打开对话。\n"
                "2. 你换到别的工作区再来接手，我这次什么都不做。\n"
                f"3. 坚持放在现在这个工作区。要先告诉你后果：两个客户的资料会混在一起，也容易发错给客户；而且{a}那边的项目记忆我不会合并进来，只留一份备查。你坚持的话，请用自己的话回我一句，我会把它记下来。")
        options = [_opt("new_workspace", f"在旁边新开{a}的工作区（推荐）", True), _opt("switch", "换到别的工作区再接手", False, False), _opt("mix_here", "坚持放在现在这个工作区")]
        probe = J.check(root, path, "mixed")                 # 第 3 种做得成吗？（只读试一遍）
        if probe["status"] != "ready":
            options = options[:2]
            talk = talk.split("3. 坚持放在现在这个工作区")[0] + "第 3 种（放在现在这个工作区）这次做不成：" + _mix_blocked_words(probe) + "所以只能选 1 或 2。"
        return _wrap("ask", sit, talk, options, package=path, ask_name=True, package_identity=theirs, workspace_identity=mine)
    if sit == "identity_unknown":
        talk = (f"这个任务（{title}）我比对不了它是不是属于你现在这个客户项目：包里或你这个工作区没有写客户项目身份。我先不动任何文件，请你告诉我是哪一种：\n"
                "1. 它就是你现在这个客户项目的任务——请你明确说一句，我会把你的话记下来，再放进来。\n"
                "2. 它是另一个客户的——我在旁边新开一个空白工作区放进去，你以后在新文件夹里打开对话。\n"
                "3. 你不确定——换到别的工作区再接手，或者先问问交出的人。")
        options = [_opt("identity_same", "就是现在这个客户项目的"), _opt("new_workspace", "是另一个客户的，在旁边新开工作区"), _opt("switch", "我不确定，先不接", False, False)]
        probe = J.check(root, path, "confirmed_same")
        if probe["status"] != "ready":
            options = options[1:]
            talk = talk.split("1. 它就是你现在这个客户项目的任务")[0] + "第 1 种（它就是你现在这个客户项目的任务）这次做不成：" + _mix_blocked_words(probe) + "所以只能选下面两种：\n2. " + talk.split("2. ", 1)[1]
        return _wrap("ask", sit, talk, options, package=path, ask_name=True)
    if sit == "task_diverged":
        talk = (f"你这里已经有一个编号相同的任务（{title}），而且两边各自改过，内容不一样。我不会盖掉任何一边，请你选：\n"
                "1. 保留你这边的，这个包不导入（压缩包我会放在旁边留作备查，我会记下你这个决定）。\n"
                "2. 把包里的这个任务另存为一个新任务：新编号，包里的成品和来源放进一份只读副本供你参考，你这边原来的任务不动。")
        return _wrap("ask", sit, talk, [_opt("fork_keep_mine", "保留我这边的，不导入"), _opt("fork_save_new", "另存为新任务")], package=path, ask_name=True)
    return _wrap("blocked", sit, result["for_strategist"], package=path, technical=result.get("technical"))


def _mix_blocked_words(probe):
    sit = probe.get("situation")
    if sit == "file_conflict":
        return f"包里有 {probe.get('count') or len(probe.get('paths') or [])} 个文件和你这边同一个位置上已有的文件内容不同，放进来会盖掉你的文件，我不会这么做。"
    if sit == "task_diverged":
        return "你这边已经有编号相同、内容不一样的任务，放进来会盖掉其中一边。"
    return probe.get("for_strategist", "") 


# ---------------- 动手 ----------------

def _safe_name(value):
    value = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "", str(value or "")).strip(" .")
    return value or "接手的工作区"


def _unique_dir(parent, base):
    parent = Path(parent)
    dest, n = parent / base, 1
    while dest.exists():
        n += 1
        dest = parent / f"{base}-{n}"
    return dest


def _extract(package, dest, overwrite_ok_if_same=True):
    """解压全量包到 dest。绝不覆盖已有的文件：同路径内容一样的跳过，内容不一样的先全部查出来，有就一个都不写。返回 (写了几个, 冲突列表)。"""
    J = _j()
    dest = Path(dest)
    with zipfile.ZipFile(package) as z:
        infos = [i for i in z.infolist()]
        conflicts = []
        for i in infos:
            if i.is_dir():
                continue
            target = dest / i.filename
            if target.exists() or target.is_symlink():
                if target.is_file() and not target.is_symlink() and overwrite_ok_if_same and J._sha_file(target) == _zip_member_sha(z, i):
                    continue
                conflicts.append(i.filename)
        if conflicts:
            return 0, conflicts
        written = 0
        for i in infos:
            target = dest / i.filename
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".writing-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as out, z.open(i) as source:
                    for chunk in iter(lambda: source.read(1 << 20), b""):
                        out.write(chunk)
                    out.flush(); os.fsync(out.fileno())
                mode = (i.external_attr >> 16) & 0o777
                os.chmod(tmp, 0o755 if mode & 0o111 else 0o644)
                os.replace(tmp, target)
            finally:
                Path(tmp).unlink(missing_ok=True)
            written += 1
    return written, []


class _ExtractConflict(Exception):
    def __init__(self, conflicts):
        super().__init__("解压位置已有同名文件")
        self.conflicts = conflicts


def _extract_blocked(conflicts):
    names = "、".join(_clean(Path(x).name) or "一份名字是英文的文件" for x in conflicts[:3])
    return _wrap("blocked", "extract_conflict", f"没有解开：这个文件夹里已经有同名但内容不同的文件（{names}{'等' if len(conflicts) > 3 else ''}，共 {len(conflicts)} 个），"
                 "解开会盖掉它们，所以我一个文件都没写。请换一个空文件夹，或者选“解到旁边新文件夹”。", written=False, conflicts=conflicts[:10])


def _zip_member_sha(z, info):
    import hashlib
    h = hashlib.sha256()
    with z.open(info) as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _child(root, script, *args):
    """用那个工作区自己的脚本跑命令。返回 (退出码, JSON 或原文)。"""
    env = {**os.environ, "HARNESS_DIAGNOSTICS": os.environ.get("HARNESS_DIAGNOSTICS", "0")}
    r = subprocess.run([sys.executable, "-B", str(Path(root) / "scripts" / script), *args], cwd=root, capture_output=True, text=True, env=env)
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:
        return r.returncode, (r.stderr + r.stdout)[-600:]


def _resume_errors(root):
    code, out = _child(root, "project_handoff.py", "resume", "--workspace", ".", "--json")
    if isinstance(out, dict):
        return list(dict.fromkeys([*out.get("errors", []), *(out.get("report", {}) or {}).get("errors", [])]))
    return [f"续做状态没能读出来：{str(out)[:120]}"]


def _note_sections(text):
    """交接说明“给接手人”一半里的各段：{标题: [要点…]}（列表项一条一个，普通段落一条）。"""
    body = text.split("## 技术附录")[0]
    out = {}
    for match in re.finditer(r"\*\*(.+?)\*\*：(.*?)(?=\n\*\*.+?\*\*：|\Z)", body, re.S):
        items = []
        for raw in match.group(2).splitlines():
            line = re.sub(r"^\s*[-*]\s*", "", raw).strip()
            if line:
                items.append(line.rstrip("。；;"))
        out[match.group(1).strip()] = items
    return out


def _brief(items, limit, width=80):
    """要点列表压成一句：行末是冒号的小标题并进下一条；每条最多 width 个字，超过的截断；只取前 limit 条，其余让他看交接说明。"""
    merged = []
    for item in items:
        if merged and merged[-1].endswith(("：", ":")):
            merged[-1] = merged[-1] + item
        else:
            merged.append(item)
    cut = [x if len(x) <= width else x[:width].rstrip("，、；：,; ") + "……" for x in merged[:limit]]
    return "；".join(cut) + ("（其余的在交接说明里）" if len(merged) > limit else "")


def three_sentences(note_text):
    """读交接说明，用三句话报现在到哪了 / 要你做什么 / 下一步。每句只取前几条要点、每条只留开头，其余让他看交接说明。
    旧版本做的交接说明里可能还带着任务编号、版本号写法：读出来的要点先去掉这些再念给他。"""
    D = _d()
    names = {}
    for line in (note_text or "").splitlines():                       # 技术附录里的“任务编号对照”：编号=任务名；只替换这里写到的编号
        if "任务编号对照" in line and "：" in line:
            for pair in line.split("：", 1)[1].split("；"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if k.strip() and v.strip():
                        names[k.strip()] = v.strip()
    sec = {k: [x for x in (D.plainify(i, names) for i in v) if x] for k, v in _note_sections(note_text or "").items()}
    where = _brief(sec.get("现在到哪了") or [], 2) or "交接说明里没写现在到哪了，我先带你把里面的资料和记录看一遍"
    nxt = _brief(sec.get("下一步") or [], 2) or "先看一遍交接说明和成品，再决定先做哪一项"
    todo_parts = []
    waiting = [x for x in (sec.get("在等谁拍板") or []) if "暂时没有" not in x]
    if waiting:
        todo_parts.append("等你拍板或确认的：" + _brief(waiting, 2))
    notes = [x for x in (sec.get("注意事项") or []) if "暂时没有" not in x]
    if notes:
        todo_parts.append("要留意：" + _brief(notes, 2))
    todo = "；".join(todo_parts) or "暂时没有谁在等你拍板，你先看一眼就行"
    return {"where": where, "todo": todo, "next": nxt}


def _read_note(root, info, import_id=None):
    root = Path(root)
    if import_id:
        p = root / _j().IMPORT_DIR / import_id / "交接说明.md"
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    if info.get("note_path") and (root / info["note_path"]).is_file():
        return (root / info["note_path"]).read_text(encoding="utf-8", errors="replace")
    notes = sorted((root / _d().NOTE_DIR).glob("*.md")) if (root / _d().NOTE_DIR).is_dir() else []
    return notes[-1].read_text(encoding="utf-8", errors="replace") if notes else ""


def _record_received(root, row):
    from workspace_lock import serialized
    root = Path(root)
    with serialized(root):
        path = root / RECEIVED_LOG
        raw = path.read_bytes() if path.is_file() else b""
        data = raw + (b"" if not raw or raw.endswith(b"\n") else b"\n") + (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        _j()._atomic_write(path, data)


def _receipt(info, kind, name, from_who, actor, words, choice, root, **extra):
    row = {"schema_version": 1, "event": "handoff_received", "received_id": uuid.uuid4().hex, "at": _now(), "receiver": name or "接手人", "received_from": from_who or "交出的人",
           "actor": actor, "words": words, "kind": kind, "choice": choice, "basis": BASIS,
           "package": {"manifest_id": info["manifest_id"], "zip_name": info["name"], "zip_sha256": info["sha256"], "created_at": info.get("created_at"),
                       "from_actor": info.get("from_actor"), "recipient": info.get("recipient")}, "workspace": Path(root).name}
    row.update(extra)
    return row


def _finish_text(kind, name, three, open_folder, extra=""):
    parts = [f"接好了。{extra}".rstrip(), f"现在到哪了：{three['where'].rstrip('。；; ')}。", f"要你做什么：{three['todo'].rstrip('。；; ')}。", f"下一步：{three['next'].rstrip('。；; ')}。"]
    if open_folder:
        parts.append(f"以后在这个文件夹里打开对话：\n\n```\n{open_folder}\n```")
    if kind == "full":
        parts.append(_d().OLD_WORDS_SENTENCE)
    parts.append(PLATFORM_REMINDER)
    return "\n".join(parts)


def make_workspace_beside(receiver, label):
    """在接收方工作区旁边新开一个空白工作区：只复制工作包本身（发行清单里 project/ 以外的文件），project/ 重建成空白。名字不覆盖。"""
    D = _d()
    receiver = Path(receiver).resolve()
    inventory_path = receiver / "package-inventory.json"
    try:
        items = json.loads(inventory_path.read_text(encoding="utf-8"))["inventory"]
    except (OSError, ValueError, KeyError):
        raise WorkflowError("找不到这个工作区的发行清单，没法用它复制一份新的工作区") from None
    dest = _unique_dir(receiver.parent, _safe_name(label))
    staging = receiver.parent / f".接手新建-{uuid.uuid4().hex[:10]}"
    try:
        staging.mkdir()
        for item in items:
            rel, kind = item.get("path"), item.get("kind")
            if not isinstance(rel, str) or item.get("excluded") or rel.startswith("/") or ".." in rel.split("/"):
                continue
            src, dst = receiver / rel, staging / rel
            if rel == "project" or rel.startswith("project/"):
                if kind == "directory":
                    dst.mkdir(parents=True, exist_ok=True)
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                if rel == "project/state.json":
                    state = dict(BLANK_STATE); state["last_updated"] = _now()
                    dst.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                elif rel in BLANK_JSON:
                    dst.write_text(json.dumps(BLANK_JSON[rel], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                elif rel.endswith(".jsonl"):
                    dst.write_text("", encoding="utf-8")
                elif rel.endswith(".md") and src.is_file():
                    shutil.copy2(src, dst)
                else:
                    raise WorkflowError(f"发行清单里的 {rel} 我不知道怎么建成空白的")
                continue
            if kind == "directory":
                dst.mkdir(parents=True, exist_ok=True)
            elif kind == "file":
                if not src.is_file() or src.is_symlink():
                    raise WorkflowError(f"工作包文件 {rel} 在这个工作区里找不到，没法复制")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        shutil.copy2(inventory_path, staging / "package-inventory.json")
        os.replace(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)               # 只清理我们自己刚建的半成品，不碰任何用户文件
        raise
    return dest


def _full_accept(root, info, name, from_who, actor, words, choice, mode, extra_receipt=None, intro=""):
    """全量包解好之后：用那个工作区自己的脚本 accept → 记接手 → 读说明 → 三句话。"""
    D = _d()
    code, out = _child(root, "project_handoff.py", "accept", "--workspace", ".", "--actor", actor, "--manifest-id", info["manifest_id"], "--json")
    if not (isinstance(out, dict) and out.get("accepted")):
        errs = out.get("errors") if isinstance(out, dict) else [str(out)[:200]]
        return _wrap("blocked", "accept_failed", "解开之后每个文件的核对没有通过（有文件和清单对不上），所以我不算它接好了，也没有往下做。"
                     "请你找交出的人重新发一份；已经解开的文件夹我没有删，留在原处。", written=True, workspace=str(root), errors=(errs or [])[:5])
    note = _read_note(root, info)
    three = three_sentences(note)
    row = _receipt(info, "full", name, from_who, actor, words, choice, root, mode=mode, **(extra_receipt or {}))
    _record_received(root, row)
    errors = _resume_errors(root)
    text = _finish_text("full", name, three, str(root) if mode != "here_in_current" else "", intro)
    if errors:
        text += f"\n不过接好之后工作区的检查有 {len(errors)} 个问题，先别往下做，告诉我。"
    return _wrap("received" if not errors else "received_with_errors", "received", text, written=True, three=three, workspace=str(root), resume_errors=errors,
                 received_id=row["received_id"], technical=f"project_handoff.py accept 通过；接手记录 {RECEIVED_LOG}；续做状态 errors={len(errors)}")


def receive(workspace, package, choice, words, actor, name=None, from_who=None):
    """他选了之后才动手。每个会写文件的选项都要他当次的原话。"""
    from registration_guard import actor_check
    actor_check(actor)
    D, J = _d(), _j()
    root = Path(workspace).expanduser().resolve()
    if not isinstance(words, str) or not words.strip():
        raise WorkflowError("接手要有接手人当次的原话（--words），没有就不动任何文件")
    g = guide(root, package)
    if g.get("situation") == "version_mismatch":
        return g                                         # J14：版本对不上，什么都不写
    offered = {o["key"]: o for o in g.get("options", [])}
    if g["situation"] in ("choose_package", "not_found"):
        return g                                         # 还没确定是哪个包：先让他选 / 先放进文件夹，什么都不做
    if choice not in offered:
        return _wrap("not_offered", g["situation"], "这一步现在不能这样选：" + (g["for_strategist"] or ""), g.get("options", []), package=g.get("package"))
    path = g.get("package")
    info = inspect_package(path) if path else None
    sit = g["situation"]
    if choice in ("switch",):
        return _wrap("done", "switch", "好的，这次我什么都没做，也没有动你的任何文件。你换到要放这个任务的工作区，把压缩包拖进对话，再说一次“接手”。", written=False)
    if choice in ("install_first", "ask_full", "upgrade_first"):
        return _wrap("done", sit, g["for_strategist"], written=False)
    # ---------- 全量包 ----------
    if choice == "accept_here":
        if info is None:
            info = _info_from_unzipped(root)
        return _full_accept(root, info, name, from_who, actor, words, choice, "unzipped_here", intro="")
    if choice in ("full_here", "full_beside"):
        label = _safe_name(_project_label(info, "接手") + "-接手")
        moved = None
        if choice == "full_beside":
            dest = _unique_dir(root.parent, label)
            staging = root.parent / f".接手解压-{uuid.uuid4().hex[:10]}"
            try:
                written, conflicts = _extract(path, staging)
                if conflicts:
                    raise _ExtractConflict(conflicts)
                os.replace(staging, dest)
            except _ExtractConflict as stop:
                shutil.rmtree(staging, ignore_errors=True)
                return _extract_blocked(stop.conflicts)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)         # 只清理自己刚建的半成品
                raise
            target, mode = dest, "beside"
            if Path(path).resolve().parent == root:
                moved = J.stash_beside(root, path)               # 压缩包放在当前工作区根目录的话，挪到旁边，免得当前工作区被它挡住
        else:
            folder = classify_folder(root, path)
            if folder["kind"] in ("workspace_project", "workspace_blank"):
                return _wrap("insist_blocked", "full_mixing",
                             "你坚持放在现在这个工作区里，我没有动手：这一整间办公室里的文件和你这个工作区里已有的文件在同一批位置上，放进来会盖掉你已有的内容，"
                             "也会让这个工作区的检查报错，我不会这么做。我建议解到旁边的新文件夹，你说“好”我就这样做。", g["options"], written=False)
            if folder["kind"] == "other_folder":
                # 他坚持放在这个有别的东西的文件夹里：放进里面一个新的子文件夹，不和他原有的东西混在一起，原有的一个字节不动
                target, mode = _unique_dir(root, _safe_name(_project_label(info, "接手") + "-接手")), "subfolder_in_current"
                staging = root / f".接手解压-{uuid.uuid4().hex[:10]}"
                try:
                    written, conflicts = _extract(path, staging)
                    if conflicts:
                        raise _ExtractConflict(conflicts)
                    os.replace(staging, target)
                except _ExtractConflict as stop:
                    shutil.rmtree(staging, ignore_errors=True)
                    return _extract_blocked(stop.conflicts)
                except BaseException:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
            else:
                written, conflicts = _extract(path, root)
                if conflicts:
                    return _extract_blocked(conflicts)
                target, mode = root, "here_in_current"
            if Path(path).resolve().parent == root:
                moved = J.stash_beside(root, path)
        intro = f"压缩包已经核对并解开了{('，原来放在文件夹里的压缩包我挪到了文件夹旁边（没删）' if moved else '')}。"
        res = _full_accept(target, info, name, from_who, actor, words, choice, mode, {"insisted": choice == "full_here" and sit == "full_mixing", "zip_moved_beside": bool(moved)}, intro)
        res["workspace"] = str(target)
        return res
    # ---------- 任务包 ----------
    identity = {"identity_same": "confirmed_same", "mix_here": "mixed"}.get(choice)
    if choice in ("import", "identity_same", "mix_here"):
        out = J.apply(root, path, words, actor, identity)
        return _task_finish(root, info, out, name, from_who, actor, words, choice, str(root), root, path)
    if choice == "new_workspace":
        label = _safe_name(_project_label(info, "接手的任务") + "-工作区")
        try:
            new_root = make_workspace_beside(root, label)
        except WorkflowError as exc:
            return _wrap("blocked", "new_workspace_failed", f"没能在旁边新开工作区：{exc}。你可以选“换到别的工作区再接手”。", written=False)
        out = J.apply(new_root, path, words, actor)
        res = _task_finish(new_root, info, out, name, from_who, actor, words, choice, str(new_root), root, path)
        res["new_workspace"] = str(new_root)
        return res
    if choice == "fork_keep_mine":
        return _fork_keep(root, info, path, name, from_who, actor, words)
    if choice == "fork_save_new":
        return _fork_new(root, info, path, name, from_who, actor, words)
    return _wrap("not_offered", sit, "这个选项我还不会处理。")


def _info_from_unzipped(root):
    D = _d()
    manifest = json.loads((Path(root) / D.MANIFEST_REL).read_text(encoding="utf-8"))
    pkg = manifest.get("package") or {}
    events = []
    ev = Path(root) / D.EVENTS_REL
    if ev.is_file():
        for line in ev.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    prepared = [e for e in events if e.get("event") == "handoff_prepared" and e.get("manifest_id") == manifest["manifest_id"]]
    return {"ok": True, "kind": "full", "manifest_id": manifest["manifest_id"], "package_version": pkg.get("package_version"), "identity": pkg.get("identity") or {},
            "recipient": pkg.get("recipient"), "from_actor": prepared[-1].get("actor") if prepared else None, "created_at": pkg.get("created_at"), "note_path": pkg.get("note_path"),
            "name": "（已在文件夹里解开）", "sha256": None}


def memory_talk(diffs):
    """同一客户已有项目记忆：逐条列出“包里这样说 / 你这里这样说”（最多先列 10 条，其余说还有几条）；怎么选也说清。"""
    if not diffs:
        return ""
    def value(x):
        text = str(x)
        return text if len(text) <= 60 else text[:60] + "……"
    lines = []
    for d in diffs[:10]:
        topic = _clean(str(d.get("topic") or "")) or "一条记忆"
        mine = d.get("mine_value")
        lines.append(f"第 {d['n']} 条{topic}：包里这样说「{value(d.get('pkg_value'))}」；你这里" + (f"这样说「{value(mine)}」" if mine is not None else "没有这一条"))
    text = f"你这里已经有项目记忆，我没有覆盖它。和包里说法不一样的有 {len(diffs)} 条：\n" + "\n".join("- " + x for x in lines)
    if len(diffs) > 10:
        text += f"\n还有 {len(diffs) - 10} 条没有列出来，要看的话告诉我。"
    text += ("\n想用包里的哪一条，就说“第 1 条用包里的”（把 1 换成条的序号，几条都可以一起说）；我会追加一条等你确认的记录，你原来的记忆一个字都不改。"
             "不说就保持你这边的。")
    return text


def _import_event(root, import_id):
    path = Path(root) / _j().IMPORT_LOG
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") == "task_imported" and row.get("import_id") == import_id:
                return row
    return None


def memory_choose(workspace, import_id, numbers, words, actor):
    """接手人说“第 N 条用包里的”：用现有的项目记忆登记（project_memory.py save）追加一条待确认的记录，记他的原话；他原来的记忆一个字节不改。每一条幂等。"""
    from registration_guard import actor_check
    from workspace_lock import serialized
    actor_check(actor)
    J = _j()
    root = Path(workspace).expanduser().resolve()
    if not isinstance(words, str) or not words.strip():
        raise WorkflowError("要有接手人当次的原话（--words），没有就不追加任何记忆")
    event = _import_event(root, import_id)
    if event is None:
        raise WorkflowError(f"找不到这次接手（{import_id}）的导入记录，不知道你说的是哪一次接手")
    diffs = {d["n"]: d for d in (event.get("memory") or {}).get("differences", []) if isinstance(d, dict) and "n" in d}
    try:
        wanted = sorted({int(x) for x in re.split(r"[,，、\s]+", str(numbers).strip()) if x})
    except ValueError:
        raise WorkflowError("条的序号要写数字，比如 1 或 1,3") from None
    if not wanted:
        raise WorkflowError("没有说要用包里的哪一条")
    missing = [n for n in wanted if n not in diffs]
    if missing:
        raise WorkflowError("没有第 " + "、".join(map(str, missing)) + f" 条（这次接手一共列了 {len(diffs)} 条不一样的），这次什么都没追加")
    pkg_rows = {}
    backup = root / event["backup_dir"] / "记录/project-memory.jsonl"
    if backup.is_file():
        for line in backup.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                pkg_rows[row["memory_id"]] = row
    from project_memory_store import history
    state = json.loads((root / "project/state.json").read_text(encoding="utf-8"))
    mid12 = (event.get("package") or {}).get("manifest_id", "")[:12]
    applied, skipped = [], []
    for n in wanted:
        d = diffs[n]
        row = pkg_rows.get(d["memory_id"])
        if row is None:
            raise WorkflowError(f"备查里找不到第 {n} 条在包里的原记录（{event['backup_dir']}），没有追加")
        request_id = f"imp-{mid12}-m{n}"
        events, _ = history(root)
        if any(e["request_id"] == request_id for e in events):
            skipped.append({"n": n, "why": "已经追加过"})
            continue
        target_id = d.get("mine_memory_id") or d["memory_id"]
        current = next((e for e in reversed(events) if e["memory_id"] == target_id), None)
        if current and current["current_value"] == row["current_value"]:
            skipped.append({"n": n, "why": "两边说法其实一样，只是状态不同，不用改"})
            continue
        request = {"memory_id": target_id, "request_id": request_id, "expected_revision": current["revision"] if current else 0, "project_id": state.get("project_id"),
                   "entity_type": row["entity_type"], "entity_id": row["entity_id"], "topic": row["topic"], "current_value": row["current_value"],
                   "replaces": current["current_value"] if current else None, "scope": row["scope"], "source_refs": [],
                   "reason": f"接手人选用交接包里的说法（{import_id}）；接手人原话：{words}", "status": "needs_confirmation"}
        code, out = _child(root, "project_memory.py", "save", "--workspace", ".", "--actor", actor, "--request-json", json.dumps(request, ensure_ascii=False))
        if code != 0 or not isinstance(out, dict) or out.get("errors"):
            raise WorkflowError(f"第 {n} 条没能追加：{(out.get('errors') if isinstance(out, dict) else str(out))}")
        applied.append({"n": n, "memory_id": target_id, "revision": out.get("revision")})
    with serialized(root):
        J._append_lines(root / J.IMPORT_LOG, [json.dumps({"schema_version": 1, "event": "memory_choice", "at": _now(), "actor": actor, "words": words, "import_id": import_id,
                                                         "numbers": wanted, "applied": applied, "skipped": skipped, "basis": BASIS + "：同客户已有项目记忆，逐条选用包里的说法"},
                                                        ensure_ascii=False, allow_nan=False)])
    talk = (f"好的，第 {'、'.join(str(a['n']) for a in applied)} 条我按包里的说法追加了一条记录，状态是“待确认”，要等你确认才算数；你原来的记忆一个字都没改。" if applied else "")
    if skipped:
        talk += "".join(f"第 {s['n']} 条：{s['why']}。" for s in skipped)
    return {"status": "chosen", "applied": applied, "skipped": skipped, "for_strategist": talk or "没有需要追加的。", "written": bool(applied)}


def _task_finish(target, info, out, name, from_who, actor, words, choice, open_folder, moved_from, package_path):
    J = _j()
    if out.get("status") not in ("imported", "imported_with_errors", "already_imported"):
        return _wrap("blocked", out.get("situation", "blocked"), out.get("for_strategist", "没能接手。"), written=out.get("written", False), technical=out.get("technical"))
    if out.get("status") == "already_imported":
        return _wrap("already_imported", "already_imported", out["for_strategist"], written=False)
    note = _read_note(target, info, out["import_id"])
    three = three_sentences(note)
    row = _receipt(info, "task", name, from_who, actor, words, choice, target, import_id=out["import_id"], task_id=out["task_id"], task_title=info.get("task_title"),
                   identity_choice=("mixed" if choice == "mix_here" else "confirmed_same" if choice == "identity_same" else None))
    _record_received(target, row)
    moved = None
    if Path(moved_from) != Path(target):
        moved = J.stash_beside(moved_from, package_path)
    errors = _resume_errors(target)
    intro = out["for_strategist"]
    if moved:
        intro += "压缩包原来放在别的工作区里，我把它挪到了文件夹旁边（没删）。"
    text = _finish_text("task", name, three, open_folder if choice == "new_workspace" else "", intro)
    diffs = out.get("memory_differences") or []
    if diffs:
        text += "\n\n" + memory_talk(diffs)
    if errors:
        text += f"\n不过接好之后工作区的检查有 {len(errors)} 个问题，先别往下做，告诉我。"
    return _wrap("received" if not errors else "received_with_errors", "received", text, written=True, three=three, workspace=str(target), resume_errors=errors,
                 import_id=out["import_id"], task_id=out["task_id"], received_id=row["received_id"], memory_differences=diffs,
                 technical=f"接手记录 {RECEIVED_LOG}；{out.get('technical', '')}" + ("；逐条选用包里的项目记忆：python3 scripts/jiaojie_yindao.py memory-choose --workspace . --import-id <导入编号> "
                                                                          "--numbers <序号,序号> --words \"<他的原话>\" --actor <实例>" if diffs else ""))


# ---------------- 同编号分叉 ----------------

def _package_events(lines):
    """包里任务记录的完整事件（存的可能是只写变化的格式；放进临时目录用读取时同一套办法还原）。"""
    from standalone_store import history
    with tempfile.TemporaryDirectory(prefix="jiejie-fenpa-") as tmp:
        records = Path(tmp) / "project/records"
        records.mkdir(parents=True)
        plain = []
        for line in lines:
            obj = json.loads(line)
            obj.pop("prev_line_sha256", None)
            plain.append(json.dumps(obj, ensure_ascii=False))
        (records / "standalone-tasks.jsonl").write_text("\n".join(plain) + "\n", encoding="utf-8")
        events, _ = history(Path(tmp))
    return events


def _fork_event_exists(root, manifest_id, kind):
    path = Path(root) / _j().IMPORT_LOG
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == kind and (row.get("package") or row.get("declined_package") or {}).get("manifest_id") == manifest_id:
            return row
    return None


def _fork_keep(root, info, path, name, from_who, actor, words):
    from workspace_lock import serialized
    J = _j()
    with serialized(root):
        if _fork_event_exists(root, info["manifest_id"], "task_import_declined"):
            return _wrap("already_imported", "task_diverged", "这个包你已经决定过“保留你这边的、不导入”了，这次没有再记。", written=False)
        event = {"schema_version": 1, "event": "task_import_declined", "at": _now(), "actor": actor, "words": words, "basis": BASIS + "：同编号任务两边各改过，接手人选保留自己这边",
                 "declined_package": {"manifest_id": info["manifest_id"], "zip_name": info["name"], "zip_sha256": info["sha256"], "created_at": info.get("created_at"), "from_actor": info.get("from_actor")},
                 "task": {"task_id": info.get("task_id"), "title": info.get("task_title")}}
        J._append_lines(root / J.IMPORT_LOG, [json.dumps(event, ensure_ascii=False, allow_nan=False)])
    moved = J.stash_beside(root, path)
    row = _receipt(info, "task", name, from_who, actor, words, "fork_keep_mine", root, outcome="kept_mine_not_imported")
    _record_received(root, row)
    talk = (f"好的，保留你这边的{_clean(info.get('task_title') or '') or '这个任务'}，这个包没有导入，你这边的任何东西一个字节都没动。"
            "你这个决定我记下了；压缩包没有删" + ("，我把它挪到了文件夹旁边留作备查" if moved else "，还在原处留作备查") + "。")
    return _wrap("done", "task_diverged", talk, written=True, received_id=row["received_id"])


def _fork_new(root, info, path, name, from_who, actor, words):
    """另存为新任务：新编号，来源指向导入的只读副本，旧历史在 handoff-imports/ 里。接收方原有的任务和记录一个字节不动。"""
    from standalone_store import history
    from standalone_tasks import save
    from workspace_lock import serialized
    D, J, R = _d(), _j(), _j()._r()
    with serialized(root):
        done = _fork_event_exists(root, info["manifest_id"], "task_forked")
        if done:
            return _wrap("already_imported", "task_diverged", f"这个包已经另存成新任务「{done['new_task']['task_id']}」了，不用再来一次。", written=False)
        z, tm, excerpts, keep = J._load(path)
        with z:
            events, _ = history(root)
            taken = {e["task_id"] for e in events}
            n = 1
            while f"{tm['task_id']}-IMP{n}" in taken:
                n += 1
            new_id = f"{tm['task_id']}-IMP{n}"                      # 任务编号只能是字母、数字、短横线、下划线
            import_id = "导入-" + info["manifest_id"][:12]
            backup = root / J.IMPORT_DIR / import_id
            J._atomic_write(backup / "任务交接.json", z.read(R.TASK_MANIFEST))
            J._atomic_write(backup / "包清单.json", z.read(D.MANIFEST_REL))
            note_path = tm.get("note_path")
            if note_path and note_path in z.namelist():
                J._atomic_write(backup / "交接说明.md", z.read(note_path))
            for fname, lines in excerpts.items():
                J._atomic_write(backup / "记录" / fname, ("\n".join(lines) + "\n").encode("utf-8"))
            for fname, lines in keep.items():
                J._atomic_write(backup / "备查" / fname, ("\n".join(lines) + "\n").encode("utf-8"))
            copies, member = [], {m["name"] for m in json.loads(z.read(D.MANIFEST_REL))["package"]["members"]}
            for item in tm["files"]:
                rel = item["path"]
                if not J.safe_target(rel) or rel not in member or item.get("role") == R.MEMORY_ROLE:
                    continue
                dest = backup / "只读副本" / rel
                J._place(z, rel, dest)
                os.chmod(dest, 0o444)
                copies.append({"label": Path(rel).name, "path": dest.relative_to(root).as_posix()})
        old = _package_events(excerpts.get("standalone-tasks.jsonl", []))[-1]["task"]
        title = f"{old.get('title') or tm.get('title')}（接手的副本）"
        request = {"task_id": new_id, "request_id": f"{new_id}-r1", "expected_revision": 0,
                   "reason": "同编号任务两边各改过，接手人选了另存为新任务（原话见 request_text）；包里的成品与来源放在只读副本里供参考",
                   "task": {"title": title, "project_label": old.get("project_label") or "接手", "request_text": "接手人原话：" + words, "goal": old.get("goal") or title,
                            "status": "in_progress", "summary": f"从交接包另存的新任务；来源是原任务 {tm['task_id']} 的只读副本，原任务的旧历史在 {J.IMPORT_DIR}/{import_id}/",
                            "next_action": "对照只读副本，决定要沿用哪些内容", "owner": None, "due_date": None, "sources": copies, "deliverables": [], "completion_evidence": ""}}
        saved = save(root, request, actor)
        event = {"schema_version": 1, "event": "task_forked", "import_id": import_id, "at": _now(), "actor": actor, "words": words, "basis": BASIS + "：同编号任务两边各改过，接手人选另存为新任务",
                 "package": {"manifest_id": info["manifest_id"], "zip_name": info["name"], "zip_sha256": info["sha256"], "created_at": info.get("created_at"), "from_actor": info.get("from_actor")},
                 "task": {"task_id": tm["task_id"], "title": tm.get("title")}, "new_task": {"task_id": new_id, "revision": saved["revision"], "read_only_copies": len(copies)},
                 "backup_dir": f"{J.IMPORT_DIR}/{import_id}", "memory": {"action": "backup_only"}, "decision_rows_not_imported": tm.get("decision_rows_not_carried", {})}
        J._append_lines(root / J.IMPORT_LOG, [json.dumps(event, ensure_ascii=False, allow_nan=False)])
    moved = J.stash_beside(root, path)
    note = _read_note(root, info, import_id)
    three = three_sentences(note)
    three["where"] = f"这是从交接包另存的新任务{_clean(title) or ''}：包里的成品和来源放在一份只读副本里供你参考，还没有开始做；你这边原来的同名任务没动。"
    three["next"] = "先对照只读副本，决定要沿用哪些内容。"
    row = _receipt(info, "task", name, from_who, actor, words, "fork_save_new", root, import_id=import_id, task_id=new_id, task_title=title, outcome="saved_as_new_task")
    _record_received(root, row)
    errors = _resume_errors(root)
    text = _finish_text("task", name, three, "", f"已另存为一个新任务{_clean(title) or ''}（原来包里的编号不会和你这边的混），你这边原来的任务没动；包里的成品和来源放在一份只读副本里，旧的历史留在备查文件夹里。"
                        + ("压缩包我挪到了文件夹旁边（没删）。" if moved else ""))
    if errors:
        text += f"\n不过接好之后工作区的检查有 {len(errors)} 个问题，先别往下做，告诉我。"
    return _wrap("received" if not errors else "received_with_errors", "received", text, written=True, three=three, workspace=str(root), resume_errors=errors,
                 new_task_id=new_id, import_id=import_id, received_id=row["received_id"])


# ---------------- 命令行 ----------------

def main():
    parser = argparse.ArgumentParser(description="接手引导（v1.7.8 N5）：find 找包；guide 只读看清情况和选项；receive 他选了之后才动手")
    sub = parser.add_subparsers(dest="command", required=True)
    mc = sub.add_parser("memory-choose", help="同客户已有项目记忆时，接手人说“第 N 条用包里的”：追加一条待确认的记录")
    mc.add_argument("--workspace", type=Path, required=True)
    mc.add_argument("--import-id", required=True)
    mc.add_argument("--numbers", required=True, help="条的序号，如 1 或 1,3")
    mc.add_argument("--words", default="", help="接手人当次原话")
    mc.add_argument("--actor", required=True)
    mc.add_argument("--json", action="store_true")
    for name in ("find", "guide", "receive"):
        s = sub.add_parser(name)
        s.add_argument("--workspace", type=Path, required=True)
        s.add_argument("--package", type=Path, default=None)
        s.add_argument("--json", action="store_true")
        if name == "receive":
            s.add_argument("--choice", required=True)
            s.add_argument("--words", default="", help="接手人当次原话")
            s.add_argument("--actor", required=True)
            s.add_argument("--name", help="接手人的称呼（不说就记“接手人”）")
            s.add_argument("--from-who", help="这个包是谁交给他的（可不填）")
    args = parser.parse_args()
    if args.command == "memory-choose":
        out = memory_choose(args.workspace, args.import_id, args.numbers, args.words, args.actor)
    elif args.command == "find":
        out = find_package(args.workspace, args.package)
    elif args.command == "guide":
        out = guide(args.workspace, args.package)
    else:
        out = receive(args.workspace, args.package, args.choice, args.words, args.actor, args.name, args.from_who)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["status"] in {"found", "choose_package", "ask", "ready", "info", "done", "received", "already_imported", "chosen"} else 1


if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main

    def main():
        return cli(_business_main, __file__)

    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
