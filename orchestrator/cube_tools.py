"""Cube Tool API — the only hands the orchestrator has.

Four tools (see tools/catalog.json). Every workflow behind them is a
deterministic Activepieces flow on the Metatron Cube: Ember proposes,
a human approves, the flow executes. run_workflow refuses to run without
a single-use approval token issued by the orchestrator's approval step.

Phase 0 backends:
  - FixtureCubeTools — demo data from fixtures/, zero side effects.
  - HttpCubeTools    — the real Cube endpoints (AIOps /api/overview +
    Activepieces sync webhooks), configured by environment; used from
    Phase 1 on the Cube itself.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tools with side effects. The orchestrator's approval step mints a
# single-use token for each gated step; Ember never sees the token.
GATED_TOOLS = {"run_workflow", "file_tickets"}

TICKET_CATEGORIES = {"Network", "Billing", "Access", "Hardware",
                     "Account", "Software", "Other"}
TICKET_PRIORITIES = {"P1", "P2", "P3", "P4"}


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
        raise ValueError(f"unknown tool: {name}")


class FixtureCubeTools(CubeTools):
    """Phase 0 default: fixture data, simulated runs, no side effects."""

    def __init__(self, tokens: ApprovalTokens):
        super().__init__(tokens)
        with open(os.path.join(ROOT, "fixtures", "handover-sample.json")) as f:
            self.fixture = json.load(f)
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
    Workflow ids map to Activepieces flow ids via the catalog's flow_id.
    """

    def __init__(self, tokens: ApprovalTokens):
        super().__init__(tokens)
        self.overview_url = os.environ.get(
            "AIOPS_OVERVIEW_URL", "http://127.0.0.1:8008/api/overview")
        self.webhook_base = os.environ.get(
            "AP_WEBHOOK_BASE", "http://127.0.0.1:8085/api/v1/webhooks")
        self._flow_ids = {w["id"]: w.get("flow_id")
                          for w in self.catalog["workflows"]}
        self.runs: dict[str, dict] = {}

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
