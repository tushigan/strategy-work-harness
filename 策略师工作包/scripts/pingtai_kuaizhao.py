#!/usr/bin/env python3
"""产品线测算平台的快照登记：同一次导出的 HTML 提报与 JSON 数据一起存档、登记，并记下哪些成果绑定在哪一次快照上。

- 本文件不含任何利润、价格、成本计算：只登记文件、读平台已经算好的字段，字段缺失就报“平台数据格式变了”，不推算。
- 不读取、不保存、不打印平台登录凭证；导出工具返回的下载链接带一次性凭据，不允许存进快照。
- 快照文件存 project/exports/平台快照/<快照编号>/，记录追加在 project/records/pingtai-snapshots.jsonl（身份核对、快照、绑定、关联计划四种事件）。
- 登记新快照时，绑定旧快照的成果标“平台数据有更新，需按新快照复核”，复核后重新绑定到新快照。
- v1.7.6 W1：登记前把 HTML 提报（对比表每款的款名、零售价、出厂价、净利率、净含量、出厂价 / 500g、零售价 / 500g）和 JSON 逐款交叉核对，只比“显示取舍”（平台页面写两位小数、
  百分数写两位小数加 %），不做任何计算；对不上或提报页里读不出来都拒绝登记。
- v1.7.6 W3：登记时可指明产品矩阵计划（--plan；工作区里只有一个计划时自动关联），该计划的平台测算状态随之显示“已有平台快照，第 N 版”。"""
import argparse
import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

from phase2_store import WorkflowError, local, now
from workspace_lock import serialized

LOG = "project/records/pingtai-snapshots.jsonl"
FOLDER = "project/exports/平台快照"
FORMAT_CHANGED = "平台数据格式变了，按平台当前说明核对"
STALE_LABEL = "平台数据有更新，需按新快照复核"
UNVERIFIED_LABEL = "已绑定，但核对没通过，需复核"
CHANGED_LABEL = "绑定核对之后这份文件又被改过（或不见了），需重新核对"
COMPETITORS_LABEL = "绑定核对之后竞品对照表又被改过（或不见了），需重新核对"  # 全盘细查返修 P2-6
OTHER_NOT_ALLOWED = ("这份文件不能按“不含平台数字的文件”绑定：{why}。要绑定就按演示稿或矩阵表绑定，当场核对通过才算数；"
                     "核对没通过就先把数字改对。这次没有绑定。")
CLASH = "文件里写的快照与绑定的不一致，先改文件"
BEFORE_CLIENT = ("给客户前请看一眼提报页上的“负责人”“状态”（草稿、待审核、审核通过等）和“审核意见”有没有保留、对不对；"
                 "这几项是平台上的审核信息，客户看到的版本里不该丢，也不该是别人没确认的。")
CLIENT_LABELS = ("负责人", "状态", "审核意见")  # 平台提报页（presentation.ts）真实用字：小标签“状态”“负责人”，有审核记录时才有“…审核意见”块
KINDS = ("deck", "matrix", "other")
MAX_BYTES = 52428800  # 50 MB； 快照文件与被核对的文件读入的上限
TOO_BIG = "这个文件超过 50 MB，不读：确认是不是导出错了，或把内容拆小再来"


class FormatChanged(WorkflowError):
    """平台数据的形状和本包认识的对不上（不推算，让人按平台当前说明核对）。"""


def read_limited(path):
    """读文件，超过 MAX_BYTES 拒绝（快照与被核对的演示稿都走这里）。"""
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise WorkflowError(f"{path.name}：{TOO_BIG}")
    return path.read_bytes()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _events(root):
    path = local(root, LOG)
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"平台快照记录第 {number} 行损坏，未修改原件") from exc
        if not isinstance(item, dict) or item.get("schema_version") != 1 or item.get("event") not in {"identity", "snapshot", "bind", "plan_link"}:
            raise WorkflowError(f"平台快照记录第 {number} 行格式无效")
        rows.append(item)
    return rows


def _append(root, event):
    path = local(root, LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"schema_version": 1, **event}, ensure_ascii=False) + "\n")
    return event


# ---------------- 读平台数据（只读平台已算好的字段） ----------------
def normalize(data):
    """把平台数据读成 {project_id, name, updated_at, skus:[{id, name, fields}]}。只认 get_project 的回读结果（data.skus[]，每款有品项编号和 result）；
    导出工具的行数据（data.rows[]，没有品项编号、项目编号、更新时间）和其它形状一律拒绝，不猜。"""
    body = data.get("data", data) if isinstance(data, dict) else None
    if not isinstance(body, dict):
        raise FormatChanged(FORMAT_CHANGED + "（顶层不是对象）")
    if isinstance(body.get("rows"), list) and not isinstance(body.get("skus"), list):
        raise FormatChanged("这是导出工具给的一行一行的数据（没有品项编号、项目编号、平台更新时间），不能当快照用：请改存平台里重新读出来的项目数据。技术附录（给 Agent）：用 get_project 的返回")
    if not isinstance(body.get("skus"), list):
        raise FormatChanged(FORMAT_CHANGED + "（读不到品项列表）。技术附录（给 Agent）：回读结果里没有 skus 列表")
    skus = []
    for sku in body["skus"]:
        if not isinstance(sku, dict) or not isinstance(sku.get("name"), str) or not sku["name"]:
            raise FormatChanged(FORMAT_CHANGED + "（品项缺款名）")
        fields = {k: v for k, v in sku.items() if not isinstance(v, (dict, list)) or k == "mainChannel"}
        if isinstance(sku.get("result"), dict):
            fields.update({k: v for k, v in sku["result"].items() if not isinstance(v, (dict, list))})
        fields.update(_reverse_fields(sku.get("reverseResult")))
        skus.append({"id": sku.get("id"), "name": sku["name"], "fields": fields})
    return {"project_id": body.get("id"), "name": body.get("name"), "updated_at": body.get("updatedAt"), "skus": skus}


