#!/usr/bin/env python3
"""接手任务交接包（v1.7.8 J03、J04，N4 导入核心；接手引导层在 jiaojie_yindao.py）。

两个命令：
  check  --workspace . --package <压缩包> [--json]   只读：核对包、认出现在是哪种情况，返回状况代码和白话；不写任何文件
  apply  --workspace . --package <压缩包> --words "<接手人当次原话>" --actor <实例> [--json]   确认后导入

check 返回的状况（situation）：
  可导入：same_client 同一个客户项目 / blank 空白工作区（采用包里的客户项目身份）/ blank_no_identity 空白工作区、包里也没写身份 / append_newer 你这边有这个任务的旧版，只补多出来的版本
  不用导入：already_imported 这个包已经导入过 / nothing_to_import 你这边已经有同样或更新的版本
  要停下问（check 只识别、不写；怎么选、选了怎么做在 jiaojie_yindao.py 的引导里）：different_client 不同客户项目 / identity_unknown 比对不了是不是同一个客户 / task_diverged 同编号任务两边各改了不同的内容 /
    file_conflict 有同路径但内容不同的文件 / request_conflict 请求编号冲突 / full_package 这是全量交接包放错了地方 / version_differs 工作包版本不同 /
    package_damaged 包坏了或被改过 / not_workspace 这里还不是装好工作包的工作区 / receiver_has_problems 你这边的检查本来就有没解决的问题 / unsupported_package 看不懂的包
apply 的顺序（持工作区锁，每步幂等，原样重试返回“已导入”）：
  核对包 → 内存演练拼接校验 → 原记录备查到 project/records/handoff-imports/<导入编号>/ → 放文件 → 采用客户项目身份（仅空白）→ 项目记忆（仅空白重放）→
  分派记录 → 独立任务记录（接收方日志末尾追加，prev_line_sha256 接在接收方最后一条后面，更新截尾锚点）→ 检核记录 → 发客户等事实记录 → 其他事实行 →
  一条导入事件追加到 project/records/handoff-imports.jsonl → 续做状态必须 0 错误。
为什么不破坏记录链：event_digest 不含 prev_line_sha256，原事件逐字追加，任务内的 base_task_sha256 / task_sha256 链、版本号、请求号、改稿预算都和原来一致；接收方已有的行一个字节不改。
决定类（豁免、最终版、不用再查、已看到提醒、内部确认、客户确认、手改接纳）不随包生效，只在备查里；门槛一点不放宽：发客户时接收方的判定和本地做出来的一样。
"""
import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from phase2_store import WorkflowError

IMPORT_LOG = "project/records/handoff-imports.jsonl"
IMPORT_DIR = "project/records/handoff-imports"
BASIS = "J03/J04 接手任务交接包"
RECORD_TARGET = {"standalone-tasks.jsonl": "project/records/standalone-tasks.jsonl", "agent-dispatch.jsonl": "project/records/agent-dispatch.jsonl",
                 "phase6-events.jsonl": "project/records/phase6-events.jsonl", "delivery-gate.jsonl": "project/records/delivery-gate.jsonl",
                 "project-memory.jsonl": "project/records/project-memory.jsonl"}
OTHER_FACTS = ("off-package-actions.jsonl", "external-docs.jsonl", "task-receipts.jsonl", "fact-ledger.jsonl")
SITUATION_WORDS = {
    "same_client": "这个任务和你现在的工作区是同一个客户项目，可以直接放进来。",
    "blank": "你这里是一个空白工作区，我会用包里的客户项目身份把它初始化，再放进这个任务。",
    "blank_no_identity": "你这里是空白工作区，包里也没写客户项目身份，我只把任务放进来，客户项目身份以后再登记。",
    "append_newer": "你这边已经有这个任务的旧版本，包里的更新，我只补上多出来的版本。",
    "already_imported": "这个交接包已经导入过了，不用再导入。",
    "nothing_to_import": "你这边已经有这个任务，而且和包里一样新或更新，不用导入。",
    "different_client": "这个任务属于另一个客户项目，和你现在打开的工作区对不上。",
    "identity_unknown": "比对不了这是不是同一个客户的项目（包里或你这边没有写客户项目身份）。",
    "task_diverged": "你这边已经有同编号的任务，而且两边各自改过、内容不一样。",
    "file_conflict": "包里有的文件在你这边已经有同一个位置、但内容不同的文件。",
    "request_conflict": "包里任务记录的请求编号和你这边已有的别的任务冲突。",
    "full_package": "这是一个整个工作区的全量交接包，不是单个任务的交接包，放在这里会和你现有的案子混在一起。",
    "version_differs": "你这边的工作包版本和交接包里的不一样。",
    "package_damaged": "这个压缩包坏了，或者被人改动过。",
    "not_workspace": "这里还不是装好工作包的工作区，没法接手。",
    "receiver_has_problems": "你这边的工作区本来就有没解决的检查问题，先处理好再接手，免得分不清是谁带来的。",
    "unsupported_package": "这个压缩包不是用交接功能做的任务交接包，我看不懂。",
    "mixed_here": "你选了把这个任务放进现在的工作区：两个客户项目的资料会混在一起，原来的客户项目身份我不采用，包里的项目记忆也不合并，只留备查。",
}
RESULT_STATUS = {"same_client": "ready", "blank": "ready", "blank_no_identity": "ready", "append_newer": "ready", "already_imported": "already_imported",
                 "nothing_to_import": "nothing_to_import", "mixed_here": "ready"}
