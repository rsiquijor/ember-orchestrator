"""PLC Code Converter tests (Nov 27, 2026).

Ember converts PLC programs Allen-Bradley <-> Siemens with the
vendor manuals as long context; plc_converter validates the
per-instruction coverage report deterministically (unsupported
instructions flagged, never silently replaced). Converted code
is DRAFT — never deployed. Change approval routes through the
RPA flow (fixture-simulated here).

Offline, $0: stub Ember + fixture tools + fake auth. No network.

    python3 -m unittest discover -s tests -v
"""
import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.cube_tools import (ApprovalError, ApprovalTokens,
                                     FixtureCubeTools, GATED_TOOLS,
                                     LOCAL_ONLY_TOOLS, load_catalog)
from orchestrator.ember_client import (EmberResponse, StubEmberClient,
                                       ToolCall)
from orchestrator.orchestrator import Orchestrator
from orchestrator.plc_converter import (SAMPLE_CONVERSIONS, SAMPLES,
                                        SOURCE_PLATFORM,
                                        load_manual_context,
                                        parse_source_instructions,
                                        validate_conversion)
from orchestrator.spend import SpendMeter
from tests.test_ui import OPERATOR, fake_auth_factory


class PlcCase(unittest.TestCase):
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
                           ("EMBER_DASHBOARDS_PATH", "dashboards.json"),
                           ("EMBER_PLC_CONVERSIONS_PATH",
                            "plc_conversions.json")):
            self._saved[var] = os.environ.get(var)
            os.environ[var] = os.path.join(self._tmp.name, fname)
        # Tiny fake manuals corpus so the long-context assembly
        # is exercised without the real PDFs.
        self.manuals_dir = os.path.join(self._tmp.name, "manuals")
        os.makedirs(self.manuals_dir)
        with open(os.path.join(self.manuals_dir,
                               "ab-fake-reference.txt"), "w") as f:
            f.write("TON timer on delay. CTU count up. CPT compute. "
                    "LIM limit test. MSG message. " * 40)
        with open(os.path.join(self.manuals_dir,
                               "siemens-fake-reference.txt"), "w") as f:
            f.write("TON on-delay. CTU count up. NORM_X normalize. "
                    "SCALE_X scale. LIMIT limit. WRREC write record. "
                    * 40)
        self._saved["EMBER_PLC_MANUALS_DIR"] = os.environ.get(
            "EMBER_PLC_MANUALS_DIR")
        os.environ["EMBER_PLC_MANUALS_DIR"] = self.manuals_dir
        self.cap_path = os.path.join(self._tmp.name, "cap.json")
        self.recorded_path = os.path.join(self._tmp.name,
                                          "recorded.json")
        self.reports_path = os.path.join(self._tmp.name,
                                         "reports.json")

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


def fixture_tools():
    return FixtureCubeTools(ApprovalTokens())


def canned(direction):
    conv = copy.deepcopy(SAMPLE_CONVERSIONS[direction])
    conv["direction"] = direction
    return conv


# ---------------------------------------------------------- parser
class TestParser(unittest.TestCase):
    def test_ab_sample_instructions(self):
        _, src = SAMPLES["ab_to_siemens"]
        found = parse_source_instructions(src, "ab")
        self.assertEqual(
            found,
            ["TON", "CTU", "RES", "CPT", "LIM", "MUL", "GRT", "MSG"])

    def test_siemens_sample_instructions(self):
        _, src = SAMPLES["siemens_to_ab"]
        found = parse_source_instructions(src, "siemens")
        self.assertEqual(found,
                         ["SCALE_X", "NORM_X", "LIMIT", "WRREC"])

    def test_comments_and_keywords_ignored(self):
        code = ("(* MSG mentioned in a comment *)\n"
                "IF Start THEN\n  TON(T1);\nEND_IF;\n")
        self.assertEqual(parse_source_instructions(code, "ab"),
                         ["TON"])

    def test_unknown_allcaps_call_is_candidate(self):
        code = "FOOBAR(X);\nTON(T1);"
        found = parse_source_instructions(code, "ab")
        self.assertIn("FOOBAR", found)
        self.assertIn("TON", found)


