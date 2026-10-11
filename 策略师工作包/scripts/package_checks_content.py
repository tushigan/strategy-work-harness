"""Parse structured records and inspect literals without echoing matched values."""
import ast
import io
import json
from pathlib import PurePosixPath
import re
import tokenize
import tomllib
import xml.etree.ElementTree as ET

from package_checks import issue

# 凭据判定（v1.7.5 G14；r3 返修收紧，r4 收回未申报的变松）：发行检查（文本、JSON、Python 字面量、XML）、启动检查、记一笔三处共用本节。
# 总原则：只能比 v1.7.4 更严或相同；放宽只限下面明确列出的规范占位写法与“只说明凭据”的键名尾巴。
#
# 键名规则 secret_key_name（取代原全匹配正则与启动检查另写的子串规则）：键名里含 api_key、access_token、refresh_token、
# auth_token、client_secret、secret、password、passwd、private_key、authorization（不分大小写，任意前后缀：app_secret、appSecret、
# FEISHU_APP_SECRET、secretKey、aws_secret_access_key、passwords……）；或以单独的 token 结尾（token、x-token、x.token；
# max_token、doc_token 这类下划线连写的不算，同 v1.7.4）。只说明凭据、不存凭据的尾巴（明确列出才放行）：
# _level、_expires / _expires_in / _expires_at、_expiry、_expiration、_ttl、_length（如 secret_level、access_token_expires_in）。
# r4：尾巴前的分隔符必须是 - 或 _（secretttl、passwordlength、password.expires、secretLevel 不算尾巴）；而且只在值“只说明凭据”时豁免。
# 批次二开工（批次一第 4 次核对 P2）收紧“只说明凭据”的值，只认这几类：数字（JSON 数值；文字写的纯数字，不以 0 开头、最多 13 位）、
# 数字加时间单位（7200s、30d、12 h、30天）、日期（2026-10-10、2026-10-10T08:30:00+08:00、2026/10/10、2026年10月10日）、
# 布尔与布尔词（true/false/yes/no/on/off、是/否）、等级词（none/low/medium/moderate/high/critical/strict/normal、低/中/高/严重）、
# 空（None、空串、空列表、空对象；文字里不带引号的 null 按 None）。别的一律不豁免：字母数字混排、带符号或空格、全字母或全数字但不属上面几类（带符号的口令样式、
# 全字母重复串、全字母十六进制串、16 位数字、0 开头的数字串）都照拦。
_KEY_WORD = re.compile(r"api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|client[-_]?secret|"
                       r"secret|password|passwd|private[-_]?key|authorization", re.I)
_KEY_META = re.compile(r"[-_](?:level|expires(?:[-_](?:in|at))?|expiry|expiration|ttl|length)", re.I)
_BARE_TOKEN = re.compile(r"(?:^|[^A-Za-z0-9_])token\Z", re.I)
_NUMBER = r"(?:0|[1-9][0-9]{0,12})(?:\.[0-9]{1,6})?"
_META_TEXT = re.compile(
    _NUMBER + r"|" + _NUMBER + r"[ \t]?(?:ms|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?|w|wks?|weeks?|mo|months?|y|yrs?|years?|"
    r"秒|分钟|分|小时|天|日|周|个月|月|年)|"
    r"true|false|yes|no|on|off|是|否|none|low|medium|moderate|high|critical|strict|normal|低|中|高|严重", re.I)
_META_DATE = re.compile(r"([0-9]{4})(?:-([0-9]{2})-([0-9]{2})(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?|"
                        r"/([0-9]{1,2})/([0-9]{1,2})|年([0-9]{1,2})月([0-9]{1,2})日)")
_UNICODE_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})")
_NO_VALUE = object()


def _meta_date(text):
    """日期（只认真实存在的年月日；时分秒只核格式）。"""
    import datetime
    match = _META_DATE.fullmatch(text)
    if not match:
        return False
    year, *rest = match.groups()
    month, day = next((rest[i], rest[i + 1]) for i in (0, 2, 4) if rest[i] is not None)
    try:
        datetime.date(int(year), int(month), int(day))
    except ValueError:
        return False
    return True


def meta_value(value):
    """尾巴豁免的值（批次二开工收紧，见上方说明）：数字、数字加时间单位、日期、布尔与布尔词、等级词、空。"""
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, (list, dict)):
        return not value
    if not isinstance(value, str):
        return False
    if value == "":
        return True
    text = value.strip()
    # 文字里按赋值认出的值（不带引号时到空白、逗号、分号为止）：JSON 原文的 7200} 去掉收尾括号再判；JSON 原文的 \uXXXX 转义先还原（30\u5929 → 30天）
    text = text.rstrip("}])") if text[:1] not in "{[(" else text
    text = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text[:240])
    return len(text) <= 40 and (bool(_META_TEXT.fullmatch(text)) or _meta_date(text))


