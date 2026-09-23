"""Durable at-most-once sends; only an exact source readback can prove success."""
import connector_safety as s
import connector_store as store
from connector_host import TransportError, invoke, transport_for
from connector_requests import prepare_write


def _lookup(history, request_id):
    return [item for item in history if item.get("operation") == "write"
            and item.get("request_id") == request_id]


def _verify(root, transport, pending):
    try:
        raw = invoke(transport, "shared-memory", "source", {
            "source_id": pending["source_id"], "content_length": pending["content_length"]})
        s.simulation_guard(root, raw)
        if not isinstance(raw, dict) or raw.get("ok") is False or raw.get("error"):
            raise TransportError("unknown")
        item = raw.get("source", raw.get("data", raw))
        if isinstance(item, dict) and isinstance(item.get("source"), dict):
            item = item["source"]
        if not isinstance(item, dict):
            raise TransportError("unknown")
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise TransportError("unknown")
        valid = (item.get("source_id") == pending["source_id"]
                 and isinstance(item.get("content"), str)
                 and s.digest(item["content"]) == pending["content_sha256"]
                 and s.digest(item.get("title")) == pending["title_sha256"]
                 and all(item.get(key, metadata.get(key)) == pending["scope"][local]
                         for key, local in (("project", "project_name"), ("brand", "brand_name"))))
        if item.get("truncated") or raw.get("truncated") or not valid:
            return "unknown", {"readback": "mismatch_or_incomplete"}
        return "synced", {"readback": "matched", "source_id": pending["source_id"],
                          "content_sha256": pending["content_sha256"]}
    except TransportError as exc:
        return "unknown", {"readback": exc.status}
    except (ValueError, TypeError, KeyError):
        return "unknown", {"readback": "invalid"}


def _finish(root, transport, pending):
    status, remote = _verify(root, transport, pending)
    record = {key: value for key, value in pending.items()
              if key not in {"schema", "event_id", "event_sha256", "previous", "time"}}
    record.update(status=status, remote_result=remote, attempt="readback")
    return store.result(store.append(root, record))


def write(root, request, authorization, transport=None):
    prepared = prepare_write(root, request)
    approved = s.consent(root, prepared, authorization)
    client = transport_for(root, transport)
    with store.locked(root):
        approved = s.consent(root, prepared, authorization)
        history = store.events(root)
        existing = _lookup(history, prepared["request_id"])
        if existing:
            if existing[-1]["status"] in {"synced", "denied", "unavailable"}:
                return store.result(existing[-1], duplicate=True)
            value = _finish(root, client, existing[0])
            value["duplicate"] = True
            return value
        if any(item.get("source_id") == prepared["source_id"]
               and item.get("operation") == "write" for item in history):
            raise s.c.WorkflowError("Source already used with another payload; use an explicit new version")
        pending = {key: value for key, value in prepared.items() if key != "payload"}
        pending.update(status="pending", consent=approved, sources=[], attempt="send",
                       remote_result={"readback": "not_checked"})
        pending = store.append(root, pending)
        try:
            reply = invoke(client, "shared-memory", "write", prepared["payload"])
            s.simulation_guard(root, reply)
            # Acknowledgements alone never prove synchronization. Only keep their fingerprint.
            receipt = s.digest(reply)
        except TransportError as exc:
            if exc.status in {"denied", "unavailable"}:
                failed = {k: v for k, v in pending.items() if k not in {
                    "schema", "event_id", "event_sha256", "previous", "time"}}
                failed.update(status=exc.status, remote_result={"send": exc.status})
                return store.result(store.append(root, failed))
            receipt = None
        except (ValueError, TypeError):
            receipt = None
        pending["receipt_sha256"] = receipt
        return _finish(root, client, pending)


def reconcile(root, request_id, explicit=False, transport=None):
    if explicit is not True:
        raise s.c.WorkflowError("Explicit permission for remote readback is required")
    s.scope(root)
    s.identifier(request_id)
    client = transport_for(root, transport)
    with store.locked(root):
        history = _lookup(store.events(root), request_id)
        if not history:
            raise s.c.WorkflowError("Unknown local request; nothing will be sent")
        if history[-1]["status"] == "synced":
            return store.result(history[-1], duplicate=True)
        return _finish(root, client, history[0])
