#!/usr/bin/env python3
"""产品矩阵的两项核对：角色与测算（roles）、演示稿数字与平台快照（deck）。

- 本文件不含任何利润、价格、成本计算：只读绑定快照里平台已经算好的字段，做“相等”“谁最高 / 谁最低”的比较。
  字段缺失就报“平台数据格式变了，按平台当前说明核对”，不推算、不猜；没有快照就说“待平台测算”，不给结果。
- 利润款比工厂净利率（平台 netMargin，用户 2026-10-13 原话“利潤款看淨利率就好”）；引流款比每 500 克零售价最低（retailPer500g），
  引流款可以亏、不设亏损上限；形象款与基础盘不做数字核对。
- 显示换算（只此一类，不是业务计算）：演示稿照提报页抄的数字是平台原始值取了几位小数（提报页写 2 位），所以按演示稿写出的小数位，把平台值四舍五入
  （ROUND_HALF_UP）到同一位再比；写成百分数的比例字段先移两位小数点再取舍。用户填进平台的值不取舍，必须相等。哪些字段算哪一类见下面“字段口径”。"""
import argparse
import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from html.parser import HTMLParser
from pathlib import Path

from phase2_store import WorkflowError, local

ROLES = ("形象款", "引流款", "利润款", "基础盘")
# ---------------- 字段口径（全盘细查返修 P1-1：按平台真实语义列全，只此一处；依据平台源码 core/src/types.ts、formulas.ts、glossary.ts、
# server/src/presentation.ts，2026-10-14 只读核对） ----------------
# ① 用户填进平台的值：演示稿 / 矩阵表写的数必须和平台原值完全相等（9.9 = 9.90，不等于 10），不按小数位取舍；不一致时提示里写平台原值。
INPUT_FIELDS = {"retailPrice", "onlineRetailPrice", "targetRetailPrice", "netWeight", "unitCount", "unitWeight", "reverseNetWeight", "materialTon",
                "piecesPerMinUnit", "minUnitsPerSalesUnit", "unitsPerBox", "cartonCostPerBox", "innerPackPerUnit", "onlineExtraWeightG",
                # 收尾小修 P2-C：对照 core/src/types.ts 的 SkuInput 补上其余用户填的数值
                "outerPackPerUnit", "pdqCostPerBox", "salesUnitsPerPdq", "onlineFreight", "reverseOnlineExtraWeightG", "reverseOnlineFreight",
                "middlePackPerUnit", "innerPacksPerPdq", "innerPackGrossWeightG", "pdqsPerCarton", "reverseUnitWeight", "reversePiecesPerMinUnit",
                "reverseMinUnitsPerSalesUnit"}
# 已知边界：没列进来的用户输入（平台以后新增的输入字段、本表没见过的字段）按显示取舍比（小数位取舍），比完全相等松；新增后补进上面这张表。
# ② 出厂价看品项：线下品项是用户填的（同 ①）；线上品项的 factoryPrice 是平台算出的线上出厂价（types.ts“线上渠道取系统计算的 onlineFactoryPrice”，同 ③）。
#    线上还是线下按快照提报页里这一款所在的对比表区块认（线上运营对比 / 线上反推对比），不按渠道名推断。
OFFLINE_INPUT_FIELDS = {"factoryPrice"}
ONLINE_BLOCKS = {"线上运营对比", "线上反推对比"}
# ③ 其余是平台算出的值（线上品项的 factoryPrice、onlineFactoryPrice、maxFactoryPrice、每 500 克价、毛利润额、总成本、各种利润率结果等）：
#    按演示稿写出的小数位，把平台值四舍五入到同一位再比（提报页写 2 位）。
# ④ 比例字段（平台 glossary 里单位是 % 的全部字段）：认百分数写法（先移两位小数点）；其中用户填的比例（RATIO_INPUT_FIELDS）移位后必须完全相等。
RATIO_INPUT_FIELDS = {"otherRatio", "freightRate", "marketRate", "taxRate", "onlineProductionGrossMarginRate", "onlinePlatformCommissionRate",
                      "onlineMarketingReserveRate", "targetNetMargin", "channelMargin", "reverseTargetNetMargin", "reverseMarketRate", "reverseFreightRate",
                      "reverseTaxRate", "reverseOtherRatio", "reverseOnlineProductionGrossMarginRate", "reverseOnlinePlatformCommissionRate",
                      "reverseOnlineMarketingReserveRate"}
