"""Project evidence and transferable methods are separate, explicit projections."""
import re
from datetime import datetime, timezone

import connector_safety as s
from connector_host import TransportError

GROUPS = ("direct_memories", "project_context", "brand_context", "methodologies",
          "cross_brand_insights", "cross_project_insights")
METHOD = ("methodology", "link_reason", "applicable_when", "not_applicable_when")


def original_time(item):
    value = item.get("updated_at") or item.get("created_at") or item.get("create_time")
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value,
                                          timezone.utc).isoformat()
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo:
                return parsed.isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return "unknown"


def ownership(item, scope):
    aliases = {"project_name": ("project", "project_name"),
               "brand_name": ("brand", "brand_name"), "client_name": ("client", "client_name")}
    for key, names in aliases.items():
        values = [item[name] for name in names if item.get(name)]
        if not values or any(value != scope[key] for value in values):
            return False
    return not item.get("project_id") or item["project_id"] == scope["project_id"]


def source(provider, remote_id, item, selected, role, scope):
    s.text(remote_id, maximum=1000)
    s.safe(selected)
    return {"source_id": "src-" + s.digest([provider, remote_id]), "provider": provider,
            "scope": {"kind": role, "project_id": scope["project_id"] if role != "method_reference" else None},
            "time": original_time(item), "retrieved_at": s.c.now(),
            "status": "retrieved_not_business_confirmed", "content_sha256": s.digest(selected)}


def card(entry):
    if not isinstance(entry, dict):
        raise TransportError("unknown")
    nested = entry.get("memory", entry.get("card", {}))
    if not isinstance(nested, dict):
        raise TransportError("unknown")
    metadata = nested.get("metadata", entry.get("metadata", {}))
    if not isinstance(metadata, dict):
        raise TransportError("unknown")
    # Conflicting ownership declarations must not be silently overwritten.
    owners = {"project", "project_name", "project_id", "brand", "brand_name", "client", "client_name"}
    for key in owners:
        values = [obj[key] for obj in (metadata, nested, entry) if obj.get(key)]
        if values and any(value != values[0] for value in values):
            return {}
    return {**metadata, **nested, **entry}


def brain(request, raw):
    if (not isinstance(raw, dict) or raw.get("ok") is False or raw.get("error")
            or not any(k in raw for k in GROUPS)):
        raise TransportError("unknown")
    result, excluded, seen = [], 0, set()
    for group in GROUPS:
        values = raw.get(group, [])
        if not isinstance(values, list):
            raise TransportError("unknown")
        for entry in values:
            item = card(entry)
            remote_id = item.get("source_id") or item.get("slug") or item.get("id")
            if not isinstance(remote_id, str) or not remote_id:
                excluded += 1
                continue
            marker = s.digest(remote_id)
            if marker in seen:
                continue
            if request["mode"] == "hard":
                if not ownership(item, request["scope"]):
                    excluded += 1
                    continue
                selected = {"content": item.get("content"), "title": item.get("title", "")}
                role = "project_source"
            else:
                if any(not item.get(key) for key in METHOD):
                    excluded += 1
                    continue
                selected = {key: item[key] for key in METHOD}
                if any(not isinstance(v, (str, list)) for v in selected.values()):
                    excluded += 1
                    continue
                # Do not retain a source client's names inside a purported generic method.
                foreign = [item.get(k) for k in ("client", "client_name", "brand", "brand_name",
                                                 "project", "project_name")]
                body = str(selected)
                if any(isinstance(v, str) and v not in request["scope"].values()
                       and len(v) > 1 and v in body for v in foreign):
                    excluded += 1
                    continue
                role = "method_reference"
            try:
                if role == "project_source":
                    s.text(selected["content"])
                for value in selected.values():
                    if isinstance(value, list):
                        if not value:
                            s.fail()
                        for part in value:
                            s.text(part)
                    elif role == "method_reference":
                        s.text(value)
                meta = source("shared-memory", remote_id, item, selected, role, request["scope"])
            except s.c.WorkflowError:
                excluded += 1
                continue
            result.append({"source": meta, "locator": remote_id, **selected})
            seen.add(marker)
    return result, excluded, False, "", []


def lark(request, raw):
    if not isinstance(raw, dict) or raw.get("ok") is not True or not isinstance(raw.get("data"), dict):
        raise TransportError("unknown")
    data, operation = raw["data"], request["operation"]
    if operation == "fetch":
        items = [data.get("document")]
    elif operation == "transcript":
        items = data.get("minutes")
    else:
        items = next((data[k] for k in ("results", "items", "tasks") if k in data), None)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise TransportError("unknown")
    result, excluded, seen, gaps = [], 0, set(), []
    bindings = {v["resource_id"]: v for v in request.get("bindings", [])}
    for item in items:
        meta = item.get("result_meta", {})
        if not isinstance(meta, dict):
            raise TransportError("unknown")
        remote_id = next((item[k] for k in ("document_id", "minute_token", "guid", "doc_token",
                                           "token", "id") if item.get(k)), meta.get("token"))
        if remote_id not in bindings or (operation != "search" and remote_id != request["resource_id"]):
            excluded += 1
            continue
        if remote_id in seen:
            continue
        selected = {"title": item.get("title", item.get("summary", ""))}
        if operation != "search":
            selected["content"] = item.get("content") if operation == "fetch" else item.get("transcript")
            if not isinstance(selected["content"], str) or not selected["content"].strip():
                raise TransportError("unknown")
            if operation == "transcript" and not re.search(
                    r"\b\d{1,2}:\d{2}(?::\d{2})?", selected["content"]):
                gaps.append("transcript_time_or_speaker_requires_verification")
            if operation == "fetch" and any(word in selected["content"] for word in (
                    "<sheet", "<bitable", "<whiteboard", "<vc-transcribe-tab", "<synced_reference")):
                gaps.append("embedded_resources_not_read")
        try:
            ref = source(request["provider"], remote_id, item, selected,
                         "project_candidate" if operation == "search" else "project_source",
                         request["scope"])
            ref["ownership_evidence"] = bindings[remote_id]["evidence"]
        except s.c.WorkflowError:
            excluded += 1
            continue
        result.append({"source": ref, "locator": remote_id, **selected})
        seen.add(remote_id)
    more = data.get("has_more", False)
    cursor = data.get("page_token", "")
    if type(more) is not bool or not isinstance(cursor, str):
        raise TransportError("unknown")
    s.text(cursor, maximum=4000, empty=True)
    if more and not cursor:
        gaps.append("next_page_cursor_missing")
    return result, excluded, more, cursor, gaps


def normalize(root, request, raw):
    s.simulation_guard(root, raw)
    items, excluded, more, cursor, gaps = (brain(request, raw) if
        request["provider"] == "shared-memory" else lark(request, raw))
    return {"items": items, "sources": [item["source"] for item in items],
            "excluded_count": excluded, "has_more": more, "page_cursor": cursor,
            "uncovered": sorted(set(gaps)),
            "status": "partial" if excluded or more or gaps else "read" if items else "empty"}
