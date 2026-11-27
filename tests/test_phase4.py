"""Phase 4 tests — Agent Builder, Multimodal Intake, Full Picture,
Document Intelligence.

Offline, $0: stub Ember + fixture tools + fake auth. No network, no
real Activepieces writes (the live verification on the Cube covers
those, once).

    python3 -m unittest discover -s tests -v
"""
import base64
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.agent_builder import (build_flow_trigger,
                                        compose_report_text,
                                        evaluate_condition,
                                        validate_agent_params,
                                        validate_cron)
from orchestrator.cube_tools import (ApprovalError, ApprovalTokens,
                                     FixtureCubeTools, GATED_TOOLS)
from orchestrator.ember_client import StubEmberClient, _normalize_messages
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter
from tests.test_ui import OPERATOR, VIEWER, fake_auth_factory

GOOD_PARAMS = {"name": "Morning Watcher WhatsApp", "trigger": "schedule",
               "cron": "0 6 * * *", "source": "watcher_overview",
               "action": "whatsapp"}


class Phase4Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = {}
        for var, fname in (("EMBER_BOARD_PATH", "board.json"),
                           ("EMBER_INTAKE_PATH", "intake.json"),
                           ("EMBER_REPORTS_PATH", "reports.json"),
                           ("EMBER_AGENT_FLOWS_PATH", "agent_flows.json"),
                           ("EMBER_GOALS_PATH", "goals.json"),
                           ("EMBER_PREFS_PATH", "preferences.json"),
                           ("EMBER_EXPORTS_DIR", "exports")):
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

    def make_service(self, user=OPERATOR, daily_cap=100):
        meter = SpendMeter()
        tokens = ApprovalTokens()
        orch = Orchestrator(StubEmberClient(), FixtureCubeTools(tokens),
                            meter, tokens)
        return serve.DemoService(
            orch, meter, auth_fn=fake_auth_factory(user),
            cap_path=self.cap_path, recorded_path=self.recorded_path,
            daily_cap=daily_cap, reports_path=self.reports_path)


# ---------------------------------------------------------- validator
class TestValidator(unittest.TestCase):
    def test_good_params_pass(self):
        clean = validate_agent_params(dict(GOOD_PARAMS))
        self.assertEqual(clean["cron"], "0 6 * * *")
        self.assertEqual(clean["source"], "watcher_overview")

    def test_webhook_needs_no_cron(self):
        clean = validate_agent_params(
            {"name": "On-demand check", "trigger": "webhook",
             "source": "finops", "action": "board"})
        self.assertEqual(clean["cron"], "")

    def test_rejects_unknown_key(self):
        bad = dict(GOOD_PARAMS, url="http://evil.example/x")
        with self.assertRaises(ValueError):
            validate_agent_params(bad)

    def test_rejects_unknown_source_and_action(self):
        with self.assertRaises(ValueError):
            validate_agent_params(dict(GOOD_PARAMS, source="internet"))
        with self.assertRaises(ValueError):
            validate_agent_params(dict(GOOD_PARAMS, action="email"))

    def test_rejects_bad_trigger(self):
        with self.assertRaises(ValueError):
            validate_agent_params(dict(GOOD_PARAMS, trigger="always"))

    def test_rejects_bad_names(self):
        for name in ("", "x", "a; rm -rf /", "name\nwith newline",
                     "x" * 81):
            with self.assertRaises(ValueError, msg=name):
                validate_agent_params(dict(GOOD_PARAMS, name=name))

    def test_cron_validation(self):
        self.assertEqual(validate_cron("*/15 9-17 * * 1-5"),
                         "*/15 9-17 * * 1-5")
        for bad in ("", "0 6 * *", "0 6 * * * *", "99 6 * * *",
                    "0 25 * * *", "0 6 32 * *", "0 6 * 13 *",
                    "0 6 * * 8", "a b c d e", "0 6 * * *; reboot"):
            with self.assertRaises(ValueError, msg=bad):
                validate_cron(bad)

    def test_cron_rejected_for_webhook(self):
        with self.assertRaises(ValueError):
            validate_agent_params(
                {"name": "Hook agent", "trigger": "webhook",
                 "cron": "0 6 * * *", "source": "finops",
                 "action": "board"})


