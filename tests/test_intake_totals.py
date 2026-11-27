"""Deterministic invoice-totals verification tests (Nov 27, 2026).

Regression for the intake #59 false alarm: Ember extracted a correct
invoice but populated amount_paid with the total's value, then wrote
its own "Arithmetic mismatch" critique into notes and repeated it in
the summary. Totals are now verified deterministically by
orchestrator/intake_totals.py; Ember reports that verdict exactly.

Offline, $0: unit tests on the verifier + FixtureCubeTools filing.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.cube_tools import ApprovalTokens, FixtureCubeTools
from orchestrator.intake_totals import (line_item_amount, parse_money,
                                        verify_invoice_totals)
from orchestrator.orchestrator import NEXT_STEPS_INSTRUCTION, SYSTEM_PROMPT

# Rom's intake #59 (Nov 27, 2026) exactly as stored on the box: a
# correct invoice whose extraction copied the total into amount_paid.
ROM_FIELDS = {
    "vendor": "ALBERT SORT",
    "invoice_number": "123456",
    "date": "12 JANUARY, 2027",
    "due_date": "12 FEBRUARY, 2027",
    "po_number": "0122",
    "bill_to": {"name": "Elon Musk",
                "address": "6033, Fifth Avenue, New York",
                "phone": "(123)-456-7890"},
    "ship_to": {"name": "Elon Musk",
                "address": "6033, Fifth Avenue, New York",
                "phone": "(123)-456-7890"},
    "line_items": [
        "{'description': 'Item 1', 'price': '$10.00', 'qty': 1, "
        "'total': '$10.00'}",
        "{'description': 'Item 2', 'price': '$10.00', 'qty': 1, "
        "'total': '$10.00'}",
        "{'description': 'Item 3', 'price': '$10.00', 'qty': 1, "
        "'total': '$10.00'}",
    ],
    "subtotal": "$30.00",
    "tax": "$0.00",
    "discount": "$0.00",
    "shipping": "$0.00",
    "total_due": "$30.00",
    "amount_paid": "$30.00",
    "balance_due": "$30.00",
}

# The canonical true positive: INV-88410, $7.50 planted overstatement
# (items sum to the stated subtotal; total due is $7.50 higher with no
# tax/shipping lines).
INV_88410_FIELDS = {
    "vendor": "Hydraulic Supply Co.",
    "invoice_number": "INV-88410",
    "date": "11/18/2026",
    "po_number": "PO-2214",
    "line_items": [
        "Hydraulic hose, 3/4 in x 6 ft x2 @ 86.50 = 173.00",
        "Hydraulic oil ISO 46, 55 gal drum x1 @ 812.00 = 812.00",
        "Manifold fitting kit x1 @ 257.50 = 257.50",
    ],
    "subtotal": "1,242.50",
    "total_due": "1,250.00",
}


class TestParseMoney(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_money("$1,242.50"), 1242.50)
        self.assertEqual(parse_money("30"), 30.0)
        self.assertEqual(parse_money(30), 30.0)
        self.assertEqual(parse_money(1242.5), 1242.5)
        self.assertEqual(parse_money("(30.00)"), -30.0)
        self.assertEqual(parse_money("-5.25"), -5.25)

    def test_rejects(self):
        for bad in ("", "  ", "see note", "30 USD", None, True, [], {}):
            self.assertIsNone(parse_money(bad), bad)


class TestLineItemAmount(unittest.TestCase):
    def test_dict_forms(self):
        self.assertEqual(
            line_item_amount({"description": "x", "total": "$10.00"}),
            10.0)
        self.assertEqual(
            line_item_amount({"qty": 2, "unit_price": 86.5}), 173.0)
        self.assertEqual(
            line_item_amount({"qty": "3", "price": "$10.00"}), 30.0)

    def test_string_forms(self):
        self.assertEqual(
            line_item_amount(
                "Hydraulic hose, 3/4 in x 6 ft x2 @ 86.50 = 173.00"),
            173.0)
        self.assertEqual(line_item_amount("Widget x3 @ 10.00"), 30.0)
        self.assertEqual(
            line_item_amount(
                "{'description': 'Item 1', 'price': '$10.00', "
                "'qty': 1, 'total': '$10.00'}"), 10.0)

    def test_unreadable(self):
        self.assertIsNone(line_item_amount("some prose"))
        self.assertIsNone(line_item_amount({"description": "x"}))
        self.assertIsNone(line_item_amount(None))


class TestVerifyInvoiceTotals(unittest.TestCase):
    def test_rom_intake59_no_false_alarm(self):
        """Rom's exact field set: correct invoice, amount_paid copied
        from the total. Must NOT flag."""
        verdict = verify_invoice_totals(ROM_FIELDS)
        self.assertEqual(verdict["status"], "ok", verdict)
        self.assertTrue(verdict.get("amount_paid_unreliable"))
        balance = [c for c in verdict["checks"]
                   if c["name"] == "balance_arithmetic"]
        self.assertEqual(len(balance), 1)
        self.assertEqual(balance[0]["status"], "skipped")

    def test_inv88410_still_flags(self):
        """The planted $7.50 overstatement must still flag."""
        verdict = verify_invoice_totals(INV_88410_FIELDS)
        self.assertEqual(verdict["status"], "mismatch", verdict)
        self.assertIn("$7.50", verdict["detail"])
        items = [c for c in verdict["checks"]
                 if c["name"] == "line_items_vs_subtotal"]
        self.assertEqual(items[0]["status"], "ok")

    def test_genuine_balance_error_flags(self):
        """Total 100, paid 40, balance shown 50 -> flag (should be 60)."""
        verdict = verify_invoice_totals({
            "line_items": [{"description": "x", "total": "100.00"}],
            "subtotal": "100.00", "total_due": "100.00",
            "amount_paid": "40.00", "balance_due": "50.00"})
        self.assertEqual(verdict["status"], "mismatch", verdict)
        self.assertIn("$60.00", verdict["detail"])

    def test_partial_payment_correct_balance_ok(self):
        verdict = verify_invoice_totals({
            "subtotal": "100.00", "total_due": "100.00",
            "amount_paid": "40.00", "balance_due": "60.00"})
        self.assertEqual(verdict["status"], "ok", verdict)

    def test_paid_in_full_ok(self):
        verdict = verify_invoice_totals({
            "subtotal": "100.00", "total_due": "100.00",
            "amount_paid": "100.00", "balance_due": "0.00"})
        self.assertEqual(verdict["status"], "ok", verdict)
        self.assertFalse(verdict.get("amount_paid_unreliable"))

    def test_overpayment_not_flagged(self):
        verdict = verify_invoice_totals({
            "subtotal": "100.00", "total_due": "100.00",
            "amount_paid": "120.00", "balance_due": "0.00"})
        self.assertEqual(verdict["status"], "ok", verdict)

    def test_items_not_summing_to_subtotal_flags(self):
        verdict = verify_invoice_totals({
            "line_items": [{"total": "10.00"}, {"total": "10.00"}],
            "subtotal": "25.00", "total_due": "25.00"})
        self.assertEqual(verdict["status"], "mismatch", verdict)

    def test_tax_and_shipping_accounted(self):
        verdict = verify_invoice_totals({
            "subtotal": "100.00", "tax": "8.00", "shipping": "5.00",
            "discount": "3.00", "total_due": "110.00"})
        self.assertEqual(verdict["status"], "ok", verdict)

    def test_unverifiable_when_no_numbers(self):
        verdict = verify_invoice_totals(
            {"vendor": "X", "invoice_number": "1"})
        self.assertEqual(verdict["status"], "unverifiable", verdict)
        verdict = verify_invoice_totals({})
        self.assertEqual(verdict["status"], "unverifiable", verdict)
        verdict = verify_invoice_totals(None)
        self.assertEqual(verdict["status"], "unverifiable", verdict)

    def test_unparseable_component_skips_not_flags(self):
        verdict = verify_invoice_totals({
            "subtotal": "100.00", "tax": "see note",
            "total_due": "108.00"})
        self.assertNotEqual(verdict["status"], "mismatch", verdict)


class TestFileIntakeVerdict(unittest.TestCase):
    """End-to-end through the gated tool: the verdict is returned to
    Ember (compose turn) and stored on the intake record."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("EMBER_INTAKE_PATH")
        os.environ["EMBER_INTAKE_PATH"] = os.path.join(
            self._tmp.name, "intake.json")
        self.tokens = ApprovalTokens()
        self.tools = FixtureCubeTools(self.tokens)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("EMBER_INTAKE_PATH", None)
        else:
            os.environ["EMBER_INTAKE_PATH"] = self._old
        self._tmp.cleanup()

    def _file(self, fields):
        record = {"kind": "invoice", "fields": fields, "notes": ""}
        return self.tools.file_intake(record, self.tokens.issue())

    def test_rom_fields_file_clean(self):
        result = self._file(dict(ROM_FIELDS))
        self.assertEqual(result["totals_check"]["status"], "ok")
        stored = self.tools.intake.list()[-1]
        self.assertEqual(stored["totals_check"]["status"], "ok")

    def test_inv88410_fields_flag(self):
        result = self._file(dict(INV_88410_FIELDS))
        self.assertEqual(result["totals_check"]["status"], "mismatch")
        stored = self.tools.intake.list()[-1]
        self.assertEqual(stored["totals_check"]["status"], "mismatch")

    def test_non_invoice_gets_no_verdict(self):
        record = {"kind": "audio_note",
                  "fields": {"transcript": "hello"}, "notes": ""}
        result = self.tools.file_intake(record, self.tokens.issue())
        self.assertNotIn("totals_check", result)
        stored = self.tools.intake.list()[-1]
        self.assertNotIn("totals_check", stored)


class TestPrompts(unittest.TestCase):
    def test_no_free_form_mismatch_instruction(self):
        self.assertNotIn("flag any arithmetic mismatch", SYSTEM_PROMPT)
        self.assertNotIn("flag any arithmetic mismatch",
                         NEXT_STEPS_INSTRUCTION)

    def test_verdict_reporting_instructed(self):
        self.assertIn("totals_check", SYSTEM_PROMPT)
        self.assertIn("amount_paid", SYSTEM_PROMPT)
        self.assertIn("totals_check", NEXT_STEPS_INSTRUCTION)

    def test_catalog_notes_description(self):
        import json
        root = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        with open(os.path.join(root, "tools", "catalog.json")) as f:
            cat = json.load(f)
        tool = [t for t in cat["tools"] if t["name"] == "file_intake"][0]
        notes_desc = (tool["parameters"]["properties"]["record"]
                      ["properties"]["notes"]["description"])
        self.assertNotIn("arithmetic mismatches", notes_desc)
        self.assertIn("totals_check", tool["returns"])


if __name__ == "__main__":
    unittest.main()