def _reverse_fields(reverse):
    """反推结果里的几项，按导出数据的列名改个名放进来（只改名，不算任何东西）：maxFactoryPrice、targetTotalCost、targetMaterialTon、
    reverseFactoryPer500g、reverseRetailPer500g。平台没给这一块（reverseResult 为空）就不放。"""
    if not isinstance(reverse, dict):
        return {}
    target = reverse.get("costTarget") if isinstance(reverse.get("costTarget"), dict) else {}
    pairs = (("maxFactoryPrice", reverse.get("maxFactoryPrice")), ("targetTotalCost", target.get("targetTotalCost")),
             ("targetMaterialTon", target.get("targetMaterialTon")), ("reverseFactoryPer500g", reverse.get("factoryPer500g")),
             ("reverseRetailPer500g", reverse.get("retailPer500g")))
    return {name: value for name, value in pairs if not isinstance(value, (dict, list))}


def find_sku(snapshot, key):
    """按品项编号找，找不到再按款名找；同名多款不猜。"""
    for sku in snapshot["skus"]:
        if sku["id"] is not None and sku["id"] == key:
            return sku
    named = [s for s in snapshot["skus"] if s["name"] == key]
    if len(named) > 1:
        raise WorkflowError(f"快照里有 {len(named)} 款叫“{key}”，请改用平台品项编号")
    return named[0] if named else None


def field_of(sku, field):
    """读平台已算好的字段；字段不存在报格式变了（None 是平台给的空值，原样返回）。"""
    if field not in sku["fields"]:
        from juzhen_hedui import label
        raise FormatChanged(f"{FORMAT_CHANGED}（“{sku['name']}”少了{label(field)}这一项）。技术附录（给 Agent）：快照里这一款没有字段 {field}")
    return sku["fields"][field]


# ---------------- 快照 ----------------
def snapshots(root):
    """按登记先后列出全部快照；同一平台项目内版本从 1 起。"""
    return [e for e in _events(root) if e["event"] == "snapshot"]


def latest(root, project_id=None):
    rows = [s for s in snapshots(root) if project_id is None or s["project_id"] == project_id]
    return rows[-1] if rows else None


def _snapshot_by_id(root, snapshot_id):
    found = [s for s in snapshots(root) if s["snapshot_id"] == snapshot_id]
    if not found:
        raise WorkflowError("找不到要用的这一版平台快照：确认是不是写错了，或者先导出并登记。"
                            f"技术附录（给 Agent）：快照编号 {snapshot_id} 不在 {LOG}；用 pingtai_kuaizhao.py register 登记")
    return found[-1]


def files_state(root, snap):
    """快照文件现在的状态：ok / changed / missing（指纹对不上就不再采用）。"""
    out = {}
    for name in ("html", "json"):
        ref = snap[name]
        path = local(root, ref["path"])
        out[name] = "missing" if not path.is_file() else ("ok" if _sha(path.read_bytes()) == ref["sha256"] else "changed")
    return out


def snapshot_data(root, snapshot_id=None, project_id=None):
    """核对用：返回 (快照记录, 规整后的平台数据)。快照文件改过或丢了就拒绝，不用被改过的数据去核对。"""
    snap = _snapshot_by_id(root, snapshot_id) if snapshot_id else (latest(root, project_id) if project_id else default_snapshot(root))
    if snap is None:
        raise WorkflowError("还没有登记过平台快照：测算一步仍是“待平台测算”，不能核对数字。先导出并登记。技术附录（给 Agent）：pingtai_kuaizhao.py register")
    state = files_state(root, snap)
    if any(v != "ok" for v in state.values()):
        raise WorkflowError("这一版平台快照的文件已被改过或丢失，不能用它核对；重新导出并登记新快照。"
                            f"技术附录（给 Agent）：快照 {snap['snapshot_id']} 文件状态 {state}")
    data = normalize(json.loads(local(root, snap["json"]["path"]).read_text(encoding="utf-8")))
    # 全盘细查返修 P1-1：每款在提报页哪个对比表区块（线下渠道 / 线上运营 / 反推），核对时据此认线上、线下的字段口径。登记时已交叉核对过，读不到记 None
    try:
        blocks = page_rows(local(root, snap["html"]["path"]).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        blocks = {}
    for sku in data["skus"]:
        sku["block"] = (blocks.get(str(sku["id"])) or {}).get("block") if sku["id"] is not None else None
    return snap, data


def _iso(value, what):
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{what}要写成年月日加时间，如 2026-10-13T10:00:00+09:00")
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowError(f"{what}“{value}”读不懂，要写成 2026-10-13T10:00:00+09:00 这样") from exc
    return value.strip()


def _safe_id(value):
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(value))[:60].strip("-") or "project"


def page_info(html_text):
    """平台提报页上给客户前要看的三样（照 presentation.ts 的真实用字）：小标签“负责人”“状态”各带一个值，有审核记录时才有“…审核意见”块。
    返回 (labels_in_html: 三项各自有没有, values: 负责人与状态的值)。没有审核意见块不一定有错——项目没被审核过就没有。"""
    values = {}
    for name in ("负责人", "状态"):
        found = re.search(r"<strong>\s*" + name + r"\s*</strong>\s*([^<]*)", html_text)
        values[name] = found.group(1).strip() if found and found.group(1).strip() else None
    labels = {"负责人": values["负责人"] is not None, "状态": values["状态"] is not None, "审核意见": "审核意见" in html_text}
    return labels, values


