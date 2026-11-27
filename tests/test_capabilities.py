"""Capability-table tests (Nov 26, 2026).

"What can you do?" is answered deterministically from the tool
catalog — $0, no Ember call. Detection must stay tight (whole
command), the table must cover every catalog tool, gated tools
must be labelled, and the refusal path must be untouched.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.cube_tools import GATED_TOOLS, load_catalog
from tests.test_ui import OPERATOR, UiTestCase

EXCLUDED = {"create_ticket", "knowledge_search", "ask_cube"}


class TestDetection(unittest.TestCase):
    def test_positives(self):
        for cmd in ("What can you do?", "what can you do",
                    "What do you do?", "help", "Help!", "menu",
                    "capabilities", "What are your capabilities?",
                    "list your tools", "List your functions",
                    "What tools do you have?",
                    "What functions do you have?",
                    "What are you able to do?",
                    "Please show me what you can do."):
            self.assertTrue(serve.is_capability_question(cmd), cmd)

    def test_negatives(self):
        for cmd in ("What can you do about capacity?",
                    "What can you do about the disk?",
                    "Order a pizza and book a flight",
                    "Check the Watcher status",
                    "Help me check the Watcher status",
                    "Send the Watcher status to my WhatsApp.",
                    "How is our capacity looking?",
                    "", "   "):
            self.assertFalse(serve.is_capability_question(cmd), cmd)


class TestPayload(unittest.TestCase):
    def test_covers_all_catalog_tools(self):
        payload = serve.capabilities_payload()
        self.assertEqual(payload["type"], "capabilities")
        names = [t["name"] for g in payload["groups"] for t in g["tools"]]
        catalog_names = [t["name"] for t in load_catalog()["tools"]]
        self.assertEqual(sorted(names), sorted(catalog_names))
        self.assertEqual(payload["tool_count"], len(catalog_names))
        self.assertTrue(EXCLUDED.isdisjoint(names))

    def test_groups_and_gated_labels(self):
        payload = serve.capabilities_payload()
        areas = [g["area"] for g in payload["groups"]]
        self.assertEqual(areas, serve._CAPABILITY_AREA_ORDER)
        for g in payload["groups"]:
            self.assertTrue(g["tools"], g["area"])
            for tool in g["tools"]:
                self.assertTrue(tool["does"], tool)
                self.assertTrue(tool["example"], tool)
                self.assertEqual(tool["gated"],
                                 tool["name"] in GATED_TOOLS, tool)
                if tool["gated"]:
                    self.assertIn("approval", tool["does"], tool)


class TestService(UiTestCase):
    def test_capability_question_costs_nothing(self):
        svc = self.make_service()
        code, body = svc.propose("What can you do?", "c")
        self.assertEqual(code, 200)
        self.assertEqual(body["type"], "capabilities")
        self.assertEqual(body["run"],
                         {"tokens_in": 0, "tokens_out": 0,
                          "calls": 0, "cost_usd": 0.0})
        self.assertEqual(body["meter"]["spent_usd"], 0.0)
        # No daily-cap consumption either.
        self.assertEqual(serve.load_cap_state(self.cap_path)["count"], 0)

    def test_normal_command_still_proposes(self):
        svc = self.make_service()
        code, body = svc.propose("Send the Watcher status to WhatsApp", "c")
        self.assertEqual(code, 200)
        self.assertNotIn("type", body)
        self.assertTrue(body["steps"])

    def test_out_of_scope_not_swallowed(self):
        # The stub routes unknown intents to list_workflows, so a
        # stub "refusal" still carries steps; what matters here is
        # that capability detection does NOT intercept the command —
        # it goes through the normal propose path (live Ember then
        # refuses for real, as the recorded demo proves).
        svc = self.make_service()
        code, body = svc.propose(
            "Order a pizza for the office and book a flight to Denver.",
            "c")
        self.assertEqual(code, 200)
        self.assertNotIn("type", body)
        self.assertIn("steps", body)
        self.assertIn("refusal", body)

    def test_capability_still_requires_operator(self):
        svc = self.make_service(user=None)
        code, _ = svc.propose("What can you do?", "")
        self.assertEqual(code, 403)


class TestPage(unittest.TestCase):
    def _page(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            return f.read()

    def test_capabilities_rendering(self):
        page = self._page()
        self.assertIn("function renderCapabilities", page)
        self.assertIn('r.type==="capabilities"', page)
        self.assertIn("EMBER · CAPABILITIES", page)
        self.assertIn("cap-table", page)
        self.assertIn("cap-row", page)
        # Example prompts load the composer (no run fired).
        i = page.index("function renderCapabilities")
        fn = page[i:page.index("/* ---------- live run", i)]
        self.assertIn("c.value=tool.example", fn)
        self.assertNotIn("proposeLive", fn)
        self.assertNotIn("fetch(", fn)


if __name__ == "__main__":
    unittest.main()