def secret_key_name(key, value=_NO_VALUE):
    """键名像凭据（见上方说明）。value 给出时，只有尾巴的键名在值“只说明凭据”时才不算；不给值按像凭据算（宁可多拦）。
    逐个关键词线性扫描，长键名不回溯。"""
    if not isinstance(key, str):
        return False
    words = list(_KEY_WORD.finditer(key))
    if any(not _KEY_META.fullmatch(key, m.end()) for m in words) or _BARE_TOKEN.search(key):
        return True
    return bool(words) and (value is _NO_VALUE or not meta_value(value))


SECRET = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,}|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bxox[baprs]-[A-Za-z0-9-]{12,}|"
    r"\bBearer[ \t]+[A-Za-z0-9._~+/-]{12,}|"
    # r3：Authorization 头的 Basic / Digest / Negotiate 值整段算凭据（只认 Authorization 后面的，避免“Basic 信息”之类误报）
    r"\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?(?:basic|digest|negotiate)[ \t]+[A-Za-z0-9+/=._~-]+|"
    r"https?://[^/\s:@]+:[^/\s@]+@)", re.I)
# 赋值写法：键名[引号][空白](: 或 =)[空白]值。r4 #2 拆两步（复杂度线性）：先只认“键名 + 分隔符”（键名只从一串 [A-Za-z0-9_-] 的开头
# 尝试），键名像凭据时再从分隔符后单独认值；键名不像凭据时只跳过键名、从后面接着找（值里再出现凭据键名的赋值照拦）。
# 逐条取用 assignments()，不直接用这两个正则。
ASSIGNMENT_KEY = re.compile(r"""(?<![A-Za-z0-9_-])([A-Za-z0-9_-]+)["']?\s*(?::|=(?!=|>))""")
ASSIGNMENT_VALUE = re.compile(r"""\s*(?:"([^"\r\n]*)"|'([^'\r\n]*)'|([^\s,;#]+))""")


class Assignment:
    """一条凭据键名的赋值：group(1) 键名，group(2)—group(4) 值（双引号、单引号、不带引号），start()/end() 整段位置。"""
    __slots__ = ("key", "value")

    def __init__(self, key, value):
        self.key, self.value = key, value

    def group(self, n=0):
        if n == 0:
            return self.key.string[self.start():self.end()]
        return self.key.group(1) if n == 1 else self.value.group(n - 1)

    def groups(self):
        return (self.key.group(1),) + self.value.groups()

    def start(self):
        return self.key.start()

    def end(self):
        return self.value.end()


def assignments(text):
    """文字里凭据键名的赋值（Assignment）。"""
    position = 0
    while True:
        key = ASSIGNMENT_KEY.search(text, position)
        if not key:
            return
        # 先只看键名（不看值时像凭据才往下认值）：键名不像凭据的赋值不读值，长值不重复扫描
        value = ASSIGNMENT_VALUE.match(text, key.end()) if secret_key_name(key.group(1)) else None
        if value and secret_key_name(key.group(1), _literal_value(value)):
            yield Assignment(key, value)
            position = value.end()
        else:
            position = key.end(1)


def _literal_value(value):
    """赋值里认出的值：不带引号的 null（JSON 原文的 null、null}）按 None；带引号的 "null" 仍是文字。"""
    quoted = next((p for p in value.groups()[:2] if p is not None), None)
    if quoted is not None:
        return quoted
    bare = value.group(3) or ""
    return None if bare.rstrip("}])").lower() == "null" else bare


def assignment_value(match):
    return next((part for part in match.groups()[1:] if part is not None), "")


def replace_assignments(text, replacement, tail=None):
    """把凭据键名的赋值整段换成 replacement(match)（去敏、发行报告遮挡用）。
    tail（去敏用，r4 #5）：不带引号的值后面再按 tail 正则往后多去一段（值中有空格的凭据去到行尾或下一个分隔符）。"""
    parts, last = [], 0
    for match in assignments(text):
        if match.start() < last:
            continue
        end = match.end()
        if tail is not None and match.group(4) is not None:
            end = tail.match(text, end).end()
        parts += [text[last:match.start()], replacement(match)]
        last = end
    return "".join(parts) + text[last:]