def labels_in(html_text):
    return page_info(html_text)[0]


# ---------------- v1.7.6 W1：提报页与平台数据交叉核对（只做显示取舍，不算任何东西） ----------------
DASH = {"—", "-", "–", ""}
PAGE_COLUMNS = (("零售定价", "零售价"), ("出厂价", "出厂价"), ("净利率", "净利率"), ("净含量", "净含量"), ("出厂价 / 500g", "每 500 克出厂价"),
                ("零售价 / 500g", "每 500 克零售价"))
# 对比表每个区块（平台 presentation.ts 的 renderComparisonTable）各列对应的平台字段：按区块标题认（标题是页面原样文字，不推断渠道）。
# None 表示不比：线上反推的净利率平台本来就显示“—”；反推的净含量是平台按反推包装另算的（回读数据里没有这个值），不比。
# 全盘细查返修 P2-7：多比净含量、出厂价 / 500g、零售价 / 500g 三列（仍只是显示取舍）。
BLOCK_FIELDS = {"线下渠道对比": ("retailPrice", "factoryPrice", "netMargin", "netWeight", "factoryPer500g", "retailPer500g"),
                "线上运营对比": ("onlineRetailPrice", "factoryPrice", "onlineEcomTeamNetMargin", "netWeight", "factoryPer500g", "retailPer500g"),
                "线下反推对比": ("targetRetailPrice", "maxFactoryPrice", "reverseTargetNetMargin", None, "reverseFactoryPer500g", "reverseRetailPer500g"),
                "线上反推对比": ("targetRetailPrice", "maxFactoryPrice", None, None, "reverseFactoryPer500g", "reverseRetailPer500g")}
PAGE_NUMBER = re.compile(r"^(-?\d+(?:\.\d+)?)(%?)$")
REDO = "多半是平台数据那份文件被手抄或改过：把平台读回来的原文整段重新存一份（不手抄、不改写），和同一次导出的提报页一起再登记。"


