"""MCP wiring tests (Nov 26, 2026) — Direction 1 + 2.

Direction 1: Ember discovers eligible read tools from the Metatron
MCP Bus (tools/list -> Ember schemas), eligibility-filtered, merged
over the static catalog with a static fallback on any failure.
Direction 2: the bus-facing service surface on serve.py (services
key, propose-only + reads; approval stays in the UI).

Offline, $0: fake bus payloads + stub Ember + fixture tools.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.cube_tools import (LOCAL_ONLY_TOOLS, READ_TOOLS,
                                     ApprovalTokens, FixtureCubeTools,
                                     HttpCubeTools, load_catalog)
from orchestrator.ember_client import StubEmberClient
from orchestrator.mcp_discovery import (EXCLUDED_BUS_TOOLS,
                                        discover_bus_tools, merge_schemas)
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter
from tests.test_ui import UiTestCase

# A bus tools/list payload shaped like the real bus (22 tools + the
# three ember_* Direction-2 tools), trimmed to the interesting ones.
FAKE_BUS_TOOLS = [
    {"name": "cube_status", "description": "Live Cube estate status.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "capacity_report", "description": "Capacity report.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_asset", "description": "Get one CMDB asset by ref.",
     "inputSchema": {"type": "object",
                     "properties": {"ref": {"type": "string"}},
                     "required": ["ref"]}},
    {"name": "list_connectors", "description": "CMDB connectors.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "graph_search", "description": "Search the graph.",
     "inputSchema": {"type": "object",
                     "properties": {"q": {"type": "string"}},
                     "required": []}},  # bus heuristic: q not required
    {"name": "create_ticket", "description": "Create a ticket.",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string"}},
                     "required": ["title"]}},
    {"name": "knowledge_search", "description": "RAG search.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "ask_cube", "description": "Ask the brain.",
     "inputSchema": {"type": "object",
                     "properties": {"prompt": {"type": "string"}},
                     "required": ["prompt"]}},
    {"name": "ember_status", "description": "Ember status.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "ember_propose", "description": "Ember propose.",
     "inputSchema": {"type": "object",
                     "properties": {"command": {"type": "string"}},
                     "required": ["command"]}},
    {"name": "ember_list_automations", "description": "Ember registry.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "future_bus_tool", "description": "A tool added later.",
     "inputSchema": {"type": "object",
                     "properties": {"x": {"type": "integer"}},
                     "required": []},
     "annotations": {"readOnlyHint": True}},
    # A later bus tool that is NOT marked read-only: it would run
    # without an approval token, so discovery must quarantine it.
    {"name": "future_write_tool", "description": "Restart a service.",
     "inputSchema": {"type": "object",
                     "properties": {"svc": {"type": "string"}},
                     "required": ["svc"]}},
    {"name": "future_destructive_tool", "description": "Delete things.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "destructiveHint": True}},
]


def fake_fetch(url, key, timeout):
    return FAKE_BUS_TOOLS


def failing_fetch(url, key, timeout):
    raise ConnectionError("bus down")


class TestDiscoveryFiltering(unittest.TestCase):
    def test_exclusions_and_self_tools_filtered(self):
        schemas = discover_bus_tools("http://bus/mcp", "k",
                                     fetch_fn=fake_fetch)
        names = {s["name"] for s in schemas}
        self.assertFalse(names & EXCLUDED_BUS_TOOLS)
        self.assertFalse(any(n.startswith("ember_") for n in names))
        self.assertIn("capacity_report", names)
        self.assertIn("future_bus_tool", names)

    def test_unannotated_unknown_tools_quarantined(self):
        import orchestrator.mcp_discovery as md
        schemas = discover_bus_tools("http://bus/mcp", "k",
                                     fetch_fn=fake_fetch)
        names = {s["name"] for s in schemas}
        self.assertNotIn("future_write_tool", names)
        self.assertNotIn("future_destructive_tool", names)
        self.assertEqual(md.LAST_QUARANTINED,
                         ["future_destructive_tool", "future_write_tool"])
        # Known reads are admitted by name even without annotations.
        self.assertIn("cube_status", names)

    def test_failure_returns_none(self):
        self.assertIsNone(discover_bus_tools("http://bus/mcp", "k",
                                             fetch_fn=failing_fetch))

    def test_schema_shape(self):
        schemas = discover_bus_tools("http://bus/mcp", "k",
                                     fetch_fn=fake_fetch)
        by_name = {s["name"]: s for s in schemas}
        self.assertEqual(by_name["get_asset"]["parameters"]["required"],
                         ["ref"])
        self.assertEqual(by_name["capacity_report"]["description"],
                         "Capacity report.")


class TestMerge(unittest.TestCase):
    def _local(self):
        return [{"name": t["name"], "description": t["description"],
                 "parameters": t["parameters"]}
                for t in load_catalog()["tools"]]

    def test_merge_keeps_local_and_adds_discovered(self):
        discovered = discover_bus_tools("u", "k", fetch_fn=fake_fetch)
        merged = merge_schemas(self._local(), discovered,
                               LOCAL_ONLY_TOOLS)
        names = [s["name"] for s in merged]
        # All 29 catalog tools still present, plus the future tool.
        catalog_names = {t["name"] for t in load_catalog()["tools"]}
        self.assertTrue(catalog_names <= set(names))
        self.assertIn("future_bus_tool", names)
        self.assertEqual(len(names), len(set(names)))  # no dupes

    def test_merge_unions_required(self):
        discovered = discover_bus_tools("u", "k", fetch_fn=fake_fetch)
        merged = merge_schemas(self._local(), discovered,
                               LOCAL_ONLY_TOOLS)
        graph = next(s for s in merged if s["name"] == "graph_search")
        # Bus heuristic left q optional; the catalog requires it.
        self.assertIn("q", graph["parameters"]["required"])

    def test_local_only_tools_keep_catalog_schema(self):
        discovered = discover_bus_tools("u", "k", fetch_fn=fake_fetch)
        merged = merge_schemas(self._local(), discovered,
                               LOCAL_ONLY_TOOLS)
        local = {t["name"]: t for t in self._local()}
        for name in LOCAL_ONLY_TOOLS:
            got = next(s for s in merged if s["name"] == name)
            self.assertEqual(got["parameters"],
                             local[name]["parameters"], name)


class TestHttpToolsDiscovery(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("MCP_DISCOVERY_TTL_S")
        os.environ["MCP_DISCOVERY_TTL_S"] = "0"  # no network in init

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MCP_DISCOVERY_TTL_S", None)
        else:
            os.environ["MCP_DISCOVERY_TTL_S"] = self._old

    def _tools_with_discovery(self):
        tools = HttpCubeTools(ApprovalTokens())
        # Inject a discovery result without network.
        import orchestrator.mcp_discovery as md
        tools._discovered = md.discover_bus_tools(
            "u", "k", fetch_fn=fake_fetch)
        tools._extra_read_tools = {s["name"] for s in tools._discovered} \
            - READ_TOOLS
        return tools

    def test_schemas_include_discovered_exclude_forbidden(self):
        tools = self._tools_with_discovery()
        names = {s["name"] for s in tools.tool_schemas()}
        self.assertFalse(names & EXCLUDED_BUS_TOOLS)
        self.assertFalse(any(n.startswith("ember_") for n in names))
        self.assertIn("future_bus_tool", names)
        # Gated local tools still there.
        self.assertIn("create_agent_flow", names)
        self.assertIn("export_file", names)

    def test_fallback_is_static_catalog(self):
        tools = HttpCubeTools(ApprovalTokens())
        names = {s["name"] for s in tools.tool_schemas()}
        catalog_names = {t["name"] for t in load_catalog()["tools"]}
        self.assertEqual(names, catalog_names)
        self.assertEqual(tools.discovery_state()["source"],
                         "static-catalog-fallback")

    def test_reads_route_through_bus(self):
        tools = self._tools_with_discovery()
        calls = []

        def fake_mcp(name, args):
            calls.append((name, args))
            if name == "list_connectors":
                return {"connectors": [{"name": "SN", "type": "x",
                                        "status": "ok"}],
                        "types": ["servicenow"]}
            if name == "get_asset":
                return {"asset": {"ref": "CI-0001", "name": "Cube"},
                        "depends": [], "dependents": []}
            return {"ok": True}

        tools._mcp_call = fake_mcp
        out = tools.call("list_connectors", {})
        self.assertEqual(calls[-1][0], "list_connectors")
        self.assertEqual(out["types"], ["servicenow"])
        out = tools.call("get_asset", {"ref": "CI-0001"})
        self.assertEqual(calls[-1][0], "get_asset")
        self.assertEqual(out["asset"]["ref"], "CI-0001")
        # A discovered-only tool dispatches through the bus too.
        tools.call("future_bus_tool", {"x": 1})
        self.assertEqual(calls[-1][0], "future_bus_tool")
        # A quarantined tool is not callable at all.
        with self.assertRaises(ValueError):
            tools.call("future_write_tool", {"svc": "db"})
        self.assertNotEqual(calls[-1][0], "future_write_tool")
        self.assertIn("future_write_tool",
                      tools.discovery_state()["quarantined_tools"])

    def test_orchestrator_uses_effective_schemas(self):
        tools = self._tools_with_discovery()
        orch = Orchestrator(StubEmberClient(), tools, SpendMeter(),
                            tools.tokens)
        names = {s["name"] for s in orch.tool_schemas}
        self.assertIn("future_bus_tool", names)
        self.assertFalse(names & EXCLUDED_BUS_TOOLS)


class TestServiceSurface(UiTestCase):
    """Direction 2: services-key propose/status/automations."""

    KEY = "test-services-key"

    def setUp(self):
        super().setUp()
        self._old_key = os.environ.get("CUBE_SERVICES_API_KEY")
        os.environ["CUBE_SERVICES_API_KEY"] = self.KEY

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("CUBE_SERVICES_API_KEY", None)
        else:
            os.environ["CUBE_SERVICES_API_KEY"] = self._old_key
        super().tearDown()

    def test_status_via_service(self):
        svc = self.make_service()
        code, body = svc.status_via_service(self.KEY)
        self.assertEqual(code, 200)
        self.assertIn("meter", body)
        self.assertIn("tool_count", body)
        self.assertIn("mcp_bus", body)
        code, _ = svc.status_via_service("wrong-key")
        self.assertEqual(code, 401)

    def test_automations_via_service(self):
        svc = self.make_service()
        code, body = svc.automations_via_service(self.KEY)
        self.assertEqual(code, 200)
        # Shape only: the box registry holds real automations, the
        # local one may be empty — both are correct.
        self.assertIsInstance(body["flows"], list)
        for flow in body["flows"]:
            for field in ("id", "name", "enabled", "autonomy"):
                self.assertIn(field, flow)
        code, _ = svc.automations_via_service("")
        self.assertEqual(code, 401)

    def test_propose_via_service_executes_nothing(self):
        svc = self.make_service()
        code, body = svc.propose_via_service(
            "What is the Cube costing us this month?", self.KEY)
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "proposed")
        self.assertFalse(body["executed"])
        self.assertEqual(body["via"], "mcp-bus")
        self.assertTrue(body["steps"])
        # The plan exists but nothing ran: no trace, status proposed.
        plan = svc.orch.plans[body["id"]]
        self.assertEqual(plan["status"], "proposed")
        self.assertNotIn("trace", plan)
        # And the approval path is NOT reachable via the key: there
        # is no service-key approve method at all.
        self.assertFalse(hasattr(svc, "approve_via_service"))

    def test_propose_via_service_wrong_key(self):
        svc = self.make_service()
        code, _ = svc.propose_via_service("Check capacity", "nope")
        self.assertEqual(code, 401)

    def test_capabilities_reflect_discovered_set(self):
        discovered = discover_bus_tools("u", "k", fetch_fn=fake_fetch)
        local = [{"name": t["name"], "description": t["description"],
                  "parameters": t["parameters"]}
                 for t in load_catalog()["tools"]]
        merged = merge_schemas(local, discovered, LOCAL_ONLY_TOOLS)
        payload = serve.capabilities_payload(merged)
        names = {t["name"] for g in payload["groups"]
                 for t in g["tools"]}
        self.assertIn("future_bus_tool", names)
        self.assertFalse(names & EXCLUDED_BUS_TOOLS)
        self.assertFalse(any(n.startswith("ember_") for n in names))
        self.assertEqual(payload["tool_count"], len(merged))


if __name__ == "__main__":
    unittest.main()
