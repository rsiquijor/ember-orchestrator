"""Paralegal Assistant tests (Nov 27, 2026).

ask_paralegal answers from US public-domain legal sources
(U.S. Code, Federal Rules, US Constitution, CFR) via
deterministic keyword BM25 retrieval (SQLite FTS5 in
orchestrator/legal_reference.py) — no embeddings, no KB
search, no Cube-side model. Ember answers only from
retrieved passages with per-section/rule citations, says
so when the sources do not cover a question, and never
gives legal advice for a specific case; serve.py appends
the disclaimer deterministically.

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
from orchestrator.legal_reference import corpus_stats, search_legal
from orchestrator.orchestrator import SYSTEM_PROMPT, Orchestrator
from orchestrator.spend import SpendMeter
from orchestrator.verifier import verify_trace
from tests.test_ui import OPERATOR, fake_auth_factory

FIXTURE_CHUNKS = [
    {"id": "lg-000001", "source": "uscode",
     "group": "Title 28 — Judiciary and Judicial Procedure",
     "citation": "28 U.S.C. § 1331",
     "heading": "Federal question",
     "text": ("The district courts shall have original "
              "jurisdiction of all civil actions arising "
              "under the Constitution, laws, or treaties of "
              "the United States.")},
    {"id": "lg-000002", "source": "uscode",
     "group": "Title 28 — Judiciary and Judicial Procedure",
     "citation": "28 U.S.C. § 1332",
     "heading": "Diversity of citizenship; amount in controversy",
     "text": ("The district courts shall have original "
              "jurisdiction of all civil actions where the "
              "matter in controversy exceeds the sum or value "
              "of $75,000, exclusive of interest and costs, "
              "and is between citizens of different States.")},
    {"id": "lg-000003", "source": "rules",
     "group": "Federal Rules of Civil Procedure",
     "citation": "Fed. R. Civ. P. 26",
     "heading": "Rule 26. Duty to Disclose; General "
                "Provisions Governing Discovery",
     "text": ("Parties may obtain discovery regarding any "
              "nonprivileged matter that is relevant to any "
              "party's claim or defense and proportional to "
              "the needs of the case. Information within this "
              "scope of discovery need not be admissible in "
              "evidence to be discoverable.")},
    {"id": "lg-000004", "source": "rules",
     "group": "Federal Rules of Evidence",
     "citation": "Fed. R. Evid. 802",
     "heading": "Rule 802. The Rule Against Hearsay",
     "text": ("Hearsay is not admissible unless any of the "
              "following provides otherwise: a federal "
              "statute; these rules; or other rules "
              "prescribed by the Supreme Court.")},
    {"id": "lg-000005", "source": "constitution",
     "group": "U.S. Constitution",
     "citation": "U.S. Const. amend. XIV, § 1",
     "heading": "Amendment XIV — Section 1",
     "text": ("No State shall make or enforce any law which "
              "shall abridge the privileges or immunities of "
              "citizens of the United States; nor shall any "
              "State deprive any person of life, liberty, or "
              "property, without due process of law; nor deny "
              "to any person within its jurisdiction the equal "
              "protection of the laws.")},
    {"id": "lg-000006", "source": "cfr",
     "group": "Title 29 — Labor (CFR)",
     "citation": "29 C.F.R. § 1910.95",
     "heading": "Occupational noise exposure",
     "text": ("Protection against the effects of noise "
              "exposure shall be provided when the sound "
              "levels exceed those shown in Table G-16. When "
              "employees are subjected to sound exceeding "
              "those listed in Table G-16, feasible "
              "administrative or engineering controls shall "
              "be utilized.")},
    {"id": "lg-000007", "source": "uscode",
     "group": "Title 42 — The Public Health and Welfare",
     "citation": "42 U.S.C. § 1983",
     "heading": "Civil action for deprivation of rights",
     "text": ("Every person who, under color of any statute, "
              "ordinance, regulation, custom, or usage, of "
              "any State, subjects any citizen of the United "
              "States to the deprivation of any rights, "
              "privileges, or immunities secured by the "
              "Constitution and laws, shall be liable to the "
              "party injured in an action at law.")},
    {"id": "lg-000008", "source": "constitution",
     "group": "U.S. Constitution",
     "citation": "U.S. Const. art. III, § 2",
     "heading": "Article III — Section 2",
     "text": ("The judicial Power shall extend to all Cases, "
              "in Law and Equity, arising under this "
              "Constitution, the Laws of the United States, "
              "and Treaties made under their Authority.")},
]

FIXTURE_META = {
    "title": "US public-domain legal sources (U.S. Code, "
             "Federal Rules, Constitution, CFR)",
    "chars": sum(len(c["text"]) for c in FIXTURE_CHUNKS),
    "approx_tokens": sum(len(c["text"]) for c in FIXTURE_CHUNKS) // 4,
    "groups": ["Title 28 — Judiciary and Judicial Procedure",
               "Federal Rules of Civil Procedure",
               "Federal Rules of Evidence",
               "U.S. Constitution",
               "Title 29 — Labor (CFR)",
               "Title 42 — The Public Health and Welfare"],
    "per_source": {},
}


class LegalCase(unittest.TestCase):
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
                ("EMBER_LEGAL_CORPUS_DIR", "legal_corpus")):
            self._saved[var] = os.environ.get(var)
            os.environ[var] = os.path.join(self._tmp.name, fname)
        self.corpus_dir = os.environ["EMBER_LEGAL_CORPUS_DIR"]
        os.makedirs(self.corpus_dir)
        with open(os.path.join(self.corpus_dir, "chunks.jsonl"),
                  "w", encoding="utf-8") as f:
            for c in FIXTURE_CHUNKS:
                f.write(json.dumps(c) + "\n")
        with open(os.path.join(self.corpus_dir,
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
class TestRetrieval(LegalCase):
    def test_corpus_stats(self):
        stats = corpus_stats()
        self.assertTrue(stats["ready"])
        self.assertEqual(stats["chunks"], len(FIXTURE_CHUNKS))
        self.assertIn("public-domain", stats["title"])

    def test_code_question_cites_section(self):
        r = search_legal("federal question jurisdiction")
        self.assertTrue(r["covered"], r)
        self.assertIn("28 U.S.C. § 1331", r["citations"])
        self.assertGreater(r["reference_tokens_approx"], 0)

    def test_rules_question_cites_rule(self):
        r = search_legal("scope of discovery")
        self.assertTrue(r["covered"], r)
        self.assertIn("Fed. R. Civ. P. 26", r["citations"])

    def test_heading_term_alone_covers(self):
        r = search_legal("hearsay")
        self.assertTrue(r["covered"], r)
        self.assertIn("Fed. R. Evid. 802", r["citations"])

    def test_constitution_question(self):
        r = search_legal("equal protection due process")
        self.assertTrue(r["covered"], r)
        self.assertIn("U.S. Const. amend. XIV, § 1",
                      r["citations"])

    def test_cfr_question(self):
        r = search_legal("occupational noise exposure")
        self.assertTrue(r["covered"], r)
        self.assertIn("29 C.F.R. § 1910.95", r["citations"])

    def test_out_of_scope_not_covered(self):
        for q in ("tire pressure for a sedan",
                  "French contract law",
                  "reset my router password"):
            r = search_legal(q)
            self.assertFalse(r["covered"], q)
            self.assertEqual(r["passages"], [], q)

    def test_two_of_four_terms_not_covered(self):
        # A four-term query whose best chunk matches only two
        # stray words (diversity + jurisdiction in § 1332)
        # must not claim coverage — the Texas speed-limit
        # false positive on the live corpus.
        r = search_legal("diversity jurisdiction texas highways")
        self.assertFalse(r["covered"], r)
        self.assertEqual(r["passages"], [])

    def test_empty_question_not_covered(self):
        r = search_legal("")
        self.assertFalse(r["covered"])
        self.assertEqual(r["passages"], [])

    def test_multi_group_grouped_in_relevance_order(self):
        r = search_legal("federal jurisdiction discovery")
        self.assertTrue(r["covered"], r)
        groups = r["groups"]
        self.assertGreaterEqual(len(groups), 2, groups)
        self.assertLessEqual(len(groups), 5, groups)
        order = [g["group"] for g in groups]
        seen = [order.index(p["group"]) for p in r["passages"]]
        self.assertEqual(seen, sorted(seen), r["passages"])
        for g in groups:
            self.assertGreater(g["passages"], 0, g)
            self.assertTrue(g["citations"], g)
            self.assertEqual(
                g["passages"],
                sum(1 for p in r["passages"]
                    if p["group"] == g["group"]), g)


# ------------------------------------------------------------- tool
class TestTool(LegalCase):
    def test_dispatch_returns_passages_and_instruction(self):
        tools = FixtureCubeTools(ApprovalTokens())
        r = tools.call("ask_paralegal",
                       {"question": "federal question "
                                    "jurisdiction"})
        self.assertTrue(r["covered"], r)
        self.assertIn("Answer ONLY from the passages",
                      r["instruction"])
        self.assertIn("not legal advice", r["note"])
        self.assertIs(tools.last_legal_result, r)

    def test_dispatch_not_covered(self):
        tools = FixtureCubeTools(ApprovalTokens())
        r = tools.call("ask_paralegal",
                       {"question": "tire pressure for a sedan"})
        self.assertFalse(r["covered"])
        self.assertEqual(r["passages"], [])

    def test_instruction_per_group_structure(self):
        tools = FixtureCubeTools(ApprovalTokens())
        r = tools.call("ask_paralegal",
                       {"question": "federal question "
                                    "jurisdiction"})
        self.assertIn("paralegal assistant", r["instruction"])
        self.assertIn("ONE BLOCK PER GROUP", r["instruction"])
        self.assertIn("28 U.S.C. § 1331", r["instruction"])
        self.assertIn("synthesis across the sources",
                      r["instruction"])


# ----------------------------------------------------------- catalog
class TestCatalog(unittest.TestCase):
    def test_thirty_three_tools_and_budget(self):
        cat = load_catalog()
        self.assertEqual(len(cat["tools"]), 33)
        total = sum(len(t["description"]) for t in cat["tools"])
        self.assertLess(total // 4, 600)
        self.assertEqual(cat["version"], "1.2.0-paralegal")

    def test_legal_tool_shape(self):
        cat = load_catalog()
        tool = next(t for t in cat["tools"]
                    if t["name"] == "ask_paralegal")
        self.assertEqual(tool["parameters"]["required"],
                         ["question"])
        self.assertIn("public-domain", tool["description"])

    def test_local_only_not_gated_not_bus(self):
        self.assertIn("ask_paralegal", LOCAL_ONLY_TOOLS)
        self.assertNotIn("ask_paralegal", GATED_TOOLS)
        from orchestrator.cube_tools import READ_TOOLS
        self.assertNotIn("ask_paralegal", READ_TOOLS)

    def test_export_source_enum_has_legal(self):
        cat = load_catalog()
        tool = next(t for t in cat["tools"]
                    if t["name"] == "export_file")
        self.assertIn("legal", tool["parameters"]["properties"]
                      ["source"]["enum"])


# -------------------------------------------------------- system rules
class TestRules(unittest.TestCase):
    def test_system_prompt_legal_rule(self):
        self.assertIn("ask_paralegal", SYSTEM_PROMPT)
        self.assertIn("never give legal advice for a specific "
                      "case", SYSTEM_PROMPT)
        self.assertIn("do not cover that question", SYSTEM_PROMPT)
        self.assertIn("one block per relevant source/title",
                      SYSTEM_PROMPT)

    def test_capabilities_include_legal(self):
        payload = serve.capabilities_payload()
        areas = [g["area"] for g in payload["groups"]]
        self.assertIn("Legal Reference", areas)
        names = [t["name"] for g in payload["groups"]
                 for t in g["tools"]]
        self.assertIn("ask_paralegal", names)

    def test_default_action_is_export(self):
        trace = [{"tool": "ask_paralegal", "result": {}}]
        action = serve.derive_default_action(trace)
        self.assertEqual(action["command"],
                         "Save the legal answer as TXT.")


# ------------------------------------------------------------ stub
class TestStub(LegalCase):
    def _orch(self):
        meter = SpendMeter()
        tokens = ApprovalTokens()
        return Orchestrator(StubEmberClient(),
                            FixtureCubeTools(tokens), meter, tokens)

    def test_stub_proposes_legal_tool(self):
        orch = self._orch()
        plan = orch.propose(
            "What does the law say about federal-question "
            "jurisdiction?")
        names = [s["name"] for s in plan["steps"]]
        self.assertEqual(names, ["ask_paralegal"], plan)
        self.assertIn("jurisdiction",
                      plan["steps"][0]["arguments"]["question"])

    def test_stub_export_guard(self):
        orch = self._orch()
        plan = orch.propose("Save the legal answer as TXT.")
        names = [s["name"] for s in plan["steps"]]
        self.assertEqual(names, ["export_file"], plan)
        self.assertEqual(plan["steps"][0]["arguments"]["source"],
                         "legal")

    def test_stub_compose_covered_cites(self):
        orch = self._orch()
        plan = orch.propose(
            "What does the law say about federal-question "
            "jurisdiction?")
        done = orch.approve(plan["id"])
        self.assertIn("28 U.S.C. § 1331", done["final_text"])
        self.assertIn("## Title 28", done["final_text"])

    def test_stub_compose_not_covered_honest(self):
        orch = self._orch()
        plan = orch.propose(
            "What does the law say about zorblaxian "
            "hyperflange jurisdiction?")
        self.assertEqual([s["name"] for s in plan["steps"]],
                         ["ask_paralegal"], plan)
        done = orch.approve(plan["id"])
        self.assertIn("do not cover", done["final_text"])
        result = done["trace"][0]["result"]
        self.assertFalse(result["covered"], result)

    def test_stub_compose_multi_group_synthesis(self):
        orch = self._orch()
        plan = orch.propose(
            "What does the law say about federal-question "
            "jurisdiction and discovery?")
        self.assertEqual([s["name"] for s in plan["steps"]],
                         ["ask_paralegal"], plan)
        done = orch.approve(plan["id"])
        headings = [ln for ln in done["final_text"].splitlines()
                    if ln.startswith("## ")]
        self.assertGreaterEqual(len(headings), 2, done["final_text"])
        self.assertIn("Across these sources", done["final_text"])

    def test_legal_compose_gets_larger_budget(self):
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
            "What does the law say about federal-question "
            "jurisdiction?")
        orch.approve(plan["id"])
        self.assertIn(4096, client.budgets, client.budgets)


# ----------------------------------------------------- serve path
class TestServePath(LegalCase):
    def test_full_run_with_disclaimer(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "What does the law say about federal-question "
            "jurisdiction?", "c")
        self.assertEqual(code, 200, plan)
        self.assertEqual(plan["steps"][0]["name"],
                         "ask_paralegal")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200, done)
        self.assertIn("Not legal advice", done["final_text"])
        self.assertIn("28 U.S.C. § 1331", done["final_text"])
        result = done["trace"][0]["result"]
        self.assertTrue(result["covered"], result)
        self.assertTrue(result["citations"], result)
        self.assertEqual(done["default_action"]["command"],
                         "Save the legal answer as TXT.")

    def test_disclaimer_helper_idempotent(self):
        done = {"final_text": "Answer.",
                "trace": [{"tool": "ask_paralegal"}]}
        once = serve._with_legal_disclaimer(done)
        self.assertTrue(once.endswith(serve.LEGAL_DISCLAIMER))
        done2 = {"final_text": once,
                 "trace": [{"tool": "ask_paralegal"}]}
        self.assertEqual(serve._with_legal_disclaimer(done2),
                         once)
        plain = {"final_text": "Other.",
                 "trace": [{"tool": "cube_status"}]}
        self.assertEqual(serve._with_legal_disclaimer(plain),
                         "Other.")
        both = serve._with_disclaimers(done)
        self.assertTrue(both.endswith(serve.LEGAL_DISCLAIMER))

    def test_export_legal_source(self):
        svc, orch = self.make_service()
        code, plan = svc.propose(
            "What does the law say about federal-question "
            "jurisdiction?", "c")
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200, done)
        code, plan2 = svc.propose(
            "Save the legal answer as TXT.", "c")
        self.assertEqual(code, 200, plan2)
        self.assertEqual(plan2["steps"][0]["name"], "export_file")
        self.assertEqual(plan2["steps"][0]["arguments"]["source"],
                         "legal")
        code, done2 = svc.approve(plan2["id"], "c")
        self.assertEqual(code, 200, done2)
        result = done2["trace"][0]["result"]
        self.assertTrue(result["exported"], result)


# ---------------------------------------------------------- verifier
class TestVerifier(unittest.TestCase):
    def _trace(self, result):
        return [{"tool": "ask_paralegal",
                 "arguments": {"question": "q"}, "result": result}]

    def test_covered_with_passages_ok(self):
        checks = verify_trace(self._trace({
            "covered": True,
            "passages": [{"text": "x"}],
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
        for marker in ("Paralegal Assistant",
                       "Ask the paralegal assistant",
                       "legalStepPlain",
                       "Paralegal — sources",
                       # Nov 27 4-group reorganization: the demo now
                       # sits in Business Use Cases on the page (the
                       # capabilities table keeps "Legal Reference").
                       "Business Use Cases",
                       "summarizes each source",
                       "Not legal advice"):
            self.assertIn(marker, html, marker)


if __name__ == "__main__":
    unittest.main()
