"""Local inspection and explicit, auditable access to installed host skills."""
import argparse
import json
import sys
from pathlib import Path

import connector_safety as s
import connector_store as store
from connector_host import TransportError, discover, invoke, transport_for
from connector_requests import preview as prepare_write, read_request
from connector_sources import normalize
from connector_writes import reconcile, write


def inspect(root):
    """No subprocesses, network calls, mkdir, account reads or state refresh."""
    root = Path(root).resolve()
    skills, _ = discover(root)
    result = {"schema": 1, "network_checked": False, "skills": skills,
              "local_work_can_continue": True, "journal": store.EVENTS,
              "latest": {}, "unresolved": [], "status": "empty", "initialized": False}
    try:
        state = s.c.read_json(s.c.local(root, "project/state.json"))
        if not isinstance(state, dict):
            s.fail()
        if state.get("status") == "not_initialized" or not state.get("project_id"):
            path = s.c.local(root, store.EVENTS)
            if path.exists() and path.stat().st_size:
                result.update(status="blocked", reason="empty_workspace_has_connector_history")
            return result
        result["scope"] = s.scope(root)
        result["initialized"] = True
        history = store.events(root)
        writes = {}
        for item in history:
            result["latest"][item["provider"]] = store.result(item)
            if item["operation"] == "write":
                writes[item["request_id"]] = item
        result["unresolved"] = [store.result(item) for item in writes.values()
                                if item["status"] != "synced"]
        result["event_count"] = len(history)
        result["status"] = "attention" if result["unresolved"] else "local_only"
    except (OSError, ValueError, KeyError, TypeError):
        result.update(status="blocked", reason="local_state_or_journal_invalid")
    return result


def read(root, request, explicit=False, transport=None):
    if explicit is not True:
        raise s.c.WorkflowError("Explicit permission for external reading is required")
    request = read_request(root, request)
    client = transport_for(root, transport)
    with store.locked(root):
        store.events(root)
        record = {"provider": request["provider"], "operation": request["operation"],
                  "request_id": "read-" + s.digest(request), "scope": request["scope"],
                  "status": "pending", "sources": [], "mode": request["mode"]}
        store.append(root, record)
        try:
            raw = invoke(client, request["provider"], request["operation"], request)
            value = normalize(root, request, raw)
            read_request(root, request)
            record["response_sha256"] = s.digest(raw)
        except TransportError as exc:
            value = {"status": exc.status, "items": [], "sources": [],
                     "uncovered": ["external_material_not_obtained"]}
        except (ValueError, TypeError, KeyError):
            value = {"status": "unknown", "items": [], "sources": [],
                     "uncovered": ["external_response_not_usable"]}
        record.update({k: v for k, v in value.items() if k not in {"items", "page_cursor"}})
        item = store.append(root, record)
        return {**store.result(item), **value}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise s.c.WorkflowError("Invalid connector arguments; use --help")


def _input(root, relative):
    if relative == "-":
        raw = sys.stdin.read(300001)
        if len(raw) > 300000:
            s.fail()
        return json.loads(raw)
    path = s.c.local(root, relative)
    if path.stat().st_size > 300000:
        s.fail()
    return s.c.read_json(path)


def main(argv=None):
    parser = Parser(description="Explicit local connector gateway")
    parser.add_argument("command", choices=("inspect", "read", "prepare-write", "write", "reconcile"))
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--request")
    parser.add_argument("--authorization")
    parser.add_argument("--request-id")
    parser.add_argument("--explicit-read", action="store_true")
    try:
        args = parser.parse_args(argv)
        root = Path(args.workspace).resolve()
        if args.request == "-" and args.authorization == "-":
            s.fail()
        if args.command == "inspect":
            value = inspect(root)
        elif args.command == "reconcile":
            value = reconcile(root, args.request_id, explicit=args.explicit_read)
        else:
            request = _input(root, args.request)
            if args.command == "prepare-write":
                value = prepare_write(root, request)
            elif args.command == "read":
                value = read(root, request, explicit=args.explicit_read)
            else:
                value = write(root, request, _input(root, args.authorization))
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if value.get("status") not in {"blocked", "denied", "unavailable", "unknown"} else 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "blocked", "local_work_can_continue": True,
                          "reason": "invalid_scope_input_consent_or_local_state"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
