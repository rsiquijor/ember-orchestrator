"""File-export tests — export_file tool (csv/xlsx/json/txt/docx).

Offline, $0: stub Ember + fixture tools + fake auth. No network.
XLSX/DOCX outputs are verified by loading them back with
openpyxl/python-docx (present in the dev env and vendored on the
Cube); if a library is missing the load-back checks skip loudly.

    python3 -m unittest discover -s tests -v
"""
import csv
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.cube_tools import (ApprovalError, ApprovalTokens,
                                     FixtureCubeTools, GATED_TOOLS)
from orchestrator.ember_client import StubEmberClient
from orchestrator.exporter import (CONTENT_TYPES, EXPORT_FORMATS,
                                   EXPORT_SOURCES, ExportStore,
                                   validate_export_params)
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter
from tests.test_ui import OPERATOR, VIEWER, fake_auth_factory

INVOICE_RECORD = {
    "kind": "invoice",
    "fields": {
        "vendor": "HYDRAULIC SUPPLY CO.",
        "invoice_number": "INV-88410",
        "date": "11/18/2026",
        "po_number": "PO-2214",
        "bill_to": "Demo Plant - Accounts Payable",
        "line_items": [
            {"description": "Hydraulic hose, 3/4 in x 6 ft",
             "qty": 2, "unit_price": 86.5, "amount": 173.0},
            {"description": "Hydraulic oil ISO 46, 55 gal drum",
             "qty": 1, "unit_price": 812.0, "amount": 812.0},
            {"description": "Manifold fitting kit",
             "qty": 1, "unit_price": 257.5, "amount": 257.5},
        ],
        "subtotal": 1242.5,
        "total_due": 1250.0,
    },
    "notes": ("Arithmetic note: line items sum to 1,242.50, matching "
              "stated subtotal. Total Due 1,250.00 is 7.50 higher "
              "than subtotal with no tax/shipping line visible."),
}

AUDIO_RECORD = {
    "kind": "audio_note",
    "fields": {
        "transcript": ("Work order seven seven one two. Replace the "
                       "hydraulic pump on line three. Priority high. "
                       "Due Friday."),
        "work_order": "7712",
        "task": "Replace the hydraulic pump",
        "line": "3",
        "priority": "high",
        "due": "Friday",
    },
    "notes": "Stub extraction from audio.",
}


class ExportCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = {}
        for var, fname in (("EMBER_BOARD_PATH", "board.json"),
                           ("EMBER_INTAKE_PATH", "intake.json"),
                           ("EMBER_REPORTS_PATH", "reports.json"),
                           ("EMBER_AGENT_FLOWS_PATH", "agent_flows.json"),
                           ("EMBER_EXPORTS_DIR", "exports")):
            self._saved[var] = os.environ.get(var)
            os.environ[var] = os.path.join(self._tmp.name, fname)
        self.cap_path = os.path.join(self._tmp.name, "cap.json")
        self.recorded_path = os.path.join(self._tmp.name, "recorded.json")
        self.reports_path = os.path.join(self._tmp.name, "reports.json")
        self.tokens = ApprovalTokens()
        self.tools = FixtureCubeTools(self.tokens)

    def tearDown(self):
        for var, val in self._saved.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
        self._tmp.cleanup()

    def make_service(self, user=OPERATOR, daily_cap=100):
        meter = SpendMeter()
        orch = Orchestrator(StubEmberClient(), self.tools, meter,
                            self.tokens)
        return serve.DemoService(
            orch, meter, auth_fn=fake_auth_factory(user),
            cap_path=self.cap_path, recorded_path=self.recorded_path,
            daily_cap=daily_cap, reports_path=self.reports_path)

    def file_invoice(self):
        return self.tools.file_intake(
            dict(INVOICE_RECORD), self.tokens.issue(), run_tag="test")

    def export(self, fmt, source):
        return self.tools.export_file(
            {"format": fmt, "source": source}, self.tokens.issue(),
            run_tag="test")

    def read_export(self, result):
        found = self.tools.exports.read(result["export_id"])
        self.assertIsNotNone(found)
        return found[1]