RATIO_FIELDS = RATIO_INPUT_FIELDS | {"grossMargin", "netMargin", "onlineProductionGrossMargin", "onlineEcomTeamNetMargin", "channelProfit"}
# ⑤ 矩阵表的“价格”比哪个零售价：按这一款所在的对比表区块（线下渠道 → retailPrice，线上运营 → onlineRetailPrice，反推 → targetRetailPrice）
PRICE_FIELD = {"线下渠道对比": "retailPrice", "线上运营对比": "onlineRetailPrice", "线下反推对比": "targetRetailPrice", "线上反推对比": "targetRetailPrice"}
MINUS = "\u2212\uff0d\ufe63"
FIELD_NAMES = {"retailPrice": "零售价", "onlineRetailPrice": "线上零售价", "targetRetailPrice": "目标零售价", "maxFactoryPrice": "最高出厂价",
               "onlineFactoryPrice": "线上出厂价", "onlineEcomTeamNetMargin": "电商运营净利率", "reverseTargetNetMargin": "反推目标净利率",
               "factoryPrice": "出厂价", "netWeight": "净含量（克）", "netMargin": "工厂净利率", "grossMargin": "毛利率",
               "grossProfit": "毛利润额", "retailPer500g": "每 500 克零售价", "factoryPer500g": "每 500 克出厂价", "channelProfit": "渠道利润率",
               "totalCost": "总成本", "mainChannel": "主推渠道", "unitCount": "件数", "packagingCost": "包材成本", "freight": "运费"}
CHOICES = "角色和测算对不上：改这一款的角色，还是改方案让它名副其实，由策略师决定。"
MANY_NUMBERS = "一个数据标注里只能放一个数"
PENDING = "还没有登记平台快照，测算一步是“待平台测算”：不给结果、不能核对。先导出并登记快照。"
NUMBER = re.compile(r"[-" + MINUS + r"]?\d[\d,]*(?:\.\d+)?")
VOID = {"br", "img", "meta", "link", "input", "hr", "area", "base", "col", "embed", "source", "track", "wbr"}


def label(field):
    return FIELD_NAMES.get(field, f"平台字段「{field}」")


