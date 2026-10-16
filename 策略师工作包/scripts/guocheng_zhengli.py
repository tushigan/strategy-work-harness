#!/usr/bin/env python3
"""过程文件管理（v1.7.8 J06）：把 project/ 里的文件分类，标最终版或策略师同意时，把过程文件、暂存、同内容多余副本移到归档。

分类（每个文件只落在一类；受保护的永远不移）：
  referenced 记录引用——任务记录 / 检核报告 / 检核基线 / 发客户记录 / 事实台账 / 分派记录与证据 / 登记请求里写到的路径，
             正在使用的文件（guidang._current），正式流程与系统管理的目录（outputs/proposal 等），project/records 本身
  product    成品——没被记录引用，但名字写明“最终 / 终稿 / 交付 / final / deliver”的文件，或放在这类名字的文件夹里的文件（一定带，不移）
  source     来源——project/inputs、briefs、research 下没被记录引用的文件（不移）
  process    过程——放在名叫“过程”的文件夹里的文件（可移）
  temp       暂存——tmp / temp / 暂存 / cache 文件夹、.tmp 等临时文件（可移；到处都认，含未登记大文件夹和任务文件夹内部）
  duplicate  重复副本——全量 SHA-256 相同、不受保护的多余份（可移；到处都认；每组至少留一份：优先留被引用的、成品类的、
             不是“像过程”的、路径最浅的那份）
  suspect    疑似过程——文件夹或文件名里带 过程 / 草稿 / 草图 / 生图 / 上色 / 批次 / 试稿 / 备份 / 旧版 / draft / sketch / wireframe /
             iteration / previous / backup / old 等词（“候选 / 最终 / 交付 / final”不算）；只在手动整理里、策略师回“好”后一起移，
             标最终版的自动整理不动它
  （页面引用，v1.7.8 r2）留下的 HTML / CSS / SVG / Markdown（成品、记录引用的、拿不准的、来源、未登记大文件夹里的）用到的图片、样式、字体等相对引用
             （src / href / url() / 引号里的相对文件名，顺着样式表继续找），被引用的过程 / 暂存 / 疑似过程 / 拿不准的文件改归“记录引用”，原因写“被成品页面引用”，
             不移、全量与任务交接照带；两份同内容的副本，留被页面引用的那份
  unregistered_dir  未登记大文件夹——outputs / exports 下不属于任何任务、没有任何文件被记录引用、体积达到提醒线的文件夹里，
             上面几类都不是的文件（只提醒，不移；策略师可以用 --include 点名移）
  unsure     拿不准——其余没被记录引用的文件（只列出，不移；策略师可以用 --include 点名移）
只移不删：移动沿用 guidang.archive（复制、核对指纹、写 archived 记录、再移走），目的地与 W02 版本保留相同，“<项目目录名>-归档/”；
本程序另在 project/records/archive.jsonl 记一条 event="organize" 的汇总（J06 过程文件整理）。

给其他阶段复用的函数接口（N3 交接打包、N5 接手引导）：
  classify(root, unregistered_bytes=UNREGISTERED_BYTES, hash_duplicates=True) -> dict
      {"files": [{"path","bytes","category","underlying","reason","task_id","keeper"}], "totals": {类别: {"files","bytes"}},
       "unregistered_dirs": [{"dir","bytes","files","subdirs"}], "tasks": {任务编号: [任务文件夹]}, "incomplete": bool, "warnings": [..],
       "estimate": bool}
      hash_duplicates=False 是“估计”模式：不读文件内容、不算指纹，结果里没有重复副本类（续做状态每次开工都跑，用它）；
      underlying 是去掉“重复副本”身份后本来的类别。
  select(result, task_id=None, suspects=False, include=()) -> {"chosen": [entry], "skipped": [{"path","reason"}], "include": [路径]}
      选出这次要移的：严格范围 = 过程 + 暂存 + 重复副本；suspects=True 再加疑似过程；include 是策略师点名的路径（受保护的、成品、来源跳过并说明）
  movable(result, task_id=None, suspects=False, include=()) -> [file entry]   select 的 chosen
  summarize(result, task_id=None) -> dict   各类别个数与大小、最大几项、疑似过程、未登记大文件夹
  preview(root, task_id=None) -> dict   只读，不写任何文件
  organize(root, actor, words, task_id=None, basis=BASIS_MANUAL, dry_run=False, suspects=False, include=()) -> dict   移动并记录
  auto_after_final(root, task_id, actor) -> dict   标最终版后的自动整理（严格范围）；任何失败只返回 status=failed，不抛出
  unregistered_dirs(root, threshold) -> [..]   不做哈希的轻量检查
"""
import argparse
import hashlib
import json
import os
import posixpath
import re
import urllib.parse
import uuid
from pathlib import Path

from phase2_store import WorkflowError, local, now

LOG = "project/records/archive.jsonl"
PROCESS_DIR = "过程"
TEMP_DIRS = {"tmp", "temp", "暂存", "cache", ".cache", "__pycache__"}
TEMP_FILE_RE = re.compile(r"(\.tmp|\.temp|\.swp|\.part|\.crdownload)$|^~\$|^\.~lock|^\.DS_Store$|^Thumbs\.db$", re.I)
FINAL_RE = re.compile(r"最终|终稿|交付|final|deliver", re.I)
# 疑似过程：文件夹或文件名里带这些词（中文直接包含；英文按独立的词认，可带复数 s，所以 golden 里的 old 不算）。
# 名字里同时有 候选 / 最终 / 终稿 / 交付 / final / deliver 的不算（成品优先）。只在手动整理里、策略师同意后才移。
SUSPECT_ZH = ("过程", "草稿", "草图", "生图", "上色", "批次", "试稿", "备份", "旧版", "旧稿", "淘汰", "废稿", "废弃", "中间稿", "临时")
SUSPECT_EN_RE = re.compile(r"(?<![a-z])(?:draft|sketch|wireframe|iteration|previous|backup|old|generation|intermediate|scratch)s?(?![a-z])", re.I)
NOT_SUSPECT_RE = re.compile(r"候选|最终|终稿|交付|final|deliver", re.I)
# 正式流程与系统管理的目录：整棵受保护，由各自的记录管理（不靠路径逐个对照）
SYSTEM_TREES = ("project/outputs/versions", "project/outputs/proposal", "project/outputs/brand-house", "project/outputs/control",
                "project/outputs/conflicts", "project/reviews", "project/tasks", "project/assets", "project/inputs/登记请求",
                "project/inputs/templates", "project/inputs/contracts", "project/inputs/evidence")