# ---------------------------------------------------------- validator
class TestValidator(unittest.TestCase):
    def test_accepts_both_directions(self):
        for direction in ("ab_to_siemens", "siemens_to_ab"):
            clean = validate_conversion(canned(direction))
            self.assertEqual(clean["direction"], direction)
            self.assertEqual(clean["stats"]["unsupported"], 1)

    def test_rejects_bad_direction(self):
        bad = canned("ab_to_siemens")
        bad["direction"] = "ab_to_mitsubishi"
        with self.assertRaises(ValueError):
            validate_conversion(bad)

    def test_rejects_incomplete_coverage(self):
        bad = canned("ab_to_siemens")
        bad["coverage"] = [c for c in bad["coverage"]
                           if c["instruction"] != "MSG"]
        with self.assertRaises(ValueError) as cm:
            validate_conversion(bad)
        self.assertIn("MSG", str(cm.exception))

    def test_rejects_source_only_survivors(self):
        bad = canned("ab_to_siemens")
        bad["converted_code"] += "\n#X := CPT(1 + 2);\n"
        with self.assertRaises(ValueError) as cm:
            validate_conversion(bad)
        self.assertIn("CPT", str(cm.exception))

    def test_rejects_unused_distinctive_target(self):
        bad = canned("siemens_to_ab")
        # Claim NORM_X -> CPT but remove CPT from the code.
        bad["converted_code"] = bad["converted_code"].replace(
            "CPT((Analog_In - 4000.0) * 100.0 / 16000.0)",
            "(Analog_In - 4000.0) * 100.0 / 16000.0")
        with self.assertRaises(ValueError) as cm:
            validate_conversion(bad)
        self.assertIn("CPT", str(cm.exception))

    def test_rejects_missing_code(self):
        bad = canned("ab_to_siemens")
        bad["converted_code"] = ""
        with self.assertRaises(ValueError):
            validate_conversion(bad)

    def test_rejects_bad_status(self):
        bad = canned("ab_to_siemens")
        bad["coverage"][0]["status"] = "done"
        with self.assertRaises(ValueError):
            validate_conversion(bad)

    def test_parens_in_comments_and_strings_ignored(self):
        ok = canned("ab_to_siemens")
        ok["converted_code"] += (
            "\n// note (unbalanced in a comment\n"
            "(* block comment ( with paren *)\n"
            "#Note := 'text (with paren';\n")
        clean = validate_conversion(ok)
        self.assertEqual(clean["direction"], "ab_to_siemens")

    def test_genuinely_unbalanced_parens_rejected(self):
        bad = canned("ab_to_siemens")
        bad["converted_code"] += "\n#X := (1 + 2;\n"
        with self.assertRaises(ValueError) as cm:
            validate_conversion(bad)
        self.assertIn("unbalanced", str(cm.exception))

    def test_coverage_prose_and_combined_names(self):
        ok = canned("siemens_to_ab")
        ok["coverage"] = [
            {"instruction": "NORM_X/SCALE_X (analog scaling)",
             "status": "mapped", "target": "CPT (compute)",
             "note": ""},
            {"instruction": "LIMIT — clamp to range",
             "status": "mapped", "target": "MAX", "note": ""},
            {"instruction": "WRREC (write data record)",
             "status": "unsupported", "target": "",
             "note": "no AB equivalent"},
            {"instruction": "TON", "status": "mapped",
             "target": "TON", "note": "voluntary"},
            {"instruction": "CTU", "status": "mapped",
             "target": "CTU", "note": "voluntary"},
        ]
        clean = validate_conversion(ok)
        names = [c["instruction"] for c in clean["coverage"]]
        self.assertIn("NORM_X", names)
        self.assertIn("LIMIT", names)
        self.assertIn("WRREC", names)

    def test_manual_context_assembly(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "ab-x.txt"), "w") as f:
                f.write("ABREF " * 100)
            with open(os.path.join(d, "siemens-y.txt"), "w") as f:
                f.write("SIREF " * 100)
            text, stats = load_manual_context(
                "ab_to_siemens", directory=d, char_budget=1200)
            self.assertIn("ABREF", text)
            self.assertIn("SIREF", text)
            self.assertEqual(len(stats["manuals"]), 2)
            self.assertLessEqual(stats["chars"], 1200)
        text, stats = load_manual_context(
            "ab_to_siemens", directory="/nonexistent-dir-xyz")
        self.assertEqual(text, "")
        self.assertEqual(stats["manuals"], [])


