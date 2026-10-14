#!/usr/bin/env python3
"""已确认误报登记（v1.7.6 H02；批次一返修 r2）：启动检查把资料里某一处判成“疑似凭据”、策略师确认那不是凭据时，登记这一处，
启动检查、续做状态与正式移交检查不再因它阻断；不改原始资料。

- 只登记按内容判出的那一处（文件 + 字段；读不成 JSON、按文本判的字段记“（按文本检查）”），而且必须是现在正被判成疑似凭据的一处；
  文件名像凭据（.env、*.pem、名字里带 token 等）的整份文件不能登记，照拦。只认 project/ 下的文件，本登记记录自己不能登记。
- 每条登记记下策略师原话、文件与登记时整份文件的内容指纹；字段只存指纹（r2 #1：字段路径可能含像凭据的键名，原文不进记录、
  不进提示）。文件内容一变（指纹不同）这一处照常拦，要重新确认再登记。
- 记录在 project/records/false-alarms.jsonl，只追加；每行须字段齐全、记录编号与内容一致，否则当损坏：一处也不放过并如实提示（r2 #4）。
- 影响启动检查、续做状态与正式移交检查（都经 validate_project.check）；记一笔、对外导出、发行检查照旧按原规则判。
- 报错写法只在 validate_project 写一份（credential_location 反解，r2 #6）。
- 全盘细查返修 P1-3：放行范围是“这一字段”（按文本检查的是“这份文件”），不是其中某一行。登记只能放过软规则命中（像密码、密钥这类键名后面赋了一个值的写法）；
  同一字段（或文件）里有硬规则命中——密钥样式（如 sk-、ghp_、AKIA 开头的长串）、Bearer、Authorization 头、带凭据的网址、私钥头
  （package_checks_content.SECRET）——登记时拒绝，旧记录放行前也再查一次，命中照拦。同一字段里的几处软规则命中一起放行（已知边界）。
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from phase2_store import WorkflowError, local, now
from standalone_store import digest, text
from workspace_lock import serialized

LOG = "project/records/false-alarms.jsonl"
LINE = re.compile(r"^(?P<path>.+\.jsonl):(?P<line>\d+)$")
FIELDS = ("schema_version", "event", "record_id", "created_at", "path", "source", "field_sha256", "sha256", "words", "actor")


def location(message):
    from validate_project import credential_location
    return credential_location(message)


def source_file(source):
    match = LINE.match(source)
    return match.group("path") if match else source


def field_digest(field):
    return hashlib.sha256(field.encode("utf-8")).hexdigest()


def _sha(root, rel):
    try:
        path = local(root, rel)
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
    except (OSError, WorkflowError):
        return None


def _nodes(value, prefix=""):
    """(字段路径, 值)：路径写法与 validate_project.walk_sensitive_keys 报错里的一致（a.b、a[0]、顶层文字）。"""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            yield child_path, child
            yield from _nodes(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _nodes(child, f"{prefix}[{index}]")
    elif isinstance(value, str):
        yield prefix or "（顶层文字）", value


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def hard_hit(root, source, field):
    """P1-3：这一字段（按文本检查时是整份文件）里有没有硬规则命中（package_checks_content.SECRET：密钥样式、Bearer、Authorization 头、
    带凭据的网址、私钥头）。读不出、定位不到这一字段时按“有”算（宁可多拦）。"""
    from package_checks_content import SECRET
    from validate_project import CREDENTIAL_TEXT_FIELD
    try:
        path = local(root, source_file(source))
        if field == CREDENTIAL_TEXT_FIELD:
            with path.open(encoding="utf-8", errors="replace") as handle:
                return any(SECRET.search(line) for line in handle)
        found = LINE.match(source)
        if found:
            wanted = int(found.group("line"))
            with path.open(encoding="utf-8") as handle:
                body = next((line for number, line in enumerate(handle, 1) if number == wanted), None)
        else:
            body = path.read_text(encoding="utf-8")
        if body is None:
            return True
        data = json.loads(body)
    except (OSError, ValueError, WorkflowError):
        return True
    if SECRET.search(field):
        return True
    nodes = [value for where, value in _nodes(data) if where == field]
    return not nodes or any(SECRET.search(t) for value in nodes for t in _strings(value))


def scope_words(field):
    from validate_project import CREDENTIAL_TEXT_FIELD
    return "这份文件" if field == CREDENTIAL_TEXT_FIELD else "这一字段"


HARD_REFUSAL = ("这一处里还有像密钥的一长串字符（不是“密码: 某某”这种写法），这类不能登记成误报，这次没有登记。"
                "请先把真的密钥从资料里删掉或换成说明文字，剩下的再确认要不要登记。")


def records(root):
    """读登记记录；任何一行读不出、字段不齐或记录编号与内容不符都当损坏（抛 WorkflowError，不改原件）。"""
    path = Path(root) / LOG
    if not path.is_file():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"误报登记第 {number} 行损坏，未修改原件") from exc
        if (not isinstance(item, dict) or any(k not in item for k in FIELDS) or item["schema_version"] != 1
                or item["event"] != "false_alarm" or not all(isinstance(item[k], str) and item[k] for k in FIELDS[2:])):
            raise WorkflowError(f"误报登记第 {number} 行缺字段或格式不对，未修改原件")
        if digest({k: v for k, v in item.items() if k != "record_id"})[:32] != item["record_id"]:
            raise WorkflowError(f"误报登记第 {number} 行内容与记录编号不符（可能被改过），未修改原件")
        rows.append(item)
    return rows


def apply(root, errors):
    """启动检查用：登记过、且文件内容指纹没变的那一处移出错误，改记一条提示（不复述字段原文）。返回 (剩下的错误, 提示, 放过的登记编号)。
    登记记录损坏时一处也不放过，并如实提示。"""
    try:
        rows = records(root)
    except (OSError, ValueError) as exc:
        return errors, [f"误报登记读不出来，登记过的地方照常拦：{exc}"], []
    if not rows:
        return errors, [], []
    kept, notes, accepted, cache = [], [], [], {}
    for message in errors:
        spot = location(message)
        mine = [r for r in rows if spot and r["source"] == spot[0] and r["field_sha256"] == field_digest(spot[1])]
        if not mine:
            kept.append(message)
            continue
        rel = source_file(spot[0])
        if rel not in cache:
            cache[rel] = _sha(root, rel)
        match = [r for r in mine if r["sha256"] == cache[rel]]
        if match and hard_hit(root, spot[0], spot[1]):   # P1-3：登记只放过软规则命中；同一字段 / 文件里有密钥样式照拦
            kept.append(message)
            notes.append(f"{spot[0]} 里登记过误报的{scope_words(spot[1])}还有像密钥的一长串字符，登记放不过这类，照常拦："
                         "先把真的密钥删掉或换成说明文字")
        elif match:
            accepted.append(match[-1]["record_id"])
            notes.append(f"已确认误报，不阻断：{spot[0]} 里的{scope_words(spot[1])}（{match[-1]['created_at'][:10]} 登记，原话“{match[-1]['words']}”）")
        else:
            kept.append(message)
            notes.append(f"{spot[0]} 里有一处登记过误报，但文件内容后来变了，照常拦；确认新内容也不是凭据后重新登记")
    return kept, notes, list(dict.fromkeys(accepted))


def current_hits(root):
    """现在启动检查判出的疑似凭据（不套用登记）：按内容判的列出来源与字段；文件名判的另列（带路径），不能登记。"""
    from validate_project import check
    errors, _, _ = check(Path(root), confirmed=False)
    spots, names = [], []
    for message in errors:
        spot = location(message)
        if spot:
            spots.append({"source": spot[0], "field": spot[1], "path": source_file(spot[0]), "message": message})
        elif message.startswith("疑似敏感文件不得进入工作包"):
            names.append(message)
    return spots, names


def inspect(root):
    spots, names = current_hits(root)
    rows = records(root)
    for spot in spots:
        sha = _sha(root, spot["path"])
        done = [r for r in rows if r["source"] == spot["source"] and r["field_sha256"] == field_digest(spot["field"])]
        spot["registered"] = bool([r for r in done if r["sha256"] == sha])
        spot["registered_before_change"] = bool(done) and not spot["registered"]
        spot["has_secret_style"] = hard_hit(root, spot["source"], spot["field"])   # 收尾小修 P2-F：含密钥样式的不能登记，先说在前面
    return {"hits": spots, "file_name_hits": names, "records": len(rows),
            "notice": "按内容判出的疑似凭据，策略师确认不是凭据后可以登记为误报（register）；其中含密钥样式的长串字符（has_secret_style）不能登记，要先把真的密钥删掉；"
                      "文件名像凭据的不能登记，改名或移出工作包"}


def register(root, path, field, words, actor):
    from mingan_xinxi import credential_name, credential_text
    from registration_guard import actor_check
    actor_check(actor); text(words, "策略师原话"); text(path, "文件"); text(field, "字段")
    if credential_text(words):
        raise WorkflowError("策略师原话里有像凭据的字，这次没有登记。请只记她确认的那句话，不要把资料内容抄进来。"
                            "技术附录（给 Agent）：--words 过不了 mingan_xinxi.credential_text")
    rel = path.strip()
    local(root, rel)
    if not rel.startswith("project/") or source_file(rel) == LOG:
        raise WorkflowError("只能登记项目资料里的一处，这次没有登记。"
                            f"技术附录（给 Agent）：--path 须在 project/ 下、不能是 {LOG}")
    if credential_name(Path(source_file(rel)).name):  # r2 #7：文件名本身像凭据，整份照拦，不给登记
        raise WorkflowError("这份资料的文件名像密码或密钥，不能登记为误报，这次没有登记。要用它，先给文件改个名字或移出项目文件夹。"
                            "技术附录（给 Agent）：文件名命中 mingan_xinxi.credential_name（启动检查“疑似敏感文件不得进入工作包”）")
    # r2 #9：完整判定在锁外做（只读）；判定前后文件指纹一致才算判的是这一份；锁内只再核一次指纹、查重、追加
    before = _sha(root, source_file(rel))
    spots, _ = current_hits(root)
    target = [s for s in spots if (s["source"], s["field"]) == (rel, field)]
    if not target:
        listed = "；".join(s["source"] for s in spots[:10]) or "现在没有按内容判出的疑似凭据"
        raise WorkflowError("这一处现在没有被判成疑似凭据，不用登记，这次没有登记。"
                            f"技术附录（给 Agent）：--path 与 --field 须与启动检查报错里的写法一字不差（.jsonl 的 --path 带“:行号”）；"
                            f"现在判出疑似凭据的文件：{listed}（字段见 inspect 或启动检查报错）")
    if before is None or before != _sha(root, source_file(rel)):
        raise WorkflowError("这份资料读不了或刚被改动，这次没有登记；等它不再变化后重试。"
                            "技术附录（给 Agent）：判定前后文件指纹不一致，或文件不存在、是快捷方式（软链接）、读不出")
    if hard_hit(root, rel, field):
        raise WorkflowError(HARD_REFUSAL + "技术附录（给 Agent）：这一字段（按文本检查时是整份文件）命中 package_checks_content.SECRET 硬规则"
                            "（密钥样式、Bearer、Authorization 头、带凭据的网址、私钥头），全盘细查返修 P1-3 起误报登记只放过软规则命中")
    with serialized(root):
        if _sha(root, source_file(rel)) != before:
            raise WorkflowError("这份资料刚被改动，这次没有登记；等它不再变化后重试。技术附录（给 Agent）：加锁后文件指纹与判定时不一致")
        mark = field_digest(field)
        for row in records(root):
            if (row["source"], row["field_sha256"], row["sha256"]) == (rel, mark, before):
                return {**row, "status": "existing", "text": "这一处已经登记过误报，不用再登记。"}
        record = {"schema_version": 1, "event": "false_alarm", "created_at": now(), "path": source_file(rel), "source": rel,
                  "field_sha256": mark, "sha256": before, "words": words, "actor": actor,
                  "notice": f"策略师确认{scope_words(field)}里被拦的不是凭据（放行范围是{scope_words(field)}，只放过软规则命中）；字段只存指纹；文件内容一变照常拦。不改原始资料"}
        record["record_id"] = digest(record)[:32]
        log = local(root, LOG)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        scope = scope_words(field)
        return {**record, "status": "registered",
                "text": f"记下了：你确认{scope}里被拦下的不是密码或密钥，之后打开项目不会再因为{scope}停下。"
                        f"资料本身没有改；这份资料以后内容变了，会重新提醒一次；{scope}里要是有像密钥的一长串字符，照样会拦。"}


def plain(result, command):
    """不加 --json 时的白话摘要（r2 #10）；文件与字段放技术附录。"""
    if command == "register":
        return (result["text"] + "\n\n## 技术附录（给 Agent）\n\n"
                f"- 状态 {result['status']}，登记编号 {result['record_id']}，文件 {result['source']}，记录 {LOG}\n")
    hits, names = result["hits"], result["file_name_hits"]
    lines = [f"现在有 {len(hits)} 处按内容判出的疑似凭据（其中已登记为误报 {sum(h['registered'] for h in hits)} 处），"
             f"{len(names)} 份文件因为文件名像凭据被挡（不能登记，要改名或移出项目文件夹）。"]
    secret = sum(bool(h.get("has_secret_style")) for h in hits)
    if secret:
        lines.append(f"其中 {secret} 处里含有像密钥的一长串字符，不能登记成误报：请先把真的密钥删掉或换成说明文字。")
    lines += ["", "## 技术附录（给 Agent）", ""]
    lines += [f"- {h['message']}（{'已登记' if h['registered'] else '登记过但文件后来变了' if h['registered_before_change'] else '未登记'}"
              f"{'；含密钥样式，不能登记' if h.get('has_secret_style') else ''}）" for h in hits]
    lines += [f"- {n}" for n in names]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="已确认误报登记：策略师确认不是凭据的那一处不再阻断启动检查")
    commands = parser.add_subparsers(dest="command", required=True)
    look = commands.add_parser("inspect", help="列出现在判出的疑似凭据与登记情况（只读）")
    look.add_argument("--workspace", type=Path, required=True)
    look.add_argument("--json", action="store_true")
    add = commands.add_parser("register", help="登记一处已确认误报（须策略师原话）")
    add.add_argument("--workspace", type=Path, required=True)
    add.add_argument("--path", required=True, help="启动检查报错里的文件（.jsonl 带“:行号”）")
    add.add_argument("--field", required=True, help="报错里的字段；按文本判的写“（按文本检查）”")
    add.add_argument("--words", required=True, help="策略师确认不是凭据的原话")
    add.add_argument("--actor", required=True)
    add.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    result = inspect(root) if args.command == "inspect" else register(root, args.path, args.field, args.words, args.actor)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else plain(result, args.command), end="" if not args.json else "\n")
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
