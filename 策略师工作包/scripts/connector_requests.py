"""Small allowlisted request contracts, independent of review and version gates."""
import connector_safety as s

READS = {"shared-memory": {"context"}, "lark-drive": {"search"},
         "lark-minutes": {"search", "transcript"}, "lark-task": {"search"},
         "lark-doc": {"fetch"}}


def read_request(root, request):
    if not isinstance(request, dict):
        s.fail()
    allowed = {"provider", "operation", "mode", "query", "resource_id", "scope",
               "bindings", "container_id", "page_cursor"}
    if set(request) - allowed:
        s.fail()
    s.safe(request)
    provider, operation = request.get("provider"), request.get("operation")
    if provider not in READS or operation not in READS[provider]:
        s.fail()
    current = s.scope(root)
    if "scope" in request and request["scope"] != current:
        s.fail()
    mode = request.get("mode", "hard")
    if mode not in {"hard", "soft"} or (mode == "soft" and provider != "shared-memory"):
        s.fail()
    result = {**request, "mode": mode, "scope": current}
    if operation in {"search", "context"}:
        maximum = 30 if provider == "lark-drive" else 2000
        s.text(request.get("query"), maximum=maximum)
        if operation == "search" and not any(
                current[k] in request["query"] for k in ("project_name", "brand_name")):
            raise s.c.WorkflowError("Search must include the current project or brand")
    else:
        s.identifier(request.get("resource_id"))
    if "container_id" in request:
        if provider != "lark-drive":
            s.fail()
        s.identifier(request["container_id"])
    if "page_cursor" in request:
        if operation != "search":
            s.fail()
        s.text(request["page_cursor"], maximum=4000)
    bindings = request.get("bindings", [])
    if not isinstance(bindings, list):
        s.fail()
    for item in bindings:
        if not isinstance(item, dict) or set(item) != {"resource_id", "scope", "evidence"}:
            s.fail()
        s.identifier(item["resource_id"])
        if item["scope"] != current:
            s.fail()
        s.evidence(root, item["evidence"])
    if operation in {"fetch", "transcript"} and not any(
            item["resource_id"] == request["resource_id"] for item in bindings):
        raise s.c.WorkflowError("Detail read needs a verified local project ownership binding")
    return result


def prepare_write(root, request):
    if not isinstance(request, dict) or set(request) - {
            "provider", "source_key", "title", "content", "kind", "run_dream", "scope"}:
        s.fail()
    s.safe(request)
    if request.get("provider") != "shared-memory":
        raise s.c.WorkflowError("Only shared-memory writing is supported")
    current = s.scope(root)
    if "scope" in request and request["scope"] != current:
        s.fail()
    key = s.identifier(request.get("source_key"))
    title = s.text(request.get("title"), maximum=500)
    content = s.text(request.get("content"))
    if content != content.strip():
        raise s.c.WorkflowError("Content must have no leading or trailing whitespace")
    kind = request.get("kind", "conversation")
    if kind not in {"conversation", "strategy", "methodology"}:
        s.fail()
    dream = request.get("run_dream", False)
    if type(dream) is not bool:
        s.fail()
    source_id = "harness-" + s.digest({"scope": current, "source_key": key})[:40]
    payload = {"application": "harness", "source_id": source_id, "title": title,
               "content": content, "project": current["project_name"],
               "brand": current["brand_name"], "kind": kind, "run_dream": dream}
    payload_hash = s.digest(payload)
    return {"provider": "shared-memory", "operation": "write", "scope": current,
            "source_id": source_id, "request_id": "req-" + payload_hash,
            "payload_sha256": payload_hash, "content_sha256": s.digest(content),
            "title_sha256": s.digest(title), "content_length": len(content), "payload": payload}


def preview(root, request):
    return {k: v for k, v in prepare_write(root, request).items() if k != "payload"}