# ------------------------------------------------------------- validator
class TestValidate(unittest.TestCase):
    def test_good_params(self):
        for fmt in EXPORT_FORMATS:
            for source in EXPORT_SOURCES:
                clean = validate_export_params(
                    {"format": fmt, "source": source})
                self.assertEqual(clean, {"format": fmt, "source": source})

    def test_rejects_unknown_format_and_source(self):
        with self.assertRaises(ValueError):
            validate_export_params({"format": "pdf", "source": "intake"})
        with self.assertRaises(ValueError):
            validate_export_params({"format": "csv", "source": "email"})
        with self.assertRaises(ValueError):
            validate_export_params({"format": "csv"})
        with self.assertRaises(ValueError):
            validate_export_params({"source": "intake"})

    def test_rejects_unknown_keys(self):
        with self.assertRaises(ValueError):
            validate_export_params({"format": "csv", "source": "intake",
                                    "path": "/etc/passwd"})

    def test_gated_registration(self):
        self.assertIn("export_file", GATED_TOOLS)


# ------------------------------------------------------------- gating
class TestGating(ExportCase):
    def test_no_token_no_file(self):
        self.file_invoice()
        with self.assertRaises(ApprovalError):
            self.tools.export_file({"format": "csv", "source": "intake"})
        self.assertEqual(self.tools.exports.list(), [])

    def test_token_is_single_use(self):
        self.file_invoice()
        token = self.tokens.issue()
        ok = self.tools.export_file({"format": "csv", "source": "intake"},
                                    token)
        self.assertTrue(ok["exported"])
        with self.assertRaises(ApprovalError):
            self.tools.export_file({"format": "csv", "source": "intake"},
                                   token)

    def test_empty_sources_are_honest(self):
        for source in EXPORT_SOURCES:
            result = self.export("csv", source)
            self.assertFalse(result["exported"], source)
            self.assertTrue(result["message"], source)
        self.assertEqual(self.tools.exports.list(), [])


# ------------------------------------------------------------- generators
class TestCsv(ExportCase):
    def test_invoice_csv_exact(self):
        self.file_invoice()
        result = self.export("csv", "intake")
        self.assertEqual(result["filename"], "invoice-INV-88410.csv")
        rows = list(csv.reader(io.StringIO(
            self.read_export(result).decode("utf-8"))))
        self.assertEqual(rows[0], ["Field", "Value"])
        fields = {r[0]: r[1] for r in rows[1:] if len(r) == 2 and r[0]}
        self.assertEqual(fields["vendor"], "HYDRAULIC SUPPLY CO.")
        self.assertEqual(fields["invoice_number"], "INV-88410")
        self.assertEqual(fields["subtotal"], "1242.5")
        self.assertEqual(fields["total_due"], "1250.0")
        self.assertIn("7.50 higher", fields["notes"])
        # Line items as proper columns after the blank separator row.
        hdr = rows.index(["description", "qty", "unit_price", "amount"])
        self.assertEqual(rows[hdr - 1], [])
        items = rows[hdr + 1:hdr + 4]
        self.assertEqual(items[0], ["Hydraulic hose, 3/4 in x 6 ft",
                                    "2", "86.5", "173.0"])
        self.assertEqual(items[1][0],
                         "Hydraulic oil ISO 46, 55 gal drum")
        self.assertEqual(items[2], ["Manifold fitting kit",
                                    "1", "257.5", "257.5"])

    def test_board_csv(self):
        self.tools.file_tickets([{
            "id": "TKT-001", "category": "Network", "priority": "P2",
            "draft_reply": "Looking into it.", "route": "NOC"}],
            self.tokens.issue())
        result = self.export("csv", "board")
        rows = list(csv.reader(io.StringIO(
            self.read_export(result).decode("utf-8"))))
        self.assertEqual(rows[0], ["Field", "Value"])
        hdr = next(r for r in rows if r[:1] == ["ticket_id"])
        self.assertIn("draft_reply", hdr)
        data = rows[rows.index(hdr) + 1]
        self.assertEqual(data[hdr.index("ticket_id")], "TKT-001")
        self.assertEqual(data[hdr.index("category")], "Network")

    def test_last_run_csv_flattens(self):
        self.tools.last_run_trace = [
            {"tool": "finops_summary", "arguments": {},
             "result": {"costs": {"today_usd": 12.5}, "alerts": []}}]
        result = self.export("csv", "last_run")
        text = self.read_export(result).decode("utf-8")
        self.assertIn("finops_summary.costs", text)
        self.assertIn("today_usd", text)