def _dec(value):
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _platform_value(value):
    """平台值转成可比较的东西：数字 → Decimal；列表 → 用顿号连起来的文字；文字原样；空值 → None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, list):
        return "、".join(str(x) for x in value)
    return str(value).strip()


def numbers_in(text):
    """P2-11：文字里有几个数（一个数据标注里只能放一个数）。"""
    return len(NUMBER.findall(str(text)))


def shown_parts(text):
    """演示稿 / 矩阵表里写的数：返回 (数, 是不是写成百分数, 小数位数)；读不出数返回 (None, False, 0)。"""
    found = NUMBER.search(str(text))
    if not found:
        return None, False, 0
    raw = found.group(0).replace(",", "")
    for sign in MINUS:
        raw = raw.replace(sign, "-")
    places = len(raw.split(".")[1]) if "." in raw else 0
    return _dec(raw), ("%" in str(text) or "％" in str(text)), places


def exact_field(field, sku=None):
    """这一项是不是用户填进平台的值（完全相等，不取舍）。字段口径见文件顶部；出厂价按这一款在提报页里是线上还是线下。"""
    if field in INPUT_FIELDS or field in RATIO_INPUT_FIELDS:
        return True
    return field in OFFLINE_INPUT_FIELDS and not is_online(sku)


def is_online(sku):
    return isinstance(sku, dict) and sku.get("block") in ONLINE_BLOCKS


def same_number(text, reference, field=None, exact=None):
    """演示稿写的数和平台（或对照表）里的数是不是同一个。exact（不写时：没有字段名、或字段是用户填的）——数值必须相等（9.9 = 9.90，不等于 10），
    比例字段写成百分数时只移小数点、不取舍；否则按演示稿写出的小数位，把参照值四舍五入到同一位再比。竞品价格、矩阵表数字不写字段名，按相等比。
    非比例字段写成百分数一律不算。读不出、过长的数都按“不一致”处理，不抛错。"""
    number, percent, places = shown_parts(text)
    if number is None or not isinstance(reference, Decimal):
        return False
    if percent and field not in RATIO_FIELDS:
        return False
    if exact is None:
        exact = field is None or exact_field(field)
    try:
        with localcontext() as ctx:
            ctx.prec = 1000
            if exact:
                return number == shifted(reference, percent)
            return number == as_shown(reference, percent, places)
    except (InvalidOperation, ArithmeticError):
        return False


def shifted(number, percent):
    """全包唯一一处小数点位移：写成百分数时把小数点右移两位（0.0315 → 3.15），否则原样。只是写法换算，不是业务计算。"""
    with localcontext() as ctx:
        ctx.prec = 1000
        return number.scaleb(2) if percent else number


def as_shown(number, percent, places):
    """显示取舍：把平台的数按页面写法取到 places 位小数（ROUND_HALF_UP）；percent 时先移两位小数点（shifted）。
    不是业务计算——平台页面、演示稿上看到的就是这个样子。"""
    return shifted(number, percent).quantize(Decimal((0, (1,), -places)), rounding=ROUND_HALF_UP)


def page_number(value, percent, places=2):
    """平台值（JSON 里的数）按提报页写法取舍后的 Decimal；不是数返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        return as_shown(Decimal(str(value)), percent, places)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def page_text(value, percent=False, places=2):
    """v1.7.6 W5：给策略师看的平台数字按提报页写法（两位小数；比例写成百分数）。不是数原样写文字，空值写“没有值”。原值另留技术附录。"""
    shown = page_number(value, percent, places)
    if shown is None:
        return "没有值" if value is None else str(value)
    return format(shown, "f") + ("%" if percent else "")


def browser_value(value, percent):
    """浏览器里的样子（P2-1）：平台页面先把数当二进制浮点数，百分数先在浮点里“乘 100”再两位小数（presentation.ts 的 pct）。
    这里用精确的小数点位移再取最近的浮点数——结果和浏览器的浮点乘法逐位相同（两者都是“精确值取最近的浮点数”），仍只是显示写法，不是业务计算。"""
    binary = Decimal(float(value))
    return Decimal(float(shifted(binary, True))) if percent else binary


def page_candidates(value, percent, places):
    """v1.7.6 W1：提报页上这个数可能的写法。十进制取舍一种；浏览器写法（browser_value 后按二进制精确值取舍，正好一半取大的）一种，
    两者在“正好一半”时可能差一位，都认（仍只是显示取舍）。不是数返回空集合。"""
    out = set()
    first = page_number(value, percent, places)
    if first is not None:
        out.add(first)
        try:
            out.add(as_shown(browser_value(value, percent), False, places))
        except (InvalidOperation, ValueError, OverflowError, ArithmeticError):
            pass
    return out


def _show(value):
    """技术附录用：平台原值，不取舍。"""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return "没有值" if value is None else str(value)


