"""Next-steps stage tests (Nov 26, 2026).

After EMBER SUMMARIZES, Ember proposes next steps (parsed from the
compose-final call), the page offers Take-an-action / Automate / Done,
and recordings carry hand-authored next_steps. Offline, $0: stub
Ember + fixture tools.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.cube_tools import ApprovalTokens, FixtureCubeTools
from orchestrator.ember_client import EmberResponse, StubEmberClient
from orchestrator.orchestrator import Orchestrator, parse_next_steps
from orchestrator.spend import SpendMeter
from tests.test_ui import OPERATOR, UiTestCase, fake_auth_factory

VALID = ('All 12 components are up.\n'
         'NEXT_STEPS: [{"label": "Check predictions", "kind": "action", '
         '"command": "What are the current failure predictions?"}, '
         '{"label": "Automate it", "kind": "automate", "command": '
         '"Build a new automated agentic workflow."}]')


class TestParseNextSteps(unittest.TestCase):
    def test_valid_block(self):
        clean, steps = parse_next_steps(VALID)
        self.assertEqual(clean, "All 12 components are up.")
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["kind"], "action")
        self.assertEqual(steps[1]["kind"], "automate")
        self.assertTrue(steps[1]["command"].startswith("Build a new automated agentic workflow"))

    def test_absent_block(self):
        clean, steps = parse_next_steps("Just a summary.")
        self.assertEqual(clean, "Just a summary.")
        self.assertEqual(steps, [])

    def test_malformed_json_stripped_but_empty(self):
        clean, steps = parse_next_steps("Summary.\nNEXT_STEPS: [not json")
        self.assertEqual(clean, "Summary.")
        self.assertEqual(steps, [])

    def test_empty_array(self):
        clean, steps = parse_next_steps("Summary.\nNEXT_STEPS: []")
        self.assertEqual(clean, "Summary.")
        self.assertEqual(steps, [])

    def test_invalid_items_dropped(self):
        text = ('S\nNEXT_STEPS: ['
                '{"label": "ok", "kind": "action", "command": "do it"},'
                '{"label": "bad kind", "kind": "delete", "command": "x"},'
                '{"kind": "action", "command": "no label"},'
                '{"label": "no command", "kind": "action"},'
                '"not a dict"]')
        _, steps = parse_next_steps(text)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["label"], "ok")

    def test_cap_at_four(self):
        items = [{"label": f"s{i}", "kind": "action", "command": "c"}
                 for i in range(7)]
        _, steps = parse_next_steps("S\nNEXT_STEPS: " + json.dumps(items))
        self.assertEqual(len(steps), 4)

    def test_kind_case_insensitive(self):
        _, steps = parse_next_steps(
            'S\nNEXT_STEPS: [{"label": "a", "kind": "AUTOMATE", '
            '"command": "Build a new automated agentic workflow."}]')
        self.assertEqual(steps[0]["kind"], "automate")

    def test_empty_text(self):
        self.assertEqual(parse_next_steps(""), ("", []))
        self.assertEqual(parse_next_steps(None), ("", []))

    def test_marker_mid_text_removed(self):
        clean, steps = parse_next_steps(
            'Before.\nNEXT_STEPS: []\nAfter.')
        self.assertNotIn("NEXT_STEPS", clean)
        self.assertEqual(steps, [])


class PlainStub(StubEmberClient):
    """Stub whose compose emits NO next-steps block (fallback path)."""

    def _compose(self, messages, results, prompt_text):
        return EmberResponse(text="Plain summary, no suggestions.",
                             prompt_tokens=10, completion_tokens=5)


def make_orch(client=None):
    tokens = ApprovalTokens()
    return Orchestrator(client or StubEmberClient(),
                        FixtureCubeTools(tokens), SpendMeter(), tokens)


class TestOrchestratorNextSteps(unittest.TestCase):
    def test_document_run_gets_suggestions(self):
        orch = make_orch()
        plan = orch.propose_content(
            [{"type": "text", "text": "Document attached — analyze it "
              "and file a findings report."}],
            "Document attached — analyze it and file a findings report.",
            purpose="test")
        done = orch.approve(plan["id"])
        self.assertNotIn("NEXT_STEPS", done["final_text"])
        steps = done["next_steps"]
        self.assertTrue(steps, "stub should suggest next steps")
        kinds = {s["kind"] for s in steps}
        self.assertIn("action", kinds)
        self.assertIn("automate", kinds)

    def test_watcher_run_gets_automate(self):
        orch = make_orch()
        plan = orch.propose("What is the Watcher status?", purpose="test")
        done = orch.approve(plan["id"])
        autos = [s for s in done["next_steps"] if s["kind"] == "automate"]
        self.assertEqual(len(autos), 1)
        self.assertEqual(autos[0]["command"],
                         "Build a new automated agentic workflow.")

    def test_fallback_when_block_absent(self):
        orch = make_orch(PlainStub())
        plan = orch.propose("What is the Watcher status?", purpose="test")
        done = orch.approve(plan["id"])
        self.assertEqual(done["final_text"], "Plain summary, no suggestions.")
        self.assertEqual(done["next_steps"], [])


class TestServiceNextSteps(UiTestCase):
    def test_approve_response_carries_next_steps(self):
        svc = self.make_service()
        code, plan = svc.propose("What is the Watcher status?", "c")
        self.assertEqual(code, 200)
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        self.assertIn("next_steps", done)
        self.assertTrue(done["next_steps"])
        self.assertEqual(
            done["automate_command"],
            "Build a new automated agentic workflow.")

    def test_action_dispatch_reuses_propose(self):
        svc = self.make_service()
        _, plan = svc.propose("What is the Watcher status?", "c")
        _, done = svc.approve(plan["id"], "c")
        action = next(s for s in done["next_steps"]
                      if s["kind"] == "action")
        code, follow = svc.propose(action["command"], "c")
        self.assertEqual(code, 200)
        self.assertTrue(follow["steps"])

    def test_automate_dispatch_proposes_create_agent_flow(self):
        svc = self.make_service()
        _, plan = svc.propose("What is the Watcher status?", "c")
        _, done = svc.approve(plan["id"], "c")
        auto = next(s for s in done["next_steps"]
                    if s["kind"] == "automate")
        code, follow = svc.propose(auto["command"], "c")
        self.assertEqual(code, 200)
        self.assertEqual([s["name"] for s in follow["steps"]],
                         ["create_agent_flow"])

    def test_automate_fallback_mapping(self):
        # Rom (Nov 26): the prefill is the bare phrase for every
        # trace — the wizard designs the specifics from run context.
        bare = "Build a new automated agentic workflow."
        cases = [
            [{"tool": "capacity_report"}],
            [{"tool": "file_document_report"}],
            [{"tool": "secops_status"}],
            [{"tool": "finops_summary"}],
            [{"tool": "get_watcher_status"}],
            [{"tool": "list_workflows"}],   # default
            [],                              # default
        ]
        for trace in cases:
            self.assertEqual(serve.derive_automate_command(trace),
                             bare)


class TestDefaultAction(unittest.TestCase):
    """Deterministic one-time follow-up for the Take-action primary,
    used only when Ember proposed no kind=action suggestion."""

    def test_mapping_mirrors_recordings(self):
        cases = [
            ([{"tool": "run_workflow"}], "Check failure predictions"),
            ([{"tool": "get_watcher_status"}], "Send the status to WhatsApp"),
            ([{"tool": "cube_status"}], "Send the status to WhatsApp"),
            ([{"tool": "file_tickets"}], "List current ITSM tickets"),
            ([{"tool": "file_document_report"}], "Check capacity headroom"),
            ([{"tool": "file_intake"}], "Export this extraction as Excel"),
            ([{"tool": "finops_summary"}], "Check capacity headroom"),
            ([{"tool": "secops_status"}], "What breaks if the brain goes down?"),
            ([{"tool": "graph_dependents"}], "Check the CVE posture"),
            ([{"tool": "capacity_report"}], "Give me the full picture"),
            ([{"tool": "predictions"}], "Give me the full picture"),
            ([{"tool": "create_agent_flow"}], "List the RPA flows"),
            ([{"tool": "enable_agent_flow"}], "List the RPA flows"),
        ]
        for trace, label in cases:
            action = serve.derive_default_action(trace)
            self.assertIsNotNone(action, trace)
            self.assertEqual(action["label"], label, trace)
            self.assertTrue(action["command"], trace)

    def test_status_wins_over_later_tools(self):
        # Full-picture trace: the WhatsApp send is the follow-up, as
        # in the recording — table order, not trace order.
        trace = [{"tool": "cube_status"}, {"tool": "graph_dependents"},
                 {"tool": "secops_status"}, {"tool": "predictions"},
                 {"tool": "finops_summary"}]
        self.assertEqual(serve.derive_default_action(trace)["label"],
                         "Send the status to WhatsApp")

    def test_sent_status_is_not_resent(self):
        trace = [{"tool": "get_watcher_status"}, {"tool": "run_workflow"}]
        self.assertEqual(serve.derive_default_action(trace)["label"],
                         "Check failure predictions")

    def test_no_default_for_empty_or_unknown(self):
        self.assertIsNone(serve.derive_default_action([]))
        self.assertIsNone(serve.derive_default_action(None))
        self.assertIsNone(serve.derive_default_action(
            [{"tool": "list_workflows"}]))

    def test_returned_action_is_a_copy(self):
        action = serve.derive_default_action([{"tool": "cube_status"}])
        action["label"] = "mutated"
        again = serve.derive_default_action([{"tool": "cube_status"}])
        self.assertEqual(again["label"], "Send the status to WhatsApp")


class TestServiceDefaultAction(UiTestCase):
    def make_plain_service(self):
        meter = SpendMeter()
        tokens = ApprovalTokens()
        orch = Orchestrator(PlainStub(), FixtureCubeTools(tokens),
                            meter, tokens)
        return serve.DemoService(
            orch, meter, auth_fn=fake_auth_factory(OPERATOR),
            cap_path=self.cap_path, recorded_path=self.recorded_path,
            daily_cap=25)

    def test_approve_response_carries_default_action(self):
        svc = self.make_service()
        _, plan = svc.propose("What is the Watcher status?", "c")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        self.assertEqual(done["default_action"]["label"],
                         "Send the status to WhatsApp")

    def test_empty_suggestions_still_get_default_action(self):
        svc = self.make_plain_service()
        _, plan = svc.propose("What is the Watcher status?", "c")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        self.assertEqual(done["next_steps"], [])
        self.assertEqual(done["default_action"]["label"],
                         "Send the status to WhatsApp")

    def test_default_action_dispatch_reuses_propose(self):
        svc = self.make_plain_service()
        _, plan = svc.propose("What is the Watcher status?", "c")
        _, done = svc.approve(plan["id"], "c")
        code, follow = svc.propose(done["default_action"]["command"], "c")
        self.assertEqual(code, 200)
        self.assertTrue(follow["steps"])


class TestRecordedData(unittest.TestCase):
    def test_shipped_recordings_have_next_steps(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demos", "recorded.json")
        with open(path) as f:
            data = json.load(f)
        # 17 since the PLC Code Converter recording (Nov 27, 2026);
        # 18 since Medical Manual Q&A (Nov 27, 2026); 19 since
        # the Paralegal Assistant (Nov 27, 2026).
        self.assertEqual(len(data["demos"]), 19)
        for d in data["demos"]:
            self.assertIsInstance(d["next_steps"], list, d["key"])
            self.assertEqual(
                d["automate_command"],
                "Build a new automated agentic workflow.", d["key"])
            if d["key"] == "refusal":
                self.assertEqual(d["next_steps"], [])
            else:
                self.assertTrue(d["next_steps"], d["key"])
            for s in d["next_steps"]:
                self.assertIn(s["kind"], ("action", "automate"))
                self.assertTrue(s["label"] and s["command"])

    def test_intake_recordings_suggest_export_first(self):
        # After an intake replay, Take action must prefill the export
        # prompt (mirrors derive_default_action for file_intake).
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demos", "recorded.json")
        with open(path) as f:
            data = json.load(f)
        expected = {
            "intake": "Export this extraction as Excel",
            "pdf-intake": "Export this extraction as Excel",
            "audio-intake": "Save that transcript as a Word file",
            "video-intake": "Save that transcript as a Word file",
        }
        by_key = {d["key"]: d for d in data["demos"]}
        for key, label in expected.items():
            d = by_key[key]
            actions = [s for s in d["next_steps"]
                       if s["kind"] != "automate"]
            self.assertTrue(actions, key)
            self.assertEqual(actions[0]["label"], label, key)
            self.assertEqual(d["default_action"]["label"], label, key)
            self.assertEqual(d["default_action"]["command"],
                             actions[0]["command"], key)


class TestPage(unittest.TestCase):
    def test_next_steps_ui_present(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            page = f.read()
        self.assertIn('data-stage="5"', page)
        self.assertIn("NEXT", page)
        self.assertIn("renderNextSteps", page)
        self.assertIn("Glad to be of service.", page)
        self.assertIn("continueLive", page)
        # Done is local-only: its handler must not fetch. The closing
        # text sits between the done.onclick and the next function.
        i = page.index("done.onclick")
        j = page.index("Glad to be of service.", i)
        self.assertNotIn("fetch(", page[i:j])

    def test_three_primaries_with_sublabels(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            page = f.read()
        # Exactly three primaries, each with its sublabel.
        self.assertIn('mkPrimary("Take action"', page)
        self.assertIn("one-time, using Metatron's tools", page)
        self.assertIn('mkPrimary("Automate"', page)
        self.assertIn("build a new automated agentic workflow", page)
        self.assertIn('mkPrimary("Done"', page)
        self.assertIn('"wrap up"', page)
        # Take action toggles the inline editable prompt in place.
        self.assertIn("ns-editor", page)
        self.assertIn('editor.classList.toggle("open")', page)
        self.assertIn('setAttribute("aria-expanded"', page)

    def test_grouping_and_automate_pick_order(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            page = f.read()
        i = page.index("function renderNextSteps")
        fn = page[i:page.index("/* ---------- Agent Builder", i)]
        # Action suggestions group under Take action; the server's
        # deterministic default stands in only when there are none.
        self.assertIn('steps.filter(s=>s.kind!=="automate")', fn)
        self.assertIn("if(!actions.length&&defaultAction", fn)
        self.assertIn("actions.push(defaultAction)", fn)
        # Automate pick order: Ember's kind=automate suggestion first,
        # then the deterministic prefill, then the generic agent —
        # the picked command prefills the wizard's Describe step.
        self.assertIn('steps.find(s=>s.kind==="automate")', fn)
        self.assertIn(
            "autoSugg?autoSugg.command:(automateCmd||NS_GENERIC_AUTOMATE)",
            fn)
        # Take action renders only when there is something to act on;
        # Done closes with zero API calls (asserted above too).
        self.assertIn("if(actions.length){", fn)
        # Both call sites pass the default action AND the run's trace
        # tools through.
        self.assertIn("d.default_action||null", page)
        self.assertIn("out.default_action||null", page)
        self.assertIn("(d.trace||[]).map(t=>t.tool)", page)
        self.assertIn("(out.trace||[]).map(t=>t.tool)", page)

    def _page(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            return f.read()

    def test_inline_editor_prefilled_and_chips_swap(self):
        page = self._page()
        # The old top-composer loading machinery is gone entirely.
        self.assertNotIn("loadSuggestedPrompt", page)
        self.assertNotIn("composerContinuation", page)
        self.assertNotIn("composerHint", page)
        # The inline editor is prefilled with Ember's top suggestion;
        # with several suggestions, chips swap the box text.
        i = page.index("function buildInlineEditor")
        fn = page[i:page.index("function renderNextSteps", i)]
        self.assertIn("ta.value=actions[0].command", fn)
        self.assertIn("if(actions.length>1){", fn)
        self.assertIn("ta.value=s.command", fn)
        self.assertIn(
            "Suggested prompt — edit if you like, then press CHECK.", fn)
        # Opening the editor (the Take action toggle) never fetches
        # or starts a run — it only reveals + focuses the box.
        j = page.index("take.onclick")
        toggle = page[j:page.index("host.appendChild(take)", j)]
        self.assertIn('editor.classList.toggle("open")', toggle)
        self.assertNotIn("fetch(", toggle)
        self.assertNotIn("continueLive", toggle)
        self.assertNotIn("proposeLive", toggle)
        self.assertNotIn("flashAuth", toggle)

    def test_next_steps_buttons_open_editor_and_wizard(self):
        page = self._page()
        # The old immediate-run helper is gone entirely.
        self.assertNotIn("runCmd", page)
        i = page.index("function renderNextSteps")
        fn = page[i:page.index("/* ---------- Agent Builder", i)]
        # Take action builds + toggles the inline editor; Automate
        # toggles the Ember-native Agent Builder Wizard in place,
        # prefilled with the run's automate command (Rom, Nov 26 —
        # replaces the external Agents-app handoff; no Axion).
        self.assertIn("buildInlineEditor(actions,disableAll,take)", fn)
        self.assertIn(
            "buildAgentWizard(automateCmdFinal,disableAll,auto,demoKey||\"\")",
            fn)
        self.assertIn('wizard.classList.toggle("open")', fn)
        self.assertNotIn("window.open(", fn)
        # No click path inside renderNextSteps starts a run directly
        # (the inline send lives in buildInlineEditor, above).
        self.assertNotIn("continueLive(", fn)
        self.assertNotIn("proposeLive(", fn)
        # The inline CHECK sends the EDITED box text as a
        # continuation, and surfaces the sign-in banner when signed
        # out (the box stays open with the text intact).
        k = page.index("function buildInlineEditor")
        ed = page[k:page.index("function renderNextSteps", k)]
        self.assertIn('(ta.value||"").trim()', ed)
        self.assertIn("if(!WHO.can_run_live){flashAuth();return;}", ed)
        self.assertIn("continueLive(cmd)", ed)
        self.assertIn("disableAll();ta.disabled=true;", ed)

    def test_external_wizard_handoff_removed(self):
        page = self._page()
        # The external Agents-app handoff (which designs with Axion)
        # is gone from the Ember page: no URL builder, no template
        # mapping, no agents.metatroncube.online link.
        for marker in ("AGENTS_URL", "wizardUrlFor",
                       "wizardTemplateFor", "wizardTemplateFromPurpose",
                       "wizardPurpose", "WIZARD_BY_TOOL", "WIZARD_NAMES",
                       "agents.metatroncube.online"):
            self.assertNotIn(marker, page)

    def test_ember_wizard_steps_and_endpoints(self):
        page = self._page()
        i = page.index("function buildAgentWizard")
        fn = page[i:page.index("function buildInlineEditor", i)]
        # Four steps + eligibility note (division of labor explicit:
        # templates are ready-made settings, Ember designs custom
        # ones, Metatron RPA runs, human enables).
        self.assertIn("AGENT BUILDER WIZARD", fn)
        self.assertIn("1 · What", fn)
        self.assertIn("2 · Describe", fn)
        self.assertIn("3 · Review &amp; customize", fn)
        self.assertIn("4 · Approve &amp; create", fn)
        self.assertIn(
            "Pre-built templates are ready-made settings · answer "
            "a few questions to build from scratch · Ember designs "
            "custom ones from your description · runs as a "
            "Metatron RPA process · nothing runs until you enable "
            "it.", fn)
        # Step 1 (What): template cards rendered from server data +
        # the describe-it-myself path; picking a template posts to
        # the deterministic ($0) template endpoint.
        self.assertIn("What do you need?", fn)
        self.assertIn("renderTemplateCards", fn)
        self.assertIn("Describe it myself", fn)
        self.assertIn('"/api/agent-wizard/template"', fn)
        self.assertIn("opts.templates", fn)
        # One "what happens next" line per step.
        self.assertIn("AW_NEXT", fn)
        self.assertIn("next you'll review and customize", fn)
        # Describe is prefilled; design goes to the metered endpoint.
        self.assertIn("desc.value=prefill", fn)
        self.assertIn('"/api/agent-wizard/design"', fn)
        self.assertIn("Ask Ember to design it", fn)
        # Review form: every edit re-validates server-side; the
        # schedule block (presets + custom cron) is schedule-only.
        self.assertIn('"/api/agent-wizard/validate"', fn)
        self.assertIn("syncSchedVisibility", fn)
        self.assertIn("Custom cron", fn)
        # Create posts the FINAL edited params with the plan id, and
        # the enablement card + drafts panel refresh follow.
        self.assertIn('"/api/agent-wizard/create"', fn)
        self.assertIn("params:collectParams()", fn)
        self.assertIn("afterRunCards(out.trace||[])", fn)
        # fromRun=true: a fresh automation auto-expands the (now
        # collapsed-by-default) Built Automations panel.
        self.assertIn("refreshAgentFlows(true)", fn)
        # Cancel rejects the design plan (nothing is created).
        self.assertIn('"/api/reject"', fn)
        # Opening/editing needs no sign-in; sending does.
        self.assertIn("if(!WHO.can_run_live){flashAuth();", fn)
        # Options come from the server allowlist (single source).
        self.assertIn('"/api/agent-wizard/options"', page)

    def test_top_composer_check_starts_fresh_run(self):
        page = self._page()
        # CHECK routes through sendComposer; signed-out CHECK still
        # surfaces the sign-in banner clearly.
        i = page.index('$("proposeBtn").onclick')
        handler = page[i:page.index("};", i)]
        self.assertIn("if(!WHO.can_run_live){flashAuth();return;}", handler)
        self.assertIn("sendComposer()", handler)
        # The top composer is for FRESH commands only now: it reads
        # the composer text and starts a new run (no continuation —
        # continuations happen from the inline NEXT STEPS editor).
        j = page.index("async function sendComposer")
        fn = page[j:page.index("const NS_GENERIC_AUTOMATE", j)]
        self.assertIn('($("cmd").value||"").trim()', fn)
        self.assertIn("return proposeLive(cmd)", fn)
        self.assertNotIn("continueLive", fn)


if __name__ == "__main__":
    unittest.main()