NEVER_LISTED = ("project/records", "project/运行日志", "project/state.json")
SOURCE_TREES = ("project/inputs", "project/briefs", "project/research")
CONTAINER_DIRS = ("project/outputs", "project/exports")   # 任务成果文件夹与未登记大文件夹都是它们的子文件夹
GENERIC_STOP = {"project", "project/outputs", "project/exports", "project/inputs"}
# 未登记大文件夹提醒线：100MB。理由：续做状态的总体积提示线是 300MB，一个没挂在任何任务上的文件夹就占掉其中三分之一，
# 值得问一句“要不要登记成任务”；再小的零散文件夹不打扰（实际遇到过 1.7GB 的未登记详情页文件夹，远超这条线）。
UNREGISTERED_BYTES = 100 * 1024 ** 2
ACTOR_AUTO = "J06自动整理"
BASIS_AUTO = "J06 过程文件整理（用户 2026-10-16 原话：“自動移出事後告訴我”“標最終版的時候”）"
WORDS_AUTO = BASIS_AUTO
BASIS_MANUAL = "J06 过程文件整理（手动：先预览、策略师当次回“好”后执行）"
CATEGORY_NAMES = {"referenced": "记录引用", "product": "成品", "source": "来源", "process": "过程", "temp": "暂存",
                  "duplicate": "重复副本", "suspect": "疑似过程", "unregistered_dir": "未登记大文件夹", "unsure": "拿不准"}
MOVABLE = ("process", "temp", "duplicate")       # 严格范围：标最终版的自动整理只动这三类（再限定在该任务文件夹里）
PARTIAL_BYTES = 64 * 1024                        # 大文件先比头尾各这么多字节，再比全量 SHA-256
# 重复副本里“留哪一份”的优先顺序：被记录引用的 > 成品 > 来源 > 普通 > 像过程的
KEEP_RANK = {"referenced": 0, "product": 1, "source": 2, "unregistered_dir": 3, "unsure": 3, "suspect": 4}
EXTRA_OK = ("unsure", "unregistered_dir", "suspect")          # 可以被判成“多余副本”的原类别
RECORD_KINDS = (("standalone-tasks", "任务记录"), ("delivery-gate", "发客户与交付记录"), ("review-baselines", "检核基线"),
                ("phase6-evidence", "检核报告"), ("phase6-events", "检核记录"), ("agent-dispatch", "分派记录与证据"),
                ("fact", "事实台账"), ("archive", "归档记录"))


def _fold(value):
    from guidang import _fold as fold
    return fold(value)


def _mb(size):
    return round(size / 1024 ** 2, 1)


def _human(size):
    return f"{_mb(size)}MB" if size >= 1024 ** 2 else "不到 1MB"


# ---------------- 记录里被引用的路径 ----------------

def _kind(rel):
    for key, label in RECORD_KINDS:
        if key in rel:
            return label
    return "记录"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def referenced_paths(root):
    """{折叠后的路径: {记录类别}}：project/records、tasks、state、素材库索引、登记请求里所有 “project/…” 开头的字符串。
    目录路径也算（其下整棵受保护）。读不出来的记录文件抛 WorkflowError（宁可不整理）。"""
    root = Path(root).resolve()
    found = {}
    files = []
    for base in ("project/records", "project/tasks", "project/assets", "project/inputs/登记请求"):
        folder = root / base
        if folder.is_dir():
            for path in folder.rglob("*"):
                if path.is_file() and not path.is_symlink() and path.suffix in {".json", ".jsonl"}:
                    files.append(path)
    state = root / "project/state.json"
    if state.is_file():
        files.append(state)
    for path in files:
        rel = path.relative_to(root).as_posix()
        label = _kind(rel)
        try:
            text = path.read_text(encoding="utf-8")
            rows = [json.loads(x) for x in text.splitlines() if x.strip()] if path.suffix == ".jsonl" else [json.loads(text)]
        except (OSError, ValueError) as exc:
            raise WorkflowError(f"记录文件 {rel} 读不出来（{exc}），为免移走被记录用到的文件，这次不整理") from None
        for row in rows:
            for item in _strings(row):
                item = item.strip()
                if item.startswith("./"):
                    item = item[2:]
                if item.startswith("project/") and len(item) < 1024:
                    found.setdefault(_fold(item.rstrip("/")), set()).add(label)
    return found


def _is_under(rel, base):
    rel, base = _fold(rel), _fold(base)      # macOS 文件名的 Unicode 形态、大小写可能不同：折叠后再比
    return rel == base or rel.startswith(base + "/")


def _protected_by(refs, rel):
    """路径本身或它的任一上级目录被记录引用 → 返回记录类别集合，否则 None。"""
    parts = rel.split("/")
    hit = set()
    for i in range(1, len(parts) + 1):
        kinds = refs.get(_fold("/".join(parts[:i])))
        if kinds:
            hit |= kinds
    return hit or None


