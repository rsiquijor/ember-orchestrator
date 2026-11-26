"""Phase 3 tests — MCP Bus read tools, trimming, stub demos, refusal.

Stub client + fixture tools, $0, offline. Board writes go to a temp file.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.cube_tools import (READ_TOOLS, ApprovalTokens,
                                     FixtureCubeTools, _trim, load_catalog,
                                     shape_read)
from orchestrator.ember_client import StubEmberClient
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter

EXCLUDED = {"create_ticket", "knowledge_search", "ask_cube"}


class CountingTools(FixtureCubeTools):
    """Fixture tools that count executions, to prove the approval gate."""

    def __init__(self, tokens):
        super().__init__(tokens)
        self.calls = 0

    def call(self, name, arguments):
        self.calls += 1
        return super().call(name, arguments)


class BoardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("EMBER_BOARD_PATH")
        os.environ["EMBER_BOARD_PATH"] = os.path.join(self._tmp.name,
                                                      "board.json")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("EMBER_BOARD_PATH", None)
        else:
            os.environ["EMBER_BOARD_PATH"] = self._old
        self._tmp.cleanup()


class TestCatalog(unittest.TestCase):
    def test_all_eligible_read_tools_present(self):
        names = {t["name"] for t in load_catalog()["tools"]}
        self.assertTrue(READ_TOOLS <= names)
        self.assertEqual(len(READ_TOOLS), 19)

    def test_excluded_tools_absent(self):
        names = {t["name"] for t in load_catalog()["tools"]}
        self.assertFalse(EXCLUDED & names)
        self.assertFalse(EXCLUDED & READ_TOOLS)

    def test_descriptions_terse(self):
        total = sum(len(t["description"]) for t in load_catalog()["tools"])
        self.assertLess(total // 4, 600)  # ~tokens of description text

    def test_four_new_workflows_listed(self):
        ids = {w["id"] for w in load_catalog()["workflows"]}
        for wid in ("finops-summary-report", "blast-radius-report",
                    "cve-exposure-report", "capacity-outlook-report"):
            self.assertIn(wid, ids)


class TestTrimming(unittest.TestCase):
    def test_generic_trim_caps(self):
        big = {"items": [{"x": "y" * 500} for _ in range(50)],
               "nested": {"a": {"b": {"c": {"d": {"e": 1}}}}}}
        out = _trim(big)
        self.assertEqual(len(out["items"]), 10)
        self.assertEqual(len(out["items"][0]["x"]), 160)

    def test_secops_shape(self):
        raw = {"kpis": {"critical": 1},
               "priority": [{"id": f"CVE-{i}", "severity": "CRITICAL",
                             "cvss": 9.8, "description": "d" * 400,
                             "vector": "drop-me"} for i in range(12)],
               "sources": {"nvd": {"ok": True, "count": 154}},
               "estate": {"components": 27, "up": 27,
                          "open_incidents": 0, "list": [{"name": "x"}]}}
        out = shape_read("secops_status", raw)
        self.assertEqual(len(out["priority_top5"]), 5)
        self.assertLessEqual(len(out["priority_top5"][0]["description"]), 140)
        self.assertNotIn("vector", out["priority_top5"][0])
        self.assertNotIn("list", out["estate"])

    def test_predictions_sorted_and_capped(self):
        raw = {"predictions": [{"component": f"c{i}", "risk_score": i,
                                "reasons": ["r" * 200] * 5}
                               for i in range(20)],
               "risk_max": 19}
        out = shape_read("predictions", raw)
        self.assertEqual(len(out["top_risks"]), 8)
        self.assertEqual(out["top_risks"][0]["risk_score"], 19)
        self.assertLessEqual(len(out["top_risks"][0]["reasons"]), 3)
        self.assertLessEqual(len(out["top_risks"][0]["reasons"][0]), 100)

    def test_capacity_worst_first(self):
        raw = {"components": [
            {"component": "good", "uptime_7d_pct": 100.0,
             "latency_slope_ms_day": 0.0},
            {"component": "bad", "uptime_7d_pct": 90.0,
             "latency_slope_ms_day": 5.0}],
            "host": {"current": {"disk_pct": 69.8}}}
        out = shape_read("capacity_report", raw)
        self.assertEqual(out["worst_components"][0]["component"], "bad")

    def test_tickets_fields_picked(self):
        raw = {"count": 1, "tickets": [{"ref": "ITSM-0001", "title": "t",
                                        "status": "new", "priority": "P2",
                                        "category": "Network",
                                        "description": "secret-ish body",
                                        "sentiment_signals": ["x"]}]}
        out = shape_read("list_tickets", raw)
        self.assertNotIn("description", out["tickets"][0])
        self.assertNotIn("sentiment_signals", out["tickets"][0])


class TestFixtureReads(unittest.TestCase):
    def test_all_read_tools_have_fixtures(self):
        tools = FixtureCubeTools(ApprovalTokens())
        for name in READ_TOOLS:
            out = tools.call(name, {"ref": "CI-0002",
                                    "component": "model-endpoint",
                                    "q": "brain"})
            self.assertIsInstance(out, dict, name)
            self.assertTrue(out, name)

    def test_fixture_echoes_selectors(self):
        tools = FixtureCubeTools(ApprovalTokens())
        out = tools.call("graph_dependents", {"component": "stt-service"})
        self.assertEqual(out["component"], "stt-service")
        out = tools.call("get_asset", {"ref": "CI-0007"})
        self.assertEqual(out["asset"]["ref"], "CI-0007")

    def test_unknown_tool_rejected(self):
        tools = FixtureCubeTools(ApprovalTokens())
        with self.assertRaises(ValueError):
            tools.call("create_ticket", {"title": "x"})
        with self.assertRaises(ValueError):
            tools.call("ask_cube", {"prompt": "x"})


class TestApprovalGate(BoardTestCase):
    def test_propose_executes_nothing(self):
        tokens = ApprovalTokens()
        tools = CountingTools(tokens)
        orch = Orchestrator(StubEmberClient(), tools, SpendMeter(), tokens)
        plan = orch.propose("What is the Cube costing us this month?")
        self.assertEqual(tools.calls, 0)
        self.assertEqual([s["name"] for s in plan["steps"]],
                         ["finops_summary"])
        done = orch.approve(plan["id"])
        self.assertGreaterEqual(tools.calls, 1)
        self.assertEqual(done["status"], "executed")


class TestStubDemos(BoardTestCase):
    def _run(self, prompt):
        tokens = ApprovalTokens()
        orch = Orchestrator(StubEmberClient(), FixtureCubeTools(tokens),
                            SpendMeter(), tokens)
        plan = orch.propose(prompt)
        return orch.approve(plan["id"])

    def test_finops_demo(self):
        done = self._run("What is the Cube costing us this month?")
        self.assertIn("388.8", done["final_text"])
        self.assertIn("77.8", done["final_text"])

    def test_blast_radius_demo(self):
        done = self._run("What breaks if the brain goes down?")
        self.assertIn("model-endpoint", done["final_text"])
        self.assertIn("20", done["final_text"])

    def test_cve_demo(self):
        done = self._run("Any critical CVEs we should worry about?")
        self.assertIn("CVE-2026-0001", done["final_text"])
        self.assertIn("no investigation was run", done["final_text"])

    def test_capacity_demo(self):
        done = self._run("How is our capacity looking?")
        self.assertIn("69.8", done["final_text"])
        self.assertIn("Quality Harness", done["final_text"])


class TestRefusal(BoardTestCase):
    def test_refusal_proposes_no_reads(self):
        tokens = ApprovalTokens()
        orch = Orchestrator(StubEmberClient(), FixtureCubeTools(tokens),
                            SpendMeter(), tokens)
        plan = orch.propose(
            "Order a pizza for the office and book a flight to Denver.")
        names = {s["name"] for s in plan["steps"]}
        self.assertFalse(names & READ_TOOLS)
        self.assertFalse(names & {"run_workflow", "file_tickets"})


if __name__ == "__main__":
    unittest.main()
