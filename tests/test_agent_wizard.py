"""Ember-native Agent Builder Wizard tests (Nov 26, 2026).

The wizard replaces the external Agents-app handoff (which designs
with Axion) as the Automate default: Ember designs (proposes params
via the metered propose path), the human customizes in a form, and
the FINAL edited params are re-validated against the deterministic
allowlist before the gated approval path creates a DISABLED draft
on Metatron RPA. Offline, $0: stub Ember + fixture tools + fake
auth. No network, no real Activepieces writes.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.agent_builder import (CONDITION_FIELDS,
                                        SCRATCH_CONDITION_SUGGESTIONS,
                                        SCRATCH_QUESTIONS,
                                        SCRATCH_SOURCES, SOURCES,
                                        TEMPLATES,
                                        scratch_params_from_answers,
                                        validate_agent_params)
from tests.test_phase4 import Phase4Case
from tests.test_ui import OPERATOR, VIEWER

DESIGN_CMD = "Build a new automated agentic workflow."
# What StubEmberClient proposes for DESIGN_CMD.
STUB_PARAMS = {"name": "Morning Watcher WhatsApp", "trigger": "schedule",
               "cron": "0 6 * * *", "source": "watcher_overview",
               "action": "whatsapp"}


class TestWizardOptions(Phase4Case):
    def test_options_mirror_builder_allowlist(self):
        svc = self.make_service()
        code, out = svc.wizard_options("c")
        self.assertEqual(code, 200)
        self.assertEqual([s["id"] for s in out["sources"]],
                         list(SOURCES))
        for s in out["sources"]:
            self.assertEqual(s["label"], SOURCES[s["id"]]["label"])
        self.assertEqual([a["id"] for a in out["actions"]],
                         ["whatsapp", "board"])
        self.assertEqual([t["id"] for t in out["triggers"]],
                         ["schedule", "webhook"])
        crons = {p["id"]: p["cron"] for p in out["presets"]}
        self.assertEqual(crons["hourly"], "0 * * * *")
        self.assertEqual(crons["daily-6"], "0 6 * * *")
        self.assertEqual(crons["daily-7"], "0 7 * * *")
        self.assertEqual(crons["weekdays-6"], "0 6 * * 1-5")
        self.assertEqual(crons["weekly-mon-6"], "0 6 * * 1")

    def test_options_require_operator(self):
        svc = self.make_service(user=VIEWER)
        code, _ = svc.wizard_options("c")
        self.assertEqual(code, 403)


class TestWizardTemplates(Phase4Case):
    """Pre-built templates (Rom, Nov 26): the wizard's opening
    "What do you need?" step offers deterministic pre-fills served
    from the options endpoint; picking one builds a proposed plan
    with NO Ember call ($0, no cap), and create still runs the
    gated path with re-validation."""

    def test_every_template_validates_unchanged(self):
        self.assertGreaterEqual(len(TEMPLATES), 6)
        covered = set()
        for t in TEMPLATES:
            clean = validate_agent_params(dict(t["params"]))
            covered.add(clean["source"])
            self.assertTrue(t["name"] and t["does"], t)
        # Templates cover every allowlisted source.
        self.assertEqual(covered, set(SOURCES))

    def test_options_serve_templates_as_clean_params(self):
        svc = self.make_service()
        code, out = svc.wizard_options("c")
        self.assertEqual(code, 200)
        self.assertEqual([t["id"] for t in out["templates"]],
                         [t["id"] for t in TEMPLATES])
        for served, raw in zip(out["templates"], TEMPLATES):
            self.assertEqual(served["name"], raw["name"])
            self.assertEqual(served["does"], raw["does"])
            # Served params are the validator's clean copy.
            self.assertEqual(served["params"],
                             validate_agent_params(
                                 dict(raw["params"])))

    def test_template_pick_is_free_and_creates_nothing(self):
        svc = self.make_service(daily_cap=0)
        code, out = svc.wizard_template("capacity-disk-alert", "c")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["plan_id"].startswith("plan-"))
        self.assertEqual(out["proposed_params"]["source"],
                         "capacity")
        self.assertEqual(out["proposed_params"]["condition"],
                         {"field": "disk_pct", "op": ">",
                          "value": 80})
        self.assertTrue(out["validation"]["ok"])
        self.assertIn("Pre-built template", out["rationale"])
        # $0: no meter movement, no cap consumption, no creation.
        self.assertEqual(svc.meter.spent_usd, 0.0)
        self.assertEqual(out["run"], {"calls": 0, "cost_usd": 0.0})
        self.assertEqual(svc.orch.tools.agent_flows.list(), [])
        plan = svc.orch.plans[out["plan_id"]]
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(plan["steps"][0]["name"],
                         "create_agent_flow")

    def test_template_plan_creates_through_gated_path(self):
        svc = self.make_service()
        code, out = svc.wizard_template("finops-summary-board", "c")
        self.assertEqual(code, 200, out)
        edited = dict(out["proposed_params"],
                      name="FinOps Monday Board",
                      cron="0 7 * * 1")
        code, done = svc.wizard_create(out["plan_id"], edited, "c")
        self.assertEqual(code, 200, done)
        flows = svc.orch.tools.agent_flows.list()
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["name"], "FinOps Monday Board")
        self.assertEqual(flows[0]["cron"], "0 7 * * 1")
        self.assertEqual(flows[0]["source"], "finops")
        self.assertEqual(flows[0]["action"], "board")
        self.assertFalse(flows[0]["enabled"])

    def test_unknown_template_404_and_viewer_403(self):
        svc = self.make_service()
        code, _ = svc.wizard_template("no-such-template", "c")
        self.assertEqual(code, 404)
        svc_viewer = self.make_service(user=VIEWER)
        code, _ = svc_viewer.wizard_template(
            "morning-watcher-whatsapp", "c")
        self.assertEqual(code, 403)


class TestWizardScratch(Phase4Case):
    """Guided Q&A ("Start from scratch", Rom Nov 27): the wizard's
    third entry path asks questions and suggests the Metatron
    tools that fit. The Q&A structure is served from the options
    endpoint (single source of truth); chip answers assemble into
    params deterministically ($0) and create still runs the gated
    path with re-validation."""

    def test_options_serve_scratch_structure(self):
        svc = self.make_service()
        code, out = svc.wizard_options("c")
        self.assertEqual(code, 200)
        sc = out["scratch"]
        self.assertEqual([q["id"] for q in sc["questions"]],
                         [q["id"] for q in SCRATCH_QUESTIONS])
        for q in sc["questions"]:
            self.assertTrue(q["text"] and q["hint"], q)
        self.assertEqual({s["id"] for s in sc["sources"]},
                         set(SOURCES))
        for s in sc["sources"]:
            self.assertTrue(s["plain"], s)
            self.assertTrue(s["tools"], s)
            for t in s["tools"]:
                self.assertTrue(t["name"] and t["does"], t)
        self.assertEqual(set(sc["action_tools"]),
                         {"whatsapp", "board"})
        self.assertEqual(sc["builder_tool"]["name"],
                         "create_agent_flow")
        # Every suggested condition field is allowlisted for its
        # source, and every source has at least one suggestion.
        self.assertEqual(set(sc["condition_suggestions"]),
                         set(SOURCES))
        for sid, sugs in sc["condition_suggestions"].items():
            self.assertTrue(sugs, sid)
            for sug in sugs:
                self.assertIn(sug["field"],
                              CONDITION_FIELDS[sid], sug)
                self.assertTrue(sug["label"], sug)

    def test_every_suggested_condition_validates(self):
        for sid, sugs in SCRATCH_CONDITION_SUGGESTIONS.items():
            for sug in sugs:
                params = scratch_params_from_answers({
                    "source": sid, "cron": "0 6 * * *",
                    "actions": ["whatsapp", "board"],
                    "condition": {k: sug[k] for k in
                                  ("field", "op", "value")}})
                clean = validate_agent_params(params)
                self.assertEqual(clean["condition"]["field"],
                                 sug["field"])

    def test_scratch_params_for_each_source(self):
        for sid in SOURCES:
            params = scratch_params_from_answers({
                "source": sid, "cron": "0 6 * * 1-5",
                "actions": ["board"], "condition": None})
            clean = validate_agent_params(params)
            self.assertEqual(clean["source"], sid)
            self.assertEqual(clean["trigger"], "schedule")
            self.assertEqual(clean["cron"], "0 6 * * 1-5")
            self.assertEqual(clean["actions"], ["board"])
            self.assertIsNone(clean["condition"])
            # Derived name: short + cadence + Report/Alert.
            self.assertIn("Weekday Report", clean["name"])
        alert = scratch_params_from_answers({
            "source": "capacity", "cron": "0 6 * * *",
            "actions": ["whatsapp"],
            "condition": {"field": "disk_pct", "op": ">",
                          "value": 80}})
        self.assertEqual(alert["name"], "Capacity Daily Alert")

    def test_scratch_endpoint_free_and_creates_nothing(self):
        svc = self.make_service(daily_cap=0)
        code, out = svc.wizard_scratch({
            "source": "capacity", "cron": "0 6 * * *",
            "actions": ["whatsapp"],
            "condition": {"field": "disk_pct", "op": ">",
                          "value": 80}}, "c")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["plan_id"].startswith("plan-"))
        self.assertEqual(out["proposed_params"]["name"],
                         "Capacity Daily Alert")
        self.assertEqual(out["proposed_params"]["condition"],
                         {"field": "disk_pct", "op": ">",
                          "value": 80})
        self.assertTrue(out["validation"]["ok"])
        self.assertIn("Built from your answers", out["rationale"])
        # $0: no meter movement, no cap consumption, no creation.
        self.assertEqual(svc.meter.spent_usd, 0.0)
        self.assertEqual(out["run"], {"calls": 0, "cost_usd": 0.0})
        self.assertEqual(svc.orch.tools.agent_flows.list(), [])
        plan = svc.orch.plans[out["plan_id"]]
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual(plan["steps"][0]["name"],
                         "create_agent_flow")

    def test_scratch_plan_creates_through_gated_path(self):
        svc = self.make_service()
        code, out = svc.wizard_scratch({
            "source": "finops", "cron": "0 6 * * 1",
            "actions": ["board"], "condition": None}, "c")
        self.assertEqual(code, 200, out)
        code, done = svc.wizard_create(
            out["plan_id"], out["proposed_params"], "c")
        self.assertEqual(code, 200, done)
        flows = svc.orch.tools.agent_flows.list()
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["name"], "FinOps Monday Report")
        self.assertEqual(flows[0]["cron"], "0 6 * * 1")
        self.assertEqual(flows[0]["source"], "finops")
        self.assertFalse(flows[0]["enabled"])

    def test_scratch_bad_answers_400_and_viewer_403(self):
        svc = self.make_service()
        for answers in (
                {"source": "internet", "cron": "0 6 * * *",
                 "actions": ["board"]},
                {"source": "capacity", "cron": "whenever",
                 "actions": ["board"]},
                {"source": "capacity", "cron": "0 6 * * *",
                 "actions": []},
                {"source": "capacity", "cron": "0 6 * * *",
                 "actions": ["email"]},
                {"source": "capacity", "cron": "0 6 * * *",
                 "actions": ["board"],
                 "condition": {"field": "nope", "op": ">",
                               "value": 1}},
                {}):
            code, out = svc.wizard_scratch(answers, "c")
            self.assertEqual(code, 400, (answers, out))
            self.assertTrue(out["error"])
        self.assertEqual(svc.orch.tools.agent_flows.list(), [])
        svc_viewer = self.make_service(user=VIEWER)
        code, _ = svc_viewer.wizard_scratch({
            "source": "capacity", "cron": "0 6 * * *",
            "actions": ["board"]}, "c")
        self.assertEqual(code, 403)

    def test_scratch_live_values_enriched(self):
        bodies = {
            SOURCES["watcher_overview"]["url"]: {
                "totals": {"components": 28, "up": 27,
                           "open_incidents": 2},
                "host": {"disk_pct": 69.9}},
            SOURCES["capacity"]["url"]: {
                "host_disk_pct": 69.9, "host_mem_pct": 55.0,
                "disk_days_to_90": 215},
            SOURCES["predictions"]["url"]: {
                "high_count": 3, "risk_max": 72},
            SOURCES["finops"]["url"]: {
                "budget_pct": 77.8, "cost_today_usd": 12.5},
            SOURCES["secops"]["url"]: {
                "kpis": {"critical": 47, "kev_in_set": 5}},
            SOURCES["dotkrc"]["url"]: {"projects": 3475},
        }
        svc = self.make_service()
        svc.source_fetch_fn = lambda url: bodies[url]
        code, out = svc.wizard_options("c")
        self.assertEqual(code, 200)
        sugs = out["scratch"]["condition_suggestions"]
        by_field = {s["field"]: s for s in sugs["capacity"]}
        self.assertEqual(by_field["disk_pct"]["current"], 69.9)
        self.assertEqual(by_field["disk_days_to_90"]["current"],
                         215)
        by_field = {s["field"]: s
                    for s in sugs["watcher_overview"]}
        self.assertEqual(by_field["components_down"]["current"], 1)
        self.assertEqual(by_field["open_incidents"]["current"], 2)
        by_field = {s["field"]: s for s in sugs["secops"]}
        self.assertEqual(by_field["critical_cves"]["current"], 47)
        # Dead source: suggestions remain, current is None.
        svc2 = self.make_service()

        def dead(url):
            raise ConnectionError("down")
        svc2.source_fetch_fn = dead
        code, out2 = svc2.wizard_options("c")
        self.assertEqual(code, 200)
        for sid, lst in out2["scratch"][
                "condition_suggestions"].items():
            self.assertTrue(lst, sid)
            for sug in lst:
                self.assertIsNone(sug["current"], sug)

    def test_page_scratch_markers(self):
        page = TestWizardPage()._page()
        i = page.index("function buildAgentWizard")
        fn = page[i:page.index("function buildInlineEditor", i)]
        # Third entry in the What step + the Q&A step itself.
        self.assertIn("Start from scratch — answer a few "
                      "questions", fn)
        self.assertIn("Ember asks, you answer", fn)
        self.assertIn('sScratch.dataset.step="scratch"', fn)
        self.assertIn("renderScratchQ", fn)
        self.assertIn("Question ", fn)
        # Tool suggestions panel, updated per answer.
        self.assertIn("Metatron tools Ember will use", fn)
        self.assertIn("renderScratchTools", fn)
        self.assertIn("opts.scratch", fn)
        # Finish paths: deterministic scratch endpoint for chips,
        # Ember design (with the Q&A transcript) for free text
        # and for the optional refine.
        self.assertIn('"/api/agent-wizard/scratch"', fn)
        self.assertIn("scratchDescription", fn)
        self.assertIn("Ask Ember to refine", fn)
        self.assertIn("designFromText", fn)
        # Back from Review returns to the Q&A for scratch plans.
        self.assertIn('fromScratch?"scratch"', fn)
        # All approvals stay in-screen (no RPA console copy).
        self.assertNotIn("RPA console", fn)


class TestWizardValidate(Phase4Case):
    def test_good_params_ok_with_clean_copy(self):
        svc = self.make_service()
        code, out = svc.wizard_validate(dict(STUB_PARAMS), "c")
        self.assertEqual(code, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["clean"]["cron"], "0 6 * * *")

    def test_bad_cron_source_action_name_rejected_with_errors(self):
        svc = self.make_service()
        for bad in (dict(STUB_PARAMS, cron="99 6 * * *"),
                    dict(STUB_PARAMS, cron="0 6 * *"),
                    dict(STUB_PARAMS, source="internet"),
                    dict(STUB_PARAMS, action="email"),
                    dict(STUB_PARAMS, trigger="always"),
                    dict(STUB_PARAMS, name="x"),
                    dict(STUB_PARAMS, url="http://evil.example/x"),
                    {"name": "Hook agent", "trigger": "webhook",
                     "cron": "0 6 * * *", "source": "finops",
                     "action": "board"}):
            code, out = svc.wizard_validate(bad, "c")
            self.assertEqual(code, 200, bad)
            self.assertFalse(out["ok"], bad)
            self.assertTrue(out["error"], bad)

    def test_validate_is_free_and_cap_free(self):
        svc = self.make_service(daily_cap=0)
        code, out = svc.wizard_validate(dict(STUB_PARAMS), "c")
        self.assertEqual(code, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(svc.meter.spent_usd, 0.0)

    def test_validate_requires_operator(self):
        svc = self.make_service(user=VIEWER)
        code, _ = svc.wizard_validate(dict(STUB_PARAMS), "c")
        self.assertEqual(code, 403)


class TestWizardDesign(Phase4Case):
    def test_design_returns_plan_and_proposed_params(self):
        svc = self.make_service()
        code, out = svc.wizard_design(DESIGN_CMD, "c")
        self.assertEqual(code, 200)
        self.assertTrue(out["plan_id"].startswith("plan-"))
        # Proposed params are normalized to the clean params (both
        # the `actions` list and the legacy joined `action`).
        self.assertEqual(out["proposed_params"],
                         dict(STUB_PARAMS, actions=["whatsapp"],
                              condition=None, steps=None,
                              autonomy="propose-only"))
        self.assertTrue(out["validation"]["ok"])
        # Nothing created yet — design executes nothing.
        self.assertEqual(svc.orch.tools.agent_flows.list(), [])

    def test_design_without_agent_proposal_is_422_and_rejects(self):
        svc = self.make_service()
        code, out = svc.wizard_design("How is our capacity looking?",
                                      "c")
        self.assertEqual(code, 422)
        # Teaching 422 (Nov 26 UX pass): says what to add + example.
        self.assertIn("couldn't turn that into an automation",
                      out["error"])
        self.assertIn("what to check, when, and what to do",
                      out["error"])
        # The non-agent plan was rejected server-side.
        statuses = [p["status"] for p in svc.orch.plans.values()]
        self.assertEqual(statuses, ["rejected"])

    def test_design_requires_operator_and_description(self):
        svc = self.make_service(user=VIEWER)
        code, _ = svc.wizard_design(DESIGN_CMD, "c")
        self.assertEqual(code, 403)
        svc = self.make_service()
        code, _ = svc.wizard_design("   ", "c")
        self.assertEqual(code, 400)


class TestWizardCreate(Phase4Case):
    def _design(self, svc):
        code, out = svc.wizard_design(DESIGN_CMD, "c")
        self.assertEqual(code, 200)
        return out["plan_id"]

    def test_edited_params_are_what_gets_created(self):
        svc = self.make_service()
        plan_id = self._design(svc)
        edited = dict(STUB_PARAMS, name="Capacity Watch Seven",
                      cron="0 7 * * 1-5", source="capacity",
                      action="board")
        code, out = svc.wizard_create(plan_id, edited, "c")
        self.assertEqual(code, 200, out)
        trace = [t for t in out["trace"]
                 if t["tool"] == "create_agent_flow"]
        self.assertEqual(len(trace), 1)
        result = trace[0]["result"]
        self.assertEqual(result["status"], "DISABLED")
        self.assertEqual(result["name"], "Capacity Watch Seven")
        self.assertEqual(result["cron"], "0 7 * * 1-5")
        self.assertEqual(result["source"], "capacity")
        self.assertEqual(result["action"], "board")
        # Audit: the trace records the FINAL arguments and keeps
        # Ember's original proposal alongside.
        self.assertEqual(trace[0]["arguments"]["name"],
                         "Capacity Watch Seven")
        self.assertEqual(trace[0]["proposed_arguments"]["name"],
                         "Morning Watcher WhatsApp")
        # Registry entry matches the edited params.
        flows = svc.orch.tools.agent_flows.list()
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["name"], "Capacity Watch Seven")
        self.assertEqual(flows[0]["cron"], "0 7 * * 1-5")
        self.assertEqual(flows[0]["source"], "capacity")
        self.assertEqual(flows[0]["action"], "board")
        self.assertFalse(flows[0]["enabled"])

    def test_invalid_final_params_400_plan_survives(self):
        svc = self.make_service()
        plan_id = self._design(svc)
        code, out = svc.wizard_create(
            plan_id, dict(STUB_PARAMS, cron="0 25 * * *"), "c")
        self.assertEqual(code, 400)
        self.assertIn("invalid agent parameters", out["error"])
        # Nothing created; the plan is still proposed (token not
        # burned), so a corrected create succeeds.
        self.assertEqual(svc.orch.tools.agent_flows.list(), [])
        self.assertEqual(svc.orch.plans[plan_id]["status"], "proposed")
        code, out = svc.wizard_create(plan_id, dict(STUB_PARAMS), "c")
        self.assertEqual(code, 200, out)
        self.assertEqual(len(svc.orch.tools.agent_flows.list()), 1)

    def test_create_unknown_or_rejected_plan(self):
        svc = self.make_service()
        code, _ = svc.wizard_create("plan-nope", dict(STUB_PARAMS), "c")
        self.assertEqual(code, 404)
        plan_id = self._design(svc)
        code, _ = svc.reject(plan_id, "c")
        self.assertEqual(code, 200)
        code, _ = svc.wizard_create(plan_id, dict(STUB_PARAMS), "c")
        self.assertEqual(code, 409)
        self.assertEqual(svc.orch.tools.agent_flows.list(), [])

    def test_create_requires_operator(self):
        svc = self.make_service()
        plan_id = self._design(svc)
        svc_viewer = self.make_service(user=VIEWER)
        code, _ = svc_viewer.wizard_create(
            plan_id, dict(STUB_PARAMS), "c")
        self.assertEqual(code, 403)

    def test_cancel_creates_nothing(self):
        svc = self.make_service()
        plan_id = self._design(svc)
        code, out = svc.reject(plan_id, "c")
        self.assertEqual(code, 200)
        self.assertEqual(svc.orch.tools.agent_flows.list(), [])
        self.assertEqual(svc.orch.plans[plan_id]["status"], "rejected")


class TestWizardPage(unittest.TestCase):
    def _page(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            return f.read()

    def test_wizard_is_in_page_not_external(self):
        page = self._page()
        self.assertIn("function buildAgentWizard", page)
        self.assertNotIn("agents.metatroncube.online", page)
        self.assertNotIn("wizardUrlFor", page)
        # Automate toggles the in-page wizard.
        i = page.index("function renderNextSteps")
        fn = page[i:page.index("/* ---------- Agent Builder", i)]
        self.assertIn("buildAgentWizard(automateCmdFinal,disableAll,auto,demoKey||\"\")",
                      fn)
        self.assertNotIn("window.open(", fn)

    def test_all_approvals_in_screen_no_rpa_console_copy(self):
        # Rom (Nov 26): every approval happens inside the Ember
        # interaction screen. No copy may direct the user to approve
        # in the RPA console (viewing is fine; approval is not).
        page = self._page()
        self.assertNotIn("RPA CONSOLE", page)
        self.assertNotIn("RPA console", page)
        # The three in-screen approval surfaces exist: the plan gate,
        # the wizard's Approve & create, the enablement card.
        self.assertIn("✓ Approve &amp; run", page)
        self.assertIn("✓ Approve & create", page)
        self.assertIn("Enable this automation?", page)
        self.assertIn("ENABLE ANY TIME FROM BUILT AUTOMATIONS BELOW",
                      page)

    def test_wizard_form_fields_and_presets(self):
        page = self._page()
        i = page.index("function buildAgentWizard")
        fn = page[i:page.index("function buildInlineEditor", i)]
        for marker in ("Agent name", "Trigger", "Schedule",
                       "Cron (min hour day month weekday)",
                       "Source (what the process fetches)",
                       "Actions (what it does with the report)",
                       "Custom cron", "Review summary",
                       "✓ Approve & create",
                       "Runs on", "Metatron RPA"):
            self.assertIn(marker, fn)

    def test_preset_change_refreshes_summary_and_steps(self):
        # Regression (Nov 26): the preset handler set the cron value
        # programmatically but only queued validation, so the
        # plain-words summary and process step list stayed stale
        # until the cron field was edited directly. The handler must
        # run the same renderProc() update the cron input runs.
        page = self._page()
        i = page.index("function buildAgentWizard")
        fn = page[i:page.index("function buildInlineEditor", i)]
        self.assertIn(
            "cronIn.value=o.dataset.cron;renderProc();queueValidate();",
            fn)

    def test_suggestion_open_uses_dedicated_host_and_open_class(self):
        # Regression (Nov 26): clicking a SUGGESTED FOR YOU card only
        # scrolled — openWizardWithTemplate appended the wizard to a
        # fresh NEXT STEPS beat but never added the "open" class, so
        # CSS kept .aw-panel at display:none (and the template pick
        # was a fragile poll-and-click). The open path must use the
        # dedicated stage host, mark the panel open, and hand the
        # template id to buildAgentWizard for a deterministic pick.
        page = self._page()
        self.assertIn('id="stageWizardHost"', page)
        i = page.index("function openWizardWithTemplate")
        fn = page[i:page.index("async function refreshGoals", i)]
        self.assertIn('$("stageWizardHost")', fn)
        self.assertIn('wiz.classList.add("open")', fn)
        self.assertIn('buildAgentWizard("Build a new automated '
                      'agentic workflow.",()=>{},null,"",templateId)',
                      fn)
        self.assertIn("wiz.scrollIntoView", fn)
        # No beat dependency and no poll-and-click.
        self.assertNotIn("beat(", fn)
        self.assertNotIn("setInterval", fn)
        self.assertNotIn("btn.click()", fn)
        # A replaced wizard rejects its proposed plan first.
        self.assertIn("prev._awClose", fn)

    def test_wizard_auto_picks_open_template_id(self):
        # buildAgentWizard's optional openTemplateId: once options
        # load, the matching template is picked (landing at Review)
        # with no polling; unknown ids leave the wizard at step 1.
        page = self._page()
        i = page.index("function buildAgentWizard")
        fn = page[i:page.index("function buildInlineEditor", i)]
        self.assertIn(
            "function buildAgentWizard(prefill,disableAll,autoBtn,"
            "demoKey,openTemplateId)", fn)
        self.assertIn("if(openTemplateId){", fn)
        self.assertIn("if(t)pickTemplate(t);", fn)
        self.assertIn("panel._awClose=closeWizard;", fn)


# ---------------------------------- context-aware process design
class _CapturingStub:
    """Wraps StubEmberClient, recording every chat() call's messages."""

    def __init__(self):
        from orchestrator.ember_client import StubEmberClient
        self.inner = StubEmberClient()
        self.calls = []

    def chat(self, messages, tools, max_tokens=512, **kwargs):
        self.calls.append(messages)
        return self.inner.chat(messages, tools, max_tokens=max_tokens)