# ---------------- 成品页面内部的引用（全盘细查 P1-1） ----------------
PAGE_SUFFIXES = {".html", ".htm", ".css", ".svg", ".md", ".markdown"}
PAGE_READ_LIMIT = 8 * 1024 * 1024
PAGE_KEEPERS = ("referenced", "product", "source", "unsure", "unregistered_dir")        # 这些类别里的页面会被留下，它们用到的文件跟着受保护
PAGE_PROTECTABLE = ("process", "temp", "suspect", "unsure", "unregistered_dir")
_DQ_REF = re.compile(r'''"([^"\r\n<>]{1,400})"''')
_SQ_REF = re.compile(r"""'([^'\r\n<>]{1,400})'""")
_URL_REF = re.compile(r"""url\(\s*(?:"([^"]+)"|'([^']+)'|([^)'"\s][^)]*?))\s*\)""", re.I)
_MD_REF = re.compile(r"\]\(\s*(<[^>\n]+>|[^)\n]+)\)")
_TOKEN_SPLIT = re.compile(r"[\s\"'`<>()=,;{}\[\]|\\]+")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
TOKEN_MAX = 400


def _ref_strings(text):
    """页面文字里可能是文件引用的字符串：双引号、单引号里的（各自独立配对）、url()、Markdown 链接，再加上按分隔符切出来的零散词
    （JSON 里的图片路径、被引号配错位的情况也能抓到；词的长度有上限，内嵌的大段 base64 不会拖慢）。"""
    for pattern in (_DQ_REF, _SQ_REF):
        for match in pattern.finditer(text):
            yield match.group(1)
    for match in _URL_REF.finditer(text):
        yield next(g for g in match.groups() if g)
    for match in _MD_REF.finditer(text):
        raw = match.group(1).strip()
        raw = raw[1:raw.index(">")] if raw.startswith("<") else re.sub(r"""\s+["'][^"']*["']\s*$""", "", raw)
        yield raw
    for token in _TOKEN_SPLIT.split(text):
        if 2 < len(token) <= TOKEN_MAX and "." in token:
            yield token


def page_ref_candidates(rel, text):
    """页面（HTML / CSS / SVG / Markdown）文字里写到的相对文件引用 → [project/ 开头的路径]（只算出路径，不检查存在）。"""
    base = posixpath.dirname(rel)
    out, seen = [], set()
    for raw in _ref_strings(text):
        pieces = [x.strip().split()[0] for x in raw.split(",") if x.strip()] if "," in raw else [raw]
        for piece in pieces:
            cand = piece.strip().split("#", 1)[0].split("?", 1)[0]
            if not cand or cand.startswith("/") or _SCHEME.match(cand):
                continue
            cand = urllib.parse.unquote(cand)
            if len(cand) > 300 or not _EXT.search(cand) or any(len(seg) > 200 for seg in cand.split("/")):      # SVG 路径数据、整段文字这类不是文件名
                continue
            target = cand if cand.startswith("project/") else posixpath.normpath(posixpath.join(base, cand))
            if target.startswith("project/") and ".." not in target.split("/") and target not in seen:
                seen.add(target)
                out.append(target)
    return out


def _is_page(rel, size):
    return Path(rel).suffix.lower() in PAGE_SUFFIXES and size <= PAGE_READ_LIMIT