class TestXlsx(ExportCase):
    def test_invoice_xlsx_loads_back(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not available")
        self.file_invoice()
        result = self.export("xlsx", "intake")
        self.assertEqual(result["filename"], "invoice-INV-88410.xlsx")
        wb = openpyxl.load_workbook(io.BytesIO(self.read_export(result)))
        self.assertEqual(wb.sheetnames, ["Export", "Line items"])
        ws = wb["Export"]
        cells = {r[0].value: r[1].value for r in ws.iter_rows()
                 if r[0].value}
        self.assertEqual(cells["invoice_number"], "INV-88410")
        self.assertEqual(cells["subtotal"], 1242.5)
        self.assertEqual(cells["total_due"], 1250.0)
        self.assertIn("7.50 higher", cells["notes"])
        li = wb["Line items"]
        header = [c.value for c in li[1]]
        self.assertEqual(header, ["description", "qty", "unit_price",
                                  "amount"])
        row2 = [c.value for c in li[2]]
        self.assertEqual(row2, ["Hydraulic hose, 3/4 in x 6 ft",
                                2, 86.5, 173.0])
        self.assertTrue(li[1][0].font.bold)


class TestFormulaEscaping(unittest.TestCase):
    """Model/document text must never become a live spreadsheet
    formula (OWASP CSV injection)."""

    DATA = {"field_rows": [["Vendor", '=HYPERLINK("http://x","click")'],
                           ["Total", "-12.50"]],
            "table": {"columns": ["Item", "Qty", "Note"],
                      "rows": [["@SUM(A1:A2)", "-3", "+cmd"],
                               ["\uff1dEVIL()", "4", "ok"]]}}

    def test_csv_prefixes_formula_text(self):
        from orchestrator.exporter import to_csv_bytes
        rows = list(csv.reader(io.StringIO(
            to_csv_bytes(self.DATA).decode())))
        self.assertEqual(rows[1][1], '\'=HYPERLINK("http://x","click")')
        self.assertEqual(rows[2][1], "-12.50")  # numbers untouched
        self.assertEqual(rows[5][0], "'@SUM(A1:A2)")
        self.assertEqual(rows[5][1], "-3")
        self.assertEqual(rows[5][2], "'+cmd")
        self.assertEqual(rows[6][0], "'\uff1dEVIL()")

    def test_xlsx_has_no_formula_cells(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed")
        from orchestrator.exporter import to_xlsx_bytes
        wb = openpyxl.load_workbook(io.BytesIO(to_xlsx_bytes(self.DATA)))
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    self.assertNotEqual(cell.data_type, "f", cell.value)
        ws = wb.worksheets[0]
        self.assertEqual(ws["B2"].value, '\'=HYPERLINK("http://x","click")')
        self.assertEqual(ws["B3"].value, -12.5)


class TestJson(ExportCase):
    def test_intake_json_shape(self):
        self.file_invoice()
        result = self.export("json", "intake")
        payload = json.loads(self.read_export(result))
        self.assertEqual(payload["kind"], "invoice")
        fields = payload["fields"]
        self.assertEqual(fields["invoice_number"], "INV-88410")
        # line_items recovered as objects, not repr strings.
        self.assertIsInstance(fields["line_items"][0], dict)
        self.assertEqual(fields["line_items"][0]["description"],
                         "Hydraulic hose, 3/4 in x 6 ft")
        self.assertIn("7.50 higher", payload["notes"])

    def test_document_json(self):
        self.tools.file_document_report({
            "title": "Server log anomalies", "summary": "2 anomalies.",
            "findings": [{"line": 12, "severity": "high",
                          "detail": "Disk full on /var."},
                         {"line": 40, "severity": "low",
                          "detail": "Slow query."}]},
            self.tokens.issue())
        result = self.export("json", "document")
        payload = json.loads(self.read_export(result))
        self.assertEqual(payload["title"], "Server log anomalies")
        self.assertEqual(payload["findings"][0]["line"], 12)
        self.assertEqual(payload["findings"][1]["severity"], "low")


class TestTxtDocx(ExportCase):
    def file_audio(self):
        return self.tools.file_intake(
            dict(AUDIO_RECORD), self.tokens.issue(), run_tag="test")

    def test_audio_txt_verbatim_transcript(self):
        self.file_audio()
        result = self.export("txt", "intake")
        text = self.read_export(result).decode("utf-8")
        self.assertIn(AUDIO_RECORD["fields"]["transcript"], text)
        self.assertIn("work_order: 7712", text)
        self.assertIn("priority: high", text)
        # Transcript rendered once as a section, not duplicated.
        self.assertEqual(
            text.count(AUDIO_RECORD["fields"]["transcript"]), 1)

    def test_audio_docx_loads_back(self):
        try:
            import docx
        except ImportError:
            self.skipTest("python-docx not available")
        self.file_audio()
        result = self.export("docx", "intake")
        self.assertTrue(result["filename"].startswith("transcript-"))
        doc = docx.Document(io.BytesIO(self.read_export(result)))
        paragraphs = [p.text for p in doc.paragraphs]
        self.assertTrue(any(AUDIO_RECORD["fields"]["transcript"] in p
                            for p in paragraphs))
        self.assertTrue(any(p == "Transcript" for p in paragraphs))
        self.assertTrue(doc.tables, "fields table missing")
        table_text = " | ".join(
            c.text for row in doc.tables[0].rows for c in row.cells)
        self.assertIn("work_order", table_text)
        self.assertIn("7712", table_text)

    def test_invoice_docx_has_line_items_table(self):
        try:
            import docx
        except ImportError:
            self.skipTest("python-docx not available")
        self.file_invoice()
        result = self.export("docx", "intake")
        doc = docx.Document(io.BytesIO(self.read_export(result)))
        self.assertGreaterEqual(len(doc.tables), 2)
        items = doc.tables[-1]
        header = [c.text for c in items.rows[0].cells]
        self.assertEqual(header, ["description", "qty", "unit_price",
                                  "amount"])
        self.assertEqual(items.rows[1].cells[0].text,
                         "Hydraulic hose, 3/4 in x 6 ft")

    def test_document_txt_findings(self):
        self.tools.file_document_report({
            "title": "Server log anomalies", "summary": "2 anomalies.",
            "findings": [{"line": 12, "severity": "high",
                          "detail": "Disk full on /var."}]},
            self.tokens.issue())
        result = self.export("txt", "document")
        text = self.read_export(result).decode("utf-8")
        self.assertIn("line 12 [high] Disk full on /var.", text)
        self.assertIn("2 anomalies.", text)

    def test_long_transcript_truncated_in_csv_cells(self):
        record = {"kind": "audio_note",
                  "fields": {"transcript": "word " * 400},
                  "notes": ""}
        self.tools.file_intake(record, self.tokens.issue())
        result = self.export("csv", "intake")
        text = self.read_export(result).decode("utf-8")
        self.assertIn("[truncated", text)
        # ...but JSON keeps the full text.
        result = self.export("json", "intake")
        payload = json.loads(self.read_export(result))
        self.assertEqual(payload["fields"]["transcript"], "word " * 400)


# ------------------------------------------------------------- store
class TestStore(ExportCase):
    def test_manifest_written(self):
        self.file_invoice()
        result = self.export("csv", "intake")
        entries = self.tools.exports.list()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], result["export_id"])
        self.assertEqual(entry["filename"], "invoice-INV-88410.csv")
        self.assertEqual(entry["format"], "csv")
        self.assertEqual(entry["source"], "intake")
        self.assertEqual(entry["size_bytes"], result["size_bytes"])
        self.assertTrue(entry["created_at"])
        # On-disk name is the uuid, never the friendly filename.
        self.assertTrue(os.path.isfile(os.path.join(
            self.tools.exports.dir, entry["id"] + ".csv")))

    def test_content_types(self):
        self.assertEqual(CONTENT_TYPES["txt"],
                         "text/plain; charset=utf-8")
        self.assertEqual(
            CONTENT_TYPES["docx"],
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document")
        self.assertEqual(
            CONTENT_TYPES["xlsx"],
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet")
        self.assertTrue(CONTENT_TYPES["csv"].startswith("text/csv"))


# ------------------------------------------------------------- service
class TestService(ExportCase):
    def test_download_auth_and_headers(self):
        svc = self.make_service()
        self.file_invoice()
        result = self.export("xlsx", "intake")
        # Anonymous (no session) -> 403.
        svc_anon = self.make_service(user=None)
        code, _payload, _h = svc_anon.export_download(
            result["export_id"], "")
        self.assertEqual(code, 403)
        # Viewer -> 403 (Operator+ gate).
        svc_viewer = self.make_service(user=VIEWER)
        code, _payload, _h = svc_viewer.export_download(
            result["export_id"], "c")
        self.assertEqual(code, 403)
        # Operator -> bytes + headers.
        code, payload, headers = svc.export_download(
            result["export_id"], "c")
        self.assertEqual(code, 200)
        self.assertTrue(payload.startswith(b"PK"))  # zip container
        self.assertEqual(headers["Content-Type"],
                         CONTENT_TYPES["xlsx"])
        self.assertIn('filename="invoice-INV-88410.xlsx"',
                      headers["Content-Disposition"])
        # Unknown id -> 404.
        code, _payload, _h = svc.export_download("nope", "c")
        self.assertEqual(code, 404)

    def test_exports_list(self):
        svc = self.make_service()
        svc_anon = self.make_service(user=None)
        code, obj = svc_anon.exports_list("")
        self.assertEqual(code, 403)
        self.file_invoice()
        self.export("csv", "intake")
        code, obj = svc.exports_list("c")
        self.assertEqual(code, 200)
        self.assertEqual(len(obj["exports"]), 1)
        self.assertEqual(obj["exports"][0]["filename"],
                         "invoice-INV-88410.csv")
        self.assertTrue(obj["exports"][0]["download_path"]
                        .startswith("/api/exports/"))

    def test_two_step_flow_intake_then_export(self):
        """The Rom flow: file an extraction, then 'Save that as CSV'
        through the normal propose -> approve loop."""
        svc = self.make_service()
        self.file_invoice()
        code, plan = svc.propose("Save that as CSV", "c")
        self.assertEqual(code, 200)
        self.assertEqual([s["name"] for s in plan["steps"]],
                         ["export_file"])
        self.assertEqual(plan["steps"][0]["arguments"],
                         {"format": "csv", "source": "intake"})
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        result = done["trace"][0]["result"]
        self.assertTrue(result["exported"])
        self.assertEqual(result["filename"], "invoice-INV-88410.csv")
        # After an intake filing the default action offers the export.
        self.assertEqual(serve.derive_default_action(
            [{"tool": "file_intake"}])["label"],
            "Export this extraction as Excel")

    def test_combined_intake_and_export_plan(self):
        """Attach + 'save as Excel' in one request: file_intake then
        export_file in a single approved plan."""
        import base64
        svc = self.make_service()
        img = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
        code, plan = svc.intake(img, "image/png",
                                "save it as Excel please", "c")
        self.assertEqual(code, 200)
        names = [s["name"] for s in plan["steps"]]
        self.assertEqual(names, ["file_intake", "export_file"])
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        result = done["trace"][1]["result"]
        self.assertTrue(result["exported"])
        self.assertEqual(result["format"], "xlsx")
        self.assertEqual(result["filename"], "invoice-INV-88410.xlsx")

    def test_capabilities_include_export(self):
        payload = serve.capabilities_payload()
        names = [t["name"] for g in payload["groups"]
                 for t in g["tools"]]
        self.assertIn("export_file", names)
        entry = next(t for g in payload["groups"] for t in g["tools"]
                     if t["name"] == "export_file")
        self.assertTrue(entry["gated"])
        self.assertIn("Word", entry["does"])
        self.assertIn("Word", entry["example"])

    def test_page_markers(self):
        with open(os.path.join(serve.ROOT, "demo_page.html")) as f:
            page = f.read()
        self.assertIn("dl-card", page)
        self.assertIn("Your file is ready", page)
        self.assertIn('t.tool==="export_file"', page)
        self.assertIn("refreshExports", page)
        self.assertIn("EXPORTS — GENERATED FILES", page)
        self.assertIn("/api/exports", page)


if __name__ == "__main__":
    unittest.main()
