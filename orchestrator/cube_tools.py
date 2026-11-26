"""Cube Tool API — the only hands the orchestrator has.

Core tools (see tools/catalog.json). Every workflow behind them is a
deterministic Activepieces flow on the Metatron Cube: Ember proposes,
a human approves, the flow executes. run_workflow refuses to run without
a single-use approval token issued by the orchestrator's approval step.

Phase 3 adds 19 read-only report tools over the Metatron MCP Bus (pure
data fetches — no model runs on the Cube side), plus two direct SysOps
reads where the MCP wrappers are broken server-side. Reads execute only
inside the approve step, like get_watcher_status; payloads are trimmed
deterministically before Ember sees them.

Backends:
  - FixtureCubeTools — demo data from fixtures/, zero side effects.
  - HttpCubeTools    — the real Cube endpoints (AIOps /api/overview +
    Activepieces sync webhooks + MCP Bus), configured by environment;
    used from Phase 1 on the Cube itself.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tools with side effects. The orchestrator's approval step mints a
# single-use token for each gated step; Ember never sees the token.
GATED_TOOLS = {"run_workflow", "file_tickets"}

# Phase 3 read-only report tools (MCP Bus data reads; no model runs).
# Deliberately EXCLUDED from the bus: create_ticket (Axion auto-triage),
# knowledge_search (embedding model), ask_cube (calls Axion).
READ_TOOLS = {
    "cube_status", "capacity_report", "list_assets", "get_asset",
    "list_changes", "list_tickets", "list_rca", "secops_status",
    "list_dashboards", "list_flows", "brain_status", "list_connectors",
    "graph_search", "graph_dependents", "predictions", "finops_summary",
    "list_leases", "sentiment_summary", "finclose_summary",
}

TICKET_CATEGORIES = {"Network", "Billing", "Access", "Hardware",
                     "Account", "Software", "Other"}
TICKET_PRIORITIES = {"P1", "P2", "P3", "P4"}


# ------------------------------------------------------------- trimming
def _trim(value, list_cap: int = 10, str_cap: int = 160, key_cap: int = 24,
          depth: int = 0):
    """Deterministic payload trim: cap lists, truncate strings, limit
    dict breadth and depth. Applied to every read before Ember sees it."""
    if isinstance(value, str):
        return value[:str_cap]
    if isinstance(value, list):
        return [_trim(v, list_cap, str_cap, key_cap, depth + 1)
                for v in value[:list_cap]]
    if isinstance(value, dict):
        if depth >= 4:
            return {k: _trim(v, list_cap, str_cap, key_cap, depth + 1)
                    for k, v in list(value.items())[:key_cap]
                    if not isinstance(v, (dict, list))}
        return {k: _trim(v, list_cap, str_cap, key_cap, depth + 1)
                for k, v in list(value.items())[:key_cap]}
    return value


def _pick(item: dict, fields: tuple, str_cap: int = 160) -> dict:
    return {f: (str(item[f])[:str_cap] if isinstance(item.get(f), str)
                else item.get(f))
            for f in fields if item.get(f) is not None}


def _shape_finops(d: dict) -> dict:
    return {
        "costs": d.get("costs") or {},
        "budget": d.get("budget") or {},
        "alerts": [str(a)[:160] for a in (d.get("alerts") or [])[:5]],
        "usage": d.get("usage") or {},
        "vast_balance_days_left": (d.get("vast_balance") or {}).get("days_left"),
    }


def _shape_dependents(d: dict) -> dict:
    deps = [_pick(x, ("label", "kind", "ref"), 80)
            for x in (d.get("dependents") or [])[:25]]
    return {"component": d.get("component"), "count": d.get("count", len(deps)),
            "dependents": deps}


def _shape_graph_search(d: dict) -> dict:
    return {"results": [_pick(x, ("label", "kind", "ref"), 80)
                        for x in (d.get("results") or [])[:10]]}


def _shape_secops(d: dict) -> dict:
    pri = [_pick(x, ("id", "severity", "cvss", "epss", "kev", "product",
                     "vendor", "published", "description"), 140)
           for x in (d.get("priority") or [])[:5]]
    src = d.get("sources") or {}
    estate = d.get("estate") or {}
    return {
        "kpis": d.get("kpis") or {},
        "sources": {k: {"ok": (v or {}).get("ok"), "count": (v or {}).get("count")}
                    for k, v in src.items()},
        "estate": {k: estate.get(k) for k in ("components", "up",
                                              "open_incidents")},
        "priority_top5": pri,
    }


def _shape_capacity(d: dict) -> dict:
    comps = list(d.get("components") or [])
    comps.sort(key=lambda c: (c.get("uptime_7d_pct", 100.0),
                              -(c.get("latency_slope_ms_day") or 0)))
    host = d.get("host") or {}
    return {
        "host_current": host.get("current") or {},
        "host_gpu": host.get("gpu") or {},
        "disk_days_to_90": d.get("disk_days_to_90",
                                 host.get("disk_days_to_90")),
        "components_up": d.get("components_up"),
        "components_total": d.get("components_total"),
        "worst_components": [
            _pick(c, ("component", "name", "up", "uptime_7d_pct",
                      "latency_avg_24h_ms", "latency_slope_ms_day",
                      "incidents_7d"), 60) for c in comps[:8]],
    }


def _shape_predictions(d: dict) -> dict:
    preds = sorted(d.get("predictions") or [],
                   key=lambda p: -(p.get("risk_score") or 0))
    return {
        "risk_max": d.get("risk_max"), "high_count": d.get("high_count"),
        "watch_count": d.get("watch_count"),
        "components_total": d.get("components_total"),
        "host_risk": d.get("host_risk") or {},
        "top_risks": [
            {**_pick(p, ("component", "name", "risk_score", "band",
                         "predicted_window"), 80),
             "reasons": [str(r)[:100] for r in (p.get("reasons") or [])[:3]]}
            for p in preds[:8]],
    }


def _shape_tickets(d: dict) -> dict:
    return {"count": d.get("count"),
            "tickets": [_pick(t, ("ref", "title", "status", "priority",
                                  "category", "created"), 120)
                        for t in (d.get("tickets") or [])[:10]]}


def _shape_assets(d: dict) -> dict:
    return {"count": d.get("count"),
            "assets": [_pick(a, ("ref", "name", "type", "lifecycle",
                                 "environment"), 80)
                       for a in (d.get("assets") or [])[:10]]}


def _shape_asset(d: dict) -> dict:
    asset = d.get("asset") or {}
    return {
        "asset": _pick(asset, ("ref", "name", "type", "environment",
                               "lifecycle", "owner", "location"), 120),
        "attributes": _trim(asset.get("attributes") or {}, str_cap=80),
        "depends": [_pick(x, ("kind", "ref", "name"), 80)
                    for x in (d.get("depends") or [])[:10]],
        "dependents": [_pick(x, ("kind", "ref", "name"), 80)
                       for x in (d.get("dependents") or [])[:10]],
    }


def _shape_changes(d: dict) -> dict:
    return {"count": d.get("count"),
            "changes": [_pick(c, ("ref", "kind", "summary", "status",
                                  "created"), 120)
                        for c in (d.get("changes") or [])[:10]]}


def _shape_rca(d: dict) -> dict:
    return {"rca": [_pick(r, ("ref", "component", "status", "policy",
                              "summary", "confidence"), 160)
                    for r in (d.get("rca") or [])[:5]]}


def _shape_dashboards(d: dict) -> dict:
    return {"dashboards": [_pick(x, ("slug", "title", "source_kind",
                                     "last_refresh"), 80)
                           for x in (d.get("dashboards") or [])[:12]]}


def _shape_flows(d: dict) -> dict:
    flows = d.get("flows") or []
    return {"count": d.get("count", len(flows)),
            "enabled": sum(1 for f in flows if f.get("status") == "ENABLED"),
            "flows": [_pick(f, ("name", "status"), 100) for f in flows[:15]]}


def _shape_leases(d: dict) -> dict:
    fields = ("ref", "property_name", "counterparty", "end_date",
              "days_to_expiry", "monthly_rent_usd")
    return {
        "companies_total": d.get("companies_total"),
        "properties_total": d.get("properties_total"),
        "leases_active": d.get("leases_active"),
        "leases_expiring_90d": d.get("leases_expiring_90d"),
        "leases_expired": d.get("leases_expired"),
        "monthly_rent_total_usd": d.get("monthly_rent_total_usd"),
        "pipeline_value_usd": d.get("pipeline_value_usd"),
        "pipeline_by_stage": d.get("pipeline_by_stage") or {},
        "expiring_leases": [_pick(x, fields, 80)
                            for x in (d.get("expiring_leases") or [])[:5]],
        "expired_leases": [_pick(x, fields, 80)
                           for x in (d.get("expired_leases") or [])[:5]],
    }


def _shape_finclose(d: dict) -> dict:
    return {
        "demo": d.get("demo"), "company": d.get("company"),
        "period": d.get("period"), "close_state": d.get("close_state"),
        "erp_system": d.get("erp_system"), "kpis": d.get("kpis") or {},
        "revenue_by_channel": d.get("revenue_by_channel") or {},
        "exceptions_open": d.get("exceptions_open"),
        "exceptions_total": d.get("exceptions_total"),
        "exceptions": [_pick(x, ("kind", "severity", "source", "date",
                                 "detail"), 140)
                       for x in (d.get("exceptions") or [])[:4]],
    }


def _shape_connectors(d: dict) -> dict:
    return {
        "connectors": [_pick(c, ("name", "type", "status",
                                 "has_credentials", "last_sync"), 80)
                       for c in (d.get("connectors") or [])[:12]],
        "types": sorted((d.get("types") or {}).keys())
        if isinstance(d.get("types"), dict) else [],
    }


SHAPERS = {
    "finops_summary": _shape_finops,
    "graph_dependents": _shape_dependents,
    "graph_search": _shape_graph_search,
    "secops_status": _shape_secops,
    "capacity_report": _shape_capacity,
    "predictions": _shape_predictions,
    "list_tickets": _shape_tickets,
    "list_assets": _shape_assets,
    "get_asset": _shape_asset,
    "list_changes": _shape_changes,
    "list_rca": _shape_rca,
    "list_dashboards": _shape_dashboards,
    "list_flows": _shape_flows,
    "list_leases": _shape_leases,
    "finclose_summary": _shape_finclose,
    "list_connectors": _shape_connectors,
}


def shape_read(name: str, data: dict) -> dict:
    """Deterministic per-tool shaping; generic trim as the fallback."""
    if not isinstance(data, dict):
        return {"value": _trim(data)}
    shaper = SHAPERS.get(name)
    if shaper is not None:
        return shaper(data)
    return _trim(data)


class BoardStore:
    """The Triage Desk board: a deterministic JSON store.

    Chosen for the demo over the Cube ITSM create API because ITSM ticket
    creation unconditionally runs a non-Ember model (auto-triage +
    sentiment) and sends notifications — both break the Ember-only rule.
    The board is plain file I/O: no model, no sends.
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get(
            "EMBER_BOARD_PATH", os.path.join(ROOT, "board", "tickets.json"))

    def _read(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            data = json.load(f)
        return data.get("tickets", []) if isinstance(data, dict) else []

    def _write(self, tickets: list[dict]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"tickets": tickets}, f, indent=1)
        os.replace(tmp, self.path)

    def file(self, tickets: list[dict], run_tag: str = "") -> dict:
        board = self._read()
        filed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ids = []
        for t in tickets:
            entry = {
                "ticket_id": str(t["id"])[:40],
                "category": t["category"],
                "priority": t["priority"],
                "draft_reply": str(t["draft_reply"])[:1000],
                "route": str(t.get("route") or "")[:80],
                "filed_at": filed_at,
                "run_tag": run_tag,
                "status": "filed — draft reply awaiting human send",
            }
            board.append(entry)
            ids.append(entry["ticket_id"])
        self._write(board)
        return {"filed": len(ids), "board_total": len(board),
                "ticket_ids": ids}

    def list(self) -> list[dict]:
        return self._read()

    def clear(self) -> None:
        self._write([])


