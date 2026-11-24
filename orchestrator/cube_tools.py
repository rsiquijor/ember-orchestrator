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
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        return {"line": line, "components_up": up, "components_total": total,
                "open_incidents": open_inc}

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