def _say(field, value, other=None, exact=False):
    """v1.7.6 W5：给策略师看的平台数字按提报页的写法（两位小数；比例字段写成百分数，ROUND_HALF_UP）——只是显示取舍。
    和要对比的另一个数（other）取舍后一样时多显示两位，免得“21.73% 比 21.73% 低”这种看不懂的话。原值留在技术附录字段里。
    全盘细查返修 P1-1：用户填的值（exact）显示原值，不取舍——免得“演示稿写 19.90，平台是 19.90”这种前后矛盾的话。"""
    percent = field in RATIO_FIELDS
    if exact and isinstance(value, Decimal):
        return format(shifted(value, percent).normalize(), "f") + ("%" if percent else "")
    if isinstance(value, Decimal) and isinstance(other, Decimal) and value != other and page_text(value, percent) == page_text(other, percent):
        return page_text(value, percent, 4)
    return page_text(value, percent)


def _load_json(path, what):
    from pingtai_kuaizhao import read_limited
    try:
        data = json.loads(read_limited(path).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"{what}读不出来：{exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"{what}须是一个 JSON 对象（照 templates 里的模板填）")
    return data


def _snapshot(root, snapshot_id):
    """取快照与规整后的平台数据；没有任何快照返回 (None, None)；有多个平台项目又没指明用哪个、文件改过都照实抛。"""
    from pingtai_kuaizhao import snapshot_data, snapshots
    if not snapshot_id and not snapshots(root):
        return None, None
    return snapshot_data(root, snapshot_id)


def _stale_note(root, snap):
    from pingtai_kuaizhao import latest, STALE_LABEL
    top = latest(root, snap["project_id"])
    if top and top["snapshot_id"] != snap["snapshot_id"]:
        return f"{STALE_LABEL}：这份用的是第 {snap['version']} 版，平台项目已有第 {top['version']} 版快照，先按新快照复核"
    return None


def _finish(issues, notes, stale, ok_text, bad_text, extra):
    """没有问题但用的是旧快照时状态是 stale（不是 ok）；复核提醒也进给策略师的话里。"""
    status = "mismatch" if issues else ("stale" if stale else "ok")
    lines = [i["message"] for i in issues] + ([stale] if stale else [])
    head = bad_text if issues else (ok_text if not stale else "没发现对不上的地方，但用的不是最新的平台数据：")
    return {"status": status, **extra, "issues": issues, "notes": notes, "for_strategist": head + ("\n" if lines else "") + "\n".join(lines)}


def roles(root, matrix_path, snapshot_id=None):
    """角色与测算核对。status：ok / stale / mismatch / pending_platform / format_changed / blocked / invalid。"""
    from pingtai_kuaizhao import find_sku, field_of, FormatChanged, bound_snapshot
    root = Path(root).resolve()
    matrix = _load_json(matrix_path, "产品矩阵表")
    items = matrix.get("items")
    if not isinstance(items, list) or not items:
        return {"status": "invalid", "issues": [{"message": "产品矩阵表里至少要有一款。技术附录（给 Agent）：items 须是非空列表"}]}
    problems = []
    if matrix.get("layout") not in {None, "pyramid", "series"}:
        problems.append({"message": "矩阵的摆法只能是金字塔或按系列分类。技术附录（给 Agent）：layout 只能是 pyramid 或 series"})
    for index, item in enumerate(items, 1):
        who = item.get("name") if isinstance(item, dict) and item.get("name") else f"第 {index} 款"
        if not isinstance(item, dict) or item.get("role") not in ROLES:
            problems.append({"item": who, "message": f"{who}：角色要从 {'、'.join(ROLES)} 里选"})
        elif not isinstance(item.get("sku"), str) or not item["sku"].strip():
            problems.append({"item": who, "message": f"{who}：要写平台里这一款的品项编号（或款名）"})
        elif item.get("group") is not None and not isinstance(item["group"], str):
            problems.append({"item": who, "message": f"{who}：分组要写成文字（或不写）"})
    if problems:
        return {"status": "invalid", "issues": problems}
    if not snapshot_id:
        from pingtai_kuaizhao import file_clash
        clash = file_clash(root, matrix_path, matrix.get("snapshot_id"))
        if clash:
            return {"status": "blocked", "issues": [{"message": clash}]}
    snap_id = snapshot_id or matrix.get("snapshot_id") or bound_snapshot(root, matrix_path)
    try:
        snap, data = _snapshot(root, snap_id)
    except FormatChanged as exc:
        return {"status": "format_changed", "issues": [{"message": str(exc)}]}
    except WorkflowError as exc:
        return {"status": "blocked", "issues": [{"message": str(exc)}]}
    if snap is None:
        return {"status": "pending_platform", "issues": [], "for_strategist": PENDING}
    issues, notes, rows, compared = [], [], [], 0
    stale = _stale_note(root, snap)
    try:
        for item in items:
            sku = find_sku(data, item["sku"])
            if sku is None and item.get("name"):
                sku = find_sku(data, item["name"])
                if sku is not None:
                    notes.append(f"“{item['name']}”写的品项编号“{item['sku']}”在快照里找不到，是靠款名找到的：确认一下编号写对了没有")
            if sku is None:
                issues.append({"item": item.get("name") or item["sku"], "code": "快照里没有这一款", "message":
                               f"“{item.get('name') or item['sku']}”在平台快照里找不到：确认品项编号，或快照是不是旧的"})
                continue
            if item.get("name") and item["name"] != sku["name"]:
                issues.append({"item": item["name"], "code": "款名对不上", "message":
                               f"矩阵表里品项编号“{item['sku']}”写的款名是“{item['name']}”，平台快照里这个编号是“{sku['name']}”：确认编号和款名哪个写错了"})
                continue
            rows.append({"item": item, "sku": sku, "group": item.get("group"), "margin": _platform_value(field_of(sku, "netMargin")),
                         "per500": _platform_value(field_of(sku, "retailPer500g"))})
            for key, field in (("price", PRICE_FIELD.get(sku.get("block"), "retailPrice")), ("net_weight", "netWeight")):
                if item.get(key) is not None:
                    have = _platform_value(field_of(sku, field))
                    if not same_number(item[key], have, field, exact=True):
                        issues.append({"item": sku["name"], "code": "矩阵表数字与快照不一致", "field": field, "table_value": item[key],
                                       "snapshot_value": _show(have),
                                       "message": f"“{sku['name']}”的{label(field)}：矩阵表写 {item[key]}，平台快照里是 {_say(field, have, exact=True)}"})
    except FormatChanged as exc:
        return {"status": "format_changed", "issues": [{"message": str(exc)}], "snapshot_id": snap["snapshot_id"]}
    groups = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    grouped = any(g is not None for g in groups)
    def compared_add(_):
        nonlocal compared
        compared += 1
        return True
    for name, members in groups.items():
        where = "同组" if name else ("未分组的几款里" if grouped else "整张表")
        for r in members:
            if r["margin"] is None:
                notes.append(f"“{r['sku']['name']}”平台没有给出工厂净利率，没参与比较")
        # 利润款：不低于同组所有非利润款（工厂净利率）；引流款：不高于同组所有非引流款（每 500 克零售价）
        others_margin = [(r["margin"], r) for r in members if r["item"]["role"] != "利润款" and isinstance(r["margin"], Decimal)]
        others_per500 = [(r["per500"], r) for r in members if r["item"]["role"] != "引流款" and isinstance(r["per500"], Decimal)]
        top = max(others_margin, key=lambda x: x[0]) if others_margin else None
        low = min(others_per500, key=lambda x: x[0]) if others_per500 else None
        for r in members:
            role, who = r["item"]["role"], r["sku"]["name"]
            if role == "利润款":
                if not isinstance(r["margin"], Decimal):
                    issues.append({"item": who, "role": role, "code": "角色与测算不符", "field": "netMargin",
                                   "message": f"“{who}”定为利润款，但平台没有给出它的工厂净利率，没法确认它不低于{where}其他款。{CHOICES}"})
                elif top is None:
                    notes.append(f"“{who}”定为利润款，但{where}没有别的款可以比（同组都是利润款，或平台没给出别的款的工厂净利率），这一款没核对")
                elif compared_add(top) and r["margin"] < top[0]:
                    issues.append({"item": who, "role": role, "code": "角色与测算不符", "field": "netMargin", "this_value": _show(r["margin"]),
                                   "best_item": top[1]["sku"]["name"], "best_value": _show(top[0]),
                                   "message": f"角色与测算不符：“{who}”定为利润款，它的工厂净利率是 {_say('netMargin', r['margin'], top[0])}，"
                                              f"{where}非利润款里最高的是“{top[1]['sku']['name']}”的 {_say('netMargin', top[0], r['margin'])}。{CHOICES}"})
            elif role == "引流款":
                if not isinstance(r["per500"], Decimal):
                    issues.append({"item": who, "role": role, "code": "角色与测算不符", "field": "retailPer500g",
                                   "message": f"“{who}”定为引流款，但平台没有给出它的每 500 克零售价，没法确认它不高于{where}其他款。{CHOICES}"})
                elif low is None:
                    notes.append(f"“{who}”定为引流款，但{where}没有别的款可以比（同组都是引流款，或平台没给出别的款的每 500 克零售价），这一款没核对")
                elif compared_add(low) and r["per500"] > low[0]:
                    issues.append({"item": who, "role": role, "code": "角色与测算不符", "field": "retailPer500g", "this_value": _show(r["per500"]),
                                   "best_item": low[1]["sku"]["name"], "best_value": _show(low[0]),
                                   "message": f"角色与测算不符：“{who}”定为引流款，它的每 500 克零售价是 {_say('retailPer500g', r['per500'], low[0])}，"
                                              f"{where}非引流款里最低的是“{low[1]['sku']['name']}”的 {_say('retailPer500g', low[0], r['per500'])}。{CHOICES}"})
                if isinstance(r["margin"], Decimal) and r["margin"] < 0:
                    notes.append(f"“{who}”是引流款，工厂净利率是 {_say('netMargin', r['margin'])}（亏损）：引流款可以亏、不设上限，仅提醒")
    skipped = any("没核对" in n for n in notes)
    if compared == 0:
        ok_text = "这次一款也没比较成（表里没有利润款、引流款，或它们在组里没有可比的对象），角色没核对，现在下不了结论。"
    elif skipped:
        ok_text = "比较过的款对得上；没核对的款见说明："
    else:
        ok_text = "角色和平台测算对得上。"
    out = _finish(issues, notes, stale, ok_text, "有角色和平台测算对不上的款，请逐条看：",
                  {"snapshot_id": snap["snapshot_id"], "snapshot_version": snap["version"], "checked_items": len(rows), "compared": compared})
    skipped_lines = [n for n in notes if "没核对" in n]
    if skipped_lines and not issues:
        out["for_strategist"] += ("" if out["for_strategist"].endswith("：") else "\n") + "\n".join(skipped_lines)
    return out


# ---------------- 演示稿数字 ----------------
class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children, self.texts = tag, dict(attrs), parent, [], []

    def text(self):
        return "".join(part if isinstance(part, str) else part.text() for part in self.texts).strip()


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", [], None); self.cur = self.root; self.all = []

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur); self.cur.texts.append(node); self.all.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, attrs, self.cur); self.cur.texts.append(node); self.all.append(node)

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        if self.cur.tag not in {"script", "style"}:
            self.cur.texts.append(data)