def load_catalog() -> dict:
    with open(os.path.join(ROOT, "tools", "catalog.json")) as f:
        return json.load(f)


class ApprovalError(RuntimeError):
    pass


class ApprovalTokens:
    """Single-use tokens, minted only by the human approval step."""

    def __init__(self):
        self._live: set[str] = set()

    def issue(self) -> str:
        token = secrets.token_hex(16)
        self._live.add(token)
        return token

    def consume(self, token: str) -> bool:
        if token in self._live:
            self._live.discard(token)
            return True
        return False


class CubeTools:
    def __init__(self, tokens: ApprovalTokens):
        self.tokens = tokens
        self.catalog = load_catalog()
        self.board = BoardStore()

    # ------------------------------------------------------------- tools
    def list_workflows(self) -> dict:
        return {"workflows": [
            {"id": w["id"], "name": w["name"], "status": w["status"]}
            for w in self.catalog["workflows"]]}

    def get_watcher_status(self) -> dict:
        raise NotImplementedError

    def run_workflow(self, workflow_id: str, inputs: dict | None = None,
                     approval_token: str = "") -> dict:
        if not self.tokens.consume(approval_token):
            raise ApprovalError(
                "run_workflow refused: no valid human approval token. "
                "Ember proposes; a human approves; only then does this unlock.")
        known = {w["id"] for w in self.catalog["workflows"]}
        if workflow_id not in known:
            raise ValueError(f"unknown workflow_id: {workflow_id}")
        return self._execute(workflow_id, inputs or {})

    def get_run_status(self, run_id: str) -> dict:
        raise NotImplementedError

    def file_tickets(self, tickets: list, approval_token: str = "",
                     run_tag: str = "") -> dict:
        """File human-approved triaged tickets to the board store.

        Same gate as run_workflow: no valid single-use approval token, no
        filing. Deterministic validation + file write; no model, no sends.
        """
        if not self.tokens.consume(approval_token):
            raise ApprovalError(
                "file_tickets refused: no valid human approval token. "
                "Ember proposes; a human approves; only then does this unlock.")
        if not isinstance(tickets, list) or not tickets:
            raise ValueError("file_tickets needs a non-empty tickets list")
        if len(tickets) > 20:
            raise ValueError("file_tickets capped at 20 tickets per call")
        clean = []
        for t in tickets:
            if not isinstance(t, dict):
                raise ValueError("each ticket must be an object")
            for field in ("id", "category", "priority", "draft_reply"):
                if not str(t.get(field) or "").strip():
                    raise ValueError(f"ticket missing required field: {field}")
            if t["category"] not in TICKET_CATEGORIES:
                raise ValueError(f"unknown category: {t['category']!r}")
            if t["priority"] not in TICKET_PRIORITIES:
                raise ValueError(f"unknown priority: {t['priority']!r}")
            clean.append(t)
        return self.board.file(clean, run_tag=run_tag)

    def _execute(self, workflow_id: str, inputs: dict) -> dict:
        raise NotImplementedError

    def _read_tool(self, name: str, arguments: dict) -> dict:
        """Phase 3 read-only report tools. Pure data fetches; they run
        only inside the orchestrator's approve step (propose executes
        nothing), and results are shaped/trimmed before Ember sees them."""
        raise NotImplementedError

    # ------------------------------------------------------------- dispatch
    def call(self, name: str, arguments: dict) -> dict:
        if name == "list_workflows":
            return self.list_workflows()
        if name == "get_watcher_status":
            return self.get_watcher_status()
        if name == "run_workflow":
            return self.run_workflow(arguments.get("workflow_id", ""),
                                     arguments.get("inputs") or {},
                                     arguments.get("approval_token", ""))
        if name == "get_run_status":
            return self.get_run_status(arguments.get("run_id", ""))
        if name == "file_tickets":
            return self.file_tickets(arguments.get("tickets") or [],
                                     arguments.get("approval_token", ""),
                                     arguments.get("run_tag", ""))
        if name in READ_TOOLS:
            return self._read_tool(name, arguments or {})
        raise ValueError(f"unknown tool: {name}")


