"""Phase 0 smoke tests — stub Ember + fixture Cube tools, $0, offline.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.cube_tools import (ApprovalError, ApprovalTokens,
                                     FixtureCubeTools)
from orchestrator.ember_client import StubEmberClient
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import BudgetHalt, BudgetStop, SpendMeter


def make_orchestrator(meter=None):
    tokens = ApprovalTokens()
    return Orchestrator(StubEmberClient(), FixtureCubeTools(tokens),
                        meter or SpendMeter(), tokens)


class TestApprovalGate(unittest.TestCase):
    def test_propose_executes_nothing(self):
        orch = make_orchestrator()
        plan = orch.propose("Send the Watcher status to WhatsApp")
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual(len(orch.tools.runs), 0)

    def test_approve_executes_and_summarizes(self):
        orch = make_orchestrator()
        plan = orch.propose("Send the Watcher status to WhatsApp")
        done = orch.approve(plan["id"])
        self.assertEqual(done["status"], "executed")
        run = [t for t in done["trace"] if t["tool"] == "run_workflow"][0]
        self.assertEqual(run["result"]["status"], "SUCCEEDED")
        self.assertIn("12/12 components up", done["final_text"])

    def test_run_workflow_without_token_refused(self):
        tools = FixtureCubeTools(ApprovalTokens())
        with self.assertRaises(ApprovalError):
            tools.run_workflow("watcher-status-whatsapp", {}, approval_token="bogus")

    def test_token_is_single_use(self):
        tokens = ApprovalTokens()
        tools = FixtureCubeTools(tokens)
        token = tokens.issue()
        tools.run_workflow("watcher-status-whatsapp", {}, approval_token=token)
        with self.assertRaises(ApprovalError):
            tools.run_workflow("watcher-status-whatsapp", {}, approval_token=token)

    def test_handover_demo(self):
        orch = make_orchestrator()
        plan = orch.propose("Draft the shift handover brief")
        done = orch.approve(plan["id"])
        self.assertIn("SHIFT HANDOVER BRIEF", done["final_text"])


class TestSpendMeter(unittest.TestCase):
    def test_ledger_and_exports(self):
        meter = SpendMeter(rate_input_per_1k=0.01, rate_output_per_1k=0.03)
        meter.record("experiment", 1000, 500, note="t")
        self.assertAlmostEqual(meter.spent_usd, 0.025, places=6)
        csv_text = meter.export_csv()
        self.assertIn("purpose,prompt_tokens", csv_text.splitlines()[0].replace("ts,", ""))
        self.assertEqual(len(meter.ledger), 1)
        self.assertIn('"spent_usd": 0.025', meter.export_json())

    def test_gates(self):
        meter = SpendMeter(rate_input_per_1k=1.0, rate_output_per_1k=1.0)
        meter.record("experiment", 9100, 0)  # $9.10 spent
        with self.assertRaises(BudgetHalt):
            meter.authorize("rehearsal")
        meter.authorize("final")  # essential purpose still allowed
        meter.record("final", 700, 0)  # $9.80 spent
        with self.assertRaises(BudgetStop):
            meter.authorize("final")

    def test_unset_rate_costs_zero(self):
        meter = SpendMeter()
        meter.record("experiment", 100000, 100000)
        self.assertEqual(meter.spent_usd, 0.0)


if __name__ == "__main__":
    unittest.main()
