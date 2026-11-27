"""Collapsed-by-default demo page (Rom, Nov 27, 2026).

"Have everything on a collapse state, including 'Suggested for you,
goals, built automations, exports, etc'. The idea is to make the UI
super easy to digest, navigate, and super clean."

Every auxiliary panel starts collapsed with a tappable header:
SUGGESTED FOR YOU, GOALS, BUILT AUTOMATIONS, EXPORTS, HOW IT WORKS,
the USE CASES panel (already), and the 4 suggestion-chip groups +
USE CASES groups (all widths). Core interaction stays visible:
topbar, hero, AI BRAIN bar, composer + CHECK, stage, MODEL strip.
Fresh run results auto-expand their panel (item count grew on a
post-run refresh); empty panels show an empty state when opened.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_page():
    with open(os.path.join(ROOT, "demo_page.html"), encoding="utf-8") as f:
        return f.read()


class TestCollapse(unittest.TestCase):
    def test_panels_have_collapsible_structure(self):
        page = read_page()
        for pid in ("sugPanel", "goalPanel", "afPanel", "exPanel",
                    "howWrap"):
            self.assertIn(f'id="{pid}"', page, pid)
        # One collapsible header + chevron per auxiliary panel.
        self.assertEqual(page.count('class="panel-head coll-head"'), 2)
        self.assertEqual(page.count('class="af-head font-mono2 coll-head"'), 3)
        self.assertEqual(page.count('<span class="coll-chev">▾</span>'), 5)
        # Bodies wrapped: 5 coll-body elements in markup.
        self.assertEqual(page.count('class="coll-body"'), 4)
        self.assertEqual(page.count('class="how-strip coll-body"'), 1)
        # Collapse CSS present.
        for marker in (".coll-body { display: none; }",
                       ".open > .coll-body { display: block; }",
                       ".open > .how-strip.coll-body { display: grid; }",
                       ".coll-head { cursor: pointer; }"):
            self.assertIn(marker, page, marker)

    def test_panels_start_collapsed(self):
        page = read_page()
        # No panel/group markup carries the open class on load.
        self.assertNotIn('class="af-panel open"', page)
        self.assertNotIn('class="how-wrap open"', page)
        self.assertIn('box.className="chip-group"', page)
        self.assertIn('g.className="case-group"', page)
        self.assertNotIn("(!narrow", page)
        # USE CASES stays collapsed by default (existing behavior).
        self.assertIn('id="casesBody" style="display:none"', page)
        self.assertIn('id="casesToggle" aria-label="Expand demo" '
                      'aria-expanded="false"', page)

    def test_core_interaction_not_collapsed(self):
        page = read_page()
        # Core regions exist and are not inside a coll-body.
        stage = page[page.index('id="stagePanel"'):page.index('id="sugPanel"')]
        self.assertNotIn("coll-body", stage)
        for marker in ('class="hero"', 'id="cmd"', 'id="proposeBtn"',
                       'class="brain-strip"', 'class="model-strip"',
                       'id="chatScroll"'):
            self.assertIn(marker, page, marker)

    def test_toggle_wiring_and_keyboard(self):
        page = read_page()
        for marker in ("function setPanelOpen(", "function wireCollapsible(",
                       "function maybeExpand(",
                       '["sugPanel","goalPanel","afPanel","exPanel","howWrap"]'
                       ".forEach(id=>wireCollapsible($(id)))"):
            self.assertIn(marker, page, marker)
        # Both toggle wirings are keyboard accessible.
        self.assertGreaterEqual(page.count('setAttribute("tabindex","0")'), 2)
        self.assertGreaterEqual(page.count('setAttribute("role","button")'), 2)
        self.assertGreaterEqual(page.count("onkeydown"), 2)
        self.assertIn('setAttribute("aria-expanded"', page)

    def test_auto_expand_hooks(self):
        page = read_page()
        # Count-growth auto-expand in the three list panels.
        self.assertIn("maybeExpand(panel,sugs.length,fromRun)", page)
        self.assertIn("maybeExpand(panel,exports_.length,fromRun)", page)
        self.assertIn("maybeExpand(panel,flows.length,fromRun)", page)
        # Post-run refresh sites pass fromRun=true.
        self.assertGreaterEqual(page.count("refreshAgentFlows(true)"), 3)
        self.assertGreaterEqual(page.count("refreshExports(true)"), 3)
        self.assertGreaterEqual(page.count("refreshSuggestions(true)"), 2)
        # Goal progress expands the GOALS panel.
        i = page.index("function renderGoalProgress(")
        seg = page[i:page.index("function afterRunCards(", i)]
        self.assertIn('setPanelOpen($("goalPanel"),true)', seg)
        # First refresh only records the count (no expand on load):
        # maybeExpand requires a previously recorded count.
        j = page.index("function maybeExpand(")
        self.assertIn("prev!==undefined", page[j:j + 200])

    def test_empty_states(self):
        page = read_page()
        for marker in ("No suggestions right now",
                       "Sign in to see generated files.",
                       "No generated files yet",
                       "Sign in to see your automations.",
                       "No automations yet",
                       "No goals yet"):
            self.assertIn(marker, page, marker)

    def test_expand_affordances_cyan_and_modest(self):
        page = read_page()
        # Rom, Nov 27: "Make the expand buttons cyan and bigger to
        # make it obvious" — then, same day: too big, shrink them.
        # Every chevron is a 30px cyan-ringed circular badge with a
        # 16px glyph; the USE CASES button is cyan with a 36px tap
        # target. --accent is #00e5ff in dark and the deeper #0891b2
        # in light (legible on both).
        for sel in (".grp-chev", ".coll-chev"):
            i = page.index(sel + " {")
            rule = page[i:page.index("}", i)]
            for marker in ("width: 30px", "height: 30px",
                           "border: 1.5px solid var(--accent)",
                           "border-radius: 50%",
                           "color: var(--accent)",
                           "font-size: 16px"):
                self.assertIn(marker, rule, f"{sel} {marker}")
            self.assertNotIn("color: var(--dim)", rule, sel)
            self.assertNotIn("font-size: 11px", rule, sel)
            self.assertNotIn("width: 40px", rule, sel)
        i = page.index(".btn-collapse {")
        rule = page[i:page.index("}", i)]
        for marker in ("border: 1.5px solid var(--accent)",
                       "color: var(--accent)",
                       "min-height: 36px",
                       "font-size: 13.5px"):
            self.assertIn(marker, rule, f"btn-collapse {marker}")
        self.assertNotIn("min-height: 44px", rule)
        # Light theme keeps a visible cyan wash on the affordances.
        for marker in ('html[data-theme="light"] .grp-chev',
                       'html[data-theme="light"] .coll-chev',
                       'html[data-theme="light"] .btn-collapse'):
            self.assertIn(marker, page, marker)
        # Chevron rotation on open is unchanged.
        self.assertIn(".chip-group.open .grp-chev, "
                      ".case-group.open .grp-chev "
                      "{ transform: rotate(180deg); }", page)
        self.assertIn(".open > .coll-head .coll-chev "
                      "{ transform: rotate(180deg); }", page)

    def test_no_scrollbar_wriggle_on_expand(self):
        # Rom, Nov 27: "after clicking any expand button, the screen
        # become wriggly". Cause: the page starts collapsed (short),
        # so expanding pushed it past the viewport, the vertical
        # scrollbar appeared, layout width shrank ~15px and the
        # centered column shifted sideways. Fix: reserve the gutter
        # permanently on html (and on the inner chat scroller).
        page = read_page()
        self.assertIn("html { overflow-y: scroll; "
                      "scrollbar-gutter: stable; }", page)
        i = page.index(".chat-scroll {")
        rule = page[i:page.index("}", i)]
        self.assertIn("scrollbar-gutter: stable", rule)
        # Toggle handlers stay side-effect free: no scroll/focus in
        # the collapse toggle paths (wriggle candidates ruled out).
        i = page.index("function setPanelOpen(")
        body = page[i:page.index("function wireCollapsible(", i)]
        self.assertNotIn("scrollIntoView", body)
        self.assertNotIn(".focus(", body)
        i = page.index('$("casesToggle").addEventListener(')
        body = page[i:page.index("});", i)]
        self.assertNotIn("scrollIntoView", body)
        self.assertNotIn(".focus(", body)

    def test_no_text_selection_wriggle_on_toggle(self):
        # Rom, Nov 27 (second report): "still wriggly. Looks like
        # after clicking the expand button and collapsing it again,
        # it is trying to auto select text within the page." Cause:
        # the toggle headers are clickable text elements with no
        # selection opt-out, so pointer presses (and iOS double-tap
        # word selection) started a text selection; on collapse the
        # content under the pointer vanished and the browser jumped
        # to the dangling selection. Fix: controls opt out of
        # selection + tap flash, pointer press can't focus-scroll,
        # and every toggle clears any selection first.
        page = read_page()
        # CSS: controls (and their children) opt out of selection;
        # tap highlight and double-tap zoom jitter off.
        self.assertIn(
            ".grp-head, .coll-head, .btn-collapse { "
            "-webkit-user-select: none; user-select: none; "
            "-webkit-tap-highlight-color: transparent; "
            "touch-action: manipulation; }", page)
        self.assertIn(
            ".grp-head *, .coll-head * { -webkit-user-select: none; "
            "user-select: none; }", page)
        # Content stays selectable: the property appears only in the
        # two control rules above (prefixed + standard, twice) — no
        # global/body opt-out.
        self.assertEqual(page.count("user-select"), 4)
        # Pointer press on the div toggles can't start a selection
        # or focus-scroll; keyboard activation untouched.
        self.assertGreaterEqual(
            page.count("onmousedown=e=>e.preventDefault()"), 2)
        self.assertIn('$("casesToggle").addEventListener("mousedown",'
                      'e=>e.preventDefault())', page)
        # Every toggle clears any dangling selection first.
        self.assertIn("function clearSel(){const s=window.getSelection();"
                      "if(s)s.removeAllRanges();}", page)
        i = page.index("function wireCollapsible(")
        self.assertIn("clearSel();",
                      page[i:page.index("function maybeExpand(", i)])
        i = page.index("function wireGrpToggle(")
        self.assertIn("clearSel();",
                      page[i:page.index("function buildGallery(", i)])
        i = page.index('$("casesToggle").addEventListener("click"')
        self.assertIn("clearSel();", page[i:page.index("});", i)])
        # Keyboard path intact.
        self.assertGreaterEqual(page.count("onkeydown"), 2)
        # The scrollbar-gutter fix (first wriggle) stays in place.
        self.assertIn("html { overflow-y: scroll; "
                      "scrollbar-gutter: stable; }", page)

    def test_wizard_host_flow_unchanged(self):
        page = read_page()
        # Suggestion cards still open the wizard in the stage host;
        # NEXT STEPS internals untouched.
        self.assertIn("function openWizardWithTemplate(", page)
        self.assertIn('id="stageWizardHost"', page)
        self.assertIn("wiz.classList.add(\"open\")", page)


if __name__ == "__main__":
    unittest.main()