class FixtureCubeTools(CubeTools):
    """Phase 0 default: fixture data, simulated runs, no side effects."""

    def __init__(self, tokens: ApprovalTokens):
        super().__init__(tokens)
        with open(os.path.join(ROOT, "fixtures", "handover-sample.json")) as f:
            self.fixture = json.load(f)
        with open(os.path.join(ROOT, "fixtures", "reads-sample.json")) as f:
            self.reads_fixture = json.load(f)
        self.runs: dict[str, dict] = {}
        self._seq = 0

    def get_watcher_status(self) -> dict:
        s = self.fixture["sample_watcher_status"]
        up, total = s["components_up"].split("/")
        open_inc = s["open_incidents"]
        line = (f"{up}/{total} components up, {open_inc} open incident"
                f"{'s' if open_inc != 1 else ''} (fixture data)")
        return {"line": line, "components_up": int(up), "components_total": int(total),
                "open_incidents": open_inc}

    def _read_tool(self, name: str, arguments: dict) -> dict:
        sample = self.reads_fixture.get(name)
        if sample is None:
            raise ValueError(f"no fixture read for tool: {name}")
        out = json.loads(json.dumps(sample))  # deep copy
        # Echo the caller's selector so stub demos stay coherent.
        if name == "graph_dependents" and arguments.get("component"):
            out["component"] = arguments["component"]
        if name == "get_asset" and arguments.get("ref"):
            out.setdefault("asset", {})["ref"] = arguments["ref"]
        if name == "list_tickets" and arguments.get("status"):
            out["tickets"] = [t for t in out.get("tickets", [])
                              if t.get("status") == arguments["status"]]
            out["count"] = len(out["tickets"])
        return out

    def _execute(self, workflow_id: str, inputs: dict) -> dict:
        self._seq += 1
        run_id = f"sim-{self._seq:03d}"
        result = {"run_id": run_id, "workflow_id": workflow_id,
                  "status": "SUCCEEDED",
                  "note": "Phase 0 fixture run — simulated, nothing was sent."}
        self.runs[run_id] = result
        return result

    def get_run_status(self, run_id: str) -> dict:
        return self.runs.get(run_id, {"run_id": run_id, "status": "UNKNOWN"})


