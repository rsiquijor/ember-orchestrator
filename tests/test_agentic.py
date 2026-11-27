"""Agentic improvements (Rom, Nov 26, 2026) — all eight features.

Offline, $0: stub Ember + fixture tools + fake auth + injected
source fetches. No network, no real Activepieces writes.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

import serve
from orchestrator.agent_builder import (build_flow_trigger,
                                        validate_agent_params)
from orchestrator.cube_tools import PreferenceStore
from orchestrator.orchestrator import assess_confidence
from orchestrator.verifier import verify_trace
from tests.test_phase4 import Phase4Case

CHAIN = [
    {"kind": "fetch", "source": "capacity"},
    {"kind": "condition", "field": "disk_pct", "op": ">", "value": 80},
    {"kind": "action", "action": "board"},
    {"kind": "action", "action": "whatsapp"},
]


def _count_steps(trigger):
    n = 0
    node = trigger.get("nextAction")
    while node:
        n += 1
        node = node.get("nextAction")
    return n


class AgenticCase(Phase4Case):
    def setUp(self):
        super().setUp()
        for var, fname in (("EMBER_GOALS_PATH", "goals.json"),
                           ("EMBER_PREFS_PATH", "preferences.json"),
                           ("EMBER_EXPORTS_DIR", "exports")):
            self._saved[var] = os.environ.get(var)
            os.environ[var] = os.path.join(self._tmp.name, fname)

    def make_service(self, user=None, daily_cap=100, fetch=None):
        from tests.test_ui import OPERATOR
        svc = super().make_service(user=user or OPERATOR,
                                   daily_cap=daily_cap)
        if fetch is not None:
            svc.source_fetch_fn = fetch
        return svc


# ------------------------------------------------ 1. verification
class TestVerification(AgenticCase):
    def test_create_flow_verifies_ok(self):
        svc = self.make_service()
        code, plan = svc.propose(
            "Build a new automated agentic workflow.", "c")
        self.assertEqual(code, 200)
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        checks = done["verification"]
        self.assertTrue(checks)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        self.assertIn("Verified", done["verification_summary"])

    def test_missing_registry_entry_fails_verification(self):
        svc = self.make_service()
        code, plan = svc.propose(
            "Build a new automated agentic workflow.", "c")
        code, done = svc.approve(plan["id"], "c")
        fid = done["trace"][0]["result"]["flow_id"]
        svc.orch.tools.agent_flows.remove(fid)
        checks = verify_trace(done["trace"], svc.orch.tools)
        bad = [c for c in checks if not c["ok"]]
        self.assertTrue(bad)
        self.assertIn("fix", bad[0])


# ------------------------------------------------ 2. chains
class TestChains(unittest.TestCase):
    def test_valid_chain_derives_primary(self):
        clean = validate_agent_params({
            "name": "Capacity Chain Alert", "trigger": "schedule",
            "cron": "0 6 * * *", "steps": CHAIN})
        self.assertEqual(clean["source"], "capacity")
        self.assertEqual(clean["actions"], ["board", "whatsapp"])
        self.assertEqual(clean["condition"]["field"], "disk_pct")
        self.assertEqual(len(clean["steps"]), 4)

    def test_chain_rejections(self):
        base = {"name": "Chain Test Flow", "trigger": "schedule",
                "cron": "0 6 * * *"}
        for steps in (
                CHAIN + [{"kind": "action", "action": "board"}],
                [{"kind": "condition", "field": "disk_pct",
                  "op": ">", "value": 1},
                 {"kind": "action", "action": "board"}],
                [{"kind": "fetch", "source": "internet"},
                 {"kind": "action", "action": "board"}],
                [{"kind": "fetch", "source": "capacity"}],
                [{"kind": "fetch", "source": "capacity"},
                 {"kind": "action", "action": "email"}]):
            with self.assertRaises(ValueError, msg=str(steps)):
                validate_agent_params(dict(base, steps=steps))

    def test_chain_flow_assembly_step_count(self):
        clean = validate_agent_params({
            "name": "Capacity Chain Alert", "trigger": "schedule",
            "cron": "0 6 * * *", "steps": CHAIN})
        trigger = build_flow_trigger(clean, board_key="k")
        # fetch + compose + board + whatsapp
        self.assertEqual(_count_steps(trigger), 4)

    def test_single_step_backward_compatible(self):
        clean = validate_agent_params({
            "name": "Morning Watcher WhatsApp",
            "trigger": "schedule", "cron": "0 6 * * *",
            "source": "watcher_overview", "action": "whatsapp"})
        self.assertIsNone(clean["steps"])
        self.assertEqual(clean["autonomy"], "propose-only")
        trigger = build_flow_trigger(clean, board_key="k")
        self.assertEqual(_count_steps(trigger), 3)


# ------------------------------------------------ 3. suggestions
class TestSuggestions(AgenticCase):
    def test_disk_suggestion_from_live_data(self):
        def fetch(url):
            if "capacity" in url:
                return {"host_disk_pct": 69.9}
            raise RuntimeError("down")
        svc = self.make_service(fetch=fetch)
        code, out = svc.suggestions("c")
        self.assertEqual(code, 200)
        sugs = out["suggestions"]
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0]["template_id"],
                         "capacity-disk-alert")
        self.assertIn("69.9", sugs[0]["text"])

    def test_no_suggestion_below_threshold(self):
        svc = self.make_service(
            fetch=lambda url: {"host_disk_pct": 40})
        code, out = svc.suggestions("c")
        self.assertEqual(out["suggestions"], [])


# ------------------------------------------------ 4. goals
class TestGoals(AgenticCase):
    def test_goal_lifecycle_and_progress(self):
        svc = self.make_service(
            fetch=lambda url: {"host_disk_pct": 69.9})
        code, out = svc.goals_create(
            {"text": "keep disk under 80% this week",
             "source": "capacity", "field": "disk_pct",
             "op": "<", "value": 80}, "c")
        self.assertEqual(code, 200, out)
        goal = out["goal"]
        self.assertEqual(goal["current"], 69.9)
        self.assertTrue(goal["on_track"])
        code, out = svc.goals_list("c")
        self.assertEqual(len(out["goals"]), 1)
        code, out = svc.goals_close(goal["id"], "c")
        self.assertEqual(code, 200)
        self.assertEqual(out["goal"]["status"], "closed")
        code, _ = svc.goals_close("goal-nope", "c")
        self.assertEqual(code, 404)

    def test_goal_validation(self):
        svc = self.make_service()
        code, _ = svc.goals_create(
            {"text": "x", "source": "internet", "field": "disk_pct",
             "op": "<", "value": 80}, "c")
        self.assertEqual(code, 400)
        code, _ = svc.goals_create(
            {"text": "x", "source": "capacity", "field": "nope",
             "op": "<", "value": 80}, "c")
        self.assertEqual(code, 400)


# ------------------------------------------------ 5. confidence
class TestConfidence(AgenticCase):
    def test_assess_confidence_levels(self):
        conf, q = assess_confidence([])
        self.assertEqual(conf, "low")
        conf, q = assess_confidence(
            [{"name": "create_agent_flow", "arguments": {}}])
        self.assertEqual(conf, "low")
        self.assertIn("What should", q["question"])
        conf, q = assess_confidence(
            [{"name": "create_agent_flow", "arguments": {
                "source": "capacity", "trigger": "schedule"}}])
        self.assertEqual(conf, "medium")
        conf, q = assess_confidence(
            [{"name": "create_agent_flow", "arguments": {
                "source": "capacity", "trigger": "schedule",
                "cron": "0 6 * * *"}}])
        self.assertEqual(conf, "high")

    def test_clarifying_guard_is_free_and_specific(self):
        svc = self.make_service()
        before = svc.meter.spent_usd
        code, out = svc.propose("Build an agent for me", "c")
        self.assertEqual(code, 200)
        self.assertIsNotNone(out["clarifying_question"])
        self.assertEqual(out["run"]["calls"], 0)
        self.assertEqual(svc.meter.spent_usd, before)
        # The canonical bare phrase still derives (no nagging).
        code, out = svc.propose(
            "Build a new automated agentic workflow.", "c")
        self.assertEqual(code, 200)
        self.assertTrue(out["steps"])

    def test_intake_only_context_still_asks(self):
        # An old intake record is context, but it cannot derive an
        # automation source — the guard must still ask (live E2E
        # finding, Nov 26).
        svc = self.make_service()
        svc.orch.tools.intake.append(
            {"kind": "invoice", "fields": {"vendor": "X"},
             "notes": "", "filed_at": "2026-11-26T00:00:00Z",
             "run_tag": "", "status": "filed"})
        code, out = svc.propose("Build an agent for me", "c")
        self.assertEqual(code, 200)
        self.assertIsNotNone(out["clarifying_question"])


# ------------------------------------------------ 6. preferences
class TestPreferences(AgenticCase):
    def test_record_and_prompt_line(self):
        store = PreferenceStore(
            os.path.join(self._tmp.name, "prefs.json"))
        self.assertEqual(store.prompt_line(), "")
        store.record_edit(
            {"cron": "0 6 * * *", "actions": ["whatsapp"],
             "source": "watcher_overview", "condition": None},
            {"cron": "0 7 * * 1-5", "actions": ["board"],
             "source": "watcher_overview", "condition": None})
        line = store.prompt_line()
        self.assertIn("0 7 * * 1-5", line)
        self.assertIn("board", line)
        store.reset()
        self.assertEqual(store.summary()["edits"], 0)

    def test_wizard_create_records_edit(self):
        svc = self.make_service()
        code, out = svc.wizard_design(
            "Build a new automated agentic workflow that checks "
            "capacity every morning at 6 and files the report to "
            "the board.", "c")
        self.assertEqual(code, 200, out)
        params = dict(out["proposed_params"])
        params["cron"] = "0 7 * * 1-5"
        code, done = svc.wizard_create(out["plan_id"], params, "c")
        self.assertEqual(code, 200, done)
        prefs = svc.orch.tools.preferences.summary()
        self.assertGreaterEqual(prefs["edits"], 1)
        self.assertEqual(prefs["cron"], "0 7 * * 1-5")


# ------------------------------------------------ 7. autonomy
class TestAutonomy(AgenticCase):
    def test_validate_autonomy(self):
        clean = validate_agent_params({
            "name": "Morning Watcher WhatsApp",
            "trigger": "schedule", "cron": "0 6 * * *",
            "source": "watcher_overview", "action": "whatsapp",
            "autonomy": "auto-report"})
        self.assertEqual(clean["autonomy"], "auto-report")
        with self.assertRaises(ValueError):
            validate_agent_params({
                "name": "Morning Watcher WhatsApp",
                "trigger": "schedule", "cron": "0 6 * * *",
                "source": "watcher_overview", "action": "whatsapp",
                "autonomy": "full-auto"})

    def test_set_autonomy_via_service(self):
        svc = self.make_service()
        code, plan = svc.propose(
            "Build a new automated agentic workflow.", "c")
        code, done = svc.approve(plan["id"], "c")
        fid = done["trace"][0]["result"]["flow_id"]
        code, out = svc.agent_flow_autonomy(fid, "auto-silent", "c")
        self.assertEqual(code, 200, out)
        code, flows = svc.agent_flows("c")
        self.assertEqual(flows["flows"][0]["autonomy"],
                         "auto-silent")
        code, _ = svc.agent_flow_autonomy("nope", "auto-silent", "c")
        self.assertEqual(code, 404)
        code, _ = svc.agent_flow_autonomy(fid, "bogus", "c")
        self.assertEqual(code, 400)


# ------------------------------------------------ 8. undo
class TestUndo(AgenticCase):
    def _created_flow(self, svc):
        code, plan = svc.propose(
            "Build a new automated agentic workflow.", "c")
        code, done = svc.approve(plan["id"], "c")
        return done["trace"][0]["result"]["flow_id"]

    def test_undo_flow_delete_idempotent(self):
        svc = self.make_service()
        fid = self._created_flow(svc)
        code, out = svc.undo({"kind": "flow_delete", "id": fid}, "c")
        self.assertEqual(code, 200)
        self.assertTrue(out["undone"])
        self.assertIn("audit", out)
        code, flows = svc.agent_flows("c")
        self.assertEqual(flows["flows"], [])
        code, out = svc.undo({"kind": "flow_delete", "id": fid}, "c")
        self.assertFalse(out["undone"])

    def test_undo_board_ticket_and_export(self):
        svc = self.make_service()
        code, plan = svc.propose("Triage these help-desk tickets. "
                                 "Classify each one, draft a reply "
                                 "for each, and file them.", "c")
        code, done = svc.approve(plan["id"], "c")
        ids = done["trace"][0]["result"]["ticket_ids"]
        code, out = svc.undo(
            {"kind": "board_ticket", "id": ids[0]}, "c")
        self.assertTrue(out["undone"])
        remaining = {t["ticket_id"]
                     for t in svc.orch.tools.board.list()}
        self.assertNotIn(ids[0], remaining)

    def test_undo_whatsapp_honest(self):
        svc = self.make_service()
        code, out = svc.undo({"kind": "whatsapp", "id": "x"}, "c")
        self.assertEqual(code, 200)
        self.assertFalse(out["undone"])
        self.assertIn("can't be unsent", out["message"])

    def test_undo_requires_operator(self):
        from tests.test_ui import VIEWER
        svc = self.make_service(user=VIEWER)
        code, _ = svc.undo({"kind": "flow_delete", "id": "x"}, "c")
        self.assertEqual(code, 403)


# ------------------------------------------------ page markers
class TestAgenticPage(unittest.TestCase):
    def _page(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            return f.read()

    def test_agentic_ui_markers(self):
        page = self._page()
        for marker in ("renderVerification", "EMBER · VERIFIED",
                       "renderClarifying", "clarify-btn",
                       "conf-chip", "SUGGESTED FOR YOU",
                       "refreshSuggestions", "GOALS",
                       "refreshGoals", "renderGoalProgress",
                       "/api/undo", "Undo: delete automation",
                       "can't be unsent", "af-auto-sel",
                       "Autonomy (trust level)",
                       "Reset learned preferences",
                       "Based on your past edits"):
            self.assertIn(marker, page)


if __name__ == "__main__":
    unittest.main()