class TestWizardProcess(Phase4Case):
    def test_options_include_condition_fields_ops_and_context(self):
        svc = self.make_service()
        code, out = svc.wizard_options("c")
        self.assertEqual(code, 200)
        self.assertEqual(out["condition_ops"],
                         [">", ">=", "<", "<=", "==", "!="])
        self.assertIn("context", out)
        self.assertIn("chips", out["context"])
        by_id = {s["id"]: s for s in out["sources"]}
        watcher_fields = {f["id"] for f in
                          by_id["watcher_overview"]["condition_fields"]}
        self.assertIn("components_down", watcher_fields)
        cap_fields = {f["id"] for f in
                      by_id["capacity"]["condition_fields"]}
        self.assertIn("disk_pct", cap_fields)
        for s in out["sources"]:
            for f in s["condition_fields"]:
                self.assertTrue(f["label"])

    def test_context_from_last_run_reaches_design_prompt(self):
        svc = self.make_service()
        svc.orch.tools.last_run_trace = [{
            "tool": "capacity_report",
            "result": {"host_disk_pct": 69.8,
                       "components_down": 0}}]
        stub = _CapturingStub()
        svc.orch.ember = stub
        code, out = svc.wizard_design(
            "Build a new automated agentic workflow.", "c")
        self.assertEqual(code, 200, out)
        user_texts = [m.get("content", "") for call in stub.calls
                      for m in call if m.get("role") == "user"]
        joined = "\n".join(user_texts)
        self.assertIn("Context from the user's last run", joined)
        self.assertIn("capacity_report", joined)
        self.assertIn("host_disk_pct=69.8", joined)
        self.assertTrue(any("capacity_report" in c
                            for c in out["context"]["chips"]))

    def test_context_from_recording(self):
        svc = self.make_service()
        code, out = svc.wizard_options("c", demo_key="watcher-status")
        self.assertEqual(code, 200)
        # The shipped recordings carry a trace; the context names it.
        if out["context"]["chips"]:
            self.assertTrue(any("get_watcher_status" in c
                                for c in out["context"]["chips"]))

    def test_design_only_if_proposes_condition(self):
        svc = self.make_service()
        code, out = svc.wizard_design(
            "Build a new automated agentic workflow that checks the Watcher "
            "every morning at 6 and sends WhatsApp only if a "
            "component is down.", "c")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["validation"]["ok"], out)
        self.assertEqual(out["proposed_params"]["condition"],
                         {"field": "components_down", "op": ">",
                          "value": 0})

    def test_validate_rejects_bad_condition(self):
        svc = self.make_service()
        for cond in ({"field": "nope", "op": ">", "value": 0},
                     {"field": "components_down", "op": "has",
                      "value": 0},
                     {"field": "components_down", "op": ">",
                      "value": "lots"},
                     {"field": "components_down", "op": ">",
                      "value": True}):
            code, out = svc.wizard_validate(
                dict(STUB_PARAMS, condition=cond), "c")
            # wizard_validate's contract: HTTP 200 with ok:false.
            self.assertEqual(code, 200, cond)
            self.assertFalse(out["ok"], cond)
            self.assertIn("condition", out["error"].lower())

    def test_create_with_condition_and_both_actions(self):
        svc = self.make_service()
        code, out = svc.wizard_design(DESIGN_CMD, "c")
        self.assertEqual(code, 200)
        edited = dict(STUB_PARAMS, action=None,
                      actions=["whatsapp", "board"],
                      condition={"field": "components_down",
                                 "op": ">", "value": 0})
        code, out = svc.wizard_create(out["plan_id"], edited, "c")
        self.assertEqual(code, 200, out)
        trace = [t for t in out["trace"]
                 if t["tool"] == "create_agent_flow"]
        result = trace[0]["result"]
        self.assertEqual(result["status"], "DISABLED")
        self.assertEqual(result["actions"], ["whatsapp", "board"])
        self.assertEqual(result["condition"],
                         {"field": "components_down", "op": ">",
                          "value": 0})
        self.assertTrue(any(s.startswith("condition:")
                            for s in result["steps"]))
        entry = svc.orch.tools.agent_flows.list()[0]
        self.assertEqual(entry["actions"], ["whatsapp", "board"])
        self.assertEqual(entry["condition"]["field"],
                         "components_down")

    def test_page_process_ui_markers(self):
        page = TestWizardPage()._page()
        for marker in ("awProcessSteps", "awContextChips",
                       "Only act when a condition is met",
                       "Process steps (runs as a Metatron RPA "
                       "process)",
                       "Build a new automated agentic workflow",
                       "condition_fields"):
            self.assertIn(marker, page)
        old_phrase = "Build me an " + "agent"
        self.assertNotIn(old_phrase, page)


