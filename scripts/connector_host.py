"""Installed host clients only. No account setup, shell, retries or credential copies."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import connector_safety as s

SKILLS = ("shared-memory", "lark-shared", "lark-drive", "lark-minutes", "lark-task", "lark-doc")


class TransportError(Exception):
    def __init__(self, status):
        self.status = status if status in {"denied", "unavailable", "unknown"} else "unknown"
        super().__init__(self.status)


def discover(root):
    """Paths are private in-process handles, never public status or journal fields."""
    home = Path.home()
    bases = [Path(os.environ.get("CODEX_HOME", home / ".codex")) / "skills",
             home / ".agents/skills", home / ".codex/skills",
             Path(os.environ.get("OPENCLAW_HOME", home / ".openclaw")) / "skills"]
    public, private = {}, {}
    for name in SKILLS:
        item = {"installed": False, "status": "missing", "account": "not_checked"}
        for base in bases:
            path = (base / name / "SKILL.md").resolve()
            if path.is_relative_to(Path(root).resolve()):
                continue
            try:
                raw = path.read_text(encoding="utf-8")
                if not re.search(rf"(?m)^name:\s*{re.escape(name)}\s*$", raw):
                    continue
                version = re.search(r"(?m)^version:\s*([0-9.]+)\s*$", raw)
                item.update(installed=True, status="installed_auth_unchecked",
                            version=version[1] if version else "unknown",
                            skill_sha256=s.c.sha_bytes(raw.encode()))
                private[name] = path.parent
                break
            except (OSError, UnicodeError):
                continue
        public[name] = item
    executable = shutil.which("lark-cli")
    if executable and not Path(executable).resolve().is_relative_to(Path(root).resolve()):
        private["lark-cli"] = Path(executable).resolve()
    for name in SKILLS[1:]:
        public[name]["executable_available"] = "lark-cli" in private
    script = private.get("shared-memory")
    script = script / "scripts/brains_memory.py" if script else None
    available = bool(script and script.is_file()
                     and not script.resolve().is_relative_to(Path(root).resolve()))
    public["shared-memory"]["executable_available"] = available
    if available:
        private["brains-script"] = script.resolve()
    return public, private


def _error(raw):
    try:
        value = json.loads(raw)
        error = value.get("error", {})
        code, kind = error.get("code"), error.get("type", "")
    except (ValueError, AttributeError):
        code, kind = None, ""
    if code in {401, 403, 99991661, 99991663, 99991679, 2091005} or kind in {
            "permission_denied", "unauthorized", "authentication", "no_edit_permission",
            "confirmation_required", "missing_scope"}:
        return "denied"
    if re.search(r"(?i)HTTP (401|403)\b|no .*key.*configured|not logged in|permission deny", raw):
        return "denied"
    return "unknown"


def run(argv, cwd, stdin=None):
    env = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
           "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run(argv, input=stdin, text=True, capture_output=True,
                                cwd=cwd, env=env, timeout=75, check=False, shell=False)
    except FileNotFoundError:
        raise TransportError("unavailable") from None
    except PermissionError:
        raise TransportError("denied") from None
    except (OSError, subprocess.TimeoutExpired):
        raise TransportError("unknown") from None
    if result.returncode:
        raise TransportError(_error(result.stderr or result.stdout))
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise TransportError("unknown") from None
    if not isinstance(value, dict):
        raise TransportError("unknown")
    return value


class HostTransport:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def request(self, provider, operation, payload):
        _, paths = discover(self.root)
        if provider not in paths:
            raise TransportError("unavailable")
        with tempfile.TemporaryDirectory(prefix="harness-connectors-") as temporary:
            if provider == "shared-memory":
                if "brains-script" not in paths:
                    raise TransportError("unavailable")
                return self._brain(paths, operation, payload, temporary)
            if "lark-shared" not in paths or "lark-cli" not in paths:
                raise TransportError("unavailable")
            return self._lark(paths, provider, operation, payload, temporary)

    def _brain(self, paths, operation, payload, temporary):
        argv = [sys.executable, "-B", str(paths["brains-script"])]
        if operation == "context":
            argv += ["context", payload["query"], "--project", payload["scope"]["project_name"],
                     "--brand", payload["scope"]["brand_name"], "--json"]
            if payload["mode"] == "hard":
                argv += ["--no-cross-project", "--no-methodology", "--cross-brand-limit", "0"]
            return run(argv, temporary)
        if operation == "source":
            return run(argv + ["source", payload["source_id"], "--max-chars",
                              str(payload["content_length"] + 4096)], temporary)
        if operation != "write":
            raise TransportError("unavailable")
        argv += ["write", "--application", "harness"]
        for key in ("source_id", "title", "project", "brand", "kind"):
            argv += ["--" + key.replace("_", "-"), payload[key]]
        if payload["run_dream"]:
            argv += ["--run-dream"]
        return run(argv, temporary, payload["content"])

    def _lark(self, paths, provider, operation, payload, temporary):
        if operation not in {"search", "fetch", "transcript"}:
            raise TransportError("unavailable")
        service = {"lark-doc": "docs", "lark-drive": "drive",
                   "lark-task": "task", "lark-minutes": "minutes"}[provider]
        verb = {"search": "+search", "fetch": "+fetch", "transcript": "+detail"}[operation]
        argv = [str(paths["lark-cli"]), service, verb, "--as", "user", "--format", "json"]
        if operation == "search":
            argv += ["--query", payload["query"]]
            if payload.get("container_id"):
                argv += ["--folder-tokens", payload["container_id"]]
            if payload.get("page_cursor"):
                argv += ["--page-token", payload["page_cursor"]]
        elif operation == "fetch":
            argv += ["--doc", payload["resource_id"], "--doc-format", "markdown", "--detail", "simple"]
        else:
            argv += ["--minute-tokens", payload["resource_id"], "--transcript"]
        value = run(argv, temporary)
        if value.get("ok") is not True:
            raise TransportError(_error(json.dumps(value)))
        if operation == "transcript":
            self._transcript(value, temporary)
        return value

    @staticmethod
    def _transcript(value, temporary):
        try:
            minutes = value["data"]["minutes"]
            for item in minutes:
                raw = item["artifacts"]["transcript_file"]
                path = (Path(temporary) / raw).resolve()
                if not path.is_relative_to(Path(temporary).resolve()) or not path.is_file():
                    raise TransportError("unknown")
                item["transcript"] = path.read_text(encoding="utf-8")
                del item["artifacts"]
        except (KeyError, TypeError, OSError, UnicodeError):
            raise TransportError("unknown") from None


def transport_for(root, transport):
    if transport is None:
        if s.simulated(root):
            raise s.c.WorkflowError("Synthetic workspaces cannot use the live host")
        return HostTransport(root)
    if not s.isolated(root) or getattr(transport, "synthetic", False) is not True:
        raise s.c.WorkflowError("Injected transports require an isolated synthetic test workspace")
    return transport


def invoke(transport, provider, operation, payload):
    try:
        return transport.request(provider, operation, payload)
    except TransportError:
        raise
    except PermissionError:
        raise TransportError("denied") from None
    except Exception:
        # Never echo transport exceptions: they can include keys, paths or customer text.
        raise TransportError("unknown") from None
