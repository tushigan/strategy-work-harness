"""Read HTML as data only; use the shared Node validator without executing HTML."""
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess

from phase2_store import WorkflowError


class DataContainer(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.html = html
        self.lines = [0]
        for line in html.split("\n"):
            self.lines.append(self.lines[-1] + len(line) + 1)
        self.start = self.end = None
        self.count = 0
        self.active = False
        self.closed = None
        self.feed(html)
        self.close()
        if self.count != 1 or self.end is None or self.active or self.closed is None or self.closed <= self.end:
            raise WorkflowError("HTML 数据容器缺失、重复或文件未写完整；保留原文件")

    def position(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id") != "brand-house-data":
            return
        if len(attrs) != len(values) or tag != "script" or values.get("type") != "application/json":
            raise WorkflowError("品牌屋数据容器的标签或属性不合法")
        self.count += 1
        if self.count != 1:
            raise WorkflowError("品牌屋只能有一个数据容器")
        self.active = True
        self.start = self.position() + len(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if dict(attrs).get("id") == "brand-house-data":
            raise WorkflowError("品牌屋数据容器不能自闭合")

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.end = self.position()
            self.active = False
        if tag == "html":
            self.closed = self.position()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowError("JSON 含重复键，不能选择其中一份继续")
        result[key] = value
    return result


def parse_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(WorkflowError("JSON 含非有限数值")))
    except (ValueError, TypeError, RecursionError) as exc:
        raise WorkflowError(f"品牌屋 JSON 无效：{exc}") from exc


def validate_data(data):
    try:
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise WorkflowError("品牌屋数据不是有限、可序列化的 JSON") from exc
    node = os.environ.get("HARNESS_NODE", "node")
    script = Path(__file__).with_name("brand_house_history.mjs")
    try:
        result = subprocess.run([node, str(script), "validate"], input=raw, text=True,
                                encoding="utf-8", capture_output=True, timeout=120, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise WorkflowError(f"品牌屋历史校验失败；检查 HARNESS_NODE 或 PATH：{detail[-1500:]}") from exc
    checked = parse_json(result.stdout)
    if checked != data:
        raise WorkflowError("历史校验器没有返回完整的原始合法数据")
    return checked


def extract(raw):
    try:
        html = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        container = DataContainer(html)
        return validate_data(parse_json(html[container.start:container.end]))
    except (UnicodeError, TypeError, RecursionError) as exc:
        raise WorkflowError("HTML 编码或结构无效，不能作为当前版本") from exc


def encode(data):
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    for character, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                               ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        raw = raw.replace(character, escaped)
    return raw


def embed(html, data):
    validate_data(data)
    container = DataContainer(html)
    return (html[:container.start] + encode(data) + html[container.end:]).encode("utf-8")