# ---------------------------------------------------------- gated tool
class TestConvertTool(PlcCase):
    def test_gated_and_local(self):
        self.assertIn("convert_plc_code", GATED_TOOLS)
        self.assertIn("convert_plc_code", LOCAL_ONLY_TOOLS)

    def test_requires_token(self):
        tools = fixture_tools()
        with self.assertRaises(ApprovalError):
            tools.convert_plc_code(canned("ab_to_siemens"),
                                   approval_token="bogus")

    def test_convert_stores_and_verifies(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Convert the sample Allen-Bradley conveyor program "
            "to Siemens SCL.", "c")
        self.assertEqual(code, 200, plan)
        self.assertEqual(plan["steps"][0]["name"],
                         "convert_plc_code")
        self.assertTrue(plan["plc_context"]["manuals"])
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200, done)
        result = done["trace"][0]["result"]
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["stats"]["flagged"], ["MSG"])
        entry = orch.tools.plc_conversions.get(
            result["conversion_id"])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "draft")
        self.assertTrue(entry["manual_context"]["manuals"])
        ver = [c for c in done.get("verification") or []
               if c.get("name") == "convert_plc_code"]
        self.assertTrue(ver and all(c.get("ok") for c in ver), ver)

    def test_convert_reverse_direction(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Convert the sample Siemens conveyor program to "
            "Allen-Bradley.", "c")
        self.assertEqual(code, 200, plan)
        args = plan["steps"][0]["arguments"]
        self.assertEqual(args["direction"], "siemens_to_ab")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200, done)
        result = done["trace"][0]["result"]
        self.assertEqual(result["stats"]["flagged"], ["WRREC"])

    def test_undo_registry_scoped(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Convert the sample Allen-Bradley conveyor program "
            "to Siemens SCL.", "c")
        code, done = svc.approve(plan["id"], "c")
        cid = done["trace"][0]["result"]["conversion_id"]
        out = orch.tools.undo_plc_delete(cid)
        self.assertTrue(out["undone"])
        self.assertIsNone(orch.tools.plc_conversions.get(cid))
        self.assertFalse(orch.tools.undo_plc_delete(cid)["undone"])
        self.assertFalse(
            orch.tools.undo_plc_delete("plc-nope")["undone"])

    def test_send_approval_fixture(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Convert the sample Allen-Bradley conveyor program "
            "to Siemens SCL.", "c")
        code, done = svc.approve(plan["id"], "c")
        cid = done["trace"][0]["result"]["conversion_id"]
        code, out = svc.plc_send_approval(cid, "c")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["rfc_ref"], "RFC-0000")
        entry = orch.tools.plc_conversions.get(cid)
        self.assertEqual(entry["status"], "change_requested")
        # Idempotent: second send returns the existing RFC.
        code, out2 = svc.plc_send_approval(cid, "c")
        self.assertEqual(code, 200, out2)
        self.assertTrue(out2["already_sent"])
        code, out3 = svc.plc_send_approval("plc-nope", "c")
        self.assertEqual(code, 404, out3)

    def test_download_carries_draft_banner(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Convert the sample Allen-Bradley conveyor program "
            "to Siemens SCL.", "c")
        code, done = svc.approve(plan["id"], "c")
        cid = done["trace"][0]["result"]["conversion_id"]
        code, payload, headers = svc.plc_download(cid, "c")
        self.assertEqual(code, 200)
        text = payload.decode()
        self.assertIn("DRAFT PLC CONVERSION", text)
        self.assertIn("Do not deploy", text)
        code, _, _ = svc.plc_download("plc-nope", "c")
        self.assertEqual(code, 404)

    def test_export_plc_source(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Convert the sample Allen-Bradley conveyor program "
            "to Siemens SCL.", "c")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200, done)
        code, plan2 = svc.propose(
            "Save the converted PLC code as TXT.", "c")
        self.assertEqual(code, 200, plan2)
        self.assertEqual(plan2["steps"][0]["name"], "export_file")
        self.assertEqual(
            plan2["steps"][0]["arguments"]["source"], "plc")
        code, done2 = svc.approve(plan2["id"], "c")
        self.assertEqual(code, 200, done2)
        result = done2["trace"][0]["result"]
        self.assertTrue(result["exported"], result)