class TestEntryPhrase(unittest.TestCase):
    """Rom (Nov 26): the Automate entry text is "Build a new
    automated agentic workflow" everywhere — page, recordings,
    server copy, orchestrator prompts, record scripts. (Earlier
    the same day it was briefly "…agentic process"; that phrase
    must not come back either.)"""

    def test_old_phrase_absent_repo_wide(self):
        old_phrase = "Build me an " + "agent"
        old_entry = "Build a new agentic " + "process"
        # Rom (Nov 26, later): no context tails on the entry phrase
        # either — the prefill is the bare phrase and the wizard
        # designs from run context.
        capacity_tail = ("that checks capacity every morning at 7 "
                         "and files the report to the " + "board")
        root = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        checked = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__",
                                        "board", "vendor", "tmp")]
            for fn in filenames:
                if not fn.endswith((".py", ".html", ".json",
                                    ".md")):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8",
                          errors="ignore") as f:
                    text = f.read()
                self.assertNotIn(old_phrase, text, path)
                self.assertNotIn(old_entry, text, path)
                self.assertNotIn(capacity_tail, text, path)
                checked += 1
        self.assertGreater(checked, 20)

    def test_prefills_are_exactly_the_bare_phrase(self):
        import json as _json

        import serve
        bare = "Build a new automated agentic workflow."
        for trace in ([{"tool": "capacity_report"}],
                      [{"tool": "get_watcher_status"}],
                      [{"tool": "finops_summary"}], []):
            self.assertEqual(serve.derive_automate_command(trace),
                             bare)
        root = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        with open(os.path.join(root, "demos", "recorded.json"),
                  encoding="utf-8") as f:
            data = _json.load(f)
        for d in data["demos"]:
            self.assertEqual(d["automate_command"], bare, d["key"])
            for s in d["next_steps"]:
                if s["kind"] == "automate":
                    self.assertEqual(s["command"], bare, d["key"])

    def test_new_phrase_present_in_key_surfaces(self):
        root = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        for rel in ("demo_page.html", "serve.py",
                    "demos/recorded.json",
                    "orchestrator/orchestrator.py",
                    "orchestrator/ember_client.py"):
            with open(os.path.join(root, rel),
                      encoding="utf-8") as f:
                self.assertIn("Build a new automated agentic workflow",
                              f.read(), rel)