# 本机绝对路径（v1.7.5 返修；r3）：前面只排除英文字母、数字、下划线——紧挨汉字、英文冒号、斜杠、点都认（file 网址三条斜杠后的
# 本机路径、“路径:”后紧跟的本机路径也认；普通网址里主机名后面的路径前面是字母，不会认成本机路径）；补 root、srv、data 与 macOS 数据卷前缀。
_PATH_START = (r"""(?<![A-Za-z0-9_])(?:/(?:Users|home|Volumes|private|tmp|opt|var|mnt|root|srv|data|System/Volumes/Data)/|"""
               r"""[A-Za-z]:[/\\]|~[/\\]|\\\\[\w.-]+\\)""")
DEV_PATH = re.compile(_PATH_START + r"""[^"'`\s<>]+""")
# 去敏用（运行日志首行等）：到行尾或下一个分隔符（中文标点、引号、括号）为止；路径内的空格只在“空格后不是汉字”时算路径
# （r3：遇“空格+汉字”停，后面的白话说明保留），末尾空白不算
_PATH_STOP = "\"'`<>\r\n，。；：、！？“”‘’「」『』（）()\\[\\]【】《》"
_CJK = "㐀-䶿一-鿿豈-﫿"
DEV_PATH_SPAN = re.compile(_PATH_START + "(?:[^" + _PATH_STOP + " \t]|[ \t]+(?![" + _CJK + "])(?=[^" + _PATH_STOP + " \t]))*")
# 工作区前缀替换（r3）：前缀后面紧跟“/”或到这里（空白、分隔符、结尾）才算工作区，“客户A-备份”不算“客户A”
PATH_BOUNDARY = "(?=[\\s" + _PATH_STOP + "]|\\Z)"


# 规范占位（r3 #1；r4 #3 再收）：只认“像说明文字”的写法，不加 re.I——
#   尖括号里只许小写字母、下划线、连字符、空格、汉字，不含数字，≤ 40 字（<your_api_key>、<填写密钥>）；
#   your_ / your- 后只接小写字母、下划线、连字符（your_app_secret）；
#   ${…} 只认以字母开头、含下划线的全大写名（${FEISHU_APP_SECRET}；${ABCDEF123456}、${_ABC} 不算）；三个英文句点。
#   r4：恰好三个小写 x 不再算占位（回到 v1.7.4 口径）。
# 发行检查另保留 v1.7.4 发行检查原有的固定写法 V174_PLACEHOLDERS（大小写不论）；启动检查不认这些固定写法（startup_placeholder，
# 保持 v1.7.4 启动检查口径：v1.7.4 启动检查报的，现在仍报）。
# 非文字值：只有 None、空列表、空对象算占位（False、0 照拦，同 v1.7.4 启动检查）。
PLACEHOLDER = re.compile("<[a-z_ " + _CJK + "-]{1,40}>|your[_-][a-z_-]+|\\$\\{[A-Z][A-Z0-9]*_[A-Z0-9_]*\\}|\\.\\.\\.")
V174_PLACEHOLDERS = {"", "null", "none", "false", "redacted", "<redacted>", "${api_key}", "${token}", "your_api_key"}


def placeholder(value):
    """发行检查（与文字判定 text_has_secret）的占位：v1.7.4 发行检查的固定写法，加上面列出的规范写法。"""
    if not isinstance(value, str):
        return value is None or (isinstance(value, (list, dict)) and not value)
    value = value.strip()
    return value.lower() in V174_PLACEHOLDERS or bool(PLACEHOLDER.fullmatch(value))


def startup_placeholder(value):
    """启动检查 JSON/TOML 键值（walk_sensitive_keys）的占位（r4 #3）：v1.7.4 启动检查只放过 None、空串、空列表、空对象；
    现另放过上面列出的规范写法，但 v1.7.4 发行检查专有的固定写法（null、None、false、REDACTED、<redacted>、${token}、${api_key}、
    your_api_key、只有空白）照报。"""
    if value is None or value == "" or (isinstance(value, (list, dict)) and not value):
        return True
    if not isinstance(value, str) or value.strip().lower() in V174_PLACEHOLDERS:
        return False
    return bool(PLACEHOLDER.fullmatch(value.strip()))


def text_has_secret(text, assignments_too=True):
    """文字里像凭据（发行检查、启动检查、记一笔三处共用）：常见密钥格式、Bearer、Authorization 头、带凭据的网址，
    或凭据键名（secret_key_name）赋了非占位的值。"""
    if SECRET.search(text):
        return True
    return assignments_too and any(not placeholder(assignment_value(m)) for m in assignments(text))


def scan_literal(text, name, errors, line=None, assignments=True):
    if text_has_secret(text, assignments):
        issue(errors, "credential", name, line)
    if DEV_PATH.search(text):
        issue(errors, "absolute_path", name, line)