# ---------------------------------------------------------- flow shape
class TestFlowAssembly(unittest.TestCase):
    def test_schedule_flow_shape(self):
        trig = build_flow_trigger(validate_agent_params(dict(GOOD_PARAMS)))
        self.assertEqual(trig["settings"]["pieceName"],
                         "@activepieces/piece-schedule")
        self.assertEqual(trig["settings"]["input"]["cronExpression"],
                         "0 6 * * *")
        steps = []
        step = trig["nextAction"]
        while step:
            steps.append(step)
            step = step.get("nextAction")
        self.assertEqual([s["name"] for s in steps],
                         ["step_1", "step_2", "step_3"])
        # No LLM anywhere in the assembled flow.
        blob = json.dumps(trig)
        self.assertNotIn("metatron-llm", blob)
        self.assertNotIn("chat/completions", blob)
        # Bare refs only — never the ['output'] display form.
        self.assertNotIn("['output']", blob)
        # HTTP piece body wrapped {"data": ...}.
        body = steps[2]["settings"]["input"]["body"]
        self.assertIn("data", body)
        self.assertEqual(body["data"]["summary"], "{{step_2['text']}}")

    def test_webhook_flow_has_return_step(self):
        params = validate_agent_params(
            {"name": "Hook agent", "trigger": "webhook",
             "source": "secops", "action": "board"})
        trig = build_flow_trigger(params, board_key="k")
        names = []
        step = trig["nextAction"]
        while step:
            names.append(step["name"])
            step = step.get("nextAction")
        self.assertEqual(names, ["step_1", "step_2", "step_3", "step_4"])
        self.assertEqual(trig["settings"]["pieceName"],
                         "@activepieces/piece-webhook")


