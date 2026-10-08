"""Local scope, consent and secret guards; never reads host credentials."""
import json
import re
from pathlib import Path

import phase5_common as c

FIELDS = ("project_id", "project_name", "client_name", "brand_name")
SECRET_KEY = re.compile(r"token|password|secret|api.?key|authorization|cookie|credential", re.I)
SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:[\w-]*(?:token|secret|password|api[_ -]?key|"
    r"authorization|cookie|credential)|密码|密钥)[\"']?\s*[:=]\s*\S+|"
    r"(?:sk|ghp|gho|xox[baprs])[-_][\w-]{12,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[\w-]+\.[\w-]+\.[\w-]+)")
ABSOLUTE = re.compile(r"(?:file://|[A-Za-z]:[\\/]|\\\\|~[/\\]|"
                      r"(?<![\w:/])/(?!/)[^\s<>\"']+)")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


def fail():
    raise c.WorkflowError("Unsafe or invalid connector input; no external operation performed")


def digest(value):
    return c.sha_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                 allow_nan=False, separators=(",", ":")).encode())


def text(value, *, maximum=200000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        fail()
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in value):
        fail()
    # URLs may contain public paths, but never credentials or query capabilities.
    for url in re.findall(r"https?://[^\s<>\"']+", value):
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        if parts.username or parts.password or parts.query or parts.fragment:
            fail()
    without_urls = re.sub(r"https?://[^\s<>\"']+", "", value)
    if SECRET_TEXT.search(value) or ABSOLUTE.search(without_urls):
        fail()
    return value


def safe(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or SECRET_KEY.search(key):
                fail()
            text(key)
            safe(item)
    elif isinstance(value, list):
        for item in value:
            safe(item)
    elif isinstance(value, str):
        text(value, empty=True)
    elif value is not None and type(value) not in {int, float, bool}:
        fail()
    json.dumps(value, allow_nan=False)
    return value


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        fail()
    return text(value)


def scope(root):
    state = c.read_json(c.local(root, "project/state.json"))
    if not isinstance(state, dict):
        raise c.WorkflowError("Empty workspace: connectors cannot write or read externally")
    identity_ready = all(isinstance(state.get(key), str) and state[key].strip() for key in FIELDS)
    if state.get("status") == "not_initialized" and not identity_ready:
        raise c.WorkflowError("Empty workspace: set project identity before external access")
    result = {key: text(state.get(key), maximum=200) for key in FIELDS}
    return result


def evidence(root, value):
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        fail()
    safe(value)
    path = c.local(root, value["path"])
    if not value["path"].startswith("project/") or not path.is_file():
        fail()
    raw = path.read_text(encoding="utf-8")
    text(raw)
    if c.sha(path) != value["sha256"]:
        raise c.WorkflowError("Evidence changed or missing")
    return dict(value)


def simulated(root):
    state = c.read_json(c.local(root, "project/state.json"))
    return state.get("test_mode") is True


def isolated(root):
    root = Path(root).resolve()
    state = c.read_json(c.local(root, "project/state.json"))
    return (simulated(root) and state.get("synthetic_data") is True
            and any(p.name.startswith("\u8fde\u63a5\u6d4b\u8bd5-")
                    and p.parent.name == "phase6-\u9a8c\u6536"
                    and p.parent.parent.name == "outputs" for p in (root, *root.parents)))


def simulation_guard(root, value):
    if isinstance(value, dict):
        if any(value.get(k) is True for k in ("simulation", "simulated", "synthetic", "test_mode")):
            if not isolated(root):
                fail()
        for item in value.values():
            simulation_guard(root, item)
    elif isinstance(value, list):
        for item in value:
            simulation_guard(root, item)


def consent(root, prepared, authorization):
    if not isinstance(authorization, dict):
        fail()
    allowed = {"approved", "actor", "request_id", "payload_sha256", "scope", "evidence", "simulation"}
    if set(authorization) - allowed or authorization.get("approved") is not True:
        raise c.WorkflowError("Explicit user write consent is required")
    safe(authorization)
    if authorization.get("simulation", False) is not simulated(root):
        fail()
    simulation_guard(root, authorization)
    for key in ("request_id", "payload_sha256", "scope"):
        if authorization.get(key) != prepared[key]:
            raise c.WorkflowError("Consent does not cover this exact request and scope")
    return {"actor": text(authorization.get("actor"), maximum=200),
            "evidence": evidence(root, authorization.get("evidence")),
            "simulation": simulated(root)}
