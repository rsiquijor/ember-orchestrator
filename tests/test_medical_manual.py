"""Medical Manual Q&A tests (Nov 27, 2026).

ask_medical_manual answers from The Merck Manual of Diagnosis
& Therapy, 19th Ed. via deterministic keyword BM25 retrieval
(orchestrator/medical_manual.py) — no embeddings, no KB search,
no Cube-side model. Ember answers only from retrieved
passages with citations, says so when the manual does not
cover a question, and never gives personalized medical
advice; serve.py appends the disclaimer deterministically.

Offline, $0: stub Ember + fixture tools + a tiny fixture
corpus + fake auth. No network.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.cube_tools import (ApprovalTokens, FixtureCubeTools,
                                     GATED_TOOLS, LOCAL_ONLY_TOOLS,
                                     load_catalog)
from orchestrator.ember_client import StubEmberClient
from orchestrator.medical_manual import corpus_stats, search_manual
from orchestrator.orchestrator import SYSTEM_PROMPT, Orchestrator
from orchestrator.spend import SpendMeter
from orchestrator.verifier import verify_trace
from tests.test_ui import OPERATOR, fake_auth_factory

FIXTURE_CHUNKS = [
    {"id": "mm-00001", "chapter_num": 28,
     "chapter": "Chapter 28. Hepatitis",
     "section": "Symptoms and Signs",
     "page_start": 322, "page_end": 323,
     "pdf_page_start": 370, "pdf_page_end": 371,
     "text": ("Acute viral hepatitis often begins with a "
              "prodrome of anorexia, malaise, nausea, and "
              "vomiting, sometimes with fever and arthralgias. "
              "Dark urine and clay-colored stools may precede "
              "jaundice by 1 to 5 days. Symptoms usually peak "
              "when jaundice appears and then subside.")},
    {"id": "mm-00002", "chapter_num": 28,
     "chapter": "Chapter 28. Hepatitis",
     "section": "Diagnosis",
     "page_start": 324, "page_end": 325,
     "pdf_page_start": 372, "pdf_page_end": 373,
     "text": ("Diagnosis of acute viral hepatitis is based on "
              "clinical features and laboratory tests. Serum "
              "aminotransferases are markedly elevated, and "
              "serologic testing identifies the causative "
              "virus (hepatitis A, B, or C).")},
    {"id": "mm-00003", "chapter_num": 28,
     "chapter": "Chapter 28. Hepatitis",
     "section": "Treatment",
     "page_start": 326, "page_end": 326,
     "pdf_page_start": 374, "pdf_page_end": 374,
     "text": ("Treatment of acute viral hepatitis is mainly "
              "supportive. Patients should avoid alcohol and "
              "hepatotoxic drugs. Hospitalization is reserved "
              "for severe cases with vomiting or coagulopathy.")},
    {"id": "mm-00004", "chapter_num": 4,
     "chapter": "Chapter 4. Vitamin Deficiency, Dependency, "
                "and Toxicity",
     "section": "Vitamin D Deficiency",
     "page_start": 110, "page_end": 111,
     "pdf_page_start": 158, "pdf_page_end": 159,
     "text": ("Vitamin D deficiency can cause rickets in "
              "children and osteomalacia in adults. Bone pain "
              "and muscle weakness are common symptoms. "
              "Diagnosis is by serum 25-hydroxyvitamin D "
              "levels.")},
    {"id": "mm-00005", "chapter_num": 1,
     "chapter": "Chapter 1. Malnutrition",
     "section": "Etiology",
     "page_start": 53, "page_end": 54,
     "pdf_page_start": 101, "pdf_page_end": 102,
     "text": ("Malnutrition results from inadequate intake or "
              "absorption of nutrients. Primary malnutrition "
              "is caused by inadequate food supply; secondary "
              "malnutrition results from disorders that impair "
              "digestion or increase requirements.")},
    {"id": "mm-00006", "chapter_num": 71,
     "chapter": "Chapter 71. Hypertension",
     "section": "Diagnosis",
     "page_start": 700, "page_end": 701,
     "pdf_page_start": 748, "pdf_page_end": 749,
     "text": ("Blood pressure should be measured with the "
              "correct cuff size after 5 minutes of rest. "
              "Diagnosis of hypertension requires elevated "
              "readings on at least two occasions.")},
]

FIXTURE_META = {
    "title": "The Merck Manual of Diagnosis & Therapy, 19th Ed.",
    "chars": sum(len(c["text"]) for c in FIXTURE_CHUNKS),
    "approx_tokens": sum(len(c["text"]) for c in FIXTURE_CHUNKS) // 4,
    "chapters": ["Chapter 1. Malnutrition",
                 "Chapter 4. Vitamin Deficiency, Dependency, "
                 "and Toxicity",
                 "Chapter 28. Hepatitis"],
}


class MedicalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = {}
        for var, fname in (
                ("EMBER_BOARD_PATH", "board.json"),
                ("EMBER_INTAKE_PATH", "intake.json"),
                ("EMBER_REPORTS_PATH", "reports.json"),
                ("EMBER_AGENT_FLOWS_PATH", "agent_flows.json"),
                ("EMBER_GOALS_PATH", "goals.json"),
                ("EMBER_PREFS_PATH", "preferences.json"),
                ("EMBER_EXPORTS_DIR", "exports"),
                ("EMBER_DASHBOARDS_PATH", "dashboards.json"),
                ("EMBER_PLC_CONVERSIONS_PATH",
                 "plc_conversions.json"),
                ("EMBER_MEDICAL_MANUAL_DIR", "medical_manual")):
            self._saved[var] = os.environ.get(var)
            os.environ[var] = os.path.join(self._tmp.name, fname)
        self.manual_dir = os.environ["EMBER_MEDICAL_MANUAL_DIR"]
        os.makedirs(self.manual_dir)
        with open(os.path.join(self.manual_dir, "chunks.jsonl"),
                  "w", encoding="utf-8") as f:
            for c in FIXTURE_CHUNKS:
                f.write(json.dumps(c) + "\n")
        with open(os.path.join(self.manual_dir,
                               "corpus_meta.json"),
                  "w", encoding="utf-8") as f:
            json.dump(FIXTURE_META, f)
        self.cap_path = os.path.join(self._tmp.name, "cap.json")
        self.recorded_path = os.path.join(self._tmp.name,
                                          "recorded.json")
        self.reports_path = os.environ["EMBER_REPORTS_PATH"]

    def tearDown(self):
        for var, old in self._saved.items():
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old
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


# --------------------------------------------------------- retrieval
class TestRetrieval(MedicalCase):
    def test_corpus_stats(self):
        stats = corpus_stats()
        self.assertTrue(stats["ready"])
        self.assertEqual(stats["chunks"], len(FIXTURE_CHUNKS))
        self.assertIn("Merck Manual", stats["title"])

    def test_covered_question_cites_chapter(self):
        r = search_manual("symptoms of acute viral hepatitis")
        self.assertTrue(r["covered"], r)
        self.assertTrue(r["passages"])
        self.assertTrue(any("Hepatitis" in c
                            for c in r["citations"]), r["citations"])
        self.assertTrue(any("pp. 322" in c or "p. 322" in c
                            or "pp. 324" in c
                            for c in r["citations"]), r["citations"])
        self.assertGreater(r["reference_tokens_approx"], 0)

    def test_title_term_alone_covers(self):
        r = search_manual("hepatitis")
        self.assertTrue(r["covered"], r)
        self.assertTrue(all("Hepatitis" in p["chapter"]
                            for p in r["passages"]))

    def test_out_of_scope_not_covered(self):
        for q in ("tire pressure for a sedan",
                  "reset my router password"):
            r = search_manual(q)
            self.assertFalse(r["covered"], q)
            self.assertEqual(r["passages"], [], q)

    def test_oov_term_blocks_generic_match(self):
        # "correct" + "pressure" exist in the fixture corpus
        # (Hypertension chapter) but "tire"/"sedan" never
        # appear — the topic-carrying OOV terms must block a
        # generic two-word match from claiming coverage.
        r = search_manual("correct tire pressure for a sedan")
        self.assertFalse(r["covered"], r)
        self.assertEqual(r["passages"], [])
        # The same words in a genuinely covered question work.
        r2 = search_manual("blood pressure diagnosis")
        self.assertTrue(r2["covered"], r2)

    def test_empty_question_not_covered(self):
        r = search_manual("")
        self.assertFalse(r["covered"])
        self.assertEqual(r["passages"], [])

    def test_multi_chapter_grouped_in_relevance_order(self):
        # Per-chapter organization (Nov 27, 2026): passages
        # come back grouped by chapter, chapters listed in
        # relevance order with counts/pages/sections, capped
        # at MAX_CHAPTERS so no chapter crowds out the rest.
        r = search_manual("symptoms and diagnosis")
        self.assertTrue(r["covered"], r)
        chapters = r["chapters"]
        self.assertGreaterEqual(len(chapters), 2, chapters)
        self.assertLessEqual(len(chapters), 5, chapters)
        order = [c["chapter"] for c in chapters]
        self.assertEqual(order[0], "Chapter 28. Hepatitis")
        seen = [order.index(p["chapter"]) for p in r["passages"]]
        self.assertEqual(seen, sorted(seen), r["passages"])
        for c in chapters:
            self.assertGreater(c["passages"], 0, c)
            self.assertTrue(c["pages"], c)
            self.assertTrue(c["sections"], c)
            self.assertEqual(
                c["passages"],
                sum(1 for p in r["passages"]
                    if p["chapter"] == c["chapter"]), c)

    def test_single_topic_stays_single_chapter(self):
        r = search_manual("symptoms of acute viral hepatitis")
        self.assertTrue(r["covered"], r)
        self.assertEqual([c["chapter"] for c in r["chapters"]],
                         ["Chapter 28. Hepatitis"], r["chapters"])


# ------------------------------------------------------------- tool
class TestTool(MedicalCase):
    def test_dispatch_returns_passages_and_instruction(self):
        tools = FixtureCubeTools(ApprovalTokens())
        r = tools.call("ask_medical_manual",
                       {"question": "symptoms of acute viral "
                                    "hepatitis"})
        self.assertTrue(r["covered"], r)
        self.assertIn("Answer ONLY from the passages",
                      r["instruction"])
        self.assertIn("not medical advice", r["note"])
        self.assertIs(tools.last_medical_result, r)

    def test_dispatch_not_covered(self):
        tools = FixtureCubeTools(ApprovalTokens())
        r = tools.call("ask_medical_manual",
                       {"question": "tire pressure for a sedan"})
        self.assertFalse(r["covered"])
        self.assertEqual(r["passages"], [])

    def test_instruction_per_chapter_structure(self):
        tools = FixtureCubeTools(ApprovalTokens())
        r = tools.call("ask_medical_manual",
                       {"question": "symptoms of acute viral "
                                    "hepatitis"})
        self.assertIn("medical practitioner assistant",
                      r["instruction"])
        self.assertIn("ONE BLOCK PER CHAPTER", r["instruction"])
        self.assertIn("## Chapter 28. Hepatitis",
                      r["instruction"])
        self.assertIn("synthesis across the chapters",
                      r["instruction"])
        self.assertIn("differential considerations",
                      r["instruction"])


# ----------------------------------------------------------- catalog
class TestCatalog(unittest.TestCase):
    def test_thirty_two_tools_and_budget(self):
        cat = load_catalog()
        # 33 tools / v1.2.0 since the Paralegal Assistant
        # (Nov 27, 2026).
        self.assertEqual(len(cat["tools"]), 33)
        total = sum(len(t["description"]) for t in cat["tools"])
        self.assertLess(total // 4, 600)
        self.assertEqual(cat["version"], "1.2.0-paralegal")

    def test_medical_tool_shape(self):
        cat = load_catalog()
        tool = next(t for t in cat["tools"]
                    if t["name"] == "ask_medical_manual")
        self.assertEqual(tool["parameters"]["required"],
                         ["question"])
        self.assertIn("Merck Manual", tool["description"])

    def test_local_only_not_gated_not_bus(self):
        self.assertIn("ask_medical_manual", LOCAL_ONLY_TOOLS)
        self.assertNotIn("ask_medical_manual", GATED_TOOLS)
        from orchestrator.cube_tools import READ_TOOLS
        self.assertNotIn("ask_medical_manual", READ_TOOLS)

    def test_export_source_enum_has_medical(self):
        cat = load_catalog()
        tool = next(t for t in cat["tools"]
                    if t["name"] == "export_file")
        self.assertIn("medical", tool["parameters"]["properties"]
                      ["source"]["enum"])


# -------------------------------------------------------- system rules
class TestRules(unittest.TestCase):
    def test_system_prompt_medical_rule(self):
        self.assertIn("ask_medical_manual", SYSTEM_PROMPT)
        self.assertIn("never give personalized diagnosis",
                      SYSTEM_PROMPT)
        self.assertIn("does not cover that question",
                      SYSTEM_PROMPT)
        self.assertIn("one block per relevant chapter",
                      SYSTEM_PROMPT)

    def test_capabilities_include_medical(self):
        payload = serve.capabilities_payload()
        areas = [g["area"] for g in payload["groups"]]
        self.assertIn("Medical Reference", areas)
        names = [t["name"] for g in payload["groups"]
                 for t in g["tools"]]
        self.assertIn("ask_medical_manual", names)

    def test_default_action_is_export(self):
        trace = [{"tool": "ask_medical_manual", "result": {}}]
        action = serve.derive_default_action(trace)
        self.assertEqual(action["command"],
                         "Save the medical answer as TXT.")


# ------------------------------------------------------------ stub
class TestStub(MedicalCase):
    def _orch(self):
        meter = SpendMeter()
        tokens = ApprovalTokens()
        return Orchestrator(StubEmberClient(),
                            FixtureCubeTools(tokens), meter, tokens)

    def test_stub_proposes_medical_tool(self):
        orch = self._orch()
        plan = orch.propose(
            "What does the manual say about the symptoms of "
            "acute viral hepatitis?")
        names = [s["name"] for s in plan["steps"]]
        self.assertEqual(names, ["ask_medical_manual"], plan)
        self.assertIn("hepatitis",
                      plan["steps"][0]["arguments"]["question"])

    def test_stub_export_guard(self):
        orch = self._orch()
        plan = orch.propose("Save the medical answer as TXT.")
        names = [s["name"] for s in plan["steps"]]
        self.assertEqual(names, ["export_file"], plan)
        self.assertEqual(plan["steps"][0]["arguments"]["source"],
                         "medical")

    def test_stub_compose_covered_cites(self):
        orch = self._orch()
        plan = orch.propose(
            "What does the manual say about the symptoms of "
            "acute viral hepatitis?")
        done = orch.approve(plan["id"])
        self.assertIn("Merck Manual", done["final_text"])
        self.assertIn("Chapter 28. Hepatitis", done["final_text"])

    def test_stub_compose_not_covered_honest(self):
        orch = self._orch()
        plan = orch.propose(
            "What does the manual say about zorblaxian "
            "hyperflange disorder?")
        self.assertEqual([s["name"] for s in plan["steps"]],
                         ["ask_medical_manual"], plan)
        done = orch.approve(plan["id"])
        self.assertIn("does not cover", done["final_text"])
        result = done["trace"][0]["result"]
        self.assertFalse(result["covered"], result)

    def test_stub_compose_per_chapter_blocks(self):
        orch = self._orch()
        plan = orch.propose(
            "What does the manual say about the symptoms of "
            "acute viral hepatitis?")
        done = orch.approve(plan["id"])
        self.assertIn("## Chapter 28. Hepatitis",
                      done["final_text"])
        # Single chapter: no cross-chapter synthesis line.
        self.assertNotIn("Across these chapters",
                         done["final_text"])

    def test_stub_compose_multi_chapter_synthesis(self):
        orch = self._orch()
        plan = orch.propose(
            "What does the manual say about symptoms, "
            "diagnosis and treatment of viral hepatitis?")
        self.assertEqual([s["name"] for s in plan["steps"]],
                         ["ask_medical_manual"], plan)
        done = orch.approve(plan["id"])
        headings = [ln for ln in done["final_text"].splitlines()
                    if ln.startswith("## ")]
        self.assertGreaterEqual(len(headings), 2, done["final_text"])
        self.assertIn("Across these chapters", done["final_text"])

    def test_medical_compose_gets_larger_budget(self):
        class RecordingStub(StubEmberClient):
            def __init__(self):
                self.budgets = []

            def chat(self, messages, tools, tool_choice="auto",
                     max_tokens=None):
                self.budgets.append(max_tokens)
                return super().chat(messages, tools,
                                    tool_choice=tool_choice,
                                    max_tokens=max_tokens)

        client = RecordingStub()
        meter = SpendMeter()
        tokens = ApprovalTokens()
        orch = Orchestrator(client, FixtureCubeTools(tokens),
                            meter, tokens)
        plan = orch.propose(
            "What does the manual say about the symptoms of "
            "acute viral hepatitis?")
        orch.approve(plan["id"])
        self.assertIn(4096, client.budgets, client.budgets)


# ----------------------------------------------------- serve path
class TestServePath(MedicalCase):
    def test_full_run_with_disclaimer(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "What does the manual say about the symptoms of "
            "acute viral hepatitis?", "c")
        self.assertEqual(code, 200, plan)
        self.assertEqual(plan["steps"][0]["name"],
                         "ask_medical_manual")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200, done)
        self.assertIn("Not medical advice", done["final_text"])
        self.assertIn("Merck Manual", done["final_text"])
        result = done["trace"][0]["result"]
        self.assertTrue(result["covered"], result)
        self.assertTrue(result["citations"], result)
        self.assertEqual(done["default_action"]["command"],
                         "Save the medical answer as TXT.")

    def test_disclaimer_helper_idempotent(self):
        done = {"final_text": "Answer.",
                "trace": [{"tool": "ask_medical_manual"}]}
        once = serve._with_medical_disclaimer(done)
        self.assertTrue(once.endswith(serve.MEDICAL_DISCLAIMER))
        done2 = {"final_text": once,
                 "trace": [{"tool": "ask_medical_manual"}]}
        self.assertEqual(serve._with_medical_disclaimer(done2),
                         once)
        plain = {"final_text": "Other.",
                 "trace": [{"tool": "cube_status"}]}
        self.assertEqual(serve._with_medical_disclaimer(plain),
                         "Other.")

    def test_export_medical_source(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "What does the manual say about the symptoms of "
            "acute viral hepatitis?", "c")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200, done)
        code, plan2 = svc.propose(
            "Save the medical answer as TXT.", "c")
        self.assertEqual(code, 200, plan2)
        self.assertEqual(plan2["steps"][0]["name"], "export_file")
        self.assertEqual(plan2["steps"][0]["arguments"]["source"],
                         "medical")
        code, done2 = svc.approve(plan2["id"], "c")
        self.assertEqual(code, 200, done2)
        result = done2["trace"][0]["result"]
        self.assertTrue(result["exported"], result)


# ---------------------------------------------------------- verifier
class TestVerifier(unittest.TestCase):
    def _trace(self, result):
        return [{"tool": "ask_medical_manual",
                 "arguments": {"question": "q"}, "result": result}]

    def test_covered_with_passages_ok(self):
        checks = verify_trace(self._trace({
            "covered": True,
            "passages": [{"text": "x"}],
            "manual": "The Merck Manual",
            "reference_tokens_approx": 10}),
            FixtureCubeTools(ApprovalTokens()))
        self.assertTrue(checks[0]["ok"], checks)

    def test_not_covered_ok(self):
        checks = verify_trace(self._trace({
            "covered": False, "passages": []}),
            FixtureCubeTools(ApprovalTokens()))
        self.assertTrue(checks[0]["ok"], checks)

    def test_covered_without_passages_fails(self):
        checks = verify_trace(self._trace({
            "covered": True, "passages": []}),
            FixtureCubeTools(ApprovalTokens()))
        self.assertFalse(checks[0]["ok"], checks)
        self.assertTrue(checks[0].get("fix"), checks)


# -------------------------------------------------------------- page
class TestPage(unittest.TestCase):
    def test_page_markers(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        for marker in ("Medical Manual Q&A",
                       "Ask the medical manual",
                       "medicalStepPlain",
                       "Medical Manual — sources",
                       # Nov 27 4-group reorganization: the demo now
                       # sits in Business Use Cases on the page (the
                       # capabilities table keeps "Medical Reference").
                       "Business Use Cases",
                       "summarize each",
                       "med-h",
                       "med-ch"):
            self.assertIn(marker, html, marker)


if __name__ == "__main__":
    unittest.main()
