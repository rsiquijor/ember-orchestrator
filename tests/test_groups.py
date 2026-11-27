"""4-group demo-page reorganization (Rom, Nov 27, 2026).

The page's prompt chips and USE CASES panel share four numbered
groups: 1 AIOps, 2 Process Automation, 3 Business Use Cases,
4 Grounding. All 19 demos stay reachable; chips load text into the
composer (never auto-run) except the invoice-photo attach chip,
which mirrors the intake demo's Live file-picker path.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_page():
    with open(os.path.join(ROOT, "demo_page.html"), encoding="utf-8") as f:
        return f.read()


def areas_segments(page):
    """Split the AREAS literal into one text segment per group."""
    start = page.index("const AREAS=[")
    end = page.index("\n];", start)
    block = page[start:end]
    parts = re.split(r'\{group:"', block)
    # parts[0] is the "const AREAS=[" preamble.
    segs = {}
    for part in parts[1:]:
        name = part[:part.index('"')]
        segs[name] = re.findall(r'key:"([^"]+)"', part)
    return segs


EXPECTED = {
    "AIOps": ["whatsapp", "handover", "fullpicture", "triage",
              "finops", "cve", "blast", "capacity"],
    "Process Automation": ["agent-builder", "dashboard-builder"],
    "Business Use Cases": ["plc-converter", "intake", "pdf-intake",
                           "audio-intake", "video-intake", "document",
                           "medical-manual", "paralegal"],
    "Grounding": ["refusal"],
}


class TestGroups(unittest.TestCase):
    def test_four_groups_and_promises(self):
        page = read_page()
        for marker in (
                'group:"AIOps", num:1',
                'group:"Process Automation", num:2',
                'group:"Business Use Cases", num:3',
                'group:"Grounding", num:4',
                "Ember reads live estate data and acts, "
                "with your approval.",
                "Ember designs it, you approve it, Metatron RPA runs it.",
                "Ember works from long-context reference: "
                "manuals, code, law.",
                "Ember refuses what it can't do, and says why."):
            self.assertIn(marker, page, marker)

    def test_demo_mapping_all_19(self):
        segs = areas_segments(read_page())
        self.assertEqual(segs, EXPECTED)
        all_keys = [k for keys in segs.values() for k in keys]
        self.assertEqual(len(all_keys), 19)
        self.assertEqual(len(set(all_keys)), 19)

    def test_recorded_demos_all_mapped(self):
        with open(os.path.join(ROOT, "demos", "recorded.json"),
                  encoding="utf-8") as f:
            rec = json.load(f)
        mapped = {k for keys in areas_segments(read_page()).values()
                  for k in keys}
        for d in rec["demos"]:
            self.assertIn(d["key"], mapped, d["key"])

    def test_chip_groups_counts_and_labels(self):
        page = read_page()
        start = page.index("const CHIP_GROUPS=[")
        end = page.index("\n];", start)
        block = page[start:end]
        groups = re.split(r"\{group:\d+, chips:\[", block)[1:]
        self.assertEqual(len(groups), 4)
        counts = [len(re.findall(r"\{(?:demo|label):", g))
                  for g in groups]
        self.assertEqual(counts, [8, 4, 4, 3])
        for label in (
                "Give me the full picture on the Cube: overall status.",
                "Triage these help-desk tickets. Classify each one.",
                "Export the last result as an Excel file.",
                "Send me the Watcher status on WhatsApp every "
                "morning at 6.",
                "Read an invoice photo",
                "What does the medical manual say about correct "
                "tire pressure?",
                "Delete the production database."):
            self.assertIn(label, block, label)
        # Demo chips keep the proven overrides.
        self.assertIn('chipLabel:"Create a dashboard"', page)
        self.assertIn('chipLabel:"Convert PLC code"', page)
        self.assertIn('chipLabel:"Ask the medical manual"', page)
        self.assertIn('chipLabel:"Ask the paralegal assistant"', page)

    def test_chip_tap_loads_text_never_auto_runs(self):
        page = read_page()
        i = page.index("Prompt chips in the same 4 groups")
        chip_code = page[i:page.index("/* ---------- wiring", i)]
        # Text chips load into the composer and focus it.
        self.assertIn('$("cmd").value=d.chipCmd||d.cmd', chip_code)
        self.assertIn('$("cmd").value=ch.cmd', chip_code)
        self.assertIn('$("cmd").focus()', chip_code)
        # No auto-run anywhere in the chip wiring.
        self.assertNotIn("proposeLive(", chip_code)
        # The invoice-photo chip mirrors the intake Live path:
        # auth check, hint, file picker.
        self.assertIn('ch.attach==="image"', chip_code)
        self.assertIn('$("anyFile").click()', chip_code)

    def test_group_headers_collapse_all_widths(self):
        # Nov 27 (Rom: super clean): groups start COLLAPSED at ALL
        # widths — the old phone-only collapse + first-group-open
        # rule are gone; headers toggle at every width.
        page = read_page()
        for marker in ("function grpHead(", "function wireGrpToggle(",
                       '"chip-group"', "grp-num", "grp-promise",
                       ".chip-group .chip-row { display: none; }",
                       ".case-group .case-grid { display: none; }",
                       ".chip-group.open .chip-row { display: flex; }",
                       ".case-group.open .case-grid { display: grid; }",
                       'box.className="chip-group"',
                       'g.className="case-group"'):
            self.assertIn(marker, page, marker)
        # No width-based open logic remains.
        self.assertNotIn("(!narrow", page)
        self.assertNotIn("window.innerWidth<=560", page)
        # Collapse rules are not scoped inside a phone media query.
        self.assertNotIn("@media (max-width: 560px) {\n    .grp-head",
                         page)


if __name__ == "__main__":
    unittest.main()
