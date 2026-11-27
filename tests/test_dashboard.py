"""Dashboard Builder tests (Nov 27, 2026).

Ember authors the spec; dashboard_builder validates it
deterministically (mirrors the Dashboard Generator's
validate_spec); creation runs through the generator's
deterministic path — never its Axion design step.

Offline, $0: stub Ember + fixture tools + fake auth. No network.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.cube_tools import (ApprovalError, ApprovalTokens,
                                     FixtureCubeTools, GATED_TOOLS,
                                     LOCAL_ONLY_TOOLS, load_catalog)
from orchestrator.dashboard_builder import (build_service_spec,
                                            validate_dashboard_params)
from orchestrator.ember_client import (EmberResponse, StubEmberClient,
                                       ToolCall)
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter
from tests.test_ui import OPERATOR, fake_auth_factory


class DashCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = {}
        for var, fname in (("EMBER_BOARD_PATH", "board.json"),
                           ("EMBER_INTAKE_PATH", "intake.json"),
                           ("EMBER_REPORTS_PATH", "reports.json"),
                           ("EMBER_AGENT_FLOWS_PATH", "agent_flows.json"),
                           ("EMBER_GOALS_PATH", "goals.json"),
                           ("EMBER_PREFS_PATH", "preferences.json"),
                           ("EMBER_EXPORTS_DIR", "exports"),
                           ("EMBER_DASHBOARDS_PATH", "dashboards.json")):
            self._saved[var] = os.environ.get(var)
            os.environ[var] = os.path.join(self._tmp.name, fname)
        self.cap_path = os.path.join(self._tmp.name, "cap.json")
        self.recorded_path = os.path.join(self._tmp.name, "recorded.json")
        self.reports_path = os.path.join(self._tmp.name, "reports.json")

    def tearDown(self):
        for var, val in self._saved.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
        self._tmp.cleanup()

    def make_service(self, client=None, daily_cap=100):
        meter = SpendMeter()
        tokens = ApprovalTokens()
        orch = Orchestrator(client or StubEmberClient(),
                            FixtureCubeTools(tokens), meter, tokens)
        svc = serve.DemoService(
            orch, meter, auth_fn=fake_auth_factory(OPERATOR),
            cap_path=self.cap_path, recorded_path=self.recorded_path,
            daily_cap=daily_cap, reports_path=self.reports_path)
        return svc, orch


CAP_PROFILE = FixtureCubeTools._FIXTURE_PROFILES["capacity"]
MULTI_PROFILE = None  # built per-test via FixtureCubeTools


def fixture_tools():
    return FixtureCubeTools(ApprovalTokens())


# ---------------------------------------------------------- validator
class TestDashboardValidator(unittest.TestCase):
    def test_accepts_single_source(self):
        clean = validate_dashboard_params({
            "title": "Capacity Overview", "endpoints": ["capacity"],
            "kpis": [{"label": "Disk %", "field": "host_disk_pct",
                      "format": "pct"}],
            "charts": [{"type": "bar", "title": "Uptime",
                        "from": "components",
                        "label_field": "component",
                        "value_field": "uptime_7d_pct"}],
            "table": {"title": "Components", "from": "components",
                      "columns": ["component", "up"]},
        }, CAP_PROFILE)
        self.assertEqual(clean["endpoints"], ["capacity"])
        self.assertEqual(len(clean["kpis"]), 1)

    def test_accepts_multi_prefixed(self):
        profile = fixture_tools().dashboard_profile(
            ["capacity", "finops"])
        clean = validate_dashboard_params({
            "title": "Capacity & Costs",
            "endpoints": ["capacity", "finops"],
            "kpis": [{"label": "Disk %",
                      "field": "capacity_host_disk_pct",
                      "format": "pct"},
                     {"label": "Cost today",
                      "field": "finops_costs_today_usd",
                      "format": "money"}],
            "charts": [{"type": "bar", "title": "Uptime",
                        "from": "capacity_components",
                        "label_field": "component",
                        "value_field": "uptime_7d_pct"}],
        }, profile)
        self.assertEqual(clean["endpoints"], ["capacity", "finops"])

    def test_rejects_unprefixed_in_multi(self):
        profile = fixture_tools().dashboard_profile(
            ["capacity", "finops"])
        with self.assertRaises(ValueError):
            validate_dashboard_params({
                "title": "Bad", "endpoints": ["capacity", "finops"],
                "kpis": [{"label": "Disk %", "field": "host_disk_pct"}],
            }, profile)

    def test_rejects_unknown_endpoint(self):
        with self.assertRaises(ValueError):
            validate_dashboard_params({
                "title": "Bad", "endpoints": ["nope"],
                "kpis": [{"label": "X", "field": "y"}],
            }, CAP_PROFILE)

    def test_rejects_unknown_field(self):
        with self.assertRaises(ValueError):
            validate_dashboard_params({
                "title": "Bad", "endpoints": ["capacity"],
                "kpis": [{"label": "X", "field": "made_up_field"}],
            }, CAP_PROFILE)

    def test_rejects_empty_spec(self):
        with self.assertRaises(ValueError):
            validate_dashboard_params({
                "title": "Empty", "endpoints": ["capacity"],
            }, CAP_PROFILE)

    def test_rejects_bad_chart_type(self):
        with self.assertRaises(ValueError):
            validate_dashboard_params({
                "title": "Bad", "endpoints": ["capacity"],
                "kpis": [{"label": "Disk %", "field": "host_disk_pct"}],
                "charts": [{"type": "pie", "from": "components",
                            "label_field": "component",
                            "value_field": "uptime_7d_pct"}],
            }, CAP_PROFILE)

    def test_rejects_caps(self):
        with self.assertRaises(ValueError):
            validate_dashboard_params({
                "title": "Bad", "endpoints": ["capacity"],
                "kpis": [{"label": f"K{i}", "field": "host_disk_pct"}
                         for i in range(9)],
            }, CAP_PROFILE)
        with self.assertRaises(ValueError):
            validate_dashboard_params({
                "title": "Bad",
                "endpoints": ["capacity", "finops", "predictions",
                              "sentiment"],
                "kpis": [{"label": "X", "field": "host_disk_pct"}],
            }, CAP_PROFILE)

    def test_service_spec_disables_ai(self):
        clean = validate_dashboard_params({
            "title": "Capacity Overview", "endpoints": ["capacity"],
            "kpis": [{"label": "Disk %", "field": "host_disk_pct"}],
        }, CAP_PROFILE)
        spec = build_service_spec(clean)
        self.assertFalse(spec["insight"])
        self.assertFalse(spec["chat"])
        self.assertEqual(spec["designed_by"], "ember")


# ---------------------------------------------------------- gated tool
class TestCreateDashboardTool(DashCase):
    def test_gated_and_local(self):
        self.assertIn("create_dashboard", GATED_TOOLS)
        self.assertIn("create_dashboard", LOCAL_ONLY_TOOLS)

    def test_requires_token(self):
        tools = fixture_tools()
        with self.assertRaises(ApprovalError):
            tools.create_dashboard({
                "title": "Capacity Overview",
                "endpoints": ["capacity"],
                "kpis": [{"label": "Disk %",
                          "field": "host_disk_pct"}],
            }, approval_token="bogus")

    def test_create_registers_and_verifies(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Build a dashboard showing capacity and FinOps costs.",
            "c")
        self.assertEqual(code, 200, plan)
        self.assertEqual(plan["steps"][0]["name"], "create_dashboard")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200, done)
        result = done["trace"][0]["result"]
        self.assertTrue(result["url"].startswith(
            "https://dashboards.metatroncube.online/d/"))
        self.assertEqual(result["designed_by"], "ember")
        entry = orch.tools.dashboards.get(result["slug"])
        self.assertIsNotNone(entry)
        ver = done.get("verification") or []
        dash_checks = [c for c in ver
                       if c.get("name") == "create_dashboard"]
        self.assertTrue(dash_checks and all(
            c.get("ok") for c in dash_checks), ver)

    def test_undo_registry_scoped(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Build a dashboard showing capacity.", "c")
        code, done = svc.approve(plan["id"], "c")
        slug = done["trace"][0]["result"]["slug"]
        out = orch.tools.undo_dashboard_delete(slug)
        self.assertTrue(out["undone"])
        self.assertIsNone(orch.tools.dashboards.get(slug))
        out2 = orch.tools.undo_dashboard_delete(slug)
        self.assertFalse(out2["undone"])
        out3 = orch.tools.undo_dashboard_delete("watcher-operations")
        self.assertFalse(out3["undone"])


# ---------------------------------------------------------- serve path
class _BadSpecClient(StubEmberClient):
    """Emits an invalid dashboard spec N times, then delegates."""

    def __init__(self, bad_times):
        self.bad_times = bad_times
        self.calls = 0

    def _plan(self, user, prompt_text):
        self.calls += 1
        if self.calls <= self.bad_times:
            return EmberResponse(
                text="Plan: dashboard.",
                tool_calls=[ToolCall("create_dashboard", {
                    "title": "Bad", "endpoints": ["capacity"],
                    "kpis": [{"label": "X",
                              "field": "invented_field"}]})],
                prompt_tokens=10, completion_tokens=10)
        return super()._plan(user, prompt_text)


class TestServeDashboardPath(DashCase):
    def test_command_detection(self):
        self.assertTrue(serve.is_dashboard_build_command(
            "Build a dashboard showing capacity and costs"))
        self.assertTrue(serve.is_dashboard_build_command(
            "Create a new dashboard for FinOps"))
        self.assertFalse(serve.is_dashboard_build_command(
            "List the dashboards."))
        self.assertFalse(serve.is_dashboard_build_command(
            "What is the Watcher status?"))

    def test_repair_retry_recovers(self):
        client = _BadSpecClient(bad_times=1)
        svc, orch = self.make_service(client=client)
        code, plan = svc.propose(
            "Build a dashboard showing capacity.", "c")
        self.assertEqual(code, 200, plan)
        self.assertEqual(plan["steps"][0]["name"], "create_dashboard")
        self.assertEqual(client.calls, 2)

    def test_repair_exhaustion_is_422(self):
        client = _BadSpecClient(bad_times=5)
        svc, orch = self.make_service(client=client)
        code, out = svc.propose(
            "Build a dashboard showing capacity.", "c")
        self.assertEqual(code, 422, out)
        self.assertIn("validator", out["error"])
        self.assertEqual(client.calls, 2)  # one retry, then stop
        self.assertEqual(orch.tools.dashboards.list(), [])

    def test_undo_via_service(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Build a dashboard showing capacity.", "c")
        code, done = svc.approve(plan["id"], "c")
        slug = done["trace"][0]["result"]["slug"]
        code, out = svc.undo(
            {"kind": "dashboard_delete", "id": slug}, "c")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["undone"])
        self.assertIsNone(orch.tools.dashboards.get(slug))

    def test_capabilities_include_create_dashboard(self):
        svc, orch = self.make_service()
        code, out = svc.propose("What can you do?", "c")
        self.assertEqual(code, 200)
        names = [t["name"] for g in out["groups"] for t in g["tools"]]
        self.assertIn("create_dashboard", names)
        build = next(g for g in out["groups"]
                     if g["area"] == "Build & Automate")
        tool = next(t for t in build["tools"]
                    if t["name"] == "create_dashboard")
        self.assertIn("dashboard", tool["example"].lower())
        self.assertTrue(tool["gated"])


# ---------------------------------------------------------- catalog
class TestCatalog(unittest.TestCase):
    def test_thirty_two_tools_and_budget(self):
        cat = load_catalog()
        # 30 after the Dashboard Builder (Nov 27); 31 after the
        # PLC Code Converter (Nov 27); 32 after Medical Manual
        # Q&A (Nov 27); 33 after the Paralegal Assistant
        # (Nov 27).
        self.assertEqual(len(cat["tools"]), 33)
        total = sum(len(t["description"]) for t in cat["tools"])
        self.assertLess(total // 4, 600)
        tool = next(t for t in cat["tools"]
                    if t["name"] == "create_dashboard")
        self.assertEqual(
            tool["parameters"]["properties"]["endpoints"]
                ["items"]["enum"],
            ["watcher-overview", "capacity", "predictions",
             "finops", "leases", "sentiment", "finclose",
             "dotkrc-status", "agents-status"])

    def test_page_markers(self):
        page = open(os.path.join(
            os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "demo_page.html")).read()
        for marker in ("dashboardStepPlain", "dashboard_delete",
                       "Build a dashboard", "dashboard-builder",
                       "DASH_SOURCE_LABELS"):
            self.assertIn(marker, page)

    def test_create_dashboard_chip(self):
        # Rom (Nov 27): a "Create a dashboard" prompt suggestion
        # under the composer, connected to dashboard creation.
        page = open(os.path.join(
            os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "demo_page.html")).read()
        self.assertIn('chipLabel:"Create a dashboard"', page)
        i = page.index('chipCmd:"') + len('chipCmd:"')
        chip_cmd = page[i:page.index('"', i)]
        # The loaded prompt keeps the proven detector phrasing so
        # CHECK routes into the create_dashboard proposal path.
        self.assertIn("Build a dashboard", chip_cmd)
        self.assertTrue(serve.is_dashboard_build_command(chip_cmd))
        # The chip loop honors the label/text overrides (load into
        # the composer, no auto-run — same as the other chips).
        self.assertIn("d.chipLabel||", page)
        self.assertIn("d.chipCmd||d.cmd", page)


if __name__ == "__main__":
    unittest.main()
