"""Cube Tool API — the only hands the orchestrator has.

Core tools (see tools/catalog.json). Every workflow behind them is a
deterministic Activepieces flow on the Metatron Cube: Ember proposes,
a human approves, the flow executes. run_workflow refuses to run without
a single-use approval token issued by the orchestrator's approval step.

Phase 3 adds 19 read-only report tools over the Metatron MCP Bus (pure
data fetches — no model runs on the Cube side). Reads execute only
inside the approve step, like get_watcher_status; payloads are trimmed
deterministically before Ember sees them.

Nov 26, 2026 (MCP wiring, Direction 1): HttpCubeTools discovers its
read tools from the bus at startup (tools/list → Ember schemas,
eligibility-filtered; see orchestrator/mcp_discovery.py) and routes
every read through tools/call — the two formerly-broken bus wrappers
(list_connectors, get_asset) are fixed bus-side, so the direct SysOps
workarounds are gone. If discovery fails, the static catalog is the
fallback and the demo runs exactly as before.

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

from .intake_totals import verify_invoice_totals

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tools with side effects. The orchestrator's approval step mints a
# single-use token for each gated step; Ember never sees the token.
GATED_TOOLS = {"run_workflow", "file_tickets", "create_agent_flow",
               "enable_agent_flow", "file_intake", "file_document_report",
               "export_file", "create_dashboard", "convert_plc_code"}

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

# Catalog tools that are Ember-local (deterministic, in-process) and
# never come from the bus: the gated writes plus the local reads
# (catalog listing, AIOps overview, run polling). Discovery merges
# these with the bus-discovered reads; everything else in the catalog
# is bus-backed (READ_TOOLS) and its schema may be replaced by the
# discovered one.
LOCAL_ONLY_TOOLS = {
    "list_workflows", "get_watcher_status", "run_workflow",
    "get_run_status", "file_tickets", "create_agent_flow",
    "enable_agent_flow", "file_intake", "file_document_report",
    "export_file", "create_dashboard", "convert_plc_code",
    "ask_medical_manual", "ask_paralegal",
}

TICKET_CATEGORIES = {"Network", "Billing", "Access", "Hardware",
                     "Account", "Software", "Other"}
TICKET_PRIORITIES = {"P1", "P2", "P3", "P4"}
INTAKE_KINDS = {"invoice", "document", "photo", "audio_note", "video",
                "other"}
FINDING_SEVERITIES = {"info", "low", "medium", "high", "critical"}


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
    types = d.get("types") or {}
    if isinstance(types, dict):
        # Raw SysOps shape (pre-Nov 26 direct route).
        type_ids = sorted(types.keys())
    else:
        # MCP Bus shape: already a list of type ids.
        type_ids = [str(t) for t in types]
    return {
        "connectors": [_pick(c, ("name", "type", "status",
                                 "has_credentials", "last_sync"), 80)
                       for c in (d.get("connectors") or [])[:12]],
        "types": type_ids,
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

    def remove(self, ticket_id: str) -> bool:
        """Undo support: drop one ticket by id (idempotent)."""
        board = self._read()
        kept = [t for t in board if t.get("ticket_id") != ticket_id]
        if len(kept) == len(board):
            return False
        self._write(kept)
        return True

    def clear(self) -> None:
        self._write([])


class _JsonListStore:
    """Deterministic append-only JSON list store (Phase 4 boards).

    Same pattern as BoardStore: plain file I/O, atomic replace, no
    model, no sends. `key` is the top-level list name in the file.
    """

    def __init__(self, path: str, key: str):
        self.path = path
        self.key = key

    def _read(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            data = json.load(f)
        return data.get(self.key, []) if isinstance(data, dict) else []

    def _write(self, items: list[dict]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({self.key: items}, f, indent=1)
        os.replace(tmp, self.path)

    def append(self, entry: dict) -> int:
        items = self._read()
        items.append(entry)
        self._write(items)
        return len(items)

    def list(self) -> list[dict]:
        return self._read()

    def clear(self) -> None:
        self._write([])


class IntakeStore(_JsonListStore):
    """Multimodal intake records (Ember-extracted fields, human-filed)."""

    def __init__(self, path: str | None = None):
        super().__init__(
            path or os.environ.get(
                "EMBER_INTAKE_PATH",
                os.path.join(ROOT, "board", "intake.json")), "records")


class ReportStore(_JsonListStore):
    """Document-intelligence reports + Agent Builder board filings."""

    def __init__(self, path: str | None = None):
        super().__init__(
            path or os.environ.get(
                "EMBER_REPORTS_PATH",
                os.path.join(ROOT, "board", "reports.json")), "reports")


class AgentFlowRegistry(_JsonListStore):
    """Registry of Agent-Builder-created flows (board/agent_flows.json).

    The enablement step may ONLY enable flow ids recorded here — never
    arbitrary RPA flows. Entries: id, name, created_at, trigger, cron,
    source, action, enabled, enabled_at.
    """

    def __init__(self, path: str | None = None):
        super().__init__(
            path or os.environ.get(
                "EMBER_AGENT_FLOWS_PATH",
                os.path.join(ROOT, "board", "agent_flows.json")), "flows")

    def register(self, entry: dict) -> dict:
        flows = self._read()
        flows = [f for f in flows if f.get("id") != entry.get("id")]
        flows.append(entry)
        self._write(flows)
        return entry

    def get(self, flow_id: str) -> dict | None:
        for f in self._read():
            if f.get("id") == flow_id:
                return f
        return None

    def mark_enabled(self, flow_id: str) -> dict | None:
        flows = self._read()
        for f in flows:
            if f.get("id") == flow_id:
                f["enabled"] = True
                f["enabled_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._write(flows)
                return f
        return None

    def mark_disabled(self, flow_id: str) -> dict | None:
        flows = self._read()
        for f in flows:
            if f.get("id") == flow_id:
                f["enabled"] = False
                f["enabled_at"] = None
                self._write(flows)
                return f
        return None

    def set_autonomy(self, flow_id: str, level: str) -> dict | None:
        flows = self._read()
        for f in flows:
            if f.get("id") == flow_id:
                f["autonomy"] = level
                self._write(flows)
                return f
        return None

    def remove(self, flow_id: str) -> bool:
        flows = self._read()
        kept = [f for f in flows if f.get("id") != flow_id]
        if len(kept) == len(flows):
            return False
        self._write(kept)
        return True


class DashboardRegistry(_JsonListStore):
    """Registry of Ember-created dashboards (board/dashboards.json).

    Undo may ONLY delete slugs recorded here — never the Cube's
    pre-existing dashboards. Entries: slug, title, created_at,
    endpoints, refresh_flow_id.
    """

    def __init__(self, path: str | None = None):
        super().__init__(
            path or os.environ.get(
                "EMBER_DASHBOARDS_PATH",
                os.path.join(ROOT, "board", "dashboards.json")),
            "dashboards")

    def register(self, entry: dict) -> dict:
        items = [d for d in self._read()
                 if d.get("slug") != entry.get("slug")]
        items.append(entry)
        self._write(items)
        return entry

    def get(self, slug: str) -> dict | None:
        for d in self._read():
            if d.get("slug") == slug:
                return d
        return None

    def remove(self, slug: str) -> bool:
        items = self._read()
        kept = [d for d in items if d.get("slug") != slug]
        if len(kept) == len(items):
            return False
        self._write(kept)
        return True


class PlcConversionStore(_JsonListStore):
    """PLC code conversions (board/plc_conversions.json).

    Every Ember-authored conversion that passed the deterministic
    coverage validator and human approval. Undo may ONLY delete
    conversion ids recorded here. Entries carry the full record:
    direction, program name, source + converted code, coverage
    report, stats, status (draft -> change_requested -> accepted
    / rejected via the RPA change-approval flow)."""

    def __init__(self, path: str | None = None):
        super().__init__(
            path or os.environ.get(
                "EMBER_PLC_CONVERSIONS_PATH",
                os.path.join(ROOT, "board", "plc_conversions.json")),
            "conversions")

    def add(self, entry: dict) -> dict:
        items = self._read()
        items.append(entry)
        self._write(items)
        return entry

    def get(self, conversion_id: str) -> dict | None:
        for c in self._read():
            if c.get("id") == conversion_id:
                return c
        return None

    def update(self, conversion_id: str, fields: dict) -> dict | None:
        items = self._read()
        for c in items:
            if c.get("id") == conversion_id:
                c.update(fields)
                self._write(items)
                return c
        return None

    def remove(self, conversion_id: str) -> bool:
        items = self._read()
        kept = [c for c in items if c.get("id") != conversion_id]
        if len(kept) == len(items):
            return False
        self._write(kept)
        return True


class GoalStore(_JsonListStore):
    """User-stated outcomes (board/goals.json). One metric per goal,
    from the agent-builder condition-field allowlists."""

    def __init__(self, path: str | None = None):
        super().__init__(
            path or os.environ.get(
                "EMBER_GOALS_PATH",
                os.path.join(ROOT, "board", "goals.json")), "goals")

    def add_goal(self, goal: dict) -> dict:
        goals = self._read()
        goals.append(goal)
        self._write(goals)
        return goal

    def close(self, goal_id: str) -> dict | None:
        goals = self._read()
        for g in goals:
            if g.get("id") == goal_id:
                g["status"] = "closed"
                g["closed_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._write(goals)
                return g
        return None


class PreferenceStore:
    """Learned edit preferences (board/preferences.json).

    Deterministic counts of what the user changed in the wizard
    (cron, actions, condition, source) so the next Ember design
    can be pre-aligned. Plain JSON object, atomic replace."""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get(
            "EMBER_PREFS_PATH",
            os.path.join(ROOT, "board", "preferences.json"))

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {"edits": 0, "cron": {}, "actions": {},
                    "source": {}, "condition": {}}
        with open(self.path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}

    def _write(self, prefs: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prefs, f, indent=1)
        os.replace(tmp, self.path)

    def record_edit(self, proposed: dict, final: dict) -> dict:
        prefs = self._read()
        prefs["edits"] = int(prefs.get("edits") or 0) + 1
        for key in ("cron", "source"):
            val = str(final.get(key) or "")
            if val and val != str(proposed.get(key) or ""):
                bucket = prefs.setdefault(key, {})
                bucket[val] = int(bucket.get(val) or 0) + 1
        acts = "+".join(final.get("actions") or [])
        if acts and acts != "+".join(proposed.get("actions") or []):
            bucket = prefs.setdefault("actions", {})
            bucket[acts] = int(bucket.get(acts) or 0) + 1
        cond = "yes" if final.get("condition") else "no"
        if bool(final.get("condition")) != bool(proposed.get("condition")):
            bucket = prefs.setdefault("condition", {})
            bucket[cond] = int(bucket.get(cond) or 0) + 1
        self._write(prefs)
        return prefs

    def summary(self) -> dict:
        prefs = self._read()

        def top(bucket):
            items = sorted((bucket or {}).items(),
                           key=lambda kv: -kv[1])
            return items[0][0] if items else None

        return {"edits": int(prefs.get("edits") or 0),
                "cron": top(prefs.get("cron")),
                "actions": top(prefs.get("actions")),
                "source": top(prefs.get("source")),
                "condition": top(prefs.get("condition"))}

    def prompt_line(self) -> str:
        s = self.summary()
        if not s["edits"]:
            return ""
        bits = []
        if s["cron"]:
            bits.append(f"cron '{s['cron']}'")
        if s["actions"]:
            bits.append(f"actions '{s['actions']}'")
        if s["source"]:
            bits.append(f"source '{s['source']}'")
        if s["condition"]:
            bits.append("with a condition" if s["condition"] == "yes"
                        else "without a condition")
        if not bits:
            return ""
        return (f"User preferences from {s['edits']} past wizard "
                f"edit(s): usually prefers {', '.join(bits)}. "
                "Align the proposal with these unless the request "
                "says otherwise.")

    def reset(self) -> None:
        self._write({"edits": 0, "cron": {}, "actions": {},
                     "source": {}, "condition": {}})


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
        self.intake = IntakeStore()
        self.reports = ReportStore()
        self.agent_flows = AgentFlowRegistry()
        self.dashboards = DashboardRegistry()
        self.plc_conversions = PlcConversionStore()
        # Manuals-context stats for the in-flight PLC propose,
        # stashed by serve.py under its lock so the stored record
        # can carry them (the tool params come from Ember and
        # never include server-side context facts).
        self._plc_context_stats: dict | None = None
        self.goals = GoalStore()
        self.preferences = PreferenceStore()
        from .exporter import ExportStore
        self.exports = ExportStore()
        # Trace of the last completed run (set by the orchestrator
        # after each approve); the export_file 'last_run' source.
        self.last_run_trace: list[dict] = []
        # Most recent ask_medical_manual result (export source
        # 'medical'); set by the tool itself, read by export.
        self.last_medical_result: dict | None = None
        # Most recent ask_paralegal result (export source
        # 'legal'); set by the tool itself, read by export.
        self.last_legal_result: dict | None = None
        # Bus-discovered read tools beyond the static READ_TOOLS
        # (populated by HttpCubeTools discovery; empty for fixtures).
        self._extra_read_tools: set[str] = set()

    # --------------------------------------------------- tool schemas
    def tool_schemas(self) -> list[dict]:
        """Effective Ember function-calling schemas. Base: the static
        catalog. HttpCubeTools overrides with bus discovery merged
        over the catalog (static fallback on any failure)."""
        return [{"name": t["name"], "description": t["description"],
                 "parameters": t["parameters"]}
                for t in self.catalog["tools"]]

    def discovery_state(self) -> dict:
        """MCP discovery state for /api/status (additive, honest)."""
        return {"source": "static-catalog",
                "discovered_tools": 0,
                "excluded_tools": ["create_ticket", "knowledge_search",
                                   "ask_cube"]}

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

    # ------------------------------------------------ Phase 4 tools
    def create_agent_flow(self, params: dict, approval_token: str = "",
                          run_tag: str = "") -> dict:
        """Build a deterministic RPA agent flow from Ember's high-level
        params (Agent Builder demo). Ember proposes params only; the
        allowlist validator + assembler in agent_builder.py do the
        rest. The flow is created DISABLED — never published."""
        if not self.tokens.consume(approval_token):
            raise ApprovalError(
                "create_agent_flow refused: no valid human approval "
                "token. Ember proposes; a human approves; only then "
                "does this unlock.")
        from .agent_builder import validate_agent_params
        clean = validate_agent_params(params or {})
        result = self._create_agent(clean)
        # Register every builder-created flow: the enablement step may
        # only enable ids recorded here.
        self.agent_flows.register({
            "id": result["flow_id"],
            "name": result["name"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
            "trigger": result.get("trigger"),
            "cron": result.get("cron"),
            "source": result.get("source"),
            "action": result.get("action"),
            "actions": result.get("actions"),
            "condition": result.get("condition"),
            "chain": result.get("chain"),
            "autonomy": result.get("autonomy", "propose-only"),
            "enabled": False,
            "enabled_at": None,
        })
        return result

    def _create_agent(self, clean: dict) -> dict:
        raise NotImplementedError

    def enable_agent_flow(self, flow_id: str, approval_token: str = "",
                          run_tag: str = "") -> dict:
        """Enable a builder-created draft (second human approval).

        Registry-scoped: only flow ids recorded by create_agent_flow
        can be enabled — never arbitrary RPA flows. Publishing +
        enabling happens in the backend; the registry is updated only
        after the backend confirms ENABLED."""
        if not self.tokens.consume(approval_token):
            raise ApprovalError(
                "enable_agent_flow refused: no valid human approval "
                "token. Ember proposes; a human approves; only then "
                "does this unlock.")
        flow_id = str(flow_id or "").strip()
        entry = self.agent_flows.get(flow_id)
        if entry is None:
            raise ValueError(
                f"flow {flow_id!r} is not in the Agent Builder "
                "registry — only builder-created drafts can be "
                "enabled from here")
        result = self._enable_agent(entry)
        if result.get("status") == "ENABLED":
            self.agent_flows.mark_enabled(flow_id)
        result.setdefault("name", entry.get("name"))
        result.setdefault("cron", entry.get("cron"))
        result.setdefault("trigger", entry.get("trigger"))
        return result

    def _enable_agent(self, entry: dict) -> dict:
        raise NotImplementedError

    # ------------------------------------------------- undo (Nov 26)
    # Deterministic reversals, registry/board scoped and idempotent.
    # These are service-level operations (Operator+ session in
    # serve.py), not Ember tools — Ember never undoes anything.
    def undo_flow_delete(self, flow_id: str) -> dict:
        entry = self.agent_flows.get(flow_id)
        if entry is None:
            return {"undone": False,
                    "message": f"flow {flow_id!r} is not in the "
                               "registry (already removed?)"}
        self._delete_agent(entry)
        self.agent_flows.remove(flow_id)
        return {"undone": True, "kind": "flow_delete",
                "flow_id": flow_id,
                "message": f"Automation '{entry.get('name')}' "
                           "deleted from Metatron RPA and the "
                           "registry."}

    def undo_flow_disable(self, flow_id: str) -> dict:
        entry = self.agent_flows.get(flow_id)
        if entry is None:
            return {"undone": False,
                    "message": f"flow {flow_id!r} is not in the "
                               "registry"}
        result = self._disable_agent(entry)
        if result.get("status") != "ENABLED":
            self.agent_flows.mark_disabled(flow_id)
        return {"undone": True, "kind": "flow_disable",
                "flow_id": flow_id,
                "message": f"Automation '{entry.get('name')}' "
                           "disabled — it will not run."}

    def undo_board_ticket(self, ticket_id: str) -> dict:
        removed = self.board.remove(str(ticket_id or ""))
        return {"undone": removed, "kind": "board_ticket",
                "ticket_id": ticket_id,
                "message": (f"Ticket {ticket_id} removed from the "
                            "board." if removed else
                            f"Ticket {ticket_id} was not on the "
                            "board (already removed?)")}

    def undo_export(self, export_id: str) -> dict:
        removed = self.exports.remove(str(export_id or ""))
        return {"undone": removed, "kind": "export",
                "export_id": export_id,
                "message": ("Export file deleted." if removed else
                            "Export was not found (already "
                            "deleted?)")}

    def _delete_agent(self, entry: dict) -> dict:
        raise NotImplementedError

    def _disable_agent(self, entry: dict) -> dict:
        raise NotImplementedError

    def file_intake(self, record: dict, approval_token: str = "",
                    run_tag: str = "") -> dict:
        """File an Ember-extracted intake record (Multimodal Intake).

        Ember reads the image itself and proposes the extracted fields;
        this tool only validates + stores them deterministically after
        human approval. No OCR model, no sends."""
        if not self.tokens.consume(approval_token):
            raise ApprovalError(
                "file_intake refused: no valid human approval token. "
                "Ember proposes; a human approves; only then does this "
                "unlock.")
        if not isinstance(record, dict):
            raise ValueError("file_intake needs a record object")
        kind = str(record.get("kind") or "").strip().lower()
        if kind not in INTAKE_KINDS:
            raise ValueError(
                f"intake kind must be one of {sorted(INTAKE_KINDS)}, "
                f"got {kind!r}")
        fields = record.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("file_intake needs a non-empty fields object")
        if len(fields) > 30:
            raise ValueError("file_intake capped at 30 fields per record")
        clean_fields = {}
        for k, v in fields.items():
            key = str(k)[:60]
            if isinstance(v, list):
                clean_fields[key] = [str(x)[:200] for x in v[:20]]
            elif isinstance(v, dict):
                clean_fields[key] = {str(kk)[:60]: str(vv)[:200]
                                     for kk, vv in list(v.items())[:10]}
            else:
                # Transcripts (audio/video intake) run longer than
                # typical extracted fields.
                cap = 2000 if key == "transcript" else 500
                clean_fields[key] = str(v)[:cap]
        entry = {
            "kind": kind,
            "fields": clean_fields,
            "notes": str(record.get("notes") or "")[:1000],
            "filed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_tag": run_tag,
            "status": "filed — awaiting human review",
        }
        result = {"filed": 1, "kind": kind, "fields": clean_fields}
        if kind == "invoice":
            # Deterministic totals verdict (Nov 27, 2026): the ONLY
            # source of discrepancy claims. Ember reports this
            # verdict; it never does its own arithmetic critique
            # (intake #59 false alarm).
            verdict = verify_invoice_totals(clean_fields)
            entry["totals_check"] = verdict
            result["totals_check"] = verdict
        total = self.intake.append(entry)
        result["intake_total"] = total
        return result

    def file_document_report(self, report: dict, approval_token: str = "",
                             run_tag: str = "") -> dict:
        """File an Ember document-analysis report (Document
        Intelligence). Ember parses the document and proposes findings
        with line references; this tool validates + stores them after
        human approval. Deterministic file write; no sends."""
        if not self.tokens.consume(approval_token):
            raise ApprovalError(
                "file_document_report refused: no valid human approval "
                "token. Ember proposes; a human approves; only then "
                "does this unlock.")
        if not isinstance(report, dict):
            raise ValueError("file_document_report needs a report object")
        title = str(report.get("title") or "").strip()
        summary = str(report.get("summary") or "").strip()
        if not title or not summary:
            raise ValueError("report needs a title and a summary")
        findings = report.get("findings")
        if not isinstance(findings, list):
            raise ValueError("report findings must be a list")
        if len(findings) > 50:
            raise ValueError("report capped at 50 findings")
        clean = []
        for f in findings:
            if not isinstance(f, dict):
                raise ValueError("each finding must be an object")
            sev = str(f.get("severity") or "info").strip().lower()
            if sev not in FINDING_SEVERITIES:
                raise ValueError(f"unknown finding severity: {sev!r}")
            detail = str(f.get("detail") or "").strip()
            if not detail:
                raise ValueError("each finding needs a detail")
            line = f.get("line")
            clean.append({
                "line": int(line) if str(line or "").strip().isdigit()
                        else None,
                "severity": sev,
                "detail": detail[:500],
            })
        entry = {
            "title": title[:200],
            "summary": summary[:2000],
            "findings": clean,
            "filed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_tag": run_tag,
            "status": "filed — awaiting human review",
        }
        total = self.reports.append(entry)
        return {"filed": 1, "reports_total": total, "title": entry["title"],
                "findings_count": len(clean)}

    def export_file(self, params: dict, approval_token: str = "",
                    run_tag: str = "") -> dict:
        """Export filed data to a downloadable file (CSV / Excel /
        JSON / TXT / Word). Ember proposes format + source only;
        generation is deterministic code in exporter.py ($0, no
        model). Needs the approval token like every gated write.
        A source with no data yet returns an honest no-export
        result instead of an empty file."""
        if not self.tokens.consume(approval_token):
            raise ApprovalError(
                "export_file refused: no valid human approval token. "
                "Ember proposes; a human approves; only then does this "
                "unlock.")
        from .exporter import (board_export_data, document_export_data,
                               generate_export, intake_export_data,
                               last_run_export_data, legal_export_data,
                               medical_export_data, plc_export_data,
                               validate_export_params)
        clean = validate_export_params(params or {})
        fmt, source = clean["format"], clean["source"]
        if source == "intake":
            records = self.intake.list()
            if not records:
                return {"exported": False, "format": fmt,
                        "source": source,
                        "message": "No intake extraction has been "
                                   "filed yet — attach a document, "
                                   "photo, audio, or video first and "
                                   "approve the filing, then export."}
            data = intake_export_data(records[-1])
        elif source == "board":
            tickets = self.board.list()
            if not tickets:
                return {"exported": False, "format": fmt,
                        "source": source,
                        "message": "The triage board is empty — no "
                                   "tickets have been filed yet."}
            data = board_export_data(tickets)
        elif source == "document":
            reports = [r for r in self.reports.list()
                       if r.get("run_tag") != "agent-builder-flow"]
            if not reports:
                return {"exported": False, "format": fmt,
                        "source": source,
                        "message": "No document-analysis report has "
                                   "been filed yet — attach a "
                                   "document for analysis first."}
            data = document_export_data(reports[-1])
        elif source == "plc":
            conversions = self.plc_conversions.list()
            if not conversions:
                return {"exported": False, "format": fmt,
                        "source": source,
                        "message": "No PLC conversion has been "
                                   "stored yet — convert a program "
                                   "first, then export it."}
            data = plc_export_data(conversions[-1])
        elif source == "medical":
            if not self.last_medical_result:
                return {"exported": False, "format": fmt,
                        "source": source,
                        "message": "No medical-manual answer yet — "
                                   "ask the medical manual a "
                                   "question first, then export it."}
            data = medical_export_data(self.last_medical_result)
        elif source == "legal":
            if not self.last_legal_result:
                return {"exported": False, "format": fmt,
                        "source": source,
                        "message": "No paralegal answer yet — "
                                   "ask the paralegal assistant a "
                                   "question first, then export it."}
            data = legal_export_data(self.last_legal_result)
        else:  # last_run
            if not self.last_run_trace:
                return {"exported": False, "format": fmt,
                        "source": source,
                        "message": "No completed run to export yet — "
                                   "run a command first, then export "
                                   "its results."}
            data = last_run_export_data(self.last_run_trace)
        return generate_export(self.exports, fmt, source, data)

    # ------------------------------------------------- Dashboard Builder
    def dashboard_profile(self, endpoints: list) -> dict:
        """Live data profile for dashboard sources (deterministic
        fetch, $0). Used by serve.py to brief Ember before it
        authors a spec and to validate the proposal."""
        return self._dashboard_profile(list(endpoints or []))

    def _dashboard_profile(self, endpoints: list) -> dict:
        raise NotImplementedError

    def create_dashboard(self, params: dict, approval_token: str = "",
                         run_tag: str = "") -> dict:
        """Build a live dashboard from Ember's spec (Dashboard
        Builder, Nov 27, 2026). Ember authors the spec against the
        live data profile; the deterministic validator in
        dashboard_builder.py mirrors the generator's validate_spec
        and re-checks every field before creation. The dashboard
        is created through the generator's deterministic path —
        its Axion design step is never used, and Ember specs carry
        insight/chat False so no Cube-side model runs."""
        if not self.tokens.consume(approval_token):
            raise ApprovalError(
                "create_dashboard refused: no valid human approval "
                "token. Ember proposes; a human approves; only then "
                "does this unlock.")
        from .dashboard_builder import validate_dashboard_params
        endpoints = (params or {}).get("endpoints")
        if isinstance(endpoints, str):
            endpoints = [endpoints]
        profile = self._dashboard_profile(list(endpoints or []))
        clean = validate_dashboard_params(params or {}, profile)
        result = self._create_dashboard(clean)
        # Register every Ember-created dashboard: undo may only
        # delete slugs recorded here — never pre-existing ones.
        self.dashboards.register({
            "slug": result["slug"],
            "title": result["title"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
            "endpoints": result.get("endpoints"),
            "refresh_flow_id": result.get("refresh_flow_id"),
        })
        return result

    def _create_dashboard(self, clean: dict) -> dict:
        raise NotImplementedError

    def _delete_dashboard(self, slug: str) -> dict:
        raise NotImplementedError

    def dashboard_exists(self, slug: str) -> bool:
        """Deterministic verification read (verifier.py)."""
        try:
            return bool(self._dashboard_exists(slug))
        except Exception:  # noqa: BLE001
            return False

    def _dashboard_exists(self, slug: str) -> bool:
        raise NotImplementedError

    def undo_dashboard_delete(self, slug: str) -> dict:
        entry = self.dashboards.get(str(slug or ""))
        if entry is None:
            return {"undone": False,
                    "message": f"dashboard {slug!r} is not in the "
                               "Ember registry (already removed?)"}
        self._delete_dashboard(entry["slug"])
        self.dashboards.remove(entry["slug"])
        return {"undone": True, "kind": "dashboard_delete",
                "slug": entry["slug"],
                "message": f"Dashboard '{entry.get('title')}' "
                           "deleted from the Dashboard Generator."}

    # ------------------------------------------------- PLC Code Converter
    def convert_plc_code(self, params: dict, approval_token: str = "",
                         run_tag: str = "") -> dict:
        """Store an Ember-authored PLC code conversion (PLC Code
        Converter, Nov 27, 2026). Ember wrote the converted code
        and the per-instruction coverage report at propose time,
        using the vendor manuals injected as long context; the
        deterministic validator in plc_converter.py re-checks
        coverage completeness and target-platform forms here
        before anything is stored. The result is a DRAFT record
        for qualified-engineer review — nothing is deployed to
        any PLC, ever, by this tool."""
        if not self.tokens.consume(approval_token):
            raise ApprovalError(
                "convert_plc_code refused: no valid human approval "
                "token. Ember proposes; a human approves; only "
                "then does this unlock.")
        from .plc_converter import validate_conversion
        clean = validate_conversion(params or {})
        conversion_id = "plc-" + secrets.token_hex(4)
        entry = {
            "id": conversion_id,
            "direction": clean["direction"],
            "direction_label": clean["direction_label"],
            "program_name": clean["program_name"],
            "source_code": clean["source_code"],
            "converted_code": clean["converted_code"],
            "coverage": clean["coverage"],
            "notes": clean["notes"],
            "stats": clean["stats"],
            "manual_context": self._plc_context_stats,
            "status": "draft",
            "rfc_ref": None,
            "change_id": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
            "run_tag": run_tag,
        }
        self.plc_conversions.add(entry)
        stats = clean["stats"]
        return {
            "conversion_id": conversion_id,
            "direction": clean["direction"],
            "direction_label": clean["direction_label"],
            "program_name": clean["program_name"],
            "converted_code": clean["converted_code"],
            "coverage": clean["coverage"],
            "stats": stats,
            "manual_context": self._plc_context_stats,
            "status": "draft",
            "note": (
                f"DRAFT conversion ({clean['direction_label']}): "
                f"{stats['mapped']} of "
                f"{stats['source_instructions']} source "
                "instructions mapped"
                + (f"; flagged unsupported: "
                   + ", ".join(stats["flagged"])
                   if stats["flagged"] else "")
                + ". For qualified-engineer review — not "
                  "safety-certified, nothing deployed."),
        }

    # ------------------------------------------------- Medical Manual Q&A
    def ask_medical_manual(self, question: str) -> dict:
        """Answer a medical question from The Merck Manual of
        Diagnosis & Therapy, 19th Ed. (Nov 27, 2026).

        Deterministic keyword BM25 retrieval over the chunked
        manual corpus (orchestrator/medical_manual.py) — no
        embeddings, no KB search, no Cube-side model. Returns
        the retrieved passages with chapter/section/page
        citations; Ember composes the answer ONLY from those
        passages. covered=False means the manual does not
        cover the question — Ember must say so, never answer
        from general knowledge. Demo reference only: never
        personalized diagnosis/treatment advice (serve.py
        appends the disclaimer deterministically)."""
        from .medical_manual import search_manual
        result = search_manual(str(question or ""))
        result["instruction"] = (
            "Answer ONLY from the passages above, in your own "
            "words (paraphrase — never quote long passages "
            "verbatim). Write as a medical practitioner "
            "assistant: clinical, structured, concise. The "
            "passages are grouped by chapter and the chapters "
            "list names the relevant chapters in order. "
            "Structure the answer as ONE BLOCK PER CHAPTER in "
            "that list (at most 5 blocks): begin each block "
            "with a heading line '## ' followed by the chapter "
            "name exactly as given (e.g. '## Chapter 28. "
            "Hepatitis'), then a concise summary of what that "
            "chapter says relevant to the question — "
            "presentation and signs, differential "
            "considerations, workup and diagnosis, and "
            "management, covering only the aspects the "
            "passages actually address. Cite the chapter, "
            "section, and pages for every claim, e.g. "
            "(Chapter 28. Hepatitis — Diagnosis, pp. 324–325). "
            "If a chapter's passages are only marginally "
            "related to the question, keep its block to one "
            "sentence. When more than one chapter is relevant, "
            "end with a one-or-two-sentence synthesis across "
            "the chapters. Describe what the manual says in "
            "general terms only — never give personalized "
            "diagnosis or treatment recommendations. If "
            "covered is false, say plainly that the manual "
            "does not cover this question and do not answer "
            "from general knowledge.")
        result["note"] = (
            "Demo reference from The Merck Manual of Diagnosis "
            "& Therapy, 19th Ed. — not medical advice.")
        self.last_medical_result = result
        return result

    # ------------------------------------------------ Paralegal Assistant
    def ask_paralegal(self, question: str) -> dict:
        """Answer a legal question from US public-domain legal
        sources (Nov 27, 2026): U.S. Code, Federal Rules, US
        Constitution, CFR.

        Deterministic keyword BM25 retrieval (SQLite FTS5)
        over the chunked legal corpus
        (orchestrator/legal_reference.py) — no embeddings, no
        KB search, no Cube-side model. Returns the retrieved
        passages with per-section/rule citations; Ember
        composes the answer ONLY from those passages.
        covered=False means the sources do not cover the
        question — Ember must say so, never answer from
        general knowledge. Legal information only: never
        legal advice for a specific case (serve.py appends
        the disclaimer deterministically)."""
        from .legal_reference import search_legal
        result = search_legal(str(question or ""))
        result["instruction"] = (
            "Answer ONLY from the passages above, in your own "
            "words (paraphrase — never quote long passages "
            "verbatim). Write as a paralegal assistant: "
            "precise, structured, concise. The passages are "
            "grouped by source/title and the groups list "
            "names the relevant sources in order. Structure "
            "the answer as ONE BLOCK PER GROUP in that list "
            "(at most 5 blocks): begin each block with a "
            "heading line '## ' followed by the group name "
            "exactly as given (e.g. '## Title 28 — Judiciary "
            "and Judicial Procedure' or '## Federal Rules of "
            "Civil Procedure'), then a concise summary of "
            "what that source says relevant to the question "
            "— the rule or provision, its scope, key "
            "requirements, exceptions, and deadlines, "
            "covering only the aspects the passages actually "
            "address. Cite the section or rule for every "
            "claim, e.g. (28 U.S.C. § 1331), (Fed. R. Civ. "
            "P. 26(b)(1)), (U.S. Const. amend. XIV, § 1), "
            "(29 C.F.R. § 1910.95). If a group's passages "
            "are only marginally related to the question, "
            "keep its block to one sentence. When more than "
            "one group is relevant, end with a "
            "one-or-two-sentence synthesis across the "
            "sources. Describe what the law says in general "
            "terms only — never give legal advice for a "
            "specific case or predict how a court would "
            "rule. If covered is false, say plainly that "
            "these sources do not cover this question and do "
            "not answer from general knowledge.")
        result["note"] = (
            "Legal information from US public-domain sources "
            "(U.S. Code, Federal Rules, US Constitution, "
            "CFR) — not legal advice.")
        self.last_legal_result = result
        return result

    def undo_plc_delete(self, conversion_id: str) -> dict:
        entry = self.plc_conversions.get(str(conversion_id or ""))
        if entry is None:
            return {"undone": False,
                    "message": f"conversion {conversion_id!r} is "
                               "not in the registry (already "
                               "removed?)"}
        self.plc_conversions.remove(entry["id"])
        return {"undone": True, "kind": "plc_delete",
                "conversion_id": entry["id"],
                "message": f"Conversion '{entry.get('program_name')}' "
                           f"({entry.get('direction_label')}) "
                           "deleted."}

    def send_plc_change_approval(self, entry: dict) -> dict:
        """Fire the RPA change-approval flow for a stored
        conversion. Backend-specific (Fixture simulates; Http
        fires the real webhook and verifies the RFC)."""
        raise NotImplementedError

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
        if name == "create_agent_flow":
            params = {k: arguments.get(k) for k in
                      ("name", "trigger", "cron", "source", "action",
                       "actions", "condition", "steps", "autonomy")
                      if arguments.get(k) is not None}
            return self.create_agent_flow(
                params, arguments.get("approval_token", ""),
                arguments.get("run_tag", ""))
        if name == "enable_agent_flow":
            return self.enable_agent_flow(
                arguments.get("flow_id", ""),
                arguments.get("approval_token", ""),
                arguments.get("run_tag", ""))
        if name == "file_intake":
            return self.file_intake(arguments.get("record") or {},
                                    arguments.get("approval_token", ""),
                                    arguments.get("run_tag", ""))
        if name == "file_document_report":
            return self.file_document_report(
                arguments.get("report") or {},
                arguments.get("approval_token", ""),
                arguments.get("run_tag", ""))
        if name == "export_file":
            params = {k: arguments.get(k) for k in ("format", "source")
                      if arguments.get(k) is not None}
            return self.export_file(
                params, arguments.get("approval_token", ""),
                arguments.get("run_tag", ""))
        if name == "create_dashboard":
            params = {k: arguments.get(k) for k in
                      ("title", "subtitle", "endpoints", "kpis",
                       "charts", "table", "thresholds",
                       "refresh_frequency")
                      if arguments.get(k) is not None}
            return self.create_dashboard(
                params, arguments.get("approval_token", ""),
                arguments.get("run_tag", ""))
        if name == "convert_plc_code":
            params = {k: arguments.get(k) for k in
                      ("direction", "program_name", "source_code",
                       "converted_code", "coverage", "notes")
                      if arguments.get(k) is not None}
            return self.convert_plc_code(
                params, arguments.get("approval_token", ""),
                arguments.get("run_tag", ""))
        if name == "ask_medical_manual":
            return self.ask_medical_manual(
                arguments.get("question", ""))
        if name == "ask_paralegal":
            return self.ask_paralegal(
                arguments.get("question", ""))
        if name in READ_TOOLS or name in self._extra_read_tools:
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

    def _create_agent(self, clean: dict) -> dict:
        self._seq += 1
        from .agent_builder import chain_summary, condition_label
        if clean.get("steps"):
            steps = chain_summary(clean["steps"])
        else:
            steps = ["fetch " + clean["source"], "compose report"]
            if clean.get("condition"):
                steps.append("condition: " + condition_label(
                    clean["source"], clean["condition"]))
            steps += ["whatsapp notify" if a == "whatsapp"
                      else "file to board" for a in clean["actions"]]
        return {
            "flow_id": f"sim-flow-{self._seq:03d}",
            "name": clean["name"], "status": "DISABLED",
            "trigger": clean["trigger"], "cron": clean["cron"] or None,
            "source": clean["source"], "action": clean["action"],
            "actions": clean["actions"],
            "condition": clean.get("condition"),
            "chain": clean.get("steps"),
            "autonomy": clean.get("autonomy", "propose-only"),
            "steps": steps,
            "note": ("Fixture backend — simulated DISABLED draft, "
                     "nothing was created in RPA."),
        }

    def _enable_agent(self, entry: dict) -> dict:
        return {
            "flow_id": entry["id"], "name": entry.get("name"),
            "status": "ENABLED", "cron": entry.get("cron"),
            "trigger": entry.get("trigger"),
            "note": ("Fixture backend — simulated enablement, "
                     "nothing was published in RPA."),
        }

    def _delete_agent(self, entry: dict) -> dict:
        return {"flow_id": entry["id"], "deleted": True,
                "note": "Fixture backend — simulated delete."}

    def _disable_agent(self, entry: dict) -> dict:
        return {"flow_id": entry["id"], "status": "DISABLED",
                "note": "Fixture backend — simulated disable."}

    # ------------------------------------------------- Dashboard Builder
    # Canned profiles mirroring the live generator shapes (subset).
    # Multi-endpoint profiles prefix every name with its endpoint,
    # exactly like the service's prefixed flatten.
    _FIXTURE_PROFILES = {
        "capacity": {
            "facts": [
                {"name": "host_disk_pct", "type": "number", "value": 69.9},
                {"name": "host_mem_pct", "type": "number", "value": 62.0},
                {"name": "host_cpu_pct", "type": "number", "value": 0.8},
                {"name": "disk_days_to_90", "type": "number", "value": 130.5},
                {"name": "components_up", "type": "number", "value": 28},
                {"name": "components_total", "type": "number", "value": 28},
            ],
            "lists": [
                {"name": "components", "count": 28,
                 "columns": ["component", "name", "up", "uptime_7d_pct",
                             "latency_avg_24h_ms", "incidents_7d"],
                 "sample": [
                     {"component": "stt-service", "name": "STT",
                      "up": True, "uptime_7d_pct": 99.5,
                      "latency_avg_24h_ms": 12.0, "incidents_7d": 0},
                     {"component": "tts-service", "name": "TTS",
                      "up": True, "uptime_7d_pct": 100.0,
                      "latency_avg_24h_ms": 8.0, "incidents_7d": 0}]},
            ],
        },
        "finops": {
            "facts": [
                {"name": "costs_today_usd", "type": "number", "value": 3.63},
                {"name": "costs_month_so_far_usd", "type": "number",
                 "value": 349.92},
                {"name": "costs_month_projected_usd", "type": "number",
                 "value": 388.8},
                {"name": "costs_daily_burn_usd", "type": "number",
                 "value": 12.96},
                {"name": "budget_monthly_usd", "type": "number",
                 "value": 500.0},
                {"name": "budget_projected_pct", "type": "number",
                 "value": 77.8},
            ],
            "lists": [],
        },
        "watcher-overview": {
            "facts": [
                {"name": "totals_up", "type": "number", "value": 28},
                {"name": "totals_components", "type": "number",
                 "value": 28},
                {"name": "totals_open_incidents", "type": "number",
                 "value": 0},
                {"name": "host_disk_pct", "type": "number", "value": 69.9},
                {"name": "host_mem_pct", "type": "number", "value": 62.0},
                {"name": "host_cpu_pct", "type": "number", "value": 0.8},
            ],
            "lists": [
                {"name": "components", "count": 28,
                 "columns": ["component", "name", "up", "latency_ms"],
                 "sample": [
                     {"component": "stt-service", "name": "STT",
                      "up": True, "latency_ms": 12.0},
                     {"component": "tts-service", "name": "TTS",
                      "up": True, "latency_ms": 8.0}]},
            ],
        },
        "predictions": {
            "facts": [
                {"name": "risk_max", "type": "number", "value": 42},
                {"name": "high_count", "type": "number", "value": 0},
                {"name": "watch_count", "type": "number", "value": 3},
            ],
            "lists": [
                {"name": "predictions", "count": 28,
                 "columns": ["name", "risk_score", "band"],
                 "sample": [
                     {"name": "stt-service", "risk_score": 42,
                      "band": "watch"},
                     {"name": "tts-service", "risk_score": 10,
                      "band": "low"}]},
            ],
        },
        "sentiment": {
            "facts": [
                {"name": "total_tickets", "type": "number", "value": 20},
                {"name": "frustrated_count", "type": "number", "value": 2},
            ],
            "lists": [],
        },
        "finclose": {
            "facts": [
                {"name": "kpis_revenue", "type": "number",
                 "value": 1570575.03},
                {"name": "kpis_ebitda", "type": "number",
                 "value": 207487.55},
            ],
            "lists": [],
        },
        "leases": {
            "facts": [
                {"name": "pipeline_value_usd", "type": "number",
                 "value": 44350000},
            ],
            "lists": [
                {"name": "companies", "count": 5,
                 "columns": ["stage", "est_value_usd"],
                 "sample": [
                     {"stage": "target", "est_value_usd": 1000000},
                     {"stage": "prospect", "est_value_usd": 2000000}]},
            ],
        },
        "dotkrc-status": {
            "facts": [
                {"name": "projects", "type": "number", "value": 3470},
            ],
            "lists": [],
        },
        "agents-status": {
            "facts": [
                {"name": "agents_count", "type": "number", "value": 9},
            ],
            "lists": [],
        },
    }

    def _dashboard_profile(self, endpoints: list) -> dict:
        import re as _re
        facts, lists = [], []
        multi = len(endpoints) > 1
        for ep in endpoints:
            prof = self._FIXTURE_PROFILES.get(str(ep))
            if prof is None:
                raise ValueError(f"unknown dashboard source: {ep}")
            prefix = _re.sub(r"\W+", "_", str(ep)) + "_" if multi else ""
            for f in prof["facts"]:
                facts.append({**f, "name": prefix + f["name"]})
            for lst in prof["lists"]:
                lists.append({**lst, "name": prefix + lst["name"]})
        return {"facts": facts, "lists": lists}

    def _create_dashboard(self, clean: dict) -> dict:
        from .dashboard_builder import PUBLIC_BASE, slugify
        base = slugify(clean["title"])
        taken = {d.get("slug") for d in self.dashboards.list()}
        slug, i = base, 2
        while slug in taken:
            slug = f"{base}-{i}"
            i += 1
        return {
            "slug": slug, "title": clean["title"],
            "url": PUBLIC_BASE + "/d/" + slug,
            "endpoints": clean["endpoints"],
            "source_labels": clean["source_labels"],
            "kpi_count": len(clean["kpis"]),
            "chart_count": len(clean["charts"]),
            "has_table": bool(clean.get("table")),
            "threshold_count": len(clean.get("thresholds") or []),
            "refresh_mode": ("rpa-schedule"
                             if clean.get("refresh_frequency")
                             else "manual"),
            "refresh_flow_id": ("sim-refresh-" + slug
                                if clean.get("refresh_frequency")
                                else None),
            "designed_by": "ember",
            "note": ("Fixture backend — simulated dashboard, "
                     "nothing was created in the Dashboard "
                     "Generator."),
        }

    def _delete_dashboard(self, slug: str) -> dict:
        return {"ok": True, "deleted": True, "slug": slug,
                "note": "Fixture backend — simulated delete."}

    def _dashboard_exists(self, slug: str) -> bool:
        return self.dashboards.get(slug) is not None

    def send_plc_change_approval(self, entry: dict) -> dict:
        return {
            "ok": True, "simulated": True,
            "rfc_ref": "RFC-0000",
            "change_id": 0,
            "flow_id": "sim-plc-change-flow",
            "note": ("Fixture backend — simulated change-approval "
                     "routing; no RPA flow fired, no RFC created."),
        }


class HttpCubeTools(CubeTools):
    """Real Cube endpoints (Phase 1+, runs on the Cube).

    Env:
      AIOPS_OVERVIEW_URL  default http://127.0.0.1:8008/api/overview
      AP_WEBHOOK_BASE     default http://127.0.0.1:8085/api/v1/webhooks
      MCP_URL             default http://127.0.0.1:8017/mcp
      CUBE_SERVICES_API_KEY  MCP Bus key; falls back to parsing
                          MCP_ENV_PATH (default /root/metatron/mcp.env)
                          at runtime. The key is never printed or logged.
      MCP_DISCOVERY_TTL_S  discovery cache TTL (default 300; 0 = off,
                          static catalog only — used by offline tests)
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
        self._mcp_key_cache: str | None = None
        self._flow_ids = {w["id"]: w.get("flow_id")
                          for w in self.catalog["workflows"]}
        self.runs: dict[str, dict] = {}
        # Agent Builder (Phase 4): Activepieces API + admin token read
        # from the box env file at request time (never printed/logged).
        self.ap_api_base = os.environ.get(
            "AP_API_BASE", "http://127.0.0.1:8085/api/v1")
        self.ap_project = os.environ.get(
            "AP_PROJECT_ID", "R4KLbLXTv040pofVBOvkz")
        self.ap_token_path = os.environ.get(
            "AP_TOKEN_PATH", "/root/metatron/.ap-admin-token")
        # MCP discovery (Nov 26, 2026): eligible bus tools discovered
        # at startup, cached, refreshed on TTL. None = not (yet)
        # discovered -> static catalog fallback.
        self._discovery_ttl = float(os.environ.get(
            "MCP_DISCOVERY_TTL_S", "300"))
        self._discovered: list[dict] | None = None
        self._discovered_at: float = 0.0
        if self._discovery_ttl > 0:
            self.refresh_discovery()

    # ------------------------------------------------------- discovery
    def refresh_discovery(self) -> list[dict] | None:
        """(Re)discover eligible bus tools. On failure the previous
        state is kept (None at first -> static fallback)."""
        from .mcp_discovery import discover_bus_tools
        found = discover_bus_tools(self.mcp_url, self._mcp_key(),
                                   timeout=5)
        if found is not None:
            self._discovered = found
            self._discovered_at = time.time()
            self._extra_read_tools = {
                s["name"] for s in found} - READ_TOOLS
        return self._discovered

    def tool_schemas(self) -> list[dict]:
        if self._discovery_ttl > 0 and (
                self._discovered is None
                or time.time() - self._discovered_at
                > self._discovery_ttl):
            self.refresh_discovery()
        if not self._discovered:
            return super().tool_schemas()
        from .mcp_discovery import merge_schemas
        return merge_schemas(super().tool_schemas(), self._discovered,
                             LOCAL_ONLY_TOOLS)

    def discovery_state(self) -> dict:
        from .mcp_discovery import EXCLUDED_BUS_TOOLS, LAST_QUARANTINED
        if self._discovered:
            return {"source": "mcp-bus-discovery",
                    "discovered_tools": len(self._discovered),
                    "bus_url": self.mcp_url,
                    "excluded_tools": sorted(EXCLUDED_BUS_TOOLS),
                    "quarantined_tools": list(LAST_QUARANTINED)}
        return {"source": "static-catalog-fallback",
                "discovered_tools": 0,
                "bus_url": self.mcp_url,
                "excluded_tools": sorted(EXCLUDED_BUS_TOOLS),
                "quarantined_tools": list(LAST_QUARANTINED)}

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
        # Every read goes through the MCP Bus (tools/call). The two
        # wrappers that were broken server-side (list_connectors
        # crash on the types dict, get_asset 404 on ref paths) are
        # fixed bus-side as of Nov 26, 2026 — the direct SysOps
        # workarounds that used to live here are removed.
        if name in ("list_tickets", "list_assets", "list_changes"):
            args["limit"] = max(1, min(int(args.get("limit") or 10), 20))
        return shape_read(name, self._mcp_call(name, args))

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
        # "SUCCEEDED" here means the sync webhook answered without an
        # HTTP error; the run status itself is not polled. `confirmed_by`
        # says so, and the verifier words its check accordingly.
        result = {"run_id": run_id, "workflow_id": workflow_id,
                  "status": "SUCCEEDED", "result": out,
                  "confirmed_by": "sync-webhook-response"}
        self.runs[run_id] = result
        return result

    def get_run_status(self, run_id: str) -> dict:
        return self.runs.get(run_id, {"run_id": run_id, "status": "UNKNOWN"})

    # ------------------------------------------------- Agent Builder
    def _ap_token(self) -> str:
        token = os.environ.get("AP_ADMIN_TOKEN", "")
        if not token:
            with open(self.ap_token_path) as f:
                token = f.read().strip()
        return token

    def _create_agent(self, clean: dict) -> dict:
        from .agent_builder import create_agent_flow as _build
        return _build(clean, self.ap_api_base, self._ap_token(),
                      self.ap_project, board_key=self._mcp_key())

    def _enable_agent(self, entry: dict) -> dict:
        from .agent_builder import enable_agent_flow as _enable
        return _enable(entry["id"], self.ap_api_base, self._ap_token())

    def _delete_agent(self, entry: dict) -> dict:
        from .agent_builder import delete_agent_flow as _delete
        return _delete(entry["id"], self.ap_api_base, self._ap_token())

    def _disable_agent(self, entry: dict) -> dict:
        from .agent_builder import disable_agent_flow as _disable
        return _disable(entry["id"], self.ap_api_base,
                        self._ap_token())

    # ------------------------------------------------- Dashboard Builder
    @property
    def dashboards_url(self) -> str:
        return os.environ.get("DASHBOARDS_URL",
                              "http://127.0.0.1:8013")

    def _dashboard_profile(self, endpoints: list) -> dict:
        from .dashboard_builder import fetch_profile
        return fetch_profile(endpoints, self.dashboards_url)

    def _create_dashboard(self, clean: dict) -> dict:
        from .dashboard_builder import create_dashboard as _create
        return _create(clean, self.dashboards_url)

    def _delete_dashboard(self, slug: str) -> dict:
        from .dashboard_builder import delete_dashboard as _delete
        return _delete(slug, self.dashboards_url)

    def _dashboard_exists(self, slug: str) -> bool:
        from .dashboard_builder import dashboard_page_ok
        return dashboard_page_ok(slug, self.dashboards_url)

    # ------------------------------------------------- PLC Code Converter
    @property
    def itsm_url(self) -> str:
        return os.environ.get("ITSM_URL", "http://127.0.0.1:8015")

    @property
    def plc_change_flow_id(self) -> str:
        return os.environ.get("PLC_CHANGE_FLOW_ID", "")

    def send_plc_change_approval(self, entry: dict) -> dict:
        """Fire the 'PLC Conversion — Change Approval' RPA flow
        (webhook) for a stored conversion, then verify the ITSM
        change record (RFC) the flow creates. The flow pauses at
        an approval waitpoint; acceptance happens there, never
        here. Deterministic throughout — no model runs."""
        flow_id = self.plc_change_flow_id
        if not flow_id:
            raise ValueError(
                "PLC change-approval flow is not configured "
                "(PLC_CHANGE_FLOW_ID unset)")
        stats = entry.get("stats") or {}
        payload = {
            "conversion_id": entry.get("id", ""),
            "program_name": entry.get("program_name", ""),
            "direction_label": entry.get("direction_label", ""),
            "title": (f"[PLC] Convert {entry.get('program_name', '')} "
                      f"({entry.get('direction_label', '')})"),
            "description": (
                f"MAC PLC conversion {entry.get('id', '')} "
                f"({entry.get('direction_label', '')}). "
                f"{stats.get('mapped', 0)} of "
                f"{stats.get('source_instructions', 0)} source "
                "instructions mapped"
                + (f"; flagged unsupported: "
                   + ", ".join(stats.get("flagged") or [])
                   if stats.get("flagged") else "")
                + ". Converted code is DRAFT for qualified-"
                  "engineer review; approval here accepts the "
                  "conversion for engineering review only — "
                  "nothing is deployed to any PLC."),
            "change_type": "normal",
            "risk": "medium",
            "component": "plc-converter",
            "created_by": "mac-plc-converter",
        }
        # Async webhook: the flow pauses at the approval
        # waitpoint, so a sync call would block; async returns
        # as soon as the run starts.
        self._post_json(f"{self.webhook_base}/{flow_id}", payload)
        # Verify the RFC the flow creates (deterministic re-read).
        deadline = time.time() + 45
        rfc = None
        while time.time() < deadline:
            try:
                out = self._get_json(self.itsm_url + "/api/changes")
                for ch in out.get("changes") or []:
                    if entry.get("id", "") in str(
                            ch.get("description") or ""):
                        rfc = ch
                        break
            except Exception:  # noqa: BLE001 — keep polling briefly
                pass
            if rfc:
                break
            time.sleep(2)
        if rfc is None:
            raise ValueError(
                "change-approval flow fired but no ITSM change "
                "record appeared within 45s — check the RPA "
                "console run for flow " + flow_id)
        return {
            "ok": True,
            "rfc_ref": rfc.get("ref"),
            "change_id": rfc.get("id"),
            "change_status": rfc.get("status"),
            "flow_id": flow_id,
            "note": ("Change record created and the RPA flow is "
                     "paused at the approval waitpoint — a human "
                     "accepts or rejects there. Nothing deploys."),
        }
