"""凭据与个人信息的共用判定（v1.7.5 G14、G04）：记一笔拒收像凭据的证据文件；运行日志存报错首行前先去掉本机路径、凭据、邮箱、电话。

只复用启动检查与发行检查已有的判定（validate_project.SENSITIVE_NAME / walk_sensitive_keys、package_checks_content.secret_key_name /
placeholder / text_has_secret / DEV_PATH_SPAN；r3 起三处的键名、占位与文字判定都只在 package_checks_content 一处），
另补中文文件名（密钥、密码、凭据、令牌）。判定是保守的常见样式拦截，不替代人工保密审查。"""
import re
from pathlib import Path

CHINESE_NAME = re.compile(r"密钥|密码|口令|凭据|凭证|令牌")
# 只认英文字母数字，前面紧挨的汉字不吞；r3：只从一串的开头尝试（长串无“@”时线性，不回溯）
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# 手机（含 +86，允许空格或连字符分组 138 1234 5678）、带区号座机（010-12345678、(010)12345678、（010）12345678）、国际号码；
# 前后不能紧挨英文字母或数字（避免误伤编号与指纹），紧挨汉字可以
_NB, _NA = r"(?<![A-Za-z0-9_+-])", r"(?![A-Za-z0-9_])"
PHONE = re.compile(_NB + r"(?:\+?86[- ]?)?1[3-9]\d(?:[- ]?\d{4}){2}" + _NA
                   + "|" + r"(?:\(0\d{2,3}\)|（0\d{2,3}）|" + _NB + r"0\d{2,3}[- ])[- ]?\d{3,4}[- ]?\d{4}" + _NA
                   + "|" + _NB + r"\+\d{1,3}[- ]?\d{2,4}(?:[- ]?\d{2,8}){1,3}" + _NA)
TEXT_LIMIT = 2 * 1024 * 1024
# 去敏的停止处（r4 #5）：中文标点与汉字（白话说明保留），换行
_CN_STOP = "，。；：、！？“”‘’「」『』（）【】《》"
_CJK = "㐀-䶿一-鿿豈-﫿"
_SPAN = "[^\\s" + _CN_STOP + _CJK + "]+"
# 认证头（r4 #5）：Authorization 后面“方案词 + 空白 + 值”整段去，不限定方案名（Basic、Bearer、Token、Digest 的 username/response 参数……）；
# 段内的英文引号、逗号都算值，遇中文标点、汉字或换行停
AUTH_HEADER = re.compile(r"\bauthorization[\"']?[ \t]*[:=][ \t]*" + _SPAN + "(?:[ \t]+" + _SPAN + ")*", re.I)
# 凭据键名赋值的值中有空格（值写成“前半 后半”两段）：不带引号的值后面接着去到行尾或下一个分隔符（英文逗号、分号、#，中文标点、汉字）
VALUE_TAIL = re.compile("(?:[ \t]+[^\\s,;#" + _CN_STOP + _CJK + "]+)*")


def credential_name(name):
    """文件名像凭据：与启动检查同一规则（token、secret、password、credential、.pem/.key 等），另加中文。"""
    from validate_project import SENSITIVE_NAME
    return bool(SENSITIVE_NAME.match(name) or CHINESE_NAME.search(name))


def is_credential_problem(message):
    """启动检查 / 移交检查报出的一条问题是不是“疑似凭据”类（validate_project 的两种写法都含这四个字）。交接打包遇到这类问题直接拒绝，接收核对也不放过。"""
    return "疑似凭据" in str(message)


def credential_text(text):
    """内容像凭据：与发行检查、启动检查同一判定 package_checks_content.text_has_secret（含 app_secret 等带前缀的键名，占位符放行）。"""
    from package_checks_content import text_has_secret
    return text_has_secret(text)


def credential_json(text):
    """JSON 内容：解析后按启动检查的键名规则（validate_project.walk_sensitive_keys）找有值的凭据字段；不是 JSON 返回 False。"""
    import json
    try:
        data = json.loads(text)
    except ValueError:
        return False
    from validate_project import walk_sensitive_keys
    return bool(walk_sensitive_keys(data))


def credential_file(path):
    """返回像凭据的原因（白话），不像则返回 None。只读小文件的文字；读不了或是二进制不按内容判。"""
    path = Path(path)
    if credential_name(path.name):
        return "文件名看起来是登录凭据"
    try:
        if path.stat().st_size <= TEXT_LIMIT:
            text = path.read_bytes().decode("utf-8")
            if credential_text(text) or credential_json(text):
                return "文件内容里有像登录凭据的字符串"
    except (OSError, UnicodeDecodeError):
        pass
    return None


def scrub(text, root=None):
    """去掉本机绝对路径（工作区内的换成相对路径）、凭据样式字符串（认证头整段、值中有空格的凭据赋值去到分隔符）、邮箱与电话。"""
    import os
    from package_checks_content import DEV_PATH_SPAN, PATH_BOUNDARY, SECRET, replace_assignments
    text = str(text)
    if root is not None:
        # 工作区前缀只用绝对路径（resolve() 后的，及未解析软链接的绝对写法）；“.”之类相对写法不当前缀。
        # r3：前缀后面紧跟“/”换成相对路径、到边界换成“.”；“客户A-备份”这类同名开头的别处路径不当工作区（随后按本机路径去掉）
        for prefix in sorted({str(Path(root).resolve()), os.path.abspath(root)}, key=len, reverse=True):
            if prefix and prefix != "/":
                text = re.sub(re.escape(prefix) + "(?:/|" + PATH_BOUNDARY + ")",
                              lambda m: "" if m.group(0).endswith("/") else ".", text)
    text = DEV_PATH_SPAN.sub("<本机路径>", text)
    text = AUTH_HEADER.sub("<已去掉凭据>", text)  # 整段换掉（连同头名），换后的文字不再被下面的赋值规则吞掉后面的白话
    text = SECRET.sub("<已去掉凭据>", text)
    text = replace_assignments(text, lambda m: m.group(1) + "=<已去掉凭据>", tail=VALUE_TAIL)
    text = EMAIL.sub("<邮箱>", text)
    return PHONE.sub("<电话>", text)


def first_line(text, root=None, limit=200):
    """报错首行：取第一行非空文字，去敏后截到 limit 字。"""
    line = next((x.strip() for x in str(text).splitlines() if x.strip()), "")
    line = scrub(line, root)
    return line if len(line) <= limit else line[:limit - 1] + "…"