# ---------------------------------------------------------- serve path
class _BadPlcClient(StubEmberClient):
    """Emits an invalid conversion N times, then delegates."""

    def __init__(self, bad_times):
        self.bad_times = bad_times
        self.calls = 0

    def _plan(self, user, prompt_text):
        self.calls += 1
        if self.calls <= self.bad_times:
            conv = canned("ab_to_siemens")
            conv["coverage"] = [c for c in conv["coverage"]
                                if c["instruction"] != "MSG"]
            return EmberResponse(
                text="Plan: convert.",
                tool_calls=[ToolCall("convert_plc_code", conv)],
                prompt_tokens=10, completion_tokens=10)
        return super()._plan(user, prompt_text)


class TestServePlcPath(PlcCase):
    def test_command_detection(self):
        self.assertTrue(serve.is_plc_convert_command(
            "Convert the sample Allen-Bradley conveyor program "
            "to Siemens SCL."))
        self.assertTrue(serve.is_plc_convert_command(
            "Translate this SCL to Studio 5000"))
        self.assertTrue(serve.is_plc_convert_command(
            "Convert PLC code"))
        self.assertFalse(serve.is_plc_convert_command(
            "What is the PLC status?"))
        self.assertFalse(serve.is_plc_convert_command(
            "Convert that file to Excel"))

    def test_direction_sniffing(self):
        self.assertEqual(
            serve._plc_direction_for(
                "Convert the sample Siemens conveyor program to "
                "Allen-Bradley."), "siemens_to_ab")
        self.assertEqual(
            serve._plc_direction_for(
                "Convert the sample Allen-Bradley conveyor "
                "program to Siemens SCL."), "ab_to_siemens")

    def test_repair_retry_recovers(self):
        client = _BadPlcClient(bad_times=1)
        svc, orch = self.make_service(client=client)
        code, plan = svc.propose(
            "Convert the sample Allen-Bradley conveyor program "
            "to Siemens SCL.", "c")
        self.assertEqual(code, 200, plan)
        self.assertEqual(plan["steps"][0]["name"],
                         "convert_plc_code")
        self.assertEqual(client.calls, 2)

    def test_repair_exhaustion_is_422(self):
        client = _BadPlcClient(bad_times=5)
        svc, orch = self.make_service(client=client)
        code, out = svc.propose(
            "Convert the sample Allen-Bradley conveyor program "
            "to Siemens SCL.", "c")
        self.assertEqual(code, 422, out)
        self.assertIn("validator", out["error"])
        self.assertEqual(client.calls, 2)
        self.assertEqual(orch.tools.plc_conversions.list(), [])

    def test_undo_via_service(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "Convert the sample Allen-Bradley conveyor program "
            "to Siemens SCL.", "c")
        code, done = svc.approve(plan["id"], "c")
        cid = done["trace"][0]["result"]["conversion_id"]
        code, out = svc.undo({"kind": "plc_delete", "id": cid}, "c")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["undone"])
        self.assertIsNone(orch.tools.plc_conversions.get(cid))

    def test_capabilities_include_convert_plc_code(self):
        svc, orch = self.make_service()
        code, out = svc.propose("What can you do?", "c")
        self.assertEqual(code, 200)
        names = [t["name"] for g in out["groups"] for t in g["tools"]]
        self.assertIn("convert_plc_code", names)
        build = next(g for g in out["groups"]
                     if g["area"] == "Build & Automate")
        tool = next(t for t in build["tools"]
                    if t["name"] == "convert_plc_code")
        self.assertIn("Allen-Bradley", tool["example"])
        self.assertTrue(tool["gated"])