class TestBarePhraseDerivation(Phase4Case):
    """Rom's live refusal (Nov 26, 2026): the bare prefill
    "Build a new automated agentic workflow." sent via CHECK hit
    "EMBER · NO TOOL FITS" — create_agent_flow requires
    name/trigger/source and Ember would not invent them. The fix:
    the plain propose path now appends the run context for
    agent-build commands (like the wizard design path), and the
    system prompt instructs Ember to DERIVE the parameters from
    that context — safe because a human reviews/edits before
    approval and the result is only a DISABLED draft."""

    def test_system_prompt_instructs_derivation(self):
        from orchestrator.orchestrator import SYSTEM_PROMPT
        self.assertIn("DERIVE the missing parameters", SYSTEM_PROMPT)
        self.assertIn("Refusing because", SYSTEM_PROMPT)
        self.assertIn("capacity_report -> capacity", SYSTEM_PROMPT)
        self.assertIn("'0 6 * * *'", SYSTEM_PROMPT)

    def test_is_agent_build_command(self):
        import serve
        for cmd in ("Build a new automated agentic workflow.",
                    "build a new automated agentic workflow that "
                    "checks capacity",
                    "Create an agent for the morning report",
                    "Build an automation for cost checks",
                    "Automate this",
                    "build me an agent"):
            self.assertTrue(serve.is_agent_build_command(cmd), cmd)
        for cmd in ("Check the Watcher status",
                    "What can you do?",
                    "Send the Watcher status to my WhatsApp",
                    "Save that as Excel",
                    "What breaks if the brain goes down?"):
            self.assertFalse(serve.is_agent_build_command(cmd), cmd)

    def test_propose_appends_context_for_bare_agent_build(self):
        svc = self.make_service()
        svc.orch.tools.last_run_trace = [{
            "tool": "capacity_report",
            "result": {"host_disk_pct": 69.8,
                       "components_down": 0}}]
        stub = _CapturingStub()
        svc.orch.ember = stub
        code, out = svc.propose(DESIGN_CMD, "c")
        self.assertEqual(code, 200, out)
        self.assertFalse(out["refusal"], out)
        self.assertEqual([s["name"] for s in out["steps"]],
                         ["create_agent_flow"])
        # The stub derives the source from the appended context.
        self.assertEqual(out["steps"][0]["arguments"]["source"],
                         "capacity")
        user_texts = [m.get("content", "") for call in stub.calls
                      for m in call if m.get("role") == "user"]
        joined = "\n".join(user_texts)
        self.assertIn("Context from the user's last run", joined)
        self.assertIn("capacity_report", joined)
        self.assertIn("derive the name, trigger, source", joined)
        # The recorded display text stays the bare command.
        self.assertEqual(svc.orch.plans[out["id"]]["user_text"],
                         DESIGN_CMD)

    def test_propose_no_context_for_plain_command(self):
        svc = self.make_service()
        svc.orch.tools.last_run_trace = [{
            "tool": "capacity_report",
            "result": {"host_disk_pct": 69.8}}]
        stub = _CapturingStub()
        svc.orch.ember = stub
        code, out = svc.propose("Check the Watcher status", "c")
        self.assertEqual(code, 200, out)
        user_texts = [m.get("content", "") for call in stub.calls
                      for m in call if m.get("role") == "user"]
        self.assertNotIn("Context from", "\n".join(user_texts))

    def test_wizard_context_text_instructs_derivation(self):
        svc = self.make_service()
        svc.orch.tools.last_run_trace = [{
            "tool": "get_watcher_status",
            "result": {"components_up": 28, "components_total": 28}}]
        ctx = svc._wizard_context()
        self.assertIn("derive the name, trigger, source", ctx["text"])
        self.assertIn("do not refuse for missing", ctx["text"])


