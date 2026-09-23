"""Freeze optional local CSS; never treat safety checks as a visual review.

The initial subset accepts ordinary declarations and @media/@supports groups.
It rejects ALL url() resources (including data images), imports, CSS escapes,
!important, animations/transitions and generated content. This preserves the
template's important [hidden] rules without changing its print behaviour.
Unknown functions and at-rules fail closed; this is not a full CSS validator.
"""
from pathlib import Path
import re

import phase5_common as common
from phase2_store import WorkflowError

FUNCTIONS = frozenset("""
calc clamp min max var env rgb rgba hsl hsla hwb lab lch oklab oklch color
color-mix light-dark linear-gradient radial-gradient conic-gradient
repeating-linear-gradient repeating-radial-gradient repeating-conic-gradient
repeat minmax fit-content translate translatex translatey translatez translate3d
scale scalex scaley scalez scale3d rotate rotatex rotatey rotatez rotate3d
skew skewx skewy matrix matrix3d perspective blur brightness contrast drop-shadow
grayscale hue-rotate invert opacity saturate sepia is where not has nth-child
nth-last-child nth-of-type nth-last-of-type lang dir selector
""".split())
MASK = re.compile(r'/\*.*?\*/|"[^"\r\n]*"|\'[^\'\r\n]*\'', re.S)


def _balanced(css):
    stack = []
    pairs = {"}": "{", ")": "(", "]": "["}
    for char in css:
        if char in "{([":
            stack.append(char)
            if len(stack) > 64:
                raise WorkflowError("visual_style CSS 嵌套过深")
        elif char in pairs and (not stack or stack.pop() != pairs[char]):
            raise WorkflowError("visual_style CSS 括号不完整或不匹配")
    if stack:
        raise WorkflowError("visual_style CSS 括号不完整或不匹配")


def _declarations(body):
    if any(char in body for char in "{}@"):
        raise WorkflowError("visual_style 暂不支持嵌套选择器或声明内的 @ 规则")
    for declaration in body.split(";"):
        if not declaration.strip():
            continue
        name, separator, value = declaration.partition(":")
        name = name.strip().lower()
        if not separator or not value.strip() or not re.fullmatch(r"(?:--|-?)[a-z][a-z0-9_-]*", name):
            raise WorkflowError("visual_style 必须是有效的 CSS 属性声明")
        if (name in {"content", "behavior", "-moz-binding"} or
                re.fullmatch(r"(?:-[a-z]+-)?(?:transition|animation)(?:-[a-z-]+)?", name)):
            raise WorkflowError("visual_style 禁止生成文字、脚本属性及动画/过渡，以保留正文和隐藏规则")


def _rules(css, depth=0):
    if depth > 16:
        raise WorkflowError("visual_style CSS 规则嵌套过深")
    start = 0
    while css[start:].strip():
        opening = css.find("{", start)
        if opening < 0:
            raise WorkflowError("visual_style 必须是完整的 CSS 规则，禁止导入")
        header = css[start:opening].strip()
        if not header or ";" in header or "}" in header:
            raise WorkflowError("visual_style CSS 选择器或条件无效")
        level, end = 1, opening + 1
        while level and end < len(css):
            level += (css[end] == "{") - (css[end] == "}")
            end += 1
        body = css[opening + 1:end - 1]
        if "@" in header:
            if not re.fullmatch(r"@(?:media|supports)(?:\s+|(?=\())[^@]+", header, re.I | re.S):
                raise WorkflowError("visual_style 只支持 @media/@supports，禁止导入等其他 @ 规则")
            _rules(body, depth + 1)
        else:
            _declarations(body)
        start = end


def validate(text):
    if not text.strip() or len(text.encode("utf-8")) > 256 * 1024:
        raise WorkflowError("visual_style CSS 不能为空或超过 256 KiB")
    if "<" in text or "\\" in text or any(ord(c) < 32 and c not in "\t\r\n" for c in text):
        raise WorkflowError("visual_style 禁止 HTML/style 标签注入、CSS 转义和控制字符")
    # Mask strings and comments before checking executable CSS tokens.
    css = MASK.sub(lambda m: " " if m[0].startswith("/*") else "STRING", text)
    if any(token in css for token in ('"', "'", "/*", "*/")):
        raise WorkflowError("visual_style CSS 字符串或注释不完整")
    if not css.strip():
        raise WorkflowError("visual_style CSS 必须包含实际规则")
    if "!" in css:
        raise WorkflowError("visual_style 禁止 !important，不能覆盖必需的 [hidden] 规则")
    if re.search(r"\burl\s*\(", css, re.I):
        raise WorkflowError("visual_style 暂禁全部 url() 资源，包括内嵌 data 图片")
    for name in re.findall(r"(?<![@\w-])([\w-]+)\(", css):
        if name.lower() not in FUNCTIONS:
            raise WorkflowError(f"visual_style 不支持 CSS 函数 {name}，禁止资源或脚本函数")
    _balanced(css)
    _rules(css)


def load(root, task_id, reference):
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
            or not isinstance(reference.get("path"), str)
            or not isinstance(reference.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])):
        raise WorkflowError("visual_style 必须提供 CSS 相对路径 path 和 64 位小写 sha256")
    relative = reference["path"]
    if ".." in Path(relative).parts or Path(relative).suffix.lower() != ".css":
        raise WorkflowError("visual_style 只接受当前任务目录内的 .css，禁止路径穿越")
    try:
        path = common.local(root, relative)
        scope = Path(root).resolve() / "project/outputs/proposal" / task_id
        if not path.is_relative_to(scope) or path.suffix.lower() != ".css" or not path.is_file():
            raise WorkflowError("visual_style 文件缺失或不在当前任务目录内")
        raw = path.read_bytes()
    except (OSError, ValueError, RuntimeError) as exc:
        raise WorkflowError("visual_style CSS 路径无效或文件无法读取") from exc
    if common.sha_bytes(raw) != reference["sha256"]:
        raise WorkflowError("visual_style 指纹不符或候选 CSS 已更新，拒绝自动登记")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise WorkflowError("visual_style CSS 必须使用 UTF-8 文本") from exc
    validate(text)
    return dict(reference), raw


def embed(html, css):
    if html.count("</head>") != 1:
        raise WorkflowError("PPT 模板缺少唯一 head，不能嵌入 visual_style")
    return html.replace("</head>", f'<style data-phase5-visual-style>\n{css}\n</style>\n</head>', 1)


def check_registered(meta, payload):
    """Check frozen bindings only; the original candidate may now be edited/deleted."""
    reference = payload.get("visual_style")
    for group in ("current_files", "snapshot_files"):
        styles = [f for f in meta[group] if f.get("role") == "style"]
        if reference is None and not styles:
            continue
        if (not isinstance(reference, dict) or len(styles) != 1
                or styles[0]["sha256"] != reference.get("sha256")):
            raise WorkflowError("PPT visual_style 与登记的 CSS 指纹不一致")