def python_literals(text, name, errors):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        issue(errors, "invalid_source", name)
        scan_literal(text, name, errors)
        return
    regex_modules, regex_functions = {"re"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            regex_modules.update(x.asname or x.name for x in node.names if x.name == "re")
        if isinstance(node, ast.ImportFrom) and node.module == "re":
            regex_functions.update(x.asname or x.name for x in node.names)
    ignored = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        is_regex = (isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
                    and function.value.id in regex_modules) or (
                        isinstance(function, ast.Name) and function.id in regex_functions)
        pattern = node.args[0]
        if is_regex and isinstance(pattern, ast.Constant) and isinstance(pattern.value, str):
            if re.search(r"[\[\]{}\\^$]|[.*+?](?:[+?])?|\(\?", pattern.value):
                ignored.add(id(pattern))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in ignored:
            scan_literal(node.value, name, errors, node.lineno)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Constant) and not placeholder(value.value):
                for target in targets:
                    key = target.id if isinstance(target, ast.Name) else ""
                    if secret_key_name(key, value.value):
                        issue(errors, "credential", name, node.lineno)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                        and isinstance(value, ast.Constant) and secret_key_name(key.value, value.value)
                        and not placeholder(value.value)):
                    issue(errors, "credential", name, node.lineno)
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                scan_literal(token.string, name, errors, token.start[0])
    except tokenize.TokenError:
        issue(errors, "invalid_source", name)


def reject_duplicates(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("Duplicate key")
        value[key] = child
    return value


def json_value(text):
    return json.loads(text, object_pairs_hook=reject_duplicates,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite")))


def structured(name, text, errors):
    suffix = PurePosixPath(name).suffix.lower()
    try:
        if suffix == ".toml":
            return [(None, tomllib.loads(text))]
        if suffix == ".json":
            return [(None, json_value(text))]
        if suffix == ".jsonl":
            values = []
            for number, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    try:
                        values.append((number, json_value(line)))
                    except (ValueError, RecursionError):
                        issue(errors, "invalid_structure", name, number)
            return values
    except (ValueError, RecursionError):
        issue(errors, "invalid_structure", name)
    return []


def scan_text(name, text, errors):
    if name.endswith(".py"):
        python_literals(text, name, errors)
    elif PurePosixPath(name).suffix.lower() in {".js", ".mjs", ".cjs", ".html"}:
        # Only actual quoted assignments are secret assignments in JavaScript.
        scan_literal(text, name, errors, assignments=False)
        for match in assignments(text):
            if match.group(2) is not None or match.group(3) is not None:
                if not placeholder(match.group(2) or match.group(3)):
                    issue(errors, "credential", name, text.count("\n", 0, match.start()) + 1)
    elif PurePosixPath(name).suffix.lower() not in {".json", ".jsonl", ".toml"}:
        scan_literal(text, name, errors)


def scan_values(value, name, errors, line=None):
    if isinstance(value, dict):
        for key, child in value.items():
            scan_literal(key, name, errors, line)
            if secret_key_name(key, child) and not placeholder(child):
                issue(errors, "credential", name, line)
            scan_values(child, name, errors, line)
    elif isinstance(value, list):
        for child in value:
            scan_values(child, name, errors, line)
    elif isinstance(value, str):
        scan_literal(value, name, errors, line)


class MetadataTree(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("Metadata DTD is not supported")


def scan_xml(data, name, errors):
    """Scan decoded XML values, including entity escapes, comments and attributes."""
    parser = ET.XMLParser(target=MetadataTree(insert_comments=True, insert_pis=True))
    try:
        root = ET.fromstring(data, parser=parser)
    except ET.ParseError as exc:
        raise ValueError("Invalid XML metadata") from exc
    for node in root.iter():
        scan_values(node.attrib, name, errors)
        for text in (node.text, node.tail):
            if text:
                scan_literal(text, name, errors)
        tag = node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""
        if secret_key_name(tag, node.text) and not placeholder(node.text):
            issue(errors, "credential", name)
    return root


def redacted(value):
    if isinstance(value, dict):
        return {redacted(key): "[redacted]" if secret_key_name(key, child) else redacted(child)
                for key, child in value.items()}
    if isinstance(value, list):
        return [redacted(child) for child in value]
    if isinstance(value, str):
        value = SECRET.sub("[redacted]", value)
        value = replace_assignments(value, lambda match: match.group(1) + "=[redacted]")
        return DEV_PATH.sub("[absolute-path]", value)
    return value