OVERRIDES = ("confirmed_same", "mixed")          # N5：接手人当次明确选了才生效（apply 的 identity 参数）


def version_relation(package_version, my_version):
    """工作包版本比较（只看 v 后面的三段数字；候选、定稿这类后缀不算差别）。
    relation：same 一样 / package_newer 包里的更新（接收方要先升级）/ receiver_newer 接收方更新（照常接）/ unknown 看不出版本，两边写法又不一样（按不一致停下）。"""
    def triple(text):
        match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", str(text or ""))
        return tuple(int(x) for x in match.groups()) if match else None
    a, b = triple(package_version), triple(my_version)
    if a is not None and b is not None:
        relation = "same" if a == b else "package_newer" if a > b else "receiver_newer"
    else:
        relation = "same" if str(package_version or "").strip() == str(my_version or "").strip() else "unknown"
    return {"relation": relation, "package": package_version, "receiver": my_version}


def _own_zip_filtered(problems, own_zip):
    """压缩包自己就放在工作区根目录时，续做状态会报“根目录有未归档业务文件”；只有它自己一个的话不算问题（导入后会把它挪到文件夹旁边）。"""
    if not own_zip:
        return problems
    out, prefixes = [], ("根目录有未归档业务文件：", "package-inventory.json 核验失败: 发行文件缺失、新增或指纹已变化: ")
    for item in problems:
        if any(item.startswith(prefix) and [x for x in item[len(prefix):].split(", ") if x] == [own_zip] for prefix in prefixes):
            continue
        out.append(item)
    return out


def stash_beside(root, package):
    """压缩包直接放在工作区根目录时，挪到工作区旁边（不删、不覆盖同名文件）。返回新路径或 None。"""
    package, root = Path(package).resolve(), Path(root).resolve()
    if package.parent != root or not package.is_file():
        return None
    base, n = root.parent / package.name, 1
    dest = base
    while dest.exists():
        n += 1
        dest = root.parent / f"{package.stem}-{n}{package.suffix}"
    os.replace(package, dest)
    return dest


def _d():
    import jiaojie_dabao as module
    return module


def _r():
    import jiaojie_renwu as module
    return module


