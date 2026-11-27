"""Built-automation Edit link + dry-run Test tests (Nov 26, 2026).

Rom: "For each automation item, have a link to edit the flow. Also
have a button 'test' for each to see how the automation
automatically works and behave."

The Test is a deterministic dry-run replay from the registry params
(mirroring the Agents app's POST /api/test-run pattern): next
schedule fire in America/Chicago or a simulated webhook trigger, a
live GET to the flow's allowlisted source, and the action payload
composed exactly as the flow would compose it and CAPTURED — never
sent, never filed, no RPA run created. $0: no Ember call.

Offline: stub source fetch + fixture tools + fake auth. No network.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.agent_builder import (compose_report_text,
                                        next_cron_fire)
from tests.test_phase4 import Phase4Case
from tests.test_ui import VIEWER

CT = ZoneInfo("America/Chicago")

WHATSAPP_FLOW = {"id": "testflow-wa", "name": "Watcher Morning WhatsApp",
                 "created_at": "2026-11-26T15:14:46Z",
                 "trigger": "schedule", "cron": "0 6 * * *",
                 "source": "watcher_overview", "action": "whatsapp",
                 "enabled": False, "enabled_at": None}
BOARD_FLOW = {"id": "testflow-board", "name": "Morning FinOps Board",
              "created_at": "2026-11-26T15:14:46Z",
              "trigger": "schedule", "cron": "0 7 * * *",
              "source": "finops", "action": "board",
              "enabled": True, "enabled_at": "2026-11-26T16:00:00Z"}
HOOK_FLOW = {"id": "testflow-hook", "name": "On-demand Capacity",
             "created_at": "2026-11-26T15:14:46Z",
             "trigger": "webhook", "cron": None,
             "source": "capacity", "action": "board",
             "enabled": False, "enabled_at": None}

WATCHER_BODY = {"totals": {"components": 28, "up": 28},
                "open_incidents": 0, "components": [{"name": "a"}]}


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=CT)


# --------------------------------------------------------- next fire
class TestNextCronFire(unittest.TestCase):
    # Thu Nov 26, 2026, 12:37 CST.
    NOW = at(2026, 11, 26, 12, 37)

    def test_presets(self):
        self.assertEqual(next_cron_fire("0 * * * *", self.NOW),
                         at(2026, 11, 26, 13, 0))
        self.assertEqual(next_cron_fire("0 6 * * *", self.NOW),
                         at(2026, 11, 27, 6, 0))
        self.assertEqual(next_cron_fire("0 7 * * *", self.NOW),
                         at(2026, 11, 27, 7, 0))
        self.assertEqual(next_cron_fire("0 6 * * 1-5", self.NOW),
                         at(2026, 11, 27, 6, 0))
        self.assertEqual(next_cron_fire("0 6 * * 1", self.NOW),
                         at(2026, 11, 30, 6, 0))  # next Monday

    def test_weekdays_skip_weekend(self):
        fri_after = at(2026, 11, 27, 7, 0)  # Friday 07:00
        self.assertEqual(next_cron_fire("0 6 * * 1-5", fri_after),
                         at(2026, 11, 30, 6, 0))  # Monday

    def test_step_and_range(self):
        self.assertEqual(next_cron_fire("*/15 9-17 * * 1-5", self.NOW),
                         at(2026, 11, 26, 12, 45))
        self.assertEqual(
            next_cron_fire("*/15 9-17 * * 1-5", at(2026, 11, 26, 17, 50)),
            at(2026, 11, 27, 9, 0))

    def test_dom_dow_or_rule(self):
        # 1st of month OR Monday: Monday Nov 30 comes first.
        self.assertEqual(next_cron_fire("0 6 1 * 1", self.NOW),
                         at(2026, 11, 30, 6, 0))
        # 28th OR Monday: Saturday Nov 28 comes first.
        self.assertEqual(next_cron_fire("0 6 28 * 1", self.NOW),
                         at(2026, 11, 28, 6, 0))

    def test_sunday_zero_and_seven(self):
        self.assertEqual(next_cron_fire("0 6 * * 0", self.NOW),
                         at(2026, 11, 29, 6, 0))
        self.assertEqual(next_cron_fire("0 6 * * 7", self.NOW),
                         at(2026, 11, 29, 6, 0))

    def test_month_and_year_rollover(self):
        self.assertEqual(next_cron_fire("0 6 1 1 *", self.NOW),
                         at(2027, 1, 1, 6, 0))

    def test_strictly_after_now(self):
        self.assertEqual(next_cron_fire("0 6 * * *", at(2026, 11, 26, 6, 0)),
                         at(2026, 11, 27, 6, 0))

    def test_invalid_cron_raises(self):
        for bad in ("", "0 6 * *", "99 6 * * *", "a b c d e"):
            with self.assertRaises(ValueError, msg=bad):
                next_cron_fire(bad, self.NOW)


# ------------------------------------------------------------ compose
class TestComposeReportText(unittest.TestCase):
    def test_flattens_one_level_and_counts_arrays(self):
        body = {"a": 1, "b": {"c": 2, "d": {"e": 3}},
                "list": [1, 2, 3]}
        self.assertEqual(
            compose_report_text("Label", body),
            "Cube report (Label) — a: 1; b.c: 2; list: 3 item(s)")

    def test_unwraps_output_and_body(self):
        self.assertEqual(
            compose_report_text("L", {"output": {"body": {"x": 1}}}),
            "Cube report (L) — x: 1")

    def test_string_bodies(self):
        self.assertEqual(compose_report_text("L", '{"x": 1}'),
                         "Cube report (L) — x: 1")
        self.assertEqual(compose_report_text("L", "hello"),
                         "Cube report (L) — raw: hello")

    def test_js_scalar_formatting(self):
        body = {"a": None, "b": True, "c": False, "d": 1.0, "e": 1.5}
        self.assertEqual(
            compose_report_text("L", body),
            "Cube report (L) — a: null; b: true; c: false; d: 1; e: 1.5")

    def test_part_and_length_caps(self):
        # 12-part cap (short values, length cap not binding).
        body = {f"k{i}": i for i in range(20)}
        text = compose_report_text("L", body)
        self.assertEqual(text.count(";"), 11)  # 12 parts max
        # 900-char cap (long values truncate the joined text).
        body = {f"k{i}": "v" * 200 for i in range(20)}
        text = compose_report_text("L", body)
        self.assertEqual(len(text), 900)


# ------------------------------------------------------- service test
class TestAgentFlowTest(Phase4Case):
    def make_svc(self, fetch=None, flows=(WHATSAPP_FLOW,), user=None):
        svc = self.make_service() if user is None \
            else self.make_service(user=user)
        svc.source_fetch_fn = fetch or (lambda url: dict(WATCHER_BODY))
        for f in flows:
            svc.orch.tools.agent_flows.register(dict(f))
        return svc

    def test_whatsapp_trace_shape_and_capture(self):
        svc = self.make_svc()
        reports_before = len(svc.reports.list())
        registry_before = svc.orch.tools.agent_flows.list()
        code, out = svc.agent_flow_test("testflow-wa", "c")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "dry-test")
        self.assertEqual(out["note"],
                         "Test run — nothing was sent or filed.")
        self.assertEqual(out["flow"]["name"], WHATSAPP_FLOW["name"])
        steps = out["steps"]
        self.assertEqual([s["name"] for s in steps],
                         ["trigger", "fetch", "action"])
        self.assertEqual(steps[0]["status"], "ok")
        self.assertIn("next_fire", steps[0])
        self.assertIn("next fire", steps[0]["detail"])
        self.assertEqual(steps[1]["status"], "ok")
        self.assertIn("127.0.0.1:8008/api/overview", steps[1]["detail"])
        self.assertEqual(steps[2]["status"], "captured")
        self.assertIn("CAPTURED — NOT SENT", steps[2]["detail"])
        payload = steps[2]["captured_payload"]
        self.assertEqual(payload["agent"], WHATSAPP_FLOW["name"])
        self.assertEqual(payload["source"], "watcher_overview")
        self.assertTrue(payload["summary"].startswith(
            "Cube report (Watcher overview (AIOps)) — "))
        self.assertIn("totals.components: 28", payload["summary"])
        # Captured-not-sent proof: board + registry untouched, $0.
        self.assertEqual(len(svc.reports.list()), reports_before)
        self.assertEqual(svc.orch.tools.agent_flows.list(),
                         registry_before)
        self.assertEqual(svc.meter.spent_usd, 0.0)

    def test_board_trace_capture(self):
        svc = self.make_svc(
            fetch=lambda url: {"mtd_usd": 336.96, "budget_usd": 500},
            flows=(BOARD_FLOW,))
        code, out = svc.agent_flow_test("testflow-board", "c")
        self.assertEqual(code, 200, out)
        step = out["steps"][2]
        self.assertEqual(step["status"], "captured")
        self.assertIn("CAPTURED — NOT FILED", step["detail"])
        payload = step["captured_payload"]
        self.assertEqual(payload["title"], BOARD_FLOW["name"])
        self.assertEqual(payload["source"], "finops")
        self.assertIn("mtd_usd: 336.96", payload["summary"])
        self.assertEqual(len(svc.reports.list()), 0)
        self.assertEqual(svc.meter.spent_usd, 0.0)

    def test_enabled_flow_is_testable(self):
        # BOARD_FLOW is enabled — the dry-run must still work and
        # must not change its registry state.
        svc = self.make_svc(flows=(BOARD_FLOW,))
        code, out = svc.agent_flow_test("testflow-board", "c")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["flow"]["enabled"])
        entry = svc.orch.tools.agent_flows.get("testflow-board")
        self.assertTrue(entry["enabled"])

    def test_webhook_trigger_simulated(self):
        svc = self.make_svc(flows=(HOOK_FLOW,))
        code, out = svc.agent_flow_test("testflow-hook", "c")
        self.assertEqual(code, 200, out)
        self.assertEqual(out["steps"][0]["status"], "ok")
        self.assertIn("simulated trigger", out["steps"][0]["detail"])

    def test_source_error_shown_and_action_skipped(self):
        def boom(url):
            raise OSError("connection refused")
        svc = self.make_svc(fetch=boom)
        code, out = svc.agent_flow_test("testflow-wa", "c")
        self.assertEqual(code, 200, out)
        self.assertEqual(out["steps"][1]["status"], "error")
        self.assertIn("connection refused", out["steps"][1]["detail"])
        self.assertEqual(out["steps"][2]["status"], "skipped")
        self.assertNotIn("captured_payload", out["steps"][2])

    def test_unknown_and_missing_flow_id(self):
        svc = self.make_svc()
        code, _ = svc.agent_flow_test("no-such-flow", "c")
        self.assertEqual(code, 404)
        code, _ = svc.agent_flow_test("", "c")
        self.assertEqual(code, 400)
        code, _ = svc.agent_flow_test(None, "c")
        self.assertEqual(code, 400)

    def test_requires_operator(self):
        svc = self.make_svc(user=VIEWER)
        code, _ = svc.agent_flow_test("testflow-wa", "c")
        self.assertEqual(code, 403)

    def test_edit_url_pattern(self):
        svc = self.make_svc()
        code, out = svc.agent_flows("c")
        self.assertEqual(code, 200)
        fl = out["flows"][0]
        self.assertEqual(
            fl["edit_url"],
            "https://rpa.metatroncube.online/projects/"
            "R4KLbLXTv040pofVBOvkz/flows/testflow-wa")
        self.assertEqual(fl["source_label"], "Watcher overview (AIOps)")
        self.assertEqual(
            serve.rpa_edit_url("abc"),
            "https://rpa.metatroncube.online/projects/"
            "R4KLbLXTv040pofVBOvkz/flows/abc")


# --------------------------------------- condition gate + two actions
COND_FLOW = {"id": "testflow-cond", "name": "Down Alert",
             "created_at": "2026-11-26T15:14:46Z",
             "trigger": "schedule", "cron": "0 6 * * *",
             "source": "watcher_overview", "action": "whatsapp",
             "actions": ["whatsapp"],
             "condition": {"field": "components_down", "op": ">",
                           "value": 0},
             "enabled": False, "enabled_at": None}
TWO_FLOW = {"id": "testflow-two", "name": "Down Alert Both",
            "created_at": "2026-11-26T15:14:46Z",
            "trigger": "schedule", "cron": "0 6 * * *",
            "source": "watcher_overview", "action": "whatsapp+board",
            "actions": ["whatsapp", "board"],
            "condition": {"field": "components_down", "op": ">",
                          "value": 0},
            "enabled": False, "enabled_at": None}
DOWN_BODY = {"totals": {"components": 28, "up": 26},
             "open_incidents": 1}


class TestConditionReplay(Phase4Case):
    def make_svc(self, fetch=None, flows=(COND_FLOW,)):
        svc = self.make_service()
        svc.source_fetch_fn = fetch or (lambda url: dict(DOWN_BODY))
        for f in flows:
            svc.orch.tools.agent_flows.register(dict(f))
        return svc

    def test_condition_pass_captures_action(self):
        svc = self.make_svc()
        code, out = svc.agent_flow_test("testflow-cond", "c")
        self.assertEqual(code, 200, out)
        steps = out["steps"]
        self.assertEqual([s["name"] for s in steps],
                         ["trigger", "fetch", "condition", "action"])
        cond = steps[2]
        self.assertEqual(cond["status"], "ok")
        self.assertEqual(cond["condition_value"], 2)
        self.assertTrue(cond["condition_passed"])
        self.assertIn("= 2", cond["detail"])
        self.assertEqual(steps[3]["status"], "captured")
        summary = steps[3]["captured_payload"]["summary"]
        self.assertIn("condition met: Components down = 2", summary)
        self.assertEqual(out["flow"]["condition"]["field"],
                         "components_down")

    def test_condition_fail_skips_actions(self):
        svc = self.make_svc(
            fetch=lambda url: {"totals": {"components": 28,
                                          "up": 28}})
        code, out = svc.agent_flow_test("testflow-cond", "c")
        self.assertEqual(code, 200, out)
        steps = out["steps"]
        cond = steps[2]
        self.assertEqual(cond["status"], "skipped")
        self.assertEqual(cond["condition_value"], 0)
        self.assertFalse(cond["condition_passed"])
        self.assertIn("NOT met", cond["detail"])
        self.assertEqual(steps[3]["status"], "skipped")
        self.assertNotIn("captured_payload", steps[3])

    def test_condition_field_missing_is_error(self):
        svc = self.make_svc(fetch=lambda url: {"unrelated": 1})
        code, out = svc.agent_flow_test("testflow-cond", "c")
        self.assertEqual(code, 200, out)
        cond = out["steps"][2]
        self.assertEqual(cond["status"], "error")
        self.assertIn("could not be computed", cond["detail"])
        self.assertEqual(out["steps"][3]["status"], "skipped")

    def test_two_actions_two_captured_steps(self):
        svc = self.make_svc(flows=(TWO_FLOW,))
        code, out = svc.agent_flow_test("testflow-two", "c")
        self.assertEqual(code, 200, out)
        steps = out["steps"]
        self.assertEqual([s["name"] for s in steps],
                         ["trigger", "fetch", "condition", "action",
                          "action"])
        self.assertEqual(steps[3]["status"], "captured")
        self.assertIn("NOT SENT", steps[3]["detail"])
        self.assertEqual(steps[4]["status"], "captured")
        self.assertIn("NOT FILED", steps[4]["detail"])
        self.assertEqual(out["flow"]["actions"],
                         ["whatsapp", "board"])

    def test_listing_carries_actions_and_condition_label(self):
        svc = self.make_svc(flows=(COND_FLOW, WHATSAPP_FLOW))
        code, out = svc.agent_flows("c")
        self.assertEqual(code, 200)
        by_id = {f["id"]: f for f in out["flows"]}
        self.assertEqual(by_id["testflow-cond"]["condition_label"],
                         "Components down > 0")
        # Legacy entries (action only) normalize to a one-item list.
        self.assertEqual(by_id["testflow-wa"]["actions"],
                         ["whatsapp"])
        self.assertIsNone(by_id["testflow-wa"]["condition"])


# -------------------------------------------------------------- page
class TestAgentTestPage(unittest.TestCase):
    def _page(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            return f.read()

    def test_test_button_and_edit_link_markers(self):
        page = self._page()
        i = page.index("function refreshAgentFlows")
        fn = page[i:page.index("/* ---------- gallery", i)]
        self.assertIn('class="af-test"', fn)
        self.assertIn(">Test</button>", fn)
        self.assertIn('class="af-edit"', fn)
        self.assertIn("Edit flow ↗", fn)
        self.assertIn('target="_blank"', fn)
        self.assertIn("fl.edit_url", fn)
        self.assertIn('"/api/agent-flows/test"', fn)
        self.assertIn("renderTestRun(fl,data)", fn)

    def test_test_run_beat_markers(self):
        page = self._page()
        i = page.index("function renderTestRun")
        fn = page[i:page.index("/* ---------- gallery", i)]
        self.assertIn("TEST RUN", fn)
        self.assertIn("nothing was sent or filed", fn)
        self.assertIn("captured payload (not sent)", fn)

    def test_edit_link_is_not_an_approval_surface(self):
        # Rom's rule stands: approvals stay in-screen. The Edit link
        # opens the flow editor for viewing/editing only — no copy
        # may direct approval to the RPA console.
        page = self._page()
        self.assertNotIn("RPA CONSOLE", page)
        self.assertNotIn("RPA console", page)


if __name__ == "__main__":
    unittest.main()