class HttpCubeTools(CubeTools):
    """Real Cube endpoints (Phase 1+, runs on the Cube).

    Env:
      AIOPS_OVERVIEW_URL  default http://127.0.0.1:8008/api/overview
      AP_WEBHOOK_BASE     default http://127.0.0.1:8085/api/v1/webhooks
      MCP_URL             default http://127.0.0.1:8017/mcp
      SYSOPS_BASE         default http://127.0.0.1:8016
      CUBE_SERVICES_API_KEY  MCP Bus key; falls back to parsing
                          MCP_ENV_PATH (default /root/metatron/mcp.env)
                          at runtime. The key is never printed or logged.
    Workflow ids map to Activepieces flow ids via the catalog's flow_id.
    """

    def __init__(self, tokens: ApprovalTokens):
        super().__init__(tokens)
        self.overview_url = os.environ.get(
            "AIOPS_OVERVIEW_URL", "http://127.0.0.1:8008/api/overview")
        self.webhook_base = os.environ.get(
            "AP_WEBHOOK_BASE", "http://127.0.0.1:8085/api/v1/webhooks")
        self.mcp_url = os.environ.get(
            "MCP_URL", "http://127.0.0.1:8017/mcp")
        self.sysops_base = os.environ.get(
            "SYSOPS_BASE", "http://127.0.0.1:8016").rstrip("/")
        self._mcp_key_cache: str | None = None
        self._flow_ids = {w["id"]: w.get("flow_id")
                          for w in self.catalog["workflows"]}
        self.runs: dict[str, dict] = {}

    # ------------------------------------------------------------- MCP Bus
    def _mcp_key(self) -> str:
        if self._mcp_key_cache is None:
            key = os.environ.get("CUBE_SERVICES_API_KEY", "")
            if not key:
                path = os.environ.get("MCP_ENV_PATH",
                                      "/root/metatron/mcp.env")
                try:
                    with open(path) as f:
                        for line in f:
                            if line.startswith("CUBE_SERVICES_API_KEY="):
                                key = line.split("=", 1)[1].strip()
                                break
                except OSError:
                    key = ""
            self._mcp_key_cache = key
        return self._mcp_key_cache

    def _mcp_call(self, name: str, arguments: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}}}
        req = urllib.request.Request(
            self.mcp_url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "X-API-Key": self._mcp_key(),
                     "User-Agent": "ember-orchestrator/0.3"})
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = json.load(r)
        result = payload.get("result") or {}
        if result.get("isError"):
            text = ""
            for item in result.get("content") or []:
                text += str(item.get("text") or "")
            raise RuntimeError(f"MCP tool {name} error: {text[:300]}")
        data = result.get("structuredContent")
        if data is None:
            for item in result.get("content") or []:
                try:
                    data = json.loads(item.get("text") or "")
                    break
                except ValueError:
                    continue
        if not isinstance(data, dict):
            raise RuntimeError(f"MCP tool {name} returned no structured data")
        return data

    def _read_tool(self, name: str, arguments: dict) -> dict:
        args = dict(arguments or {})
        # Direct SysOps routes where the MCP wrappers are broken
        # server-side (verified Nov 26, 2026): list_connectors crashes
        # on the types dict; get_asset 404s on ref-format paths.
        if name == "list_connectors":
            return shape_read(name, self._get_json(
                self.sysops_base + "/api/connectors"))
        if name == "get_asset":
            return shape_read(name, self._get_asset_direct(args))
        if name in ("list_tickets", "list_assets", "list_changes"):
            args["limit"] = max(1, min(int(args.get("limit") or 10), 20))
        return shape_read(name, self._mcp_call(name, args))

    def _get_asset_direct(self, args: dict) -> dict:
        ref = str(args.get("ref") or "").strip()
        if not ref:
            raise ValueError("get_asset needs a ref (e.g. CI-0001) or id")
        asset_id = ref
        if not ref.isdigit():
            listing = self._mcp_call("list_assets", {"limit": 100})
            match = next((a for a in listing.get("assets") or []
                          if str(a.get("ref")) == ref), None)
            if match is None:
                raise ValueError(f"unknown asset ref: {ref}")
            asset_id = str(match.get("id"))
        return self._get_json(
            f"{self.sysops_base}/api/assets/{urllib.parse.quote(asset_id)}")

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "ember-orchestrator/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def _post_json(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "ember-orchestrator/0.1"})
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}

    def get_watcher_status(self) -> dict:
        data = self._get_json(self.overview_url)
        comps = data.get("components", [])
        totals = data.get("totals", {})
        total = totals.get("components", len(comps))
        up = totals.get("up", sum(1 for c in comps if c.get("up")))
        open_inc = (totals.get("open_incidents", 0)
                    + totals.get("acknowledged_incidents", 0))
        down = [c["name"] for c in comps if not c.get("up")]
        line = (f"{up}/{total} components up, "
                + (f"DOWN: {', '.join(down)}, " if down else "")
                + f"{open_inc} open incident{'s' if open_inc != 1 else ''}")
        # Deterministic trim for the handover brief: most recent 5 incidents,
        # short fields only; host reduced to the headline percentages.
        incidents = []
        for inc in (data.get("recent_incidents") or [])[:5]:
            incidents.append({
                "title": str(inc.get("title") or "")[:120],
                "component": str(inc.get("component") or "")[:60],
                "severity": str(inc.get("severity") or "")[:20],
                "status": str(inc.get("status") or "")[:20],
                "first_seen": str(inc.get("first_seen") or "")[:30],
            })
        host_raw = data.get("host") or {}
        host = {k: host_raw.get(k) for k in
                ("cpu_pct", "mem_pct", "disk_pct", "load1")
                if host_raw.get(k) is not None}
        return {"line": line, "components_up": up, "components_total": total,
                "open_incidents": open_inc, "recent_incidents": incidents,
                "host": host, "sampled_at": str(data.get("sampled_at") or "")[:30]}

    def _execute(self, workflow_id: str, inputs: dict) -> dict:
        flow_id = self._flow_ids.get(workflow_id)
        if not flow_id:
            raise ValueError(f"workflow {workflow_id} has no production flow yet")
        out = self._post_json(f"{self.webhook_base}/{flow_id}/sync", inputs)
        run_id = out.get("run_id") or flow_id
        result = {"run_id": run_id, "workflow_id": workflow_id,
                  "status": "SUCCEEDED", "result": out}
        self.runs[run_id] = result
        return result

    def get_run_status(self, run_id: str) -> dict:
        return self.runs.get(run_id, {"run_id": run_id, "status": "UNKNOWN"})
