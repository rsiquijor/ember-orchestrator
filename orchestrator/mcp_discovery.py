"""MCP Bus tool discovery for the Ember Orchestrator (Nov 26, 2026).

Direction 1 of the Ember<->MCP wiring: instead of relying only on the
hand-maintained static catalog (tools/catalog.json), the live
HttpCubeTools asks the Metatron MCP Bus for `tools/list` at startup
(and refreshes on a TTL), generates Ember's function-calling schemas
from the returned JSON Schemas, and routes executions through
`tools/call`. New Cube capabilities on the bus become available to
Ember automatically.

Eligibility guardrails (hackathon: Ember is the ONLY AI in the path):
  - EXCLUDED_BUS_TOOLS are never exposed to Ember, even though the
    bus lists them: create_ticket (Axion auto-triage),
    knowledge_search (embedding model), ask_cube (calls Axion).
  - SELF_TOOL_PREFIX tools (ember_*, Ember's own bus surface from
    Direction 2) are never exposed to Ember — Ember does not call
    itself.
  - Allowlist (Nov 28): a discovered tool runs ungated, so it is
    admitted only if its name is a known bus read (READ_TOOLS) or
    the bus marks it read-only (MCP annotations.readOnlyHint true).
    Anything else is quarantined and listed in /api/status, so a new
    write tool on the bus can never bypass the approval gate.

Fallback: any discovery failure (bus unreachable, bad payload, auth)
returns None and the caller uses the static catalog, so the demo
never dies because the bus is down. The failure is logged to stderr.
"""
from __future__ import annotations

import json
import sys
import urllib.request

# Bus tools that must NEVER reach Ember (non-Ember models on the Cube
# side). Enforced here AND by the static catalog's absence of them.
EXCLUDED_BUS_TOOLS = {"create_ticket", "knowledge_search", "ask_cube"}

# Ember's own tools on the bus (Direction 2) — Ember never calls itself.
SELF_TOOL_PREFIX = "ember_"

# Cap discovered descriptions so a verbose bus tool cannot blow up
# Ember's prompt. The bus's own descriptions are far shorter.
DESC_CAP = 240

# Names quarantined by the last discovery (not read-only, not known).
LAST_QUARANTINED: list[str] = []


def _known_read_names() -> set[str]:
    from .cube_tools import READ_TOOLS
    return set(READ_TOOLS)


def _is_admissible(tool: dict, known: set[str]) -> bool:
    """A discovered tool executes WITHOUT an approval token, so only
    known reads, or tools the bus annotates as read-only, qualify.
    Annotations are hints, but the default (no annotation) is denied."""
    if tool.get("name") in known:
        return True
    ann = tool.get("annotations")
    return isinstance(ann, dict) and ann.get("readOnlyHint") is True \
        and ann.get("destructiveHint") is not True


def _log(msg: str) -> None:
    print(f"mcp-discovery: {msg}", file=sys.stderr, flush=True)


def _fetch_tools_list(mcp_url: str, api_key: str,
                      timeout: float = 10) -> list[dict]:
    """Raw MCP tools/list call. Raises on any transport/RPC failure."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {}}
    req = urllib.request.Request(
        mcp_url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "X-API-Key": api_key,
                 "User-Agent": "ember-orchestrator/mcp-discovery"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    if payload.get("error"):
        raise RuntimeError(
            "tools/list RPC error: "
            + str((payload["error"] or {}).get("message"))[:200])
    tools = (payload.get("result") or {}).get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("tools/list returned no tools array")
    return tools


def _to_ember_schema(tool: dict) -> dict | None:
    """One bus tool -> Ember function-calling schema, or None if the
    entry is malformed or excluded."""
    if not isinstance(tool, dict):
        return None
    name = str(tool.get("name") or "").strip()
    if not name or name in EXCLUDED_BUS_TOOLS \
            or name.startswith(SELF_TOOL_PREFIX):
        return None
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    # Copy so callers can merge without mutating the bus payload.
    schema = json.loads(json.dumps(schema))
    schema.setdefault("properties", {})
    return {"name": name,
            "description": str(tool.get("description") or "")[:DESC_CAP],
            "parameters": schema}


def discover_bus_tools(mcp_url: str, api_key: str,
                       timeout: float = 10,
                       fetch_fn=None) -> list[dict] | None:
    """Discover Ember-eligible bus tools as Ember schemas.

    Returns the filtered schema list, or None on any failure (the
    caller falls back to the static catalog). `fetch_fn` is
    injectable for tests: fetch_fn(mcp_url, api_key, timeout) ->
    raw tools list.
    """
    fetch = fetch_fn or _fetch_tools_list
    try:
        raw = fetch(mcp_url, api_key, timeout)
    except Exception as e:  # noqa: BLE001 — fallback is the point
        _log(f"discovery failed ({type(e).__name__}: {e}) — "
             "falling back to the static catalog")
        return None
    schemas = []
    seen = set()
    quarantined = []
    known = _known_read_names()
    for tool in raw:
        schema = _to_ember_schema(tool)
        if schema is None or schema["name"] in seen:
            continue
        if not _is_admissible(tool, known):
            if schema["name"] not in quarantined:
                quarantined.append(schema["name"])
            continue
        seen.add(schema["name"])
        schemas.append(schema)
    LAST_QUARANTINED[:] = sorted(quarantined)
    if quarantined:
        _log(f"quarantined {len(quarantined)} bus tools that are not "
             f"known reads or annotated read-only: {sorted(quarantined)}")
    if not schemas:
        _log("discovery returned no eligible tools — falling back "
             "to the static catalog")
        return None
    _log(f"discovered {len(schemas)} eligible bus tools "
         f"(excluded: {sorted(EXCLUDED_BUS_TOOLS)} + {SELF_TOOL_PREFIX}*)")
    return schemas


def merge_schemas(local: list[dict], discovered: list[dict],
                  local_only_names: set[str]) -> list[dict]:
    """Effective Ember schema list: Ember-local tools (gated writes +
    local reads, from the static catalog) + discovered bus tools.

    For a tool present in both, the discovered schema wins, except
    `required` is unioned with the static catalog's hand-tuned
    required fields (the bus marks required heuristically). Order:
    local tools in catalog order first, then discovered-only tools
    in bus order.
    """
    discovered_by_name = {s["name"]: s for s in discovered}
    out: list[dict] = []
    emitted: set[str] = set()
    for schema in local:
        name = schema["name"]
        if name in local_only_names or name not in discovered_by_name:
            out.append(schema)
        else:
            merged = dict(discovered_by_name[name])
            params = dict(merged.get("parameters") or {})
            required = list(params.get("required") or [])
            for field in (schema.get("parameters") or {}).get(
                    "required") or []:
                if field not in required:
                    required.append(field)
            if required:
                params["required"] = required
            merged["parameters"] = params
            out.append(merged)
        emitted.add(name)
    for schema in discovered:
        if schema["name"] not in emitted:
            out.append(schema)
            emitted.add(schema["name"])
    return out