class _Page(HTMLParser):
    """只读对比表：区块标题（h3）、表头（thead th）、每行（th 里 a 的 #sku-编号与款名，td 的显示文字）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.block, self.cell, self.row, self.where = [], None, None, None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and "comparison-block" in (attrs.get("class") or "").split():
            self.block = {"title": "", "header": [], "rows": []}; self.blocks.append(self.block)
        if self.block is None:
            return
        if tag == "h3" and not self.block["title"]:
            self.where = "title"
        elif tag == "thead":
            self.where = "thead"
        elif tag == "tbody":
            self.where = "tbody"
        elif tag == "tr" and self.where == "tbody":
            self.row = {"id": None, "name": "", "cells": []}; self.block["rows"].append(self.row)
        elif tag in {"th", "td"}:
            self.cell = []
        elif tag == "a" and self.row is not None and (attrs.get("href") or "").startswith("#sku-"):
            self.row["id"] = attrs["href"][len("#sku-"):]

    def handle_endtag(self, tag):
        if self.block is None:
            return
        if tag == "h3" and self.where == "title":
            self.where = None
        elif tag in {"th", "td"} and self.cell is not None:
            value = "".join(self.cell).strip(); self.cell = None
            if self.where == "thead":
                self.block["header"].append(value)
            elif self.row is not None:
                if tag == "th" and not self.row["cells"] and not self.row["name"]:
                    self.row["name"] = value
                else:
                    self.row["cells"].append(value)
        elif tag == "tr":
            self.row = None
        elif tag in {"thead", "tbody"}:
            self.where = None
        elif tag == "section":
            self.block = None

    def handle_data(self, data):
        if self.block is None:
            return
        if self.where == "title":
            self.block["title"] += data
        if self.cell is not None:
            self.cell.append(data)


def page_rows(html_text):
    """提报页对比表里每款一行：{品项编号: {"name", "block", "values": {列名: 显示文字或 None（这一列没读到）}}}。读不出的不猜。"""
    parser = _Page()
    try:
        parser.feed(html_text); parser.close()
    except (ValueError, AssertionError):
        return {}
    rows = {}
    for block in parser.blocks:
        title = block["title"].strip()
        for row in block["rows"]:
            if not row["id"]:
                continue
            values = {}
            for column, _ in PAGE_COLUMNS:
                index = block["header"].index(column) - 1 if column in block["header"] else -1
                values[column] = row["cells"][index] if 0 <= index < len(row["cells"]) else None
            rows.setdefault(row["id"], {"name": row["name"], "block": title, "values": values})
    return rows


def page_number(value, percent, places=2):
    """平台值按提报页写法取舍（显示取舍的唯一实现在 juzhen_hedui.as_shown，本文件不做小数点位移）。"""
    from juzhen_hedui import page_number as shown
    return shown(value, percent, places)


def page_text(value, percent=False, places=2):
    """给策略师看的平台数字：按提报页的写法（两位小数；比例写成百分数）。见 juzhen_hedui.page_text。"""
    from juzhen_hedui import page_text as shown
    return shown(value, percent, places)


def _page_candidates(value, percent, places):
    from juzhen_hedui import page_candidates
    return page_candidates(value, percent, places)


def cross_check(html_text, norm):
    """逐款比对提报页对比表与平台数据：款名、零售价、出厂价、净利率、净含量、每 500 克出厂价与零售价。返回问题列表（空 = 一致）。
    每条：{"sku", "name", "field", "kind": "mismatch"|"unread", "page", "data", "message"}；message 是给策略师的话。"""
    rows = page_rows(html_text)
    problems = []
    for sku in norm["skus"]:
        who = sku["name"]
        row = rows.get(str(sku["id"])) if sku["id"] is not None else None
        if row is None:
            problems.append({"sku": sku["id"], "name": who, "field": "款", "kind": "unread", "page": None, "data": who,
                             "message": f"提报页里没读到“{who}”这一款"})
            continue
        if row["name"] != who:
            problems.append({"sku": sku["id"], "name": who, "field": "款名", "kind": "mismatch", "page": row["name"], "data": who,
                             "message": f"“{who}”的款名：提报页写“{row['name']}”，平台数据是“{who}”"})
        fields = BLOCK_FIELDS.get(row["block"])
        if fields is None:
            problems.append({"sku": sku["id"], "name": who, "field": "对比表标题", "kind": "unread", "page": row["block"], "data": None,
                             "message": f"提报页里没读到“{who}”所在对比表的标题（不知道这一行是线上还是线下、正推还是反推）"})
            continue
        for (column, word), field in zip(PAGE_COLUMNS, fields):
            if field is None:
                continue
            shown, have = row["values"][column], sku["fields"].get(field)
            percent = word == "净利率"
            base = {"sku": sku["id"], "name": who, "field": word, "platform_field": field, "page": shown, "data": have}
            if shown is None:
                problems.append({**base, "kind": "unread", "message": f"提报页里没读到“{who}”的{word}"}); continue
            text_value = "".join(shown.split()).replace("元", "")
            if word == "净含量" and text_value.endswith("g"):   # 平台写“300.00 g”
                text_value = text_value[:-1]
            if text_value in DASH:
                if have is not None:
                    problems.append({**base, "kind": "mismatch", "message": f"“{who}”的{word}：提报页是空的，平台数据是 {page_text(have, percent)}"})
                continue
            found = PAGE_NUMBER.match(text_value)
            if not found or bool(found.group(2)) != percent:
                problems.append({**base, "kind": "unread", "message": f"提报页里没读到“{who}”的{word}（写的是“{shown}”，不是认得的数）"}); continue
            number = Decimal(found.group(1))
            places = len(found.group(1).split(".")[1]) if "." in found.group(1) else 0
            if number not in _page_candidates(have, percent, places):
                problems.append({**base, "kind": "mismatch",
                                 "message": f"“{who}”的{word}：提报页写 {shown}，平台数据是 {page_text(have, percent, places)}"})
    known = {str(s["id"]) for s in norm["skus"] if s["id"] is not None}
    for sku_id, row in rows.items():
        if sku_id not in known:
            problems.append({"sku": sku_id, "name": row["name"], "field": "款", "kind": "mismatch", "page": row["name"], "data": None,
                             "message": f"提报页里有“{row['name']}”，平台数据里没有这一款"})
    return problems


def _cross_check_error(problems):
    unread = [p for p in problems if p["kind"] == "unread"]
    lines = "；".join(p["message"] for p in problems[:8]) + ("等" if len(problems) > 8 else "")
    if unread and len(unread) == len(problems):
        head = "提报页里有几项没读到，不能当作和平台数据一致，这次没有登记快照：" + lines + "。请确认提报页是平台原样导出的完整文件（没被另存、裁剪或改过），再登记。"
    else:
        head = "提报页和平台数据对不上，这次没有登记快照：" + lines + "。" + REDO
    detail = [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in p.items() if k != "message"} for p in problems]
    return WorkflowError(head + "技术附录（给 Agent）：pingtai_kuaizhao.register 交叉核对 HTML 对比表（comparison-block 的 h3 标题、thead 列名、"
                         "tbody 行 a[href=#sku-品项编号]）与 get_project 回读；读不出（unread）多半是提报页不是平台原样导出或页面结构变了，"
                         "按平台当前说明核对，不要手改提报页凑一致。问题明细：" + json.dumps(detail, ensure_ascii=False)[:1500])


def _plan_choice(root, plan):
    """W3：登记关联哪个产品矩阵计划。写了就核对它存在；没写时工作区里只有一个计划才自动关联，多个不猜（返回 None 和一句提醒）。"""
    try:
        import matrix_plan
    except ImportError:
        return None, None, False
    if plan:
        matrix_plan.current(root, plan)          # 不存在会报“找不到产品矩阵计划”
        return plan, None, False
    plans = sorted({e["plan_id"] for e in matrix_plan._events(root)})
    if len(plans) == 1:
        return plans[0], None, True
    if plans:
        return None, (f"工作区里有 {len(plans)} 个产品矩阵计划，这次快照没说是哪个计划的，计划里的平台测算状态不会跟着变。"
                      "技术附录（给 Agent）：重新登记时加 --plan <计划编号>（同一份内容再登记只补关联，不新建快照）"), False
    return None, None, False


def plan_links(root, events=None):
    """{计划编号: [平台项目编号, ...]}：登记时关联的、以及之后补关联的（W3）。"""
    out = {}
    for e in events if events is not None else _events(root):
        if e["event"] in {"snapshot", "plan_link"} and e.get("plan_id"):
            ids = out.setdefault(e["plan_id"], [])
            if e["project_id"] not in ids:
                ids.append(e["project_id"])
    return out


def plan_snapshot(root, plan_id):
    """W3：这个产品矩阵计划关联的平台项目里最新登记的一版快照（没有关联返回 None）。快照记录读不出来照实抛。"""
    root = Path(root).resolve()
    events = _events(root)
    projects = plan_links(root, events).get(plan_id) or []
    rows = [e for e in events if e["event"] == "snapshot" and e["project_id"] in projects]
    if not rows:
        return None
    top = rows[-1]
    return {"snapshot_id": top["snapshot_id"], "version": top["version"], "project_id": top["project_id"],
            "platform_name": top.get("platform_name"), "label": f"已有平台快照，第 {top['version']} 版"}


def register(root, actor, html, json_file, exported_at, plan=None):
    """登记一次导出：HTML 提报 + get_project 回读的 JSON。同一份内容再登记原样返回；导出工具的行数据、带下载链接（含一次性凭据）的数据、超过 50 MB 的文件都拒绝。
    v1.7.6 W1：提报页对比表与 JSON 逐款交叉核对（款名、零售价、出厂价、净利率、净含量、出厂价 / 500g、零售价 / 500g，按提报页显示取舍），对不上或读不出都拒绝。
    v1.7.6 W3：plan 指明关联的产品矩阵计划；不写时工作区只有一个计划就自动关联；同一份内容再登记时只补关联。"""
    from registration_guard import actor_check
    actor_check(actor)
    root = Path(root).resolve()
    html_path, json_path = Path(html), Path(json_file)
    for path, what in ((html_path, "提报 HTML"), (json_path, "平台数据 JSON")):
        if not path.is_file():
            raise WorkflowError(f"{what}文件不存在：{path.name}")
    html_bytes, json_bytes = read_limited(html_path), read_limited(json_path)
    if not html_bytes.strip():
        raise WorkflowError("提报 HTML 是空的")
    try:
        html_text = html_bytes.decode("utf-8")
        json_text = json_bytes.decode("utf-8")
        data = json.loads(json_text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WorkflowError(f"提报 HTML 或平台数据读不出来：{exc}") from exc
    if "downloadUrl" in json_text or "ticket=" in json_text:
        raise WorkflowError("这份数据里带着导出下载链接（含一次性凭据），不能存进快照：要存从平台里重新查到的项目数据，不要存导出时返回的内容。技术附录（给 Agent）：存 get_project 的回读结果，不要存导出工具的返回")
    norm = normalize(data)
    if not norm["skus"]:
        raise WorkflowError("平台数据里没有任何品项，没法登记快照")
    pid = norm["project_id"]
    if not isinstance(pid, str) or not pid.strip():
        raise WorkflowError(FORMAT_CHANGED + "（回读结果里没有项目编号）")
    if not norm["updated_at"]:
        raise WorkflowError(FORMAT_CHANGED + "（回读结果里没有平台更新时间）")
    updated = _iso(norm["updated_at"], "平台更新时间")
    exported = _iso(exported_at, "导出时间")
    problems = cross_check(html_text, norm)
    if problems:
        raise _cross_check_error(problems)
    warnings = [f"提报页里没看到“{s['name']}”这个款名，请确认 HTML 和数据是同一次导出的" for s in norm["skus"] if s["name"] not in html_text]
    labels, values = page_info(html_text)
    with serialized(root):
        plan_id, plan_note, plan_auto = _plan_choice(root, plan)
        if plan_note:
            warnings.append(plan_note)
        existing = [s for s in snapshots(root) if s["project_id"] == pid]
        for snap in existing:
            if snap["html"]["sha256"] == _sha(html_bytes) and snap["json"]["sha256"] == _sha(json_bytes):
                linked = None
                if plan_id and pid not in (plan_links(root).get(plan_id) or []):
                    linked = _append(root, {"event": "plan_link", "snapshot_id": snap["snapshot_id"], "project_id": pid, "plan_id": plan_id,
                                            "automatic": plan_auto, "linked_at": now(), "actor": actor})
                return {"status": "existing", "snapshot": snap, "version": snap["version"], "needs_recheck": _stale(root),
                        "plan_id": plan_id if (linked or pid in (plan_links(root).get(plan_id) or [])) else None,
                        "plan_linked_now": bool(linked), "warnings": warnings}
        version = len(existing) + 1
        snapshot_id = f"PK-{_safe_id(pid)}-{hashlib.sha1(pid.encode('utf-8')).hexdigest()[:6]}-{version:03d}"  # 编号带原项目编号的指纹，清洗后重名也不会撞
        if any(x["snapshot_id"] == snapshot_id for x in snapshots(root)):
            raise WorkflowError(f"快照编号 {snapshot_id} 已经有人用了，不覆盖旧快照：先查一下是不是重复登记或记录被手动动过")
        folder = local(root, f"{FOLDER}/{snapshot_id}")
        html_ref = {"path": f"{FOLDER}/{snapshot_id}/提报.html", "sha256": _sha(html_bytes), "size": len(html_bytes)}
        json_ref = {"path": f"{FOLDER}/{snapshot_id}/数据.json", "sha256": _sha(json_bytes), "size": len(json_bytes)}
        if folder.exists():
            _leftover(root, folder, snapshot_id, html_ref, json_ref)
        folder.mkdir(parents=True, exist_ok=True)
        (root / html_ref["path"]).write_bytes(html_bytes)
        (root / json_ref["path"]).write_bytes(json_bytes)
        event = {"event": "snapshot", "snapshot_id": snapshot_id, "project_id": pid, "version": version, "platform_name": norm["name"],
                 "sku_count": len(norm["skus"]), "exported_at": exported, "platform_updated_at": updated,
                 "html": html_ref, "json": json_ref, "fingerprint": _sha((html_ref["sha256"] + json_ref["sha256"]).encode()),
                 "plan_id": plan_id, "plan_linked_automatically": plan_auto, "cross_checked": True,
                 "recorded_at": now(), "actor": actor}
        _append(root, event)
        event = {"schema_version": 1, **event}
        return {"status": "registered", "snapshot": event, "version": version, "labels_in_html": labels, "page_values": values,
                "before_client": BEFORE_CLIENT, "warnings": warnings, "needs_recheck": _stale(root), "plan_id": plan_id,
                "plan_status": f"已有平台快照，第 {version} 版" if plan_id else None}


def _leftover(root, folder, snapshot_id, html_ref, json_ref):
    """全盘细查返修 P2-12：快照目录已经在、记录里却没有这一编号——上次登记写完文件、记下记录之前断了。
    目录里正好只有这一次要登记的两个文件（指纹一致）就接着登记；否则给白话清理办法（不删、不覆盖用户的文件）。"""
    names = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else None
    same = names == ["提报.html", "数据.json"] and all(
        (root / ref["path"]).is_file() and _sha((root / ref["path"]).read_bytes()) == ref["sha256"] for ref in (html_ref, json_ref))
    if same:
        return
    rel = folder.relative_to(root).as_posix()
    extra = [n for n in (names or []) if n not in ("提报.html", "数据.json")]
    advice = ("先看看那个文件夹里有没有你自己的文件（有的话先拿出来），"
              if extra else "")
    raise WorkflowError("上次登记这份平台快照时中途断了，留下了一个没登记完的快照文件夹，这次没有登记。"
                        + advice + ("再把那个文件夹整个移出项目文件夹（或删掉），再重新登记一次。" if extra else
                        "把那个文件夹整个移出项目文件夹（或删掉，里面只是上次没存完的副本，原始导出文件不受影响），再重新登记一次。")
                        + "不会替你删任何东西。"
                        f"技术附录（给 Agent）：{rel} 存在，但 {LOG} 里没有快照 {snapshot_id}，目录内容与本次要写的两个文件不一致"
                        f"（现有：{names}）；移走后重跑 register")


def identity(root, actor, username, role, projects, environment=None):
    from registration_guard import actor_check
    actor_check(actor)
    for value, what in ((username, "用户名"), (role, "角色")):
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError(f"{what}不能空")
    if not isinstance(projects, int) or projects < 0:
        raise WorkflowError("可见项目数要写成不小于 0 的整数")
    with serialized(Path(root).resolve()):
        event = {"event": "identity", "username": username.strip(), "role": role.strip(), "visible_projects": projects,
                 "environment": environment, "checked_at": now(), "actor": actor,
                 "note": "只记用户名、角色、可见项目数；不记任何登录凭证"}
        _append(Path(root).resolve(), event)
    return {"status": "recorded", "identity": {"schema_version": 1, **event}}


def _rel(root, path):
    target = local(root, str(path))
    return target, target.relative_to(root).as_posix()


def bound_snapshot(root, path):
    """这份成果最近一次绑定的快照编号（没绑过返回 None）。核对时默认用它。"""
    try:
        _, rel = _rel(Path(root).resolve(), path)
    except (ValueError, OSError):
        return None
    mine = [e for e in _events(Path(root).resolve()) if e["event"] == "bind" and e["path"] == rel]
    return mine[-1]["snapshot_id"] if mine else None


def file_clash(root, path, in_file):
    """文件里自己写的快照号（演示稿的 data-pingtai-snapshot、矩阵表的 snapshot_id）和最近一次绑定的不一致，返回说明；一致、没绑过、文件里没写返回 None。"""
    bound = bound_snapshot(root, path)
    if in_file and bound and in_file != bound:
        return (f"{CLASH}：文件里写的快照和最近一次绑定的不是同一版。一般是把文件里的快照号改成最近绑定的那一版，再核对。"
                "如果你确实要用旧的那一版数据：旧数据核对得再对，也不算和平台最新的数据对上，发客户前会被拦，要先按最新一版核对通过。"
                f"技术附录（给 Agent）：文件里写 {in_file}，最近一次绑定 {bound}；改文件里的 data-pingtai-snapshot（演示稿）或 snapshot_id（矩阵表）为 {bound} 后再核对")
    return None


def default_snapshot(root):
    """没指明用哪一版时：工作区里只有一个平台项目才取它最新的一版；有多个平台项目不猜。"""
    rows = snapshots(root)
    if not rows:
        return None
    projects = sorted({s["project_id"] for s in rows})
    if len(projects) > 1:
        raise WorkflowError(f"工作区里有 {len(projects)} 个平台项目的快照，没指明用哪一个，不替你猜。技术附录（给 Agent）：用 --snapshot 指定，或先用 bind 把这份成果绑到某一次快照")
    return rows[-1]


PLATFORM_MARKS = re.compile(r"\bdata-(?:pingtai-[a-z]+|field|jingpin)\s*=", re.I)


def _other_refusal(root, rel, target):
    """全盘细查返修 P1-2：other（不核对）只用于不含平台数字的文件。同一路径以前按 deck / matrix 绑过的不许改成 other（否则“需复核”会悄悄消失，
    等于一个不留原话的豁免出口）；HTML 里有平台数据标注（data-pingtai-*、data-field、data-jingpin）的一律按 deck 绑。返回拒绝理由或 None。"""
    earlier = [e for e in _events(root) if e["event"] == "bind" and e["path"] == rel and e.get("kind") in {"deck", "matrix"}]
    if earlier:
        return "以前按" + ("演示稿" if earlier[-1]["kind"] == "deck" else "矩阵表") + "绑定核对过"
    if target.suffix.lower() in {".html", ".htm"} and PLATFORM_MARKS.search(read_limited(target).decode("utf-8", "replace")):
        return "里面标着来自平台的数字"
    return None


def bind(root, actor, snapshot_id, path, kind="other", competitors=None):
    """把一份成果（矩阵表、演示稿等）绑到某一次快照，并当场按这一次快照核对（deck 用演示稿数字核对，matrix 用角色与数字核对）：
    核对通过才记“已核对”，才消“需按新快照复核”；没通过照记，但仍算没核对。other 不核对，只用于不含平台数字的文件（P1-2）。
    演示稿里有竞品价格（data-jingpin）时要同时给竞品对照表（competitors，须在工作区内），否则核对报“没给对照表”，算没核对；
    给了的话记下它的相对路径与指纹，之后对照表变了算需复核（P2-6）。"""
    from registration_guard import actor_check
    actor_check(actor)
    root = Path(root).resolve()
    if kind not in KINDS:
        raise WorkflowError("成果种类只能是 deck（演示稿）、matrix（矩阵表）或 other")
    snap = _snapshot_by_id(root, snapshot_id)
    target, rel = _rel(root, path)
    if not target.is_file():
        raise WorkflowError(f"要绑定的成果文件不存在：{path}")
    if kind == "other":
        why = _other_refusal(root, rel, target)
        if why:
            raise WorkflowError(OTHER_NOT_ALLOWED.format(why=why) + f"技术附录（给 Agent）：{rel} 不能用 --kind other；"
                                "演示稿用 --kind deck、矩阵表用 --kind matrix（other 只用于不含平台数字的文件）")
    table = None
    if competitors is not None:
        try:
            table_path, table_rel = _rel(root, competitors)
        except (ValueError, OSError, WorkflowError) as exc:
            raise WorkflowError("竞品对照表要放在项目文件夹里，这次没有绑定。技术附录（给 Agent）：--competitors 越出工作区") from exc
        if not table_path.is_file():
            raise WorkflowError(f"竞品对照表不存在，这次没有绑定。技术附录（给 Agent）：{table_rel}")
        table = {"path": table_rel, "sha256": _sha(read_limited(table_path))}
        competitors = table_path
    check = None
    if kind in {"deck", "matrix"}:
        import juzhen_hedui
        check = (juzhen_hedui.deck(root, target, snap["snapshot_id"], competitors) if kind == "deck"
                 else juzhen_hedui.roles(root, target, snap["snapshot_id"]))
    with serialized(root):
        event = {"event": "bind", "snapshot_id": snap["snapshot_id"], "project_id": snap["project_id"], "path": rel,
                 "sha256": _sha(read_limited(target)), "kind": kind, "bound_at": now(), "actor": actor,
                 "verified": (check["status"] == "ok") if check else None, "check_status": check["status"] if check else None,
                 **({"competitors": table} if table else {})}
        _append(root, event)
    unverified = bool(check) and check["status"] != "ok"
    out = {"status": "bound_unverified" if unverified else "bound", "binding": {"schema_version": 1, **event}, "needs_recheck": _stale(root)}
    if check:
        out["check"] = {"status": check["status"], "for_strategist": check.get("for_strategist", "")}
        out["for_strategist"] = (UNVERIFIED_LABEL + "：" + check.get("for_strategist", "") if unverified
                                 else "已绑定，并且当场核对通过。")
    return out


def binding_status(b):
    """W4：一条绑定记录的状态。当场核对没通过 → bound_unverified；通过或不需核对 → bound。"""
    return "bound_unverified" if b.get("verified") is False else "bound"


def _latest_binds(events):
    """每份成果最近一次绑定。全盘细查返修 P1-2：旧版本可能留下“先按演示稿 / 矩阵表绑定、后改成 other”的记录——other 不消除之前的核对要求，
    这种 other 记录算“没核对”（verified 记为 False），照旧需复核。"""
    by_path, checked = {}, set()
    for b in (e for e in events if e["event"] == "bind"):
        if b.get("kind") in {"deck", "matrix"}:
            checked.add(b["path"])
        elif b["path"] in checked:
            b = {**b, "verified": False, "check_status": "other_after_check"}
        by_path[b["path"]] = b
    return by_path


def _stale(root):
    """每份成果最近一次绑定：绑的不是该平台项目最新快照的、或绑了最新快照但当场核对没通过的、核对后文件或竞品对照表又被改过的，都要复核。"""
    events = _events(root)
    newest = {}
    for s in (e for e in events if e["event"] == "snapshot"):
        newest[s["project_id"]] = s
    by_path = _latest_binds(events)
    rows = []
    for path, b in sorted(by_path.items()):
        top = newest.get(b["project_id"])
        if _file_changed(root, path, b):
            rows.append({"path": path, "kind": b["kind"], "bound_to": b["snapshot_id"], "latest": top["snapshot_id"] if top else b["snapshot_id"],
                         "label": CHANGED_LABEL})
        elif top and top["snapshot_id"] != b["snapshot_id"]:
            rows.append({"path": path, "kind": b["kind"], "bound_to": b["snapshot_id"], "latest": top["snapshot_id"], "label": STALE_LABEL})
        elif isinstance(b.get("competitors"), dict) and _file_changed(root, b["competitors"]["path"], b["competitors"]):
            rows.append({"path": path, "kind": b["kind"], "bound_to": b["snapshot_id"], "latest": top["snapshot_id"] if top else b["snapshot_id"],
                         "label": COMPETITORS_LABEL})
        elif b.get("verified") is False:
            rows.append({"path": path, "kind": b["kind"], "bound_to": b["snapshot_id"], "latest": top["snapshot_id"] if top else b["snapshot_id"],
                         "label": UNVERIFIED_LABEL})
    return rows


def needs_recheck(root):
    """每份成果最近一次绑定的状态里，要复核的那些（旧快照、核对没通过、绑定后文件又被改过）。发客户关口用。"""
    return _stale(Path(root).resolve())


def _file_changed(root, path, b):
    """绑定时记的文件指纹和现在的文件对不上（改过、或文件不见了）。"""
    try:
        return _sha(read_limited(local(root, path))) != b["sha256"]
    except (OSError, WorkflowError):
        return True


def inspect(root):
    root = Path(root).resolve()
    events = _events(root)
    snaps = [e for e in events if e["event"] == "snapshot"]
    out = []
    for s in snaps:
        state = files_state(root, s)
        try:
            html_text = local(root, s["html"]["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            html_text = ""
        labels, values = page_info(html_text)
        out.append({"snapshot_id": s["snapshot_id"], "project_id": s["project_id"], "version": s["version"],
                    "platform_name": s.get("platform_name"), "exported_at": s["exported_at"], "platform_updated_at": s["platform_updated_at"],
                    "sku_count": s["sku_count"], "files": state, "labels_in_html": labels, "page_values": values,
                    "is_latest": all(x["version"] <= s["version"] for x in snaps if x["project_id"] == s["project_id"])})
    ids = [e for e in events if e["event"] == "identity"]
    latest_bind = _latest_binds(events)
    bindings = [{"path": b["path"], "kind": b["kind"], "snapshot_id": b["snapshot_id"], "status": binding_status(b),
                 "check_status": b.get("check_status")} for _, b in sorted(latest_bind.items())]
    return {"status": "recorded" if out or ids else "empty", "snapshots": out, "needs_recheck": _stale(root), "bindings": bindings,
            "plans": {plan: plan_snapshot(root, plan) for plan in sorted(plan_links(root, events))},
            "last_identity_check": ({k: ids[-1][k] for k in ("username", "role", "visible_projects", "checked_at")} if ids else None),
            "before_client": BEFORE_CLIENT, "record_path": LOG}


def resume_summary(root):
    """续做状态用：每个平台项目最新一版，“产品线规划表格：第 N 版”，以及要复核的成果、被改过的快照文件。"""
    root = Path(root).resolve()
    info = inspect(root)
    latest_rows = [s for s in info["snapshots"] if s["is_latest"]]
    lines = [f"产品线规划表格：第 {s['version']} 版（平台项目 {s['platform_name'] or s['project_id']}，导出于 {s['exported_at']}，平台更新于 {s['platform_updated_at']}）"
             for s in latest_rows]
    warnings = []
    for row in info["needs_recheck"]:
        if row["label"] == CHANGED_LABEL:
            warnings.append(f"{row['path']} 在绑定核对之后又被改过（或不见了），{CHANGED_LABEL}：改好后重新绑定并核对。")
        elif row["label"] == STALE_LABEL:
            warnings.append(f"{row['path']} 绑定的是旧一版平台数据（{row['bound_to']}），{STALE_LABEL}：先看最新数据有没有改到它用的价格、规格、利润，再重新绑定到 {row['latest']}。")
        elif row["label"] == COMPETITORS_LABEL:
            warnings.append(f"{row['path']} 用到的竞品对照表在绑定核对之后又被改过（或不见了），需重新核对：确认竞品价格的来源和日期，再重新绑定。")
        else:
            warnings.append(f"{row['path']} {UNVERIFIED_LABEL}（绑的是最新一版平台数据，当场核对没通过）：改好后重新绑定。")
    for s in info["snapshots"]:
        bad = {k: v for k, v in s["files"].items() if v != "ok"}
        if bad:
            warnings.append(f"平台快照 {s['snapshot_id']} 的文件被改过或丢了，核对不会再采用它：重新导出并登记新快照。")
    return {"snapshots": latest_rows, "lines": lines, "warnings": warnings, "last_identity_check": info["last_identity_check"]}


def main():
    parser = argparse.ArgumentParser(description="产品线测算平台快照：身份核对记录、登记同一次导出的 HTML 与 JSON、把成果绑到快照、查看")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("register", "bind", "identity", "inspect"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", type=Path, required=True)   # 全盘细查返修 P2-3：总是输出 JSON，不再接受不起作用的 --json
        if name != "inspect":
            p.add_argument("--actor", required=True)
    reg = sub.choices["register"]
    reg.add_argument("--html", type=Path, required=True); reg.add_argument("--json-file", "--data", dest="json_file", type=Path, required=True)
    reg.add_argument("--exported-at", required=True)
    reg.add_argument("--plan", help="这次快照属于哪个产品矩阵计划（工作区里只有一个计划时可不写，自动关联）")
    b = sub.choices["bind"]
    b.add_argument("--snapshot", required=True); b.add_argument("--path", required=True); b.add_argument("--kind", default="other", choices=KINDS)
    b.add_argument("--competitors", type=Path, help="演示稿里有竞品价格时，竞品对照表的位置")
    i = sub.choices["identity"]
    i.add_argument("--username", required=True); i.add_argument("--role", required=True); i.add_argument("--projects", type=int, required=True)
    i.add_argument("--environment")
    args = parser.parse_args()
    if args.command == "register":
        result = register(args.workspace, args.actor, args.html, args.json_file, args.exported_at, args.plan)
    elif args.command == "bind":
        result = bind(args.workspace, args.actor, args.snapshot, args.path, args.kind, args.competitors)
    elif args.command == "identity":
        result = identity(args.workspace, args.actor, args.username, args.role, args.projects, args.environment)
    else:
        result = inspect(args.workspace)
    print(json.dumps(result, ensure_ascii=False, indent=2))
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
