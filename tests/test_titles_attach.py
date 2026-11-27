"""Cyan titles + attach row under the chat box (Rom, Nov 27, 2026).

"Put a title for '1. AIOps; 2. Process Automation; 3 Business Use
Cases; 4 Grounding' as 'USE CASES' in cyan color. Also make the other
titles in Cyan for consistency. Then move the 'Attach anything,
Paste Anything, and Analyze' directly under the chat box."

- A cyan USE CASES section title sits above the 4 chip groups.
- Every section/panel title is cyan via var(--accent) (dark #00e5ff /
  light #0891b2): SUGGESTED FOR YOU, GOALS, DEMO (lower panel,
  renamed from USE CASES Nov 27)
  via .panel-title; BUILT AUTOMATIONS, EXPORTS, HOW IT WORKS via
  .af-head; the 4 chip-group titles via .grp-name (already accent).
- The attach controls (Attach anything / Paste Anything / Analyze +
  doc zone + filename hint) sit directly under the chat box, before
  the USE CASES title and chip groups — layout-only move, same ids
  and the shared intakeFile(f) path untouched.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_page():
    with open(os.path.join(ROOT, "demo_page.html"), encoding="utf-8") as f:
        return f.read()


def css_rule(page, selector):
    # Match the standalone rule (selector at line start), not a
    # compound selector that merely ends with it.
    m = re.search(r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]*)\}",
                  page)
    return m.group(1) if m else ""


class TestCyanTitles(unittest.TestCase):
    def test_use_cases_section_title_above_chip_groups(self):
        page = read_page()
        marker = ('<div class="chips-sec-title">'
                  '<span class="panel-title font-display">USE CASES</span>'
                  '</div>')
        self.assertIn(marker, page)
        # Title sits directly above the chips container.
        self.assertLess(page.index(marker), page.index('id="chips"'))
        # And after the attach zone (attach row is under the chat box).
        self.assertLess(page.index('id="attachZone"'), page.index(marker))

    def test_panel_titles_cyan(self):
        page = read_page()
        self.assertIn("color: var(--accent)", css_rule(page, ".panel-title"))
        # The three panel-title titles are all present.
        for title in ("SUGGESTED FOR YOU", "GOALS", "DEMO"):
            self.assertIn(
                f'<span class="panel-title font-display">{title}</span>',
                page, title)

    def test_af_head_titles_cyan(self):
        page = read_page()
        rule = css_rule(page, ".af-head")
        self.assertIn("color: var(--accent)", rule)
        self.assertNotIn("color: var(--dim)", rule)
        for title in ("BUILT AUTOMATIONS — AGENT BUILDER",
                      "EXPORTS — GENERATED FILES", "HOW IT WORKS"):
            self.assertIn(
                f'<div class="af-head font-mono2 coll-head">{title}',
                page, title)

    def test_chip_group_titles_still_accent(self):
        page = read_page()
        self.assertIn("color: var(--accent)", css_rule(page, ".grp-name"))
        # Accent token is the cyan pair (dark/light).
        self.assertIn("--accent: #00e5ff", page)
        self.assertIn("--accent: #0891b2", page)

    def test_cyan_expand_badges_kept_modest(self):
        page = read_page()
        for sel in (".grp-chev", ".coll-chev"):
            rule = css_rule(page, sel)
            self.assertIn("border: 1.5px solid var(--accent)", rule, sel)
            self.assertIn("width: 30px", rule, sel)


class TestAttachRowPosition(unittest.TestCase):
    def test_attach_controls_directly_under_chat_box(self):
        page = read_page()
        order = [
            'id="cmd"',            # the chat box
            'id="proposeBtn"',     # CHECK
            'id="attachZone"',     # attach controls moved up
            'id="anyFile"',
            'id="anyName"',
            'id="anyGo"',
            'id="docToggle"',
            'id="docZone"',
            'id="docText"',
            'id="docGo"',
            'id="attachHint"',
            'class="chips-sec-title"',
            'id="chips"',
            'id="errBox"',
        ]
        positions = [page.index(m) for m in order]
        self.assertEqual(positions, sorted(positions),
                         "attach row must sit between chat box and chips")

    def test_attach_labels_and_intake_path_intact(self):
        page = read_page()
        for marker in ('for="anyFile">Attach anything</label>',
                       'id="docToggle" type="button">Paste Anything</button>',
                       'id="anyGo" type="button" disabled>Analyze</button>',
                       'id="docGo" type="button">Analyze document</button>',
                       'class="attach-name" id="anyName"',
                       'class="attach-hint" id="attachHint"',
                       "async function intakeFile(f){",
                       'for(const el of [$("attachZone"),$("docText")])',
                       '$("docText").addEventListener("paste"',
                       'document.addEventListener("paste"'):
            self.assertIn(marker, page, marker)


if __name__ == "__main__":
    unittest.main()