# --------------------------------------------- actions list + condition
class TestActionsAndCondition(unittest.TestCase):
    def test_actions_list_both(self):
        clean = validate_agent_params(
            dict(GOOD_PARAMS, action=None,
                 actions=["whatsapp", "board"]))
        self.assertEqual(clean["actions"], ["whatsapp", "board"])
        self.assertEqual(clean["action"], "whatsapp+board")
        self.assertIsNone(clean["condition"])

    def test_legacy_action_still_works(self):
        clean = validate_agent_params(dict(GOOD_PARAMS))
        self.assertEqual(clean["actions"], ["whatsapp"])
        self.assertEqual(clean["action"], "whatsapp")

    def test_duplicate_actions_rejected(self):
        with self.assertRaises(ValueError):
            validate_agent_params(
                dict(GOOD_PARAMS, action=None,
                     actions=["board", "board"]))

    def test_unknown_action_in_list_rejected(self):
        with self.assertRaises(ValueError):
            validate_agent_params(
                dict(GOOD_PARAMS, action=None, actions=["email"]))

    def test_empty_actions_rejected(self):
        with self.assertRaises(ValueError):
            validate_agent_params(
                dict(GOOD_PARAMS, action=None, actions=[]))

    def test_action_and_actions_disagree_rejected(self):
        with self.assertRaises(ValueError):
            validate_agent_params(
                dict(GOOD_PARAMS, action="board",
                     actions=["whatsapp"]))

    def test_condition_good(self):
        clean = validate_agent_params(
            dict(GOOD_PARAMS,
                 condition={"field": "components_down", "op": ">",
                            "value": 0}))
        self.assertEqual(clean["condition"],
                         {"field": "components_down", "op": ">",
                          "value": 0})

    def test_condition_bad_field_for_source_rejected(self):
        # disk_pct is a capacity field, not a watcher_overview field.
        with self.assertRaises(ValueError):
            validate_agent_params(
                dict(GOOD_PARAMS,
                     condition={"field": "nope", "op": ">",
                                "value": 0}))
        # budget_pct is a finops field, not a capacity field.
        with self.assertRaises(ValueError):
            validate_agent_params(
                {"name": "Cap watch", "trigger": "schedule",
                 "cron": "0 7 * * *", "source": "capacity",
                 "action": "board",
                 "condition": {"field": "budget_pct",
                               "op": ">", "value": 80}})

    def test_condition_bad_op_rejected(self):
        with self.assertRaises(ValueError):
            validate_agent_params(
                dict(GOOD_PARAMS,
                     condition={"field": "components_down",
                                "op": "contains", "value": 0}))

    def test_condition_bad_value_rejected(self):
        for bad in ("high", True, float("nan"), float("inf"), None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_agent_params(
                    dict(GOOD_PARAMS,
                         condition={"field": "components_down",
                                    "op": ">", "value": bad}))

    def test_evaluate_condition_direct_and_derived(self):
        body = {"totals": {"components": 28, "up": 26,
                           "open_incidents": 1}}
        actual, passed, err = evaluate_condition(
            body, "watcher_overview",
            {"field": "components_down", "op": ">", "value": 0})
        self.assertEqual((actual, passed, err), (2, True, ""))
        actual, passed, err = evaluate_condition(
            body, "watcher_overview",
            {"field": "components_down", "op": ">", "value": 5})
        self.assertEqual((actual, passed, err), (2, False, ""))
        actual, passed, err = evaluate_condition(
            body, "watcher_overview",
            {"field": "open_incidents", "op": "==", "value": 1})
        self.assertEqual((actual, passed, err), (1, True, ""))

    def test_evaluate_condition_unwraps_and_errors(self):
        wrapped = {"output": {"body": {"host_disk_pct": 85.5}}}
        actual, passed, err = evaluate_condition(
            wrapped, "capacity",
            {"field": "disk_pct", "op": ">", "value": 80})
        self.assertEqual((actual, passed, err), (85.5, True, ""))
        actual, passed, err = evaluate_condition(
            {"other": 1}, "capacity",
            {"field": "disk_pct", "op": ">", "value": 80})
        self.assertIsNone(actual)
        self.assertFalse(passed)
        self.assertIn("missing or not numeric", err)

    def test_compose_suffix_only_when_condition_passes(self):
        body = {"totals": {"components": 28, "up": 26}}
        cond = {"field": "components_down", "op": ">", "value": 0}
        text = compose_report_text("Watcher overview", body,
                                   source="watcher_overview",
                                   condition=cond)
        self.assertIn(" · condition met: Components down = 2", text)
        body_ok = {"totals": {"components": 28, "up": 28}}
        text = compose_report_text("Watcher overview", body_ok,
                                   source="watcher_overview",
                                   condition=cond)
        self.assertNotIn("condition met", text)


class TestProcessFlowAssembly(unittest.TestCase):
    def _steps(self, trig):
        out = []
        step = trig["nextAction"]
        while step:
            out.append(step)
            step = step.get("nextAction")
        return out

    def test_condition_gate_inputs_in_code_step(self):
        params = validate_agent_params(
            dict(GOOD_PARAMS,
                 condition={"field": "components_down", "op": ">",
                            "value": 0}))
        trig = build_flow_trigger(params)
        steps = self._steps(trig)
        code = steps[1]
        self.assertEqual(code["displayName"],
                         "Compose report + condition gate")
        inputs = code["settings"]["input"]
        self.assertEqual(inputs["cond_op"], ">")
        self.assertEqual(inputs["cond_value"], "0")
        self.assertEqual(inputs["cond_label"], "Components down")
        self.assertEqual(inputs["cond_a"], "totals.components")
        self.assertEqual(inputs["cond_b"], "totals.up")
        self.assertNotIn("cond_path", inputs)

    def test_no_condition_no_gate_inputs(self):
        trig = build_flow_trigger(validate_agent_params(
            dict(GOOD_PARAMS)))
        code = self._steps(trig)[1]
        self.assertEqual(code["displayName"], "Compose report")
        self.assertNotIn("cond_op", code["settings"]["input"])

    def test_two_actions_two_http_steps(self):
        params = validate_agent_params(
            dict(GOOD_PARAMS, action=None,
                 actions=["whatsapp", "board"]))
        trig = build_flow_trigger(params)
        steps = self._steps(trig)
        self.assertEqual([s["name"] for s in steps],
                         ["step_1", "step_2", "step_3", "step_4"])
        displays = [s["displayName"] for s in steps]
        self.assertIn("Notify via Watcher WhatsApp flow", displays)
        self.assertIn("File report to Ember board", displays)

    def test_webhook_two_actions_return_step_last(self):
        params = validate_agent_params(
            {"name": "Hook agent", "trigger": "webhook",
             "source": "secops",
             "actions": ["whatsapp", "board"]})
        trig = build_flow_trigger(params, board_key="k")
        steps = self._steps(trig)
        self.assertEqual([s["name"] for s in steps],
                         ["step_1", "step_2", "step_3", "step_4",
                          "step_5"])
        self.assertEqual(steps[-1]["displayName"], "Return result")


# ---------------------------------------------------------- gated tools
class TestGatedTools(Phase4Case):
    def tools(self):
        tokens = ApprovalTokens()
        return FixtureCubeTools(tokens), tokens

    def test_new_tools_are_gated(self):
        for name in ("create_agent_flow", "file_intake",
                     "file_document_report"):
            self.assertIn(name, GATED_TOOLS)

    def test_create_agent_flow_needs_token(self):
        tools, _ = self.tools()
        with self.assertRaises(ApprovalError):
            tools.create_agent_flow(dict(GOOD_PARAMS), approval_token="")

    def test_create_agent_flow_fixture(self):
        tools, tokens = self.tools()
        out = tools.create_agent_flow(dict(GOOD_PARAMS),
                                      approval_token=tokens.issue())
        self.assertEqual(out["status"], "DISABLED")
        self.assertTrue(out["flow_id"].startswith("sim-flow-"))

    def test_create_agent_flow_rejects_bad_params(self):
        tools, tokens = self.tools()
        with self.assertRaises(ValueError):
            tools.create_agent_flow(dict(GOOD_PARAMS, source="internet"),
                                    approval_token=tokens.issue())

    def test_file_intake_needs_token_and_validates(self):
        tools, tokens = self.tools()
        rec = {"kind": "invoice", "fields": {"vendor": "Acme"}}
        with self.assertRaises(ApprovalError):
            tools.file_intake(rec, approval_token="")
        with self.assertRaises(ValueError):
            tools.file_intake({"kind": "spreadsheet", "fields": {"a": 1}},
                              approval_token=tokens.issue())
        out = tools.file_intake(rec, approval_token=tokens.issue())
        self.assertEqual(out["intake_total"], 1)
        self.assertEqual(tools.intake.list()[0]["fields"]["vendor"],
                         "Acme")

    def test_file_document_report_validates(self):
        tools, tokens = self.tools()
        rep = {"title": "Log scan", "summary": "2 issues",
               "findings": [{"line": 118, "severity": "high",
                             "detail": "disk full"}]}
        with self.assertRaises(ApprovalError):
            tools.file_document_report(rep, approval_token="")
        with self.assertRaises(ValueError):
            tools.file_document_report(
                {"title": "x", "summary": "y",
                 "findings": [{"severity": "scary", "detail": "z"}]},
                approval_token=tokens.issue())
        out = tools.file_document_report(rep,
                                         approval_token=tokens.issue())
        self.assertEqual(out["findings_count"], 1)
        self.assertEqual(tools.reports.list()[0]["findings"][0]["line"],
                         118)


# ---------------------------------------------------------- endpoints
class TestEndpoints(Phase4Case):
    TINY_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()

    def test_intake_requires_operator(self):
        svc = self.make_service(user=None)
        code, _ = svc.intake(self.TINY_PNG_B64, "image/png", "", "")
        self.assertEqual(code, 403)
        svc = self.make_service(user=VIEWER)
        code, _ = svc.intake(self.TINY_PNG_B64, "image/png", "", "c")
        self.assertEqual(code, 403)

    def test_intake_rejects_bad_media_and_oversize(self):
        svc = self.make_service()
        code, _ = svc.intake(self.TINY_PNG_B64, "application/pdf", "", "c")
        self.assertEqual(code, 400)
        code, _ = svc.intake("!!!not-base64!!!", "image/png", "", "c")
        self.assertEqual(code, 400)
        big = base64.b64encode(b"x" * (serve.INTAKE_MAX_BYTES + 1)).decode()
        code, _ = svc.intake(big, "image/png", "", "c")
        self.assertEqual(code, 413)

    def test_intake_propose_approve_flow(self):
        svc = self.make_service()
        code, plan = svc.intake(self.TINY_PNG_B64, "image/png", "", "c")
        self.assertEqual(code, 200)
        self.assertEqual(plan["steps"][0]["name"], "file_intake")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        self.assertIn("Filed", done["final_text"])
        self.assertEqual(done["trace"][0]["result"]["intake_total"], 1)

    def test_document_requires_operator_and_caps_size(self):
        svc = self.make_service(user=None)
        code, _ = svc.document("line one\nline two", "", "")
        self.assertEqual(code, 403)
        svc = self.make_service()
        code, _ = svc.document("x" * (serve.DOCUMENT_MAX_CHARS + 1),
                               "", "c")
        self.assertEqual(code, 413)
        code, _ = svc.document("   ", "", "c")
        self.assertEqual(code, 400)

    def test_document_propose_approve_flow(self):
        svc = self.make_service()
        code, plan = svc.document("INFO ok\nERROR disk full\nINFO ok",
                                  "test.log", "c")
        self.assertEqual(code, 200)
        self.assertEqual(plan["steps"][0]["name"], "file_document_report")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        self.assertEqual(done["trace"][0]["result"]["findings_count"], 2)

    def test_board_file_key_gate(self):
        svc = self.make_service()
        payload = {"title": "Agent report", "summary": "all good",
                   "source": "watcher_overview"}
        old = os.environ.get("CUBE_SERVICES_API_KEY")
        os.environ["CUBE_SERVICES_API_KEY"] = "test-key-123"
        try:
            code, _ = svc.board_file(payload, "")
            self.assertEqual(code, 401)
            code, _ = svc.board_file(payload, "wrong")
            self.assertEqual(code, 401)
            code, body = svc.board_file(payload, "test-key-123")
            self.assertEqual(code, 200)
            self.assertEqual(body["reports_total"], 1)
            code, _ = svc.board_file({"title": "x"}, "test-key-123")
            self.assertEqual(code, 400)
        finally:
            if old is None:
                os.environ.pop("CUBE_SERVICES_API_KEY", None)
            else:
                os.environ["CUBE_SERVICES_API_KEY"] = old


# ---------------------------------------------------------- stub flows
class TestStubFlows(Phase4Case):
    def test_agent_builder_end_to_end(self):
        svc = self.make_service()
        code, plan = svc.propose(
            "Build a new automated agentic workflow.", "c")
        self.assertEqual(code, 200)
        self.assertEqual(plan["steps"][0]["name"], "create_agent_flow")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        result = done["trace"][0]["result"]
        self.assertEqual(result["status"], "DISABLED")
        self.assertIn("DISABLED", done["final_text"])

    def test_full_picture_chains_tools(self):
        svc = self.make_service()
        code, plan = svc.propose(
            "Give me the full picture on the Cube: overall status, "
            "what breaks if the brain goes down, CVE exposure, "
            "failure predictions, and this month's costs.", "c")
        self.assertEqual(code, 200)
        names = [s["name"] for s in plan["steps"]]
        self.assertGreaterEqual(len(names), 3)
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        self.assertIn("Full picture", done["final_text"])

    def test_multimodal_normalize_passthrough(self):
        parts = [{"type": "text", "text": "read this"},
                 {"type": "image_url",
                  "image_url": {"url": "data:image/png;base64,AAA"}}]
        out = _normalize_messages([{"role": "user", "content": parts}])
        self.assertEqual(out[0]["content"], parts)

    def test_image_scrubbed_from_stored_plan(self):
        svc = self.make_service()
        code, plan = svc.intake(
            base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
            "image/png", "", "c")
        self.assertEqual(code, 200)
        stored = svc.orch.plans[plan["id"]]["messages"]
        blob = json.dumps(stored)
        self.assertNotIn("data:image/png;base64", blob)
        self.assertIn("[image attached", blob)


# ------------------------------------------- truncation regression
# Rom's Nov 26 intake failure: a messy photo made Ember burn the whole
# 512-token budget reasoning (finish_reason "length", no tool call),
# twice — the page then rendered the partial rationale as a refusal.
class _ScriptedClient(StubEmberClient):
    """Returns queued EmberResponses; records max_tokens per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.seen_max_tokens = []

    def chat(self, messages, tools, tool_choice="auto", max_tokens=None):
        self.seen_max_tokens.append(max_tokens)
        if self._responses:
            return self._responses.pop(0)
        return super().chat(messages, tools, tool_choice, max_tokens)


def _intake_call(vendor="Acme"):
    from orchestrator.ember_client import ToolCall
    return ToolCall("file_intake", {"record": {
        "kind": "invoice", "fields": {"vendor": vendor}}})


class TestTruncationRecovery(Phase4Case):
    TINY_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()

    def make_service_with(self, client):
        meter = SpendMeter()
        tokens = ApprovalTokens()
        orch = Orchestrator(client, FixtureCubeTools(tokens),
                            meter, tokens)
        return serve.DemoService(
            orch, meter, auth_fn=fake_auth_factory(OPERATOR),
            cap_path=self.cap_path, recorded_path=self.recorded_path,
            daily_cap=100, reports_path=self.reports_path)

    def test_length_truncation_retries_with_bigger_budget(self):
        from orchestrator.ember_client import EmberResponse
        client = _ScriptedClient([
            EmberResponse(
                text="Extracting the visible ticket fields and filing "
                     "them for your approval.",
                tool_calls=[], prompt_tokens=7424,
                completion_tokens=512, finish_reason="length"),
            EmberResponse(text="Filing the extracted fields.",
                          tool_calls=[_intake_call()],
                          prompt_tokens=7484, completion_tokens=180,
                          finish_reason="tool_calls"),
        ])
        svc = self.make_service_with(client)
        code, plan = svc.intake(self.TINY_PNG_B64, "image/png", "", "c")
        self.assertEqual(code, 200)
        self.assertEqual(plan["steps"][0]["name"], "file_intake")
        # First call got the intake budget; the retry escalated past it.
        self.assertEqual(client.seen_max_tokens[0], 2048)
        self.assertGreaterEqual(client.seen_max_tokens[1], 2048)

    def test_truncated_call_args_retry(self):
        from orchestrator.ember_client import EmberResponse, ToolCall
        bad = ToolCall("file_intake", {}, args_truncated=True)
        client = _ScriptedClient([
            EmberResponse(text="", tool_calls=[bad],
                          prompt_tokens=7400, completion_tokens=512,
                          finish_reason="length"),
            EmberResponse(text="Filing.", tool_calls=[_intake_call()],
                          prompt_tokens=7500, completion_tokens=170,
                          finish_reason="tool_calls"),
        ])
        svc = self.make_service_with(client)
        code, plan = svc.intake(self.TINY_PNG_B64, "image/png", "", "c")
        self.assertEqual(code, 200)
        self.assertEqual(
            plan["steps"][0]["arguments"]["record"]["fields"]["vendor"],
            "Acme")

    def test_no_filing_step_is_clear_error_not_empty_plan(self):
        from orchestrator.ember_client import EmberResponse
        prose = EmberResponse(text="I see a blurry photo.",
                              tool_calls=[], prompt_tokens=7000,
                              completion_tokens=40,
                              finish_reason="stop")
        client = _ScriptedClient([prose, prose])
        svc = self.make_service_with(client)
        code, obj = svc.intake(self.TINY_PNG_B64, "image/png", "", "c")
        self.assertEqual(code, 502)
        self.assertIn("could not extract fields", obj["error"])

    def test_meta_client_surfaces_finish_reason_and_truncation(self):
        from unittest import mock
        from orchestrator import ember_client as ec

        def fake_open(payload):
            class _Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

                def read(self_inner):
                    return json.dumps(payload).encode()

            class _Opener:
                def open(self_inner, request, timeout=None):
                    return _Resp()

            return _Opener()

        truncated = {"choices": [{
            "finish_reason": "length",
            "message": {"content": "Extracting...",
                        "tool_calls": [{"id": "c1", "function": {
                            "name": "file_intake",
                            "arguments": "{\"record\": {\"kind\": \"inv"}}]}}],
            "usage": {"prompt_tokens": 7424, "completion_tokens": 512}}
        client = ec.MetaEmberClient(api_key="test-key")
        with mock.patch.object(ec.urllib.request, "build_opener",
                               return_value=fake_open(truncated)):
            resp = client.chat([{"role": "user", "content": "x"}], [])
        self.assertEqual(resp.finish_reason, "length")
        self.assertTrue(resp.tool_calls[0].args_truncated)
        self.assertEqual(resp.tool_calls[0].arguments, {})

        clean = {"choices": [{
            "finish_reason": "tool_calls",
            "message": {"content": "",
                        "tool_calls": [{"id": "c2", "function": {
                            "name": "file_intake",
                            "arguments": "{\"record\": {\"kind\": "
                                         "\"invoice\"}}"}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        with mock.patch.object(ec.urllib.request, "build_opener",
                               return_value=fake_open(clean)):
            resp = client.chat([{"role": "user", "content": "x"}], [])
        self.assertEqual(resp.finish_reason, "tool_calls")
        self.assertFalse(resp.tool_calls[0].args_truncated)
        self.assertEqual(resp.tool_calls[0].arguments,
                         {"record": {"kind": "invoice"}})


# ------------------------------------------- Agent Builder enablement
class TestEnablement(Phase4Case):
    def tools(self):
        tokens = ApprovalTokens()
        return FixtureCubeTools(tokens), tokens

    def test_create_registers_flow(self):
        tools, tokens = self.tools()
        out = tools.create_agent_flow(dict(GOOD_PARAMS),
                                      approval_token=tokens.issue())
        entry = tools.agent_flows.get(out["flow_id"])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], GOOD_PARAMS["name"])
        self.assertEqual(entry["cron"], "0 6 * * *")
        self.assertFalse(entry["enabled"])

    def test_enable_requires_token(self):
        tools, tokens = self.tools()
        out = tools.create_agent_flow(dict(GOOD_PARAMS),
                                      approval_token=tokens.issue())
        with self.assertRaises(ApprovalError):
            tools.enable_agent_flow(out["flow_id"], approval_token="")

    def test_enable_rejects_unknown_flow_id(self):
        tools, tokens = self.tools()
        with self.assertRaises(ValueError):
            tools.enable_agent_flow("G0eS7Pl1gEzko858Lv7PJ",
                                    approval_token=tokens.issue())

    def test_enable_happy_path_marks_registry(self):
        tools, tokens = self.tools()
        out = tools.create_agent_flow(dict(GOOD_PARAMS),
                                      approval_token=tokens.issue())
        res = tools.enable_agent_flow(out["flow_id"],
                                      approval_token=tokens.issue())
        self.assertEqual(res["status"], "ENABLED")
        entry = tools.agent_flows.get(out["flow_id"])
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["enabled_at"])

    def test_agent_flows_endpoint_auth_and_listing(self):
        svc = self.make_service(user=None)
        code, _ = svc.agent_flows("")
        self.assertEqual(code, 403)
        svc = self.make_service(user=VIEWER)
        code, _ = svc.agent_flows("c")
        self.assertEqual(code, 403)
        svc = self.make_service()
        code, plan = svc.propose(
            "Build a new automated agentic workflow.", "c")
        self.assertEqual(code, 200)
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        code, obj = svc.agent_flows("c")
        self.assertEqual(code, 200)
        self.assertEqual(len(obj["flows"]), 1)
        self.assertFalse(obj["flows"][0]["enabled"])

    def test_stub_enable_end_to_end(self):
        svc = self.make_service()
        code, plan = svc.propose(
            "Build a new automated agentic workflow.", "c")
        self.assertEqual(code, 200)
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        fid = done["trace"][0]["result"]["flow_id"]
        code, plan2 = svc.propose(
            f'Enable the automation flow {fid} ("Morning Watcher '
            f'WhatsApp").', "c")
        self.assertEqual(code, 200)
        self.assertEqual(plan2["steps"][0]["name"], "enable_agent_flow")
        self.assertEqual(
            plan2["steps"][0]["arguments"]["flow_id"], fid)
        code, done2 = svc.approve(plan2["id"], "c")
        self.assertEqual(code, 200)
        self.assertIn("Enabled ✓", done2["final_text"])
        self.assertTrue(svc.orch.tools.agent_flows.get(fid)["enabled"])


if __name__ == "__main__":
    unittest.main()