def _context_sku(node):
    while node is not None:
        if "data-pingtai-sku" in node.attrs:
            return node.attrs["data-pingtai-sku"]
        node = node.parent
    return None



def deck(root, deck_path, snapshot_id=None, competitors=None):
    """演示稿数字核对。status：ok / stale / mismatch / pending_platform / format_changed / blocked / nothing_bound。"""
    from pingtai_kuaizhao import find_sku, field_of, FormatChanged, read_limited, bound_snapshot
    root = Path(root).resolve()
    path = Path(deck_path)
    if not path.is_file():
        raise WorkflowError("演示稿文件不存在：" + path.name)
    tree = Tree()
    tree.feed(read_limited(path).decode("utf-8"))
    bound = [n for n in tree.all if "data-field" in n.attrs and _context_sku(n)]
    loose = [n for n in tree.all if "data-field" in n.attrs and not _context_sku(n)]
    jingpin = [n for n in tree.all if "data-jingpin" in n.attrs]
    page_ids = sorted({n.attrs["data-pingtai-snapshot"] for n in tree.all if "data-pingtai-snapshot" in n.attrs})
    in_page = page_ids[0] if len(page_ids) == 1 else None
    issues, notes, checked = [], [], 0
    for node in loose:
        issues.append({"field": node.attrs["data-field"], "deck_value": node.text(), "code": "这个数字没有对应的平台品项",
                       "message": f"演示稿里的“{node.text()}”（{label(node.attrs['data-field'])}）不在任何带品项标注的产品卡里，没法对上平台数据："
                                  "把它放进标了品项的区块，或去掉它的数据标注"})
    if jingpin:
        issues += _competitor_issues(jingpin, competitors)
    if not bound and not loose and not jingpin:
        return {"status": "nothing_bound", "issues": [], "for_strategist": "演示稿里没有标出哪些数字来自平台，这次核对什么也没查到，不算通过。"
                "技术附录（给 Agent）：产品卡区块用 data-pingtai-sku（品项编号或款名）、数字元素用 data-field（平台字段名）标注"}
    if not bound:
        return _finish(issues, notes, None, "竞品价格都有来源。", "有数字对不上：", {"checked_numbers": 0})
    if not snapshot_id:
        from pingtai_kuaizhao import file_clash
        if len(page_ids) > 1:
            return {"status": "blocked", "issues": issues + [{"message": f"演示稿里写了几个不同的快照号（{'、'.join(page_ids)}），不知道该拿哪一个核对：先统一成一个，"
                                                                 "或者核对时指明用哪个"}]}
        clash = file_clash(root, path, in_page)
        if clash:
            return {"status": "blocked", "issues": issues + [{"message": clash}]}
    try:
        snap, data = _snapshot(root, snapshot_id or in_page or bound_snapshot(root, path))
    except FormatChanged as exc:
        return {"status": "format_changed", "issues": [{"message": str(exc)}]}
    except WorkflowError as exc:
        return {"status": "blocked", "issues": [{"message": str(exc)}]}
    if snap is None:
        return {"status": "pending_platform", "issues": issues, "for_strategist": PENDING}
    stale = _stale_note(root, snap)
    known = {name for sku in data["skus"] for name in sku["fields"]}
    try:
        for node in bound:
            key, field = _context_sku(node), node.attrs["data-field"]
            sku = find_sku(data, key)
            shown = node.text()
            if sku is None:
                issues.append({"sku": key, "field": field, "deck_value": shown, "snapshot_value": None, "code": "快照里没有这一款",
                               "message": f"演示稿里的“{key}”在平台快照里找不到：确认品项编号，或快照是不是旧的"})
                continue
            if field not in known:
                issues.append({"sku": sku["name"], "field": field, "deck_value": shown, "snapshot_value": None, "code": "演示稿字段名不认识",
                               "message": f"演示稿里“{sku['name']}”的数据标注「{field}」不是平台数据里有的字段，多半是写错了：对照平台字段名改正"})
                continue
            have = _platform_value(field_of(sku, field))
            checked += 1
            if isinstance(have, Decimal) and numbers_in(shown) > 1:
                issues.append({"sku": sku["name"], "field": field, "deck_value": shown, "snapshot_value": _show(have), "code": MANY_NUMBERS,
                               "message": f"“{sku['name']}”的{label(field)}：演示稿这一处写的是“{shown}”，{MANY_NUMBERS}，不知道该拿哪个核对："
                                          "把别的数（如原价、第几款）挪到标注外面"})
                continue
            exact = exact_field(field, sku)
            if isinstance(have, Decimal):
                same = same_number(shown, have, field, exact=exact)
            else:
                same = have is not None and "".join(shown.split()) == "".join(str(have).split())
            if not same:
                issues.append({"sku": sku["name"], "field": field, "deck_value": shown, "snapshot_value": _show(have), "code": "演示稿数字与快照不一致",
                               "message": f"“{sku['name']}”的{label(field)}：演示稿写“{shown}”，平台快照里是 {_say(field, have, exact=exact)}"})
    except FormatChanged as exc:
        return {"status": "format_changed", "issues": issues + [{"message": str(exc)}], "snapshot_id": snap["snapshot_id"]}
    return _finish(issues, notes, stale, "演示稿里绑定的数字都和平台快照一致。", "有数字和平台快照对不上，请逐条看：",
                   {"snapshot_id": snap["snapshot_id"], "snapshot_version": snap["version"], "checked_numbers": checked})