def _read_page(root, rel):
    try:
        return (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _protect_by_pages(root, entries):
    """留下的页面用到的文件并入受保护（顺着样式表等继续找，直到没有新的为止）。返回被改归“记录引用”的条目。"""
    by_fold = {_fold(e["path"]): e for e in entries.values()}
    queue = [e for e in entries.values() if e["category"] in PAGE_KEEPERS and _is_page(e["path"], e["bytes"])]
    seen, protected = set(), []
    while queue:
        page = queue.pop()
        if page["path"] in seen:
            continue
        seen.add(page["path"])
        for target in page_ref_candidates(page["path"], _read_page(root, page["path"])):
            hit = by_fold.get(_fold(target))
            if hit is None or hit is page or hit["category"] not in PAGE_PROTECTABLE:
                continue
            hit.update(was=hit["category"], category="referenced", page_ref=page["path"], underlying=None,
                       reason=f"被成品页面引用：{Path(page['path']).name}（因此不移）")
            protected.append(hit)
            if _is_page(hit["path"], hit["bytes"]):
                queue.append(hit)
    return protected


def page_closure(root, starts):
    """从一批已决定要带的文件出发，顺着成品页面的相对引用找到磁盘上真有的文件（传递闭包）。返回 {目标路径: 引用它的页面}。只读，用于任务交接。"""
    root = Path(root).resolve()
    found, queue, seen = {}, [s for s in starts if Path(s).suffix.lower() in PAGE_SUFFIXES], set()
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        path = root / rel
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > PAGE_READ_LIMIT:
                continue
        except OSError:
            continue
        for target in page_ref_candidates(rel, _read_page(root, rel)):
            if any(_is_under(target, n) for n in NEVER_LISTED) or target in found:
                continue
            tpath = root / target
            try:
                ok = tpath.is_file() and not tpath.is_symlink()
            except OSError:           # 名字太长、含非法字符：不是真有这个文件
                ok = False
            if ok:
                found[target] = rel
                if Path(target).suffix.lower() in PAGE_SUFFIXES:
                    queue.append(target)
    return found


# ---------------- 任务与任务文件夹 ----------------

def task_folders(root):
    """{任务编号: [任务文件夹相对路径]}：由该任务所有版本的交付物所在目录向上取，到 outputs / exports 之下为止；
    与别的任务共用的上级目录不算（避免把别人的文件当成这个任务的）。"""
    from standalone_store import history
    events, _ = history(Path(root).resolve())
    deliverables = {}
    for event in events:
        task = event["task_id"]
        for item in event["task"].get("deliverables", []):
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                deliverables.setdefault(task, set()).add(item["path"])
    result = {}
    for task, paths in deliverables.items():
        others = [p for t, ps in deliverables.items() if t != task for p in ps]
        folders = []
        for path in sorted(paths):
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            while parent and parent not in GENERIC_STOP and parent not in CONTAINER_DIRS:
                if not any(_is_under(other, parent) for other in others) and parent not in folders:
                    folders.append(parent)
                parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
        result[task] = sorted(folders)
    return result


def _task_of(rel, folders):
    for task, items in folders.items():
        if any(_is_under(rel, f) for f in items):
            return task
    return None


# ---------------- 分类 ----------------

def _in_use(root):
    from guidang import _current
    return {_fold(p) for p in _current(root)}


def _walk(root):
    """project/ 下所有普通文件（相对路径、大小）；不进符号链接，不列记录、状态、运行日志。"""
    base = Path(root).resolve() / "project"
    out, skipped = [], []
    for current, dirs, names in os.walk(base):
        rel_dir = Path(current).relative_to(base.parent).as_posix()
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(current, d))
                         and not any(_is_under(f"{rel_dir}/{d}", n) for n in NEVER_LISTED))
        for name in sorted(names):
            rel = f"{rel_dir}/{name}"
            if any(_is_under(rel, n) for n in NEVER_LISTED):
                continue
            path = os.path.join(current, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not os.path.isfile(path) or os.path.islink(path):
                skipped.append(rel)
                continue
            out.append((rel, info.st_size))
    return out, skipped


def _container_child(rel):
    """outputs / exports 之下的第一级子文件夹（任务成果文件夹或未登记文件夹），否则 None。"""
    for base in CONTAINER_DIRS:
        if rel.startswith(base + "/"):
            rest = rel[len(base) + 1:]
            if "/" in rest:
                return f"{base}/{rest.split('/', 1)[0]}"
    return None


def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _partial(path, size):
    """先比一遍便宜的：小文件直接是全量 SHA-256（"f:"），大文件只读头尾各一小段（"p:"），头尾不同就不必读全文。"""
    if size <= 2 * PARTIAL_BYTES:
        return "f:" + _sha(path)
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        digest.update(stream.read(PARTIAL_BYTES))
        stream.seek(-PARTIAL_BYTES, os.SEEK_END)
        digest.update(stream.read(PARTIAL_BYTES))
    return "p:" + digest.hexdigest()


def _suspect_name(parts):
    """名字（任一级文件夹或文件名）像过程文件，且整条路径里没有 候选 / 最终 / 交付 / final 这类成品字样。parts 是 project/ 之下的各级名字。"""
    inner = parts[2:] if len(parts) > 2 else parts[-1:]
    text = "/".join(inner)
    if NOT_SUSPECT_RE.search(text):
        return False
    return any(word in text for word in SUSPECT_ZH) or bool(SUSPECT_EN_RE.search(text))


def _looks_like(name):
    """未登记大文件夹的一个子文件夹，单凭名字看起来像什么：暂存 / 过程 / 成品 / 拿不准（只给策略师参考，由她决定）。"""
    folded = _fold(name)
    if folded in TEMP_DIRS:
        return "暂存"
    if FINAL_RE.search(name):
        return "成品"
    if folded == PROCESS_DIR or _suspect_name(["project", "outputs", name]):
        return "过程"
    return "拿不准"


def _subdirs(entries, child, limit=5):
    """一个未登记大文件夹里最大的几个子文件夹（直接放在文件夹本层的文件合成一项）。"""
    groups = {}
    for entry in entries:
        rest = entry["path"][len(child) + 1:]
        key = rest.split("/", 1)[0] if "/" in rest else None
        slot = groups.setdefault(key, {"bytes": 0, "files": 0})
        slot["bytes"] += entry["bytes"]; slot["files"] += 1
    out = []
    for key, slot in sorted(groups.items(), key=lambda x: -x[1]["bytes"])[:limit]:
        out.append({"dir": f"{child}/{key}" if key else child + "（本层的文件）", **slot, "looks_like": _looks_like(key) if key else "拿不准", "top_level": key is None})
    return out


def classify(root, unregistered_bytes=UNREGISTERED_BYTES, hash_duplicates=True):
    root = Path(root).resolve()
    warnings, incomplete = [], False
    refs = referenced_paths(root)
    try:
        busy = _in_use(root)
    except Exception as exc:  # 判断不了哪些在用：宁可什么都不让移
        busy, incomplete = set(), True
        warnings.append(f"判断不了哪些文件正在使用（{exc}），这次只能看、不能整理")
    folders = task_folders(root)
    walked, skipped = _walk(root)
    if skipped:
        warnings.append(f"有 {len(skipped)} 个符号链接或特殊文件没有列入（不会被移动）")
    entries = {}
    for rel, size in walked:
        key = _fold(rel)
        entry = {"path": rel, "bytes": size, "category": None, "underlying": None, "reason": "", "task_id": _task_of(rel, folders), "keeper": None, "page_ref": None, "was": None}
        kinds = _protected_by(refs, rel)
        if kinds:
            entry.update(category="referenced", reason="记录里引用：" + "、".join(sorted(kinds)))
        elif key in busy:
            entry.update(category="referenced", reason="正在使用（当前版本的来源、交付物或分派文件）")
        elif any(_is_under(rel, tree) for tree in SYSTEM_TREES):
            entry.update(category="referenced", reason="正式流程或系统管理的文件")
        entries[rel] = entry
    # 每个第一级子文件夹是否登记过：里面有没有被记录引用 / 在用 / 属于某个任务的文件
    groups = {}
    for entry in entries.values():
        child = _container_child(entry["path"])
        if child and not any(_is_under(child, tree) for tree in SYSTEM_TREES):
            group = groups.setdefault(child, {"bytes": 0, "files": 0, "registered": False})
            group["bytes"] += entry["bytes"]; group["files"] += 1
            if entry["category"] == "referenced" or entry["task_id"]:
                group["registered"] = True
    unregistered = {d: g for d, g in groups.items() if not g["registered"] and g["bytes"] >= unregistered_bytes}
    for entry in entries.values():
        if entry["category"]:
            continue
        rel = entry["path"]; parts = rel.split("/"); name = parts[-1]
        dirs = [_fold(x) for x in parts[:-1]]
        inner_dirs = "/".join(parts[2:-1])
        # 顺序：放进“过程”文件夹的（用户明确放的）> 名字写明最终的成品 > 暂存 > 来源 > 放在最终 / 交付名字文件夹里的成品 > 像过程 > 其余
        if PROCESS_DIR in dirs:
            entry.update(category="process", reason="放在“过程”文件夹里")
        elif FINAL_RE.search(name):
            entry.update(category="product", reason="名字写明最终或交付（不移）")
        elif any(d in TEMP_DIRS for d in dirs) or TEMP_FILE_RE.search(name):
            entry.update(category="temp", reason="暂存或临时文件")
        elif any(_is_under(rel, tree) for tree in SOURCE_TREES):
            entry.update(category="source", reason="来源资料（不移）")
        elif inner_dirs and FINAL_RE.search(inner_dirs):
            entry.update(category="product", reason="放在名字写明最终或交付的文件夹里（不移）")
        elif _suspect_name(parts):
            entry.update(category="suspect", reason="名字像草稿 / 生图 / 备份这类过程文件，但没放进“过程”文件夹，要你同意才移")
        elif _container_child(rel) in unregistered:
            entry.update(category="unregistered_dir", reason="不属于任何任务的大文件夹，先问要不要登记成任务")
        else:
            entry.update(category="unsure", reason="没被任何记录引用，也说不清是什么")
        entry["underlying"] = entry["category"]
    _protect_by_pages(root, entries)         # 留下的页面用到的图片、样式并入受保护（先于重复副本：重复的两份里，被页面引用的那份优先留下）
    # 重复副本：全量 SHA-256 相同。到处都认（含未登记大文件夹、任务文件夹内部），但只把不受保护的多余份算进去，每组至少留一份。
    # 过程 / 暂存里的副本本来就是可移的，不参与；估计模式（续做状态用）不读文件内容，不做这一步。
    if hash_duplicates:
        by_size = {}
        for entry in entries.values():
            if entry["bytes"] > 0 and entry["category"] in KEEP_RANK:
                by_size.setdefault(entry["bytes"], []).append(entry)
        for size, group in by_size.items():
            if len(group) < 2 or not any(e["category"] in EXTRA_OK for e in group):
                continue
            buckets = {}
            for entry in group:
                try:
                    buckets.setdefault(_partial(root / entry["path"], size), []).append(entry)
                except OSError:
                    continue
            for tag, bucket in buckets.items():
                if len(bucket) < 2 or not any(e["category"] in EXTRA_OK for e in bucket):
                    continue
                if tag.startswith("f:"):
                    same_sets = [bucket]
                else:
                    by_hash = {}
                    for entry in bucket:
                        try:
                            by_hash.setdefault(_sha(root / entry["path"]), []).append(entry)
                        except OSError:
                            continue
                    same_sets = list(by_hash.values())
                for same in same_sets:
                    extras = [e for e in same if e["category"] in EXTRA_OK]
                    if len(same) < 2 or not extras:
                        continue
                    keeper = sorted(same, key=lambda e: (KEEP_RANK[e["category"]], len(e["path"]), e["path"]))[0]
                    for entry in extras:
                        if entry is not keeper:
                            entry.update(category="duplicate", reason=f"和 {keeper['path']} 内容完全相同（SHA-256 一致），多余的一份", keeper=keeper["path"])
    ordered = sorted(entries.values(), key=lambda e: e["path"])
    totals = {name: {"files": 0, "bytes": 0} for name in CATEGORY_NAMES}
    for entry in ordered:
        totals[entry["category"]]["files"] += 1
        totals[entry["category"]]["bytes"] += entry["bytes"]
    unreg_list = []
    for d, g in sorted(unregistered.items(), key=lambda x: -x[1]["bytes"]):
        inside = [e for e in ordered if _is_under(e["path"], d)]
        unreg_list.append({"dir": d, "bytes": g["bytes"], "files": g["files"], "subdirs": _subdirs(inside, d)})
    return {"files": ordered, "totals": totals, "tasks": folders, "incomplete": incomplete, "warnings": warnings,
            "unregistered_dirs": unreg_list, "unregistered_bytes": unregistered_bytes, "estimate": not hash_duplicates}


def _norm_include(value):
    """策略师点名的路径：只能是 project/ 里的相对路径；绝对路径、上一级、记录与日志目录都拒绝。"""
    text = str(value).strip().replace("\\", "/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not text or text.startswith("/") or ".." in parts or not parts or parts[0] != "project":
        raise WorkflowError(f"点名的路径「{value}」不对：要写项目里的相对路径，从 project/ 开头（例如 project/outputs/某个文件夹），不能是绝对路径或上一级")
    rel = "/".join(parts)
    if any(_is_under(rel, n) for n in NEVER_LISTED):
        raise WorkflowError(f"点名的路径「{value}」是记录、日志或状态文件，不能整理")
    return rel


def _skip_reason(entry):
    if entry["category"] == "referenced":
        return entry["reason"] if entry["reason"].startswith(("记录里", "正在使用")) else "记录里用到或正在使用（" + entry["reason"] + "）"
    if entry["category"] == "product":
        return "名字写明最终或交付的成品，不移"
    return "来源资料，不移"


def select(result, task_id=None, suspects=False, include=()):
    """选出这次要移的文件。严格范围 = 过程 + 暂存 + 重复副本；suspects=True 再加疑似过程；include 是策略师点名的路径（不能和 task_id 同用）。
    重复副本的“留下的那份”如果这次也要移走，多余的那份就不移了，保证每组至少留一份（点名的、和疑似过程一起移的除外）。"""
    files = result["files"]
    includes = [_norm_include(x) for x in include]
    if includes and task_id is not None:
        raise WorkflowError("点名移（--include）不能和单个任务范围一起用")
    base = set()
    for entry in files:
        if task_id is not None and entry["task_id"] != task_id:
            continue
        if entry["category"] in MOVABLE or (suspects and (entry["category"] == "suspect" or (entry["category"] == "duplicate" and entry["underlying"] == "suspect"))):
            base.add(entry["path"])
    explicit, skipped, missing = set(), [], []
    for inc in includes:
        hits = [e for e in files if _is_under(e["path"], inc)]
        if not hits:
            missing.append(inc)
        for entry in hits:
            if entry["category"] in {"referenced", "product", "source"}:
                skipped.append({"path": entry["path"], "reason": _skip_reason(entry)})
            else:
                explicit.add(entry["path"])
    if missing:
        raise WorkflowError("点名的这些路径在项目里找不到，或里面没有可整理的文件，整批没有移动：" + "、".join(missing))
    moving = base | explicit
    drop = {e["path"] for e in files if e["path"] in base and e["path"] not in explicit and e["category"] == "duplicate" and e["keeper"] in moving
            and not (suspects and e["underlying"] == "suspect")}
    chosen = []
    for entry in files:
        if entry["path"] in moving and entry["path"] not in drop:
            chosen.append({**entry, "move_kind": entry["category"] if entry["path"] in base else "named"})
    return {"chosen": chosen, "skipped": skipped, "include": includes}


def movable(result, task_id=None, suspects=False, include=()):
    """可移的文件：严格范围是过程、暂存、重复副本（不含受保护的、拿不准的、未登记大文件夹里其余的）；task_id 给出时只取该任务文件夹里的。"""
    return select(result, task_id, suspects, include)["chosen"]


def unregistered_dirs(root, threshold=UNREGISTERED_BYTES):
    """轻量检查（不做哈希、不读文件内容）：outputs / exports 下不属于任何任务、没有文件被记录引用或在用、体积达到提醒线的第一级子文件夹。
    记录读不出来或判断不了在用文件时返回空列表（只提醒，不因此报错）。"""
    root = Path(root).resolve()
    try:
        refs = referenced_paths(root); busy = _in_use(root); folders = task_folders(root)
        walked, _ = _walk(root)
    except Exception:
        return []
    groups = {}
    for rel, size in walked:
        child = _container_child(rel)
        if not child or any(_is_under(child, tree) for tree in SYSTEM_TREES):
            continue
        group = groups.setdefault(child, {"bytes": 0, "files": 0, "registered": False})
        group["bytes"] += size; group["files"] += 1
        if _protected_by(refs, rel) or _fold(rel) in busy or _task_of(rel, folders):
            group["registered"] = True
    return [{"dir": d, "bytes": g["bytes"], "files": g["files"]} for d, g in sorted(groups.items(), key=lambda x: -x[1]["bytes"])
            if not g["registered"] and g["bytes"] >= threshold]


# ---------------- 说明与预览 ----------------

def _largest(entries, limit=5):
    return [{"path": e["path"], "mb": _mb(e["bytes"]), "category": CATEGORY_NAMES[e["category"]]}
            for e in sorted(entries, key=lambda e: -e["bytes"])[:limit]]


def _folder_groups(entries, limit=5):
    """按“任务文件夹 / 第一级子文件夹”合并，列出占地最大的几处（比一个个文件更好认）。"""
    groups = {}
    for entry in entries:
        parts = entry["path"].split("/")
        key = "/".join(parts[:4]) if len(parts) >= 5 else "/".join(parts[:-1])
        slot = groups.setdefault(key, {"bytes": 0, "files": 0})
        slot["bytes"] += entry["bytes"]; slot["files"] += 1
    return [{"folder": key, "mb": _mb(v["bytes"]), "bytes": v["bytes"], "files": v["files"]}
            for key, v in sorted(groups.items(), key=lambda x: -x[1]["bytes"])[:limit]]


def summarize(result, task_id=None):
    mv = movable(result, task_id)
    by_cat = {}
    for entry in mv:
        slot = by_cat.setdefault(entry["category"], {"files": 0, "bytes": 0})
        slot["files"] += 1; slot["bytes"] += entry["bytes"]
    scoped = lambda e: task_id is None or e["task_id"] == task_id
    unsure = [e for e in result["files"] if e["category"] == "unsure" and scoped(e)]
    suspect = [e for e in result["files"] if e["category"] == "suspect" and scoped(e)]
    page = [e for e in result["files"] if e.get("page_ref") and e.get("was") in ("process", "temp", "suspect") and scoped(e)]
    pages = sorted({Path(e["page_ref"]).name for e in page})
    return {"page_protected": {"files": len(page), "bytes": sum(e["bytes"] for e in page), "pages": pages[:5], "largest": _largest(page, 3)},
            "movable": {"files": len(mv), "bytes": sum(e["bytes"] for e in mv), "by_category": by_cat},
            "largest_movable": _largest(mv), "movable_folders": _folder_groups(mv),
            "suspect": {"files": len(suspect), "bytes": sum(e["bytes"] for e in suspect), "largest": _largest(suspect),
                        "folders": _folder_groups(suspect)},
            "unsure": {"files": len(unsure), "bytes": sum(e["bytes"] for e in unsure)},
            "largest_unsure": _largest(unsure, 3), "unregistered_dirs": result["unregistered_dirs"]}


LOOKS_SAY = {"暂存": "看着像暂存", "过程": "看着像过程文件", "成品": "看着像成品", "拿不准": "我拿不准"}
SAY_NAMES = {"process": "过程文件", "temp": "暂存", "duplicate": "重复副本", "suspect": "看名字像过程的", "named": "你点名的"}


def _says(by_cat):
    parts = []
    for key in ("process", "temp", "duplicate", "suspect", "named"):
        if key in by_cat:
            parts.append(f"{SAY_NAMES[key]} {by_cat[key]['files']} 个（{_human(by_cat[key]['bytes'])}）")
    return "、".join(parts)


def _clean(name):
    """名字放进给策略师的话里会不会被说人话扫描报命中（带英文连字符、编号、路径这类）；能放就返回带引号的名字，否则 None。"""
    try:
        from shuo_renhua import scan_text
        return f"「{name}」" if not scan_text(f"「{name}」")["hits"] else None
    except Exception:
        return f"「{name}」" if not re.search(r"[A-Za-z]", name) else None


def _label(folder):
    """“任务文件夹 / 子文件夹”能放进白话就放，不行退到只放子文件夹名，再不行返回 None（那时只说“一处名字是英文的文件夹”，完整名字在清单里）。"""
    parts = folder.split("/")
    for name in (("/".join(parts[2:4]) if len(parts) > 3 else None), parts[-1]):
        if name and _clean(name):
            return _clean(name)
    return None


def _named(items, limit=3):
    """[(文件夹, 字节)] → “「A」12MB、一处名字是英文的文件夹 8MB”；完整名字在预览的清单里。"""
    return "、".join(f"{_label(folder) or '一处名字是英文的文件夹'}{_human(size)}" for folder, size in items[:limit])


def preview(root, task_id=None):
    """只读：按类别列出可整理的东西、疑似过程、拿不准的和未登记的大文件夹，不写任何文件。"""
    root = Path(root).resolve()
    result = classify(root)
    info = summarize(result, task_id)
    move, sus = info["movable"], info["suspect"]
    archive = f"{root.name}-归档"
    if result["incomplete"]:
        talk = "这次没法判断哪些文件正在使用，所以先不整理，只能看。"
    elif not move["files"] and not sus["files"]:
        talk = "我看了一遍，没有需要整理的过程文件、暂存或重复副本。"
    else:
        talk = ""
        if move["files"]:
            talk += f"确定可以整理的有：{_says(move['by_category'])}，共 {_human(move['bytes'])}。"
        if sus["files"]:
            names = _named([(g["folder"], g["bytes"]) for g in sus["folders"]])
            talk += (f" 另有 {sus['files']} 个文件（{_human(sus['bytes'])}）看名字像过程文件（名字里有草稿、生图、备份、旧版这类词），最大的几处是{names}；"
                     "我不敢直接认定，你回“好”就连它们一起移。")
        talk += (f" 整理 = 移到项目旁边的「{archive}」文件夹，不删，要用随时搬回。"
                 "记录里用到的、检查用到的、已发客户的、正在用的文件，以及成品和来源，一个都不会动。")
    if info["page_protected"]["files"]:
        who = "、".join(x for x in (_clean(n) for n in info["page_protected"]["pages"][:3]) if x) or "几个成品页面"
        talk += (f" 另有 {info['page_protected']['files']} 个文件（{_human(info['page_protected']['bytes'])}）放在过程或暂存里，"
                 f"但被成品页面（{who}）用到了，所以不移。")
    if info["unsure"]["files"]:
        talk += f" 另有 {info['unsure']['files']} 个我拿不准的文件（{_human(info['unsure']['bytes'])}），不会动；你知道其中哪些是过程文件，告诉我文件夹名，我一起移。"
    if info["unregistered_dirs"]:
        parts = []
        for d in info["unregistered_dirs"][:3]:
            subs = "、".join((f"直接放在里面的文件{_human(s['bytes'])}" if s["top_level"] else
                             f"{_clean(s['dir'].rsplit('/', 1)[-1]) or '一处名字是英文的文件夹'}{_human(s['bytes'])}") + f"（{LOOKS_SAY[s['looks_like']]}）"
                            for s in d["subdirs"][:3])
            parts.append(f"{_clean(d['dir'].rsplit('/', 1)[-1]) or '一处名字是英文的文件夹'}{_human(d['bytes'])}（里面最大的有：{subs}）")
        talk += (" 还有不属于任何任务的大文件夹：" + "；".join(parts) + "。要不要登记成任务？"
                 "不登记、只想把其中的过程文件移走也行：你点名是哪几块，我一起移。")
    if (move["files"] or sus["files"]) and not result["incomplete"]:
        talk += " 你回“好”，我就整理" + ("上面这些（连看名字像过程的）" if sus["files"] else "") + "。"
        if sus["files"] and move["files"]:
            talk += "只想移确定的，就说“只移确定的”。"
    return {"status": "preview", "workspace": root.name, "scope": task_id or "全部", "archive_folder": archive, **info,
            "all_if_agreed": {"files": move["files"] + sus["files"], "bytes": move["bytes"] + sus["bytes"]},
            "warnings": result["warnings"], "for_strategist": talk,
            "technical": "分类与保护规则见 scripts/guocheng_zhengli.py 顶部说明；执行：python3 scripts/guocheng_zhengli.py organize --workspace . "
                         "--words \"<策略师当次回复的原话>\" --actor <实例> [--only-sure 只移确定的] [--include <project/ 开头的路径> 可重复，策略师点名要移的]"
                         + (f" --task-id {task_id}" if task_id else "")}


# ---------------- 执行 ----------------

def _log_summary(root, row):
    path = local(root, LOG); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"schema_version": 1, "event": "organize", "created_at": now(), **row}, ensure_ascii=False) + "\n")
        stream.flush(); os.fsync(stream.fileno())


