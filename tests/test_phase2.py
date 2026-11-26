"""Phase 2 tests — triage filing gate, board store, integration purpose.

Stub client + fixture tools, $0, offline. Board writes go to a temp file.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.cube_tools import (ApprovalError, ApprovalTokens,
                                     FixtureCubeTools)
from orchestrator.ember_client import StubEmberClient
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter

GOOD_TICKET = {"id": "TKT-001", "category": "Network", "priority": "P2",
               "draft_reply": "We're on it.", "route": "Network team"}


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


class TestFileTicketsGate(BoardTestCase):
    def test_without_token_refused(self):
        tools = FixtureCubeTools(ApprovalTokens())
        with self.assertRaises(ApprovalError):
            tools.file_tickets([GOOD_TICKET], approval_token="bogus")

    def test_with_token_files_and_token_single_use(self):
        tokens = ApprovalTokens()
        tools = FixtureCubeTools(tokens)
        token = tokens.issue()
        out = tools.file_tickets([GOOD_TICKET], approval_token=token)
        self.assertEqual(out["filed"], 1)
        self.assertEqual(out["board_total"], 1)
        self.assertEqual(tools.board.list()[0]["ticket_id"], "TKT-001")
        with self.assertRaises(ApprovalError):
            tools.file_tickets([GOOD_TICKET], approval_token=token)

    def test_validation(self):
        tokens = ApprovalTokens()
        tools = FixtureCubeTools(tokens)
        with self.assertRaises(ValueError):
            tools.file_tickets([], approval_token=tokens.issue())
        bad = dict(GOOD_TICKET, category="Nope")
        with self.assertRaises(ValueError):
            tools.file_tickets([bad], approval_token=tokens.issue())
        bad = dict(GOOD_TICKET, priority="P9")
        with self.assertRaises(ValueError):
            tools.file_tickets([bad], approval_token=tokens.issue())


class TestTriageEndToEnd(BoardTestCase):
    def test_stub_triage_files_five(self):
        tokens = ApprovalTokens()
        orch = Orchestrator(StubEmberClient(), FixtureCubeTools(tokens),
                            SpendMeter(), tokens)
        plan = orch.propose("Triage the help-desk tickets")
        names = [s["name"] for s in plan["steps"]]
        self.assertIn("file_tickets", names)
        step = [s for s in plan["steps"] if s["name"] == "file_tickets"][0]
        self.assertEqual(len(step["arguments"]["tickets"]), 5)
        self.assertEqual(len(orch.tools.board.list()), 0)  # nothing yet
        done = orch.approve(plan["id"])
        self.assertEqual(done["status"], "executed")
        self.assertEqual(len(orch.tools.board.list()), 5)
        self.assertIn("Filed 5 tickets", done["final_text"])


class TestIntegrationPurpose(unittest.TestCase):
    def test_integration_bucket(self):
        meter = SpendMeter(rate_input_per_1k=0.0001,
                           rate_output_per_1k=0.0005)
        meter.authorize("integration")
        meter.record("integration", 1000, 1000, note="t")
        self.assertAlmostEqual(meter.by_bucket["integration"], 0.0006,
                               places=6)


class TestRefusalShape(BoardTestCase):
    def test_unknown_command_proposes_no_gated_steps(self):
        tokens = ApprovalTokens()
        orch = Orchestrator(StubEmberClient(), FixtureCubeTools(tokens),
                            SpendMeter(), tokens)
        plan = orch.propose("Order a pizza for the office")
        gated = {"run_workflow", "file_tickets"}
        self.assertFalse(gated & {s["name"] for s in plan["steps"]})


if __name__ == "__main__":
    unittest.main()