def _competitor_issues(nodes, competitors):
    from matrix_plan import check_competitors
    if competitors is None:
        return [{"code": "竞品价格没有对照表", "message": "演示稿里有竞品价格，但没给竞品对照表，没法确认每个价格的来源和日期。技术附录（给 Agent）：deck 命令需加 --competitors <对照表路径>"}]
    table = _load_json(competitors, "竞品对照表")
    verdict = check_competitors(competitors)
    rows = {r.get("id"): r for r in table.get("rows", []) if isinstance(r, dict)}
    out = []
    for node in nodes:
        rid = node.attrs["data-jingpin"]
        shown = node.text()
        if numbers_in(shown) > 1:
            out.append({"competitor": rid, "deck_value": shown, "code": MANY_NUMBERS, "message":
                        f"演示稿里的竞品价格“{shown}”（竞品 {rid}）：{MANY_NUMBERS}，不知道该拿哪个核对：把别的数挪到标注外面"})
        elif rid not in rows:
            out.append({"competitor": rid, "deck_value": shown, "code": "竞品价格没有来源", "message": f"演示稿里的竞品价格“{shown}”（竞品 {rid}）在竞品对照表里找不到"})
        elif rid not in verdict["usable_rows"]:
            reason = "查不到，演示稿里只能写“未查到”" if rid in verdict["not_found_rows"] else "没有来源或日期"
            out.append({"competitor": rid, "deck_value": shown, "code": "竞品价格没有来源", "message":
                        f"演示稿里的竞品价格“{shown}”（竞品 {rid}）{reason}，没有来源的数字不进演示稿"})
        elif not same_number(shown, _dec(rows[rid]["price"])):
            out.append({"competitor": rid, "deck_value": shown, "code": "演示稿数字与来源不一致", "message":
                        f"竞品 {rid} 的价格：演示稿写“{shown}”，竞品对照表里是 {rows[rid]['price']}"})
    return out


def main():
    parser = argparse.ArgumentParser(description="产品矩阵核对：角色与测算（roles）、演示稿数字与平台快照（deck）；只读平台已算好的字段")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("roles"); r.add_argument("--workspace", type=Path, required=True); r.add_argument("--matrix", type=Path, required=True)
    r.add_argument("--snapshot")
    d = sub.add_parser("deck"); d.add_argument("--workspace", type=Path, required=True); d.add_argument("--deck", type=Path, required=True)
    d.add_argument("--snapshot"); d.add_argument("--competitors", type=Path)
    args = parser.parse_args()
    result = (roles(args.workspace, args.matrix, args.snapshot) if args.command == "roles"
              else deck(args.workspace, args.deck, args.snapshot, args.competitors))
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