def organize(root, actor, words, task_id=None, basis=BASIS_MANUAL, dry_run=False, suspects=False, include=()):
    """把可移的文件移到归档（只移不删），记录，返回白话说明。task_id 给出时只动该任务文件夹里的。
    suspects=True（手动整理、策略师同意）时带上疑似过程；include 是策略师点名的路径。标最终版的自动整理两者都不用，保持严格范围。"""
    from guidang import archive, destination_root
    from workspace_lock import serialized
    root = Path(root).resolve()
    with serialized(root):
        result = classify(root)
        if result["incomplete"]:
            raise WorkflowError("判断不了哪些文件正在使用，这次不整理。" + "；".join(result["warnings"]))
        picked = select(result, task_id, suspects, include)
        chosen, skipped = picked["chosen"], picked["skipped"]
        inc_report = None
        if picked["include"]:
            by_reason = {}
            for item in skipped:
                by_reason[item["reason"]] = by_reason.get(item["reason"], 0) + 1
            inc_report = {"requested": picked["include"], "moved_files": len([e for e in chosen if e["move_kind"] == "named"]),
                          "skipped": {"files": len(skipped), "by_reason": by_reason, "samples": skipped[:20]}}
        if not chosen:
            return {"status": "nothing", "moved_files": 0, "moved_bytes": 0, "for_strategist": "", "scope": task_id or "全部",
                    **({"include": inc_report} if inc_report else {})}
        paths = [e["path"] for e in chosen]
        counts = {}
        for entry in chosen:
            slot = counts.setdefault(entry["move_kind"], {"files": 0, "bytes": 0})
            slot["files"] += 1; slot["bytes"] += entry["bytes"]
        total = sum(e["bytes"] for e in chosen)
        if dry_run:
            return {"status": "dry_run", "moved_files": len(paths), "moved_bytes": total, "by_category": counts, "items": paths[:50],
                    **({"include": inc_report} if inc_report else {})}
        batch = uuid.uuid4().hex[:12]
        archive(root, paths, words, actor, basis=basis, extra={"origin": "J06", "batch": batch, "scope": task_id or "全部"})
        folder = destination_root(root).name
        _log_summary(root, {"origin": "J06", "batch": batch, "scope": task_id or "全部", "basis": basis, "words": words, "actor": actor,
                            "files": len(paths), "bytes": total, "by_category": counts, "destination_root": f"{folder}/",
                            "with_suspects": bool(suspects), **({"include": picked["include"]} if picked["include"] else {}), "sample": paths[:20]})
        lead = "顺便把这个任务的过程文件整理了：" if basis == BASIS_AUTO else "按你说的整理好了："
        talk = (f"{lead}{_says(counts)}，一共 {len(paths)} 个、{_human(total)}，"
                f"移到了项目旁边的「{folder}」文件夹里，没有删除；成品、来源和检查用到的文件都没动。要找回随时说。")
        if inc_report and inc_report["skipped"]["files"]:
            talk += f" 你点名的里面有 {inc_report['skipped']['files']} 个没有移：记录里用到的、正在用的，或名字写明最终、交付的成品，这些我一律不动。"
        return {"status": "organized", "moved_files": len(paths), "moved_bytes": total, "by_category": counts, "scope": task_id or "全部",
                "destination_root": f"{folder}/", "batch": batch, "record_path": LOG, "for_strategist": talk,
                **({"include": inc_report} if inc_report else {})}