class TestAgentCreationGuidance(Phase4Case):
    """Nov 26 UX pass (Rom: "make sure it's agentic, intuitive, and
    guides users"): plain-words hints ride the options payload,
    validator errors teach (name the fix, list friendly choices),
    and the page carries examples + plain summaries."""

    def test_options_carry_plain_words_hints(self):
        svc = self.make_service()
        code, out = svc.wizard_options("c")
        self.assertEqual(code, 200)
        for s in out["sources"]:
            self.assertTrue(s["does"], s)
            self.assertEqual(s["does"], SOURCES[s["id"]]["does"])
        for a in out["actions"]:
            self.assertTrue(a["does"], a)
        for t in out["triggers"]:
            self.assertTrue(t["does"], t)

    def test_validator_errors_teach(self):
        svc = self.make_service()

        def err(params):
            code, out = svc.wizard_validate(params, "c")
            self.assertEqual(code, 200)
            self.assertFalse(out["ok"], params)
            return out["error"]

        e = err(dict(STUB_PARAMS, name="x"))
        self.assertIn("give the automation a name", e)
        self.assertIn("Daily Capacity Report", e)
        e = err(dict(STUB_PARAMS, source="internet"))
        self.assertIn("pick what the automation fetches", e)
        self.assertIn("Capacity report (AIOps)", e)
        e = err(dict(STUB_PARAMS, cron="99 6 * * *"))
        self.assertIn("pick a preset", e)
        e = err(dict(STUB_PARAMS, actions=[]))
        self.assertIn("pick at least one action", e)
        e = err(dict(STUB_PARAMS, condition={
            "field": "nope", "op": ">", "value": 1}))
        self.assertIn("condition field", e)
        self.assertIn("Components down", e)  # friendly label, not id
        self.assertNotIn("components_down", e)
        e = err(dict(STUB_PARAMS, condition={
            "field": "disk_pct", "op": ">", "value": "high"}))
        self.assertIn("must be a number (e.g. 80)", e)

    def test_page_guidance_markers(self):
        page = TestWizardPage()._page()
        for marker in (
                # Describe: ingredients + tappable examples
                "Say what to check, when, and what to do with it",
                "Every weekday at 6 AM, check the Watcher",
                "only WhatsApp me if disk usage is over 80%",
                # plain-words schedule + summary
                "function awHumanCron",
                "function awPlainSummary",
                "Every weekday (Mon–Fri) at ",
                # plain propose path summary
                "function agentStepPlain",
                "Build automation “",
                # teaching validation prefix
                "Fix this to continue: ",
                # post-creation guidance
                "press <b>Test</b> on it in Built Automations",
                "Test previews an automation without sending "
                "anything",
        ):
            self.assertIn(marker, page)


if __name__ == "__main__":
    unittest.main()