def _norm(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _result(root, situation, extra=None, technical=""):
    base = {"status": RESULT_STATUS.get(situation, "blocked"), "situation": situation, "for_strategist": SITUATION_WORDS[situation], "technical": technical, "written": False}
    base.update(extra or {})
    return base


def _lines(data):
    return [x for x in data.decode("utf-8").split("\n") if x.strip()]


def _read_jsonl_bytes(path):
    return Path(path).read_bytes() if Path(path).is_file() else b""


def _blank(root, journal_events):
    ident = _d()._identity(root)
    return not any(ident.values()) and not journal_events


def _shadow_check(raw_journal, head, memory_raw=None):
    """内存演练：拼好的任务记录、项目记忆放进临时目录，用读取时同一套校验跑一遍。返回报错文字或 None。"""
    from standalone_store import history
    with tempfile.TemporaryDirectory(prefix="jiejie-yanlian-") as tmp:
        records = Path(tmp) / "project/records"
        records.mkdir(parents=True)
        (records / "standalone-tasks.jsonl").write_bytes(raw_journal)
        if head is not None:
            (records / "standalone-tasks.head.json").write_text(json.dumps(head), encoding="utf-8")
        try:
            history(Path(tmp))
        except (WorkflowError, ValueError) as exc:
            return str(exc)
        if memory_raw:
            from project_memory_store import history as memory_history
            (records / "project-memory.jsonl").write_bytes(memory_raw)
            try:
                memory_history(Path(tmp))
            except (WorkflowError, ValueError) as exc:
                return str(exc)
    return None


def _package_events(excerpt_lines):
    """包里这个任务的各版事件摘要（不依赖 prev_line_sha256：临时去掉再读）。返回 (摘要列表, 报错或 None)。"""
    from standalone_store import event_digest, history
    with tempfile.TemporaryDirectory(prefix="jiejie-baoli-") as tmp:
        records = Path(tmp) / "project/records"
        records.mkdir(parents=True)
        plain = []
        for line in excerpt_lines:
            obj = json.loads(line)
            obj.pop("prev_line_sha256", None)
            plain.append(json.dumps(obj, ensure_ascii=False))
        (records / "standalone-tasks.jsonl").write_text("\n".join(plain) + "\n", encoding="utf-8")
        try:
            events, _ = history(Path(tmp))
        except (WorkflowError, ValueError) as exc:
            return [], str(exc)
        return [event_digest(e) for e in events], None


def _load(package):
    """打开压缩包：返回 (z, 任务清单, 各摘录的行)；坏的抛 WorkflowError(code, 文字)。"""
    D = _d()
    check = D.verify_package(package)
    if not check["ok"]:
        raise _Stop("package_damaged", {"errors": check["errors"][:5]}, "压缩包核对没通过：" + "；".join(check["errors"][:3]))
    z = zipfile.ZipFile(package)
    names = set(z.namelist())
    r = _r()
    if r.TASK_MANIFEST not in names:
        manifest = json.loads(z.read(D.MANIFEST_REL))
        if (manifest.get("package") or {}).get("kind") == "full":
            raise _Stop("full_package", {}, "包里的 package.kind 是 full")
        raise _Stop("unsupported_package", {}, "包里没有任务清单")
    try:
        tm = json.loads(z.read(r.TASK_MANIFEST))
    except ValueError:
        raise _Stop("unsupported_package", {}, "任务清单读不出来") from None
    if tm.get("format") != r.TASK_FORMAT or tm.get("kind") != "task" or not tm.get("task_id"):
        raise _Stop("unsupported_package", {}, "任务清单格式不认识")
    excerpts = {}
    for name in list(tm.get("records", {})):
        excerpts[name] = _lines(z.read(f"{r.EXCERPT_DIR}/{name}"))
    keep = {name: _lines(z.read(f"{r.KEEP_DIR}/{name}")) for name in tm.get("keep_only", {})}
    return z, tm, excerpts, keep


class _Stop(Exception):
    def __init__(self, situation, extra, technical):
        super().__init__(technical)
        self.situation, self.extra, self.technical = situation, extra, technical


def safe_target(rel):
    """包里的文件只许放进 project/ 下的资料位置：不能是工作包本身、项目状态、记录存储文件（记录只能走上面的追加），不能带上一级。
    大小写不敏感的磁盘上 project/Records/… 和 project/records/… 是同一处，所以先折叠大小写再判。"""
    import unicodedata
    from pathlib import PurePosixPath
    pp = PurePosixPath(rel) if isinstance(rel, str) else None
    if pp is None or pp.is_absolute() or ".." in pp.parts or "\\" in rel or not rel.startswith("project/"):
        return False
    fold = unicodedata.normalize("NFC", rel).casefold()
    if fold == "project/state.json" or fold.startswith("project/运行日志"):
        return False
    return not _r()._store_file(fold)


def trusted_rows(name, lines, task_id):
    """接收方再筛一遍包里的记录摘录行（发送方程序已经分过类，这里不信任包）：发客户记录只收“事实”类（发了什么、交了什么、对外版本）且是这个任务的；
    检核记录只收这个任务的独立检核；其他事实行必须是这个任务的；决定类（豁免、最终版、确认……）一律不收。"""
    R = _r()
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if name == "delivery-gate.jsonl":
            ok = row.get("task_id") == task_id and row.get("event") in R.FACT_EVENTS
        elif name == "phase6-events.jsonl":
            target = row.get("target") or {}
            ok = row.get("event") == "standalone_review" and target.get("space") == "standalone" and target.get("key") == task_id
        elif name == "agent-dispatch.jsonl":
            ok = bool(row.get("dispatch_id")) and row.get("task_id", task_id) == task_id
        else:
            ok = row.get("task_id") == task_id
        if ok:
            out.append(line)
    return out


def _identity_relation(pkg_ident, mine):
    pkg_has, my_has = any(pkg_ident.values()), any(mine.values())
    if not my_has:
        return "blank" if pkg_has else "blank_no_identity"
    if not pkg_has:
        return "identity_unknown"
    if pkg_ident.get("project_id") and mine.get("project_id"):
        same = _norm(pkg_ident["project_id"]) == _norm(mine["project_id"])
    elif pkg_ident.get("client_name") and mine.get("client_name"):
        same = _norm(pkg_ident["client_name"]) == _norm(mine["client_name"]) and \
            (not pkg_ident.get("brand_name") or not mine.get("brand_name") or _norm(pkg_ident["brand_name"]) == _norm(mine["brand_name"]))
    else:
        return "identity_unknown"
    return "same_client" if same else "different_client"


def _memory_diff(mine_lines, pkg_lines):
    """同一个客户已有项目记忆：不覆盖，逐条列出和包里不一致的（编号 n 从 1 起；带两边各自的说法，接手人说“第 n 条用包里的”时用得上）。"""
    def latest(lines):
        out = {}
        for line in lines:
            o = json.loads(line)
            out[o["memory_id"]] = o
        return out
    def key(o):
        return (o.get("project_id"), o.get("entity_type"), o.get("entity_id"), o.get("topic"), o.get("scope"))
    mine, pkg = latest(mine_lines), latest(pkg_lines)
    by_key = {key(o): o for o in mine.values()}
    diff = []
    for mid, row in pkg.items():
        have = mine.get(mid) or by_key.get(key(row))
        if have is not None and (have.get("current_value"), have.get("status")) == (row.get("current_value"), row.get("status")):
            continue
        diff.append({"n": len(diff) + 1, "memory_id": mid, "mine_memory_id": have.get("memory_id") if have else None, "topic": row.get("topic"),
                     "why": "两边写的不一样" if have else "只有包里有", "pkg_value": row.get("current_value"), "pkg_status": row.get("status"),
                     "mine_value": have.get("current_value") if have else None, "mine_status": have.get("status") if have else None})
    return diff


def _prepare(root, package, identity=None):
    """只读：核对并算出导入计划。返回 (状况代码, 计划 dict, 打开的压缩包)。出事抛 _Stop。
    identity：接手人当次明确选的客户项目处理方式（OVERRIDES）；package 就在工作区根目录时，它自己不算“根目录有未归档文件”。"""
    from standalone_store import event_digest, history
    D = _d()
    root = Path(root).resolve()
    try:
        D._require_workspace(root)
    except WorkflowError:
        raise _Stop("not_workspace", {}, "找不到 project/state.json、scripts 或 AGENTS.md") from None
    z, tm, excerpts, keep = _load(package)
    try:
        return _plan(root, z, tm, excerpts, keep, event_digest, history, identity, Path(package).name if Path(package).resolve().parent == root else None)
    except BaseException:
        z.close()
        raise


def _plan(root, z, tm, excerpts, keep, event_digest, history, identity=None, own_zip=None):
    D = _d()
    try:
        mine_events, _ = history(root)
    except WorkflowError as exc:
        raise _Stop("receiver_has_problems", {"errors": [str(exc)]}, f"接收方任务记录读不出来：{exc}") from None
    version = version_relation(tm.get("package_version"), D._package_version(root))
    if version["relation"] in ("package_newer", "unknown"):
        raise _Stop("version_differs", {"package_version": tm.get("package_version"), "workspace_version": D._package_version(root), "version": version},
                    f"包 {tm.get('package_version')}，这里 {D._package_version(root)}")
    task_id = tm["task_id"]
    relation = _identity_relation(tm.get("identity") or {}, D._identity(root))
    original_relation = relation
    if relation in ("different_client", "identity_unknown") or (relation in ("blank", "blank_no_identity") and not _blank(root, mine_events)):
        relation = "identity_unknown" if relation in ("blank", "blank_no_identity") else relation
        original_relation = relation
        if identity == "confirmed_same" and relation == "identity_unknown":
            relation = "same_client"                                   # 接手人当次明确说了“就是这个客户项目的”
        elif identity == "mixed":
            relation = "mixed_here"                                    # 接手人坚持放在这里：不采用原客户身份，项目记忆不合并
        else:
            raise _Stop(relation, {"package_identity": tm.get("identity"), "workspace_identity": D._identity(root)}, f"身份关系 {relation}")
    memory_mine = _lines(_read_jsonl_bytes(root / "project/records/project-memory.jsonl"))
    manifest = json.loads(z.read(D.MANIFEST_REL))
    member_sha = {m["name"]: m["sha256"] for m in manifest["package"]["members"]}
    # ---- 任务记录：比对两边的各版摘要
    pkg_lines = excerpts.get("standalone-tasks.jsonl", [])
    pkg_digests, err = _package_events(pkg_lines)
    if err:
        raise _Stop("package_damaged", {"errors": [err]}, "包里的任务记录读不通：" + err)
    mine = [event_digest(e) for e in mine_events if e["task_id"] == task_id]
    others = {e["request_id"] for e in mine_events if e["task_id"] != task_id}
    clash = [json.loads(x)["request_id"] for x in pkg_lines if json.loads(x)["request_id"] in others]
    if clash:
        raise _Stop("request_conflict", {"request_ids": clash[:3]}, "请求编号冲突：" + "、".join(clash[:3]))
    already = any((row.get("package") or {}).get("manifest_id") == manifest["manifest_id"] for row in _import_rows(root))
    if not mine:
        new_from = 0
    elif len(mine) < len(pkg_digests) and pkg_digests[:len(mine)] == mine:
        new_from = len(mine)
    elif len(mine) >= len(pkg_digests) and mine[:len(pkg_digests)] == pkg_digests:
        new_from = len(pkg_digests)              # 任务记录已经是同样或更新的；其余没放完的东西（文件、检核记录等）照样补
    else:
        raise _Stop("task_diverged", {"task_id": task_id}, "同编号任务两边内容不同，停下问（保留哪边 / 另存为新任务）")
    # ---- 项目记忆：空白工作区重放；同一客户已有的不覆盖，列不一致
    mem_lines = excerpts.get("project-memory.jsonl", [])
    memory = {"action": "none", "differences": []}
    if relation == "mixed_here":
        memory = {"action": "backup_only", "differences": []}         # 另一个客户的项目记忆不合并，只在备查里
    elif memory_mine:
        memory = {"action": "keep_mine", "differences": _memory_diff(memory_mine, mem_lines)}
    elif mem_lines and relation in ("blank", "blank_no_identity"):
        memory = {"action": "replay", "differences": [], "rows": len(mem_lines)}
    elif mem_lines and relation == "same_client":
        memory = {"action": "keep_mine", "differences": _memory_diff([], mem_lines)}
    # ---- 文件
    files, conflicts, append_only = [], [], set(tm.get("append_only_files", []))
    names = set(z.namelist())
    for item in tm["files"]:
        rel = item["path"]
        if not safe_target(rel):
            raise _Stop("unsupported_package", {"paths": [rel]}, f"任务清单里的 {rel} 不是可以放进项目资料位置的文件")
        if item.get("role") == _r().MEMORY_ROLE and memory["action"] != "replay":
            continue                                   # 项目记忆不重放（同一客户已有记忆）时，记忆引用的资料不用放，也不算冲突
        dest = root / rel
        if rel not in names or rel not in member_sha:
            raise _Stop("package_damaged", {"errors": [f"任务清单里的 {rel} 包里没有"]}, "任务清单与包内文件对不上")
        if any(p.is_symlink() for p in [dest, *dest.parents] if p == root or root in p.parents):
            conflicts.append(rel); continue
        if dest.exists():
            if not dest.is_file():
                conflicts.append(rel); continue
            if _sha_file(dest) == member_sha[rel]:
                continue
            if rel in append_only and z.read(rel).startswith(dest.read_bytes()):
                files.append({**item, "mode": "append"}); continue
            conflicts.append(rel); continue
        files.append({**item, "mode": "new"})
    if conflicts:
        raise _Stop("file_conflict", {"paths": conflicts[:5], "count": len(conflicts)}, "冲突文件：" + "、".join(conflicts[:5]))
    # ---- 记录行（跳过接收方已有的；再按白名单筛一遍，不信任包）
    rows, dropped, warnings = {}, 0, []
    bad_log = _import_log_unreadable(root)
    if bad_log:
        warnings.append(f"导入记录（{IMPORT_LOG}）有 {bad_log} 行读不出来，“这个包是不是已经导入过”的判断可能不准")
    for name in ("agent-dispatch.jsonl", "phase6-events.jsonl", "delivery-gate.jsonl"):
        have = {json.loads(x).get("record_id") for x in _lines(_read_jsonl_bytes(root / "project/records" / name))}
        sent = [x for x in excerpts.get(name, []) if json.loads(x).get("record_id") not in have]
        rows[name] = trusted_rows(name, sent, task_id)
        dropped += len(sent) - len(rows[name])
    for name in OTHER_FACTS:
        have = set(_lines(_read_jsonl_bytes(root / "project/records" / name)))
        sent = [x for x in excerpts.get(name, []) if x not in have]
        rows[name] = trusted_rows(name, sent, task_id)
        dropped += len(sent) - len(rows[name])
    # ---- 内存演练：拼好的记录用读取时同一套校验跑一遍
    raw = _read_jsonl_bytes(root / "project/records/standalone-tasks.jsonl")
    first_prev = event_digest(mine_events[-1]) if mine_events else None
    new_lines = _chain(pkg_lines[new_from:], pkg_digests[new_from:], first_prev)
    head = None
    if new_lines:
        candidate = raw + (b"" if not raw or raw.endswith(b"\n") else b"\n") + ("\n".join(new_lines) + "\n").encode("utf-8")
        head = {"schema_version": 1, "journal": "project/records/standalone-tasks.jsonl", "events": len(mine_events) + len(new_lines), "last_event_sha256": pkg_digests[-1]}
        err = _shadow_check(candidate, head, ("\n".join(mem_lines) + "\n").encode("utf-8") if memory["action"] == "replay" else None)
    elif memory["action"] == "replay":
        err = _shadow_check(raw, None, ("\n".join(mem_lines) + "\n").encode("utf-8"))
    else:
        err = None
    if err:
        raise _Stop("package_damaged", {"errors": [err]}, "内存演练拼接没通过：" + err)
    pending = bool(new_lines or files or any(rows.values()) or memory["action"] == "replay" or (relation == "blank" and any((tm.get("identity") or {}).values())))
    if not pending:
        raise _Stop("already_imported" if already else "nothing_to_import", {"task_id": task_id}, "没有要补的东西")
    problems = _own_zip_filtered(D._problems(root), own_zip)
    if problems:
        raise _Stop("receiver_has_problems", {"errors": problems[:5]}, "接收方续做状态有问题：" + "；".join(problems[:3]))
    situation = "append_newer" if (mine and new_lines) else relation
    plan = {"task_id": task_id, "title": tm.get("title"), "situation": situation, "new_events": len(new_lines), "new_lines": new_lines, "head": head, "files": files, "rows": rows,
            "memory": memory, "identity": tm.get("identity"), "identity_action": "adopt" if relation == "blank" else "none", "manifest_id": manifest["manifest_id"],
            "identity_choice": identity if relation in ("mixed_here",) or identity == "confirmed_same" else None, "original_relation": original_relation, "version": version,
            "created_at": tm.get("created_at"), "recipient": tm.get("recipient"), "from_actor": tm.get("actor"), "dependencies": tm.get("dependencies", []),
            "decision_rows_not_carried": {k: v for k, v in tm.get("decision_rows_not_carried", {}).items() if k != "agent-dispatch.jsonl"},
            "unfinished_dispatches": tm.get("unfinished_dispatches", []), "excerpts": excerpts, "keep": keep, "task_manifest_bytes": z.read(_r().TASK_MANIFEST),
            "memory_lines": mem_lines, "rows_dropped": dropped, "warnings": warnings}
    return situation, plan, z


def _import_log_unreadable(root):
    path = Path(root) / IMPORT_LOG
    if not path.is_file():
        return 0
    bad = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                json.loads(line)
            except ValueError:
                bad += 1
    return bad


def _import_rows(root):
    path = Path(root) / IMPORT_LOG
    out = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _chain(lines, digests, first_prev):
    """把要追加的原事件行接到接收方日志末尾：只重写 prev_line_sha256（它不在事件摘要里，原事件内容一个字不动）。"""
    out, prev = [], first_prev
    for line, digest in zip(lines, digests):
        obj = json.loads(line)
        obj["prev_line_sha256"] = prev
        out.append(json.dumps(obj, ensure_ascii=False, allow_nan=False))
        prev = digest
    return out


def check(root, package, identity=None):
    """只读：核对并说明现在是哪种情况。identity 见 _prepare。"""
    try:
        situation, plan, z = _prepare(root, package, identity)
        z.close()
    except _Stop as stop:
        return _result(root, stop.situation, stop.extra, stop.technical)
    except WorkflowError as exc:
        return {"status": "blocked", "situation": "package_damaged", "for_strategist": str(exc), "technical": str(exc), "written": False}
    summary = {"task": plan["title"], "new_events": plan["new_events"], "files_to_place": len(plan["files"]), "bytes": sum(f.get("bytes", 0) for f in plan["files"]),
               "records": {k: len(v) for k, v in plan["rows"].items() if v}, "memory": {k: v for k, v in plan["memory"].items() if k != "rows"},
               "dependencies": plan["dependencies"], "decision_rows_not_applied": plan["decision_rows_not_carried"], "version": plan["version"],
               "warnings": plan["warnings"], "rows_dropped": plan["rows_dropped"]}
    talk = SITUATION_WORDS[situation] + f"要放进来的是「{plan['title']}」，一共 {plan['new_events']} 个版本的记录、{len(plan['files'])} 个文件。"
    if plan["decision_rows_not_carried"]:
        talk += "原来的人说过的“不用检查直接发”“这就是最终版”这类话不会生效，只留一份备查；你要用得自己再说一次。"
    if plan["memory"]["differences"]:
        talk += f"你这里已有的项目记忆我不会覆盖；和包里不一致的有 {len(plan['memory']['differences'])} 条，接手后我们逐条看。"
    talk += "确认要接手的话，请你说“接手”。"
    return {"status": "ready", "situation": situation, "summary": summary, "for_strategist": talk, "written": False,
            "technical": f"python3 scripts/jiaojie_jieshou.py apply --workspace . --package <压缩包> --words \"<原话>\" --actor <实例> --json；清单 {plan['manifest_id']}"}


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _place(z, name, dest):
    """压缩包里的一个文件放到位（边读边写，大文件不整个读进内存）；先写临时文件再换名。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".writing-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as out, z.open(name) as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                out.write(chunk)
            out.flush(); os.fsync(out.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _append_lines(path, lines):
    if not lines:
        return 0
    raw = _read_jsonl_bytes(path)
    data = raw + (b"" if not raw or raw.endswith(b"\n") else b"\n") + ("\n".join(lines) + "\n").encode("utf-8")
    _atomic_write(path, data)
    return len(lines)


def apply(root, package, words, actor, identity=None):
    from registration_guard import actor_check
    from workspace_lock import serialized
    actor_check(actor)
    if not isinstance(words, str) or not words.strip():
        raise WorkflowError("接手须有接手人当次的原话（--words，比如“接手”），没有就不动任何文件")
    D, R = _d(), _r()
    root = Path(root).resolve()
    package = Path(package)
    with serialized(root):
        try:
            situation, plan, z = _prepare(root, package, identity)
        except _Stop as stop:
            return _result(root, stop.situation, stop.extra, stop.technical)
        import_id = "导入-" + plan["manifest_id"][:12]
        with z:
            backup = root / IMPORT_DIR / import_id
            # 1) 原记录备查（包里的原样，含决定类）
            for name, data in (("任务交接.json", plan["task_manifest_bytes"]), ("包清单.json", z.read(D.MANIFEST_REL))):
                _atomic_write(backup / name, data)
            note_path = json.loads(plan["task_manifest_bytes"]).get("note_path")
            if note_path and note_path in z.namelist():
                _atomic_write(backup / "交接说明.md", z.read(note_path))                  # 原来的人写给接手人的交接说明，留在备查里
            for name, lines in plan["excerpts"].items():
                _atomic_write(backup / "记录" / name, ("\n".join(lines) + "\n").encode("utf-8"))
            for name, lines in plan["keep"].items():
                _atomic_write(backup / "备查" / name, ("\n".join(lines) + "\n").encode("utf-8"))
            # 2) 文件
            placed = 0
            for item in plan["files"]:
                _place(z, item["path"], root / item["path"])
                placed += 1
            # 3) 身份（仅空白）
            ident_note = "none"
            if plan["identity_action"] == "adopt":
                from task_context import identity_status, save_identity
                ident = plan["identity"]
                if all(ident.get(k) for k in ("project_id", "project_name", "client_name", "brand_name")):
                    save_identity(root, {"request_id": "imp-" + plan["manifest_id"][:16], "expected_state_sha256": identity_status(root)["state_sha256"],
                                         "reason": "接手任务交接包，采用包里的客户项目身份", "identity": {k: ident[k] for k in ("project_id", "project_name", "client_name", "brand_name")}}, actor)
                    ident_note = "adopted"
                else:
                    ident_note = "incomplete_not_adopted"
            # 4) 项目记忆（仅空白重放）
            memory_rows = 0
            if plan["memory"]["action"] == "replay":
                memory_rows = _append_lines(root / "project/records/project-memory.jsonl", plan["memory_lines"])
            # 5) 分派
            counts = {}
            if plan["rows"]["agent-dispatch.jsonl"]:
                counts["agent-dispatch.jsonl"] = _append_lines(root / "project/records/agent-dispatch.jsonl", plan["rows"]["agent-dispatch.jsonl"])
                last = _lines(_read_jsonl_bytes(root / "project/records/agent-dispatch.jsonl"))[-1]
                from phase2_store import atomic_json, local
                atomic_json(local(root, "project/records/agent-dispatch-index.json"), {"schema_version": 1, "last_record_id": json.loads(last)["record_id"]})
            # 6) 独立任务记录
            if plan["new_events"]:
                from standalone_store import HEAD, history
                _append_lines(root / "project/records/standalone-tasks.jsonl", plan["new_lines"])
                _atomic_write(root / HEAD, json.dumps(plan["head"]).encode("utf-8"))      # 记录先写、锚点紧跟着写（和登记时同一顺序）
                history(root)                                                                # 写完按读取时同一套校验读一遍
                counts["standalone-tasks.jsonl"] = len(plan["new_lines"])
            # 7) 检核记录、事实行
            for name in ("phase6-events.jsonl", "delivery-gate.jsonl") + OTHER_FACTS:
                if plan["rows"].get(name):
                    counts[name] = _append_lines(root / "project/records" / name, plan["rows"][name])
            # 8) 导入事件
            event = {"schema_version": 1, "event": "task_imported", "import_id": import_id, "at": datetime.now().astimezone().isoformat(), "actor": actor, "words": words,
                     "situation": situation, "basis": BASIS,
                     "package": {"manifest_id": plan["manifest_id"], "zip_name": package.name, "zip_sha256": _sha_file(package), "created_at": plan["created_at"],
                                 "from_actor": plan["from_actor"], "recipient": plan["recipient"]},
                     "task": {"task_id": plan["task_id"], "title": plan["title"], "versions_imported": plan["new_events"]},
                     "files_placed": placed, "records_imported": {**counts, **({"project-memory.jsonl": memory_rows} if memory_rows else {})}, "rows_dropped_not_trusted": plan["rows_dropped"],
                     "decision_rows_not_imported": plan["decision_rows_not_carried"], "dependencies_read_only": plan["dependencies"],
                     "memory": {"action": plan["memory"]["action"], "differences": plan["memory"]["differences"]}, "identity": ident_note, "backup_dir": f"{IMPORT_DIR}/{import_id}",
                     "identity_relation": plan["original_relation"], "identity_choice": plan["identity_choice"], "version": plan["version"]}
            if plan["identity_choice"]:
                event["package_identity"], event["workspace_identity"] = plan["identity"], D._identity(root)      # 坚持放在这里 / 确认同一客户时，原客户身份与现在的身份都记下
            if (package.resolve().parent == root):
                event["package_moved_beside"] = True
            _append_lines(root / IMPORT_LOG, [json.dumps(event, ensure_ascii=False, allow_nan=False)])
        moved = stash_beside(root, package)               # 压缩包就放在工作区根目录的话，挪到文件夹旁边（不删），免得续做状态报“根目录有未归档文件”
        from workspace_resume import resume
        state = resume(root)
        errors = state.get("errors", [])
        talk = (f"接手好了：「{plan['title']}」的 {plan['new_events']} 个版本的记录和 {placed} 个文件已经放进来。" if plan["new_events"] else f"接手好了：「{plan['title']}」已经在你这里。")
        if plan["decision_rows_not_carried"]:
            talk += "原来的人说过的“不检查直接发”“最终版”“确认”这类话没有生效，要用得你自己再说一次。"
        if moved:
            talk += "压缩包原来放在工作区里，我把它挪到了文件夹旁边（没删）。"
        if errors:
            talk += f"不过放进来之后工作区的检查有 {len(errors)} 个问题，先别往下做，告诉我。"
        return {"status": "imported" if not errors else "imported_with_errors", "situation": situation, "import_id": import_id, "task_id": plan["task_id"],
                "files_placed": placed, "records_imported": event["records_imported"], "decision_rows_not_imported": plan["decision_rows_not_carried"],
                "memory": event["memory"], "memory_differences": event["memory"]["differences"], "resume_errors": errors, "for_strategist": talk, "written": True,
                "package_moved_to": str(moved) if moved else None, "warnings": plan["warnings"],
                "technical": f"导入事件 {IMPORT_LOG}；原记录备查 {IMPORT_DIR}/{import_id}/；续做状态 errors={len(errors)}"}


def main():
    parser = argparse.ArgumentParser(description="接手任务交接包（v1.7.8 N4 核心）：check 只读看现在是哪种情况；apply 确认后导入")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "apply"):
        s = sub.add_parser(name)
        s.add_argument("--workspace", type=Path, required=True)
        s.add_argument("--package", type=Path, required=True)
        s.add_argument("--json", action="store_true")
        s.add_argument("--identity", choices=list(OVERRIDES), help="接手人当次明确选的客户项目处理方式：confirmed_same 比对不了身份时他说就是同一个客户 / mixed 不同客户他坚持放在这里")
        if name == "apply":
            s.add_argument("--words", default="", help="接手人当次原话")
            s.add_argument("--actor", required=True)
    args = parser.parse_args()
    out = check(args.workspace, args.package, args.identity) if args.command == "check" else apply(args.workspace, args.package, args.words, args.actor, args.identity)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["status"] in {"ready", "imported", "already_imported", "nothing_to_import"} else 1


if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main

    def main():
        return cli(_business_main, __file__)

    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