def auto_after_final(root, task_id, actor):
    """标最终版成功登记之后调用：只整理该任务的过程文件夹、暂存、可证明同内容的多余副本（严格范围，不带疑似过程、不接受点名）。
    任何失败都只返回 status=failed，不抛出。"""
    try:
        return organize(root, ACTOR_AUTO, WORDS_AUTO, task_id=task_id, basis=BASIS_AUTO)
    except Exception as exc:  # 整理失败不影响最终版登记
        return {"status": "failed", "moved_files": 0, "moved_bytes": 0, "technical": str(exc)[:400],
                "for_strategist": "最终版已经记下了。顺便整理过程文件这次没成功，不影响最终版；需要的话你说“整理过程文件”，我再试一次。"}


def main():
    parser = argparse.ArgumentParser(description="过程文件管理（J06）：预览、整理（只移不删）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("preview", help="只读预览")
    p.add_argument("--workspace", type=Path, required=True); p.add_argument("--task-id"); p.add_argument("--json", action="store_true")
    o = sub.add_parser("organize", help="策略师回“好”之后执行")
    o.add_argument("--workspace", type=Path, required=True); o.add_argument("--task-id"); o.add_argument("--actor", required=True)
    o.add_argument("--words", required=True, help="策略师看过预览后当次回复的原话")
    o.add_argument("--only-sure", action="store_true", help="只移确定的（过程、暂存、重复副本），不带看名字像过程的")
    o.add_argument("--include", action="append", default=[], metavar="路径", help="策略师点名要移的文件夹或文件（project/ 开头，可重复）；记录里用到的、成品、来源照样不动")
    o.add_argument("--dry-run", action="store_true"); o.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "preview":
        print(json.dumps(preview(args.workspace, args.task_id), ensure_ascii=False, indent=2))
    else:
        from registration_guard import actor_check
        actor_check(args.actor)
        if not args.words.strip():
            raise WorkflowError("整理须有策略师看过预览后的当次原话（--words），没有就不执行")
        print(json.dumps(organize(args.workspace, args.actor, args.words, args.task_id, dry_run=args.dry_run,
                                  suspects=not args.only_sure, include=args.include), ensure_ascii=False, indent=2))
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