# ---------------------------------------------------------- catalog
class TestCatalog(unittest.TestCase):
    def test_thirty_two_tools_and_budget(self):
        cat = load_catalog()
        # 33 since the Paralegal Assistant (Nov 27, 2026).
        self.assertEqual(len(cat["tools"]), 33)
        total = sum(len(t["description"]) for t in cat["tools"])
        self.assertLess(total // 4, 600)
        tool = next(t for t in cat["tools"]
                    if t["name"] == "convert_plc_code")
        self.assertEqual(
            tool["parameters"]["properties"]["direction"]["enum"],
            ["ab_to_siemens", "siemens_to_ab"])
        export = next(t for t in cat["tools"]
                      if t["name"] == "export_file")
        self.assertIn(
            "plc",
            export["parameters"]["properties"]["source"]["enum"])

    def test_page_markers(self):
        page = open(os.path.join(
            os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "demo_page.html")).read()
        for marker in ("plcStepPlain", "plc_delete", "plc-converter",
                       "Convert PLC code", "PLC_DIR_LABELS",
                       "Send for change approval",
                       "DRAFT for qualified-engineer review"):
            self.assertIn(marker, page)

    def test_plc_chip_routes_to_converter(self):
        page = open(os.path.join(
            os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "demo_page.html")).read()
        self.assertIn('chipLabel:"Convert PLC code"', page)
        i = page.index('chipCmd:"Convert the sample Allen-Bradley')
        chip_cmd = page[i + len('chipCmd:"'):page.index('"', i + 9)]
        self.assertTrue(serve.is_plc_convert_command(chip_cmd))



class TestPlcInputTokenGuard(unittest.TestCase):
    """PLC conversions stay under Ember's 200K-token pricing tier:
    manuals are trimmed to fit, and an oversized paste is refused
    before any spend."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        for name in ("ab-ref.txt", "siemens-ref.txt"):
            with open(os.path.join(self._tmp.name, name), "w") as f:
                f.write("REF " * 50_000)  # 200K chars each
        self._old = os.environ.get("EMBER_PLC_MANUALS_DIR")
        os.environ["EMBER_PLC_MANUALS_DIR"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("EMBER_PLC_MANUALS_DIR", None)
        else:
            os.environ["EMBER_PLC_MANUALS_DIR"] = self._old
        self._tmp.cleanup()

    def test_under_cap_untouched(self):
        cmd = "Convert the sample Allen-Bradley program to Siemens"
        context, stats = serve._plc_fit_context(cmd, cap=500_000)
        self.assertNotIn("trimmed_to_fit", stats)
        self.assertEqual(context, serve._plc_context(cmd)[0])

    def test_trims_manuals_to_fit(self):
        cmd = "Convert the sample Allen-Bradley program to Siemens"
        context, stats = serve._plc_fit_context(cmd, cap=40_000)
        self.assertTrue(stats["trimmed_to_fit"])
        self.assertLessEqual(serve._est_tokens(cmd + "\n\n" + context),
                             40_000)
        self.assertTrue(stats["manuals"])

    def test_oversized_paste_refused(self):
        cmd = "Convert to Siemens: x := 1; " + ("y := y + 1; " * 20_000)
        with self.assertRaises(serve.PlcTooLarge):
            serve._plc_fit_context(cmd, cap=10_000)

if __name__ == "__main__":
    unittest.main()
