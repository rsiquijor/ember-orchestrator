"""Unified multimodal intake tests (Nov 26, 2026).

POST /api/intake-file routes image / PDF / audio / video / text to
the Ember input format the Meta Model API accepts (probed live:
image_url, file, input_audio, video_url-object). Offline, $0: stub
Ember + fixture tools. The stub returns canned extractions per part
type, so routing + filing are exercised end to end.

    python3 -m unittest discover -s tests -v
"""
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.orchestrator import parse_next_steps  # noqa: F401  (import sanity)
from tests.test_ui import UiTestCase, VIEWER

B64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
FAKE_PDF = b"%PDF-1.4 fake invoice bytes"
FAKE_WAV = b"RIFF....WAVEfmt fake audio"
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42 fake video"
FAKE_PNG = b"\x89PNG\r\n\x1a\n fake image"
FAKE_TXT = b"line one\nERROR disk full at line two\nline three\n"


class TestRouting(UiTestCase):
    def test_pdf_routes_to_file_intake(self):
        svc = self.make_service()
        code, plan = svc.intake_file("invoice.pdf", "application/pdf",
                                     B64(FAKE_PDF), "", "c")
        self.assertEqual(code, 200)
        self.assertEqual([s["name"] for s in plan["steps"]], ["file_intake"])
        rec = plan["steps"][0]["arguments"]["record"]
        self.assertEqual(rec["kind"], "invoice")
        self.assertEqual(rec["fields"]["invoice_number"], "INV-88410")

    def test_audio_routes_to_audio_note(self):
        svc = self.make_service()
        code, plan = svc.intake_file("note.wav", "audio/wav",
                                     B64(FAKE_WAV), "", "c")
        self.assertEqual(code, 200)
        rec = plan["steps"][0]["arguments"]["record"]
        self.assertEqual(rec["kind"], "audio_note")
        self.assertIn("transcript", rec["fields"])
        self.assertEqual(rec["fields"]["work_order"], "7712")

    def test_video_routes_to_video_kind(self):
        svc = self.make_service()
        code, plan = svc.intake_file("fault.mp4", "video/mp4",
                                     B64(FAKE_MP4), "", "c")
        self.assertEqual(code, 200)
        rec = plan["steps"][0]["arguments"]["record"]
        self.assertEqual(rec["kind"], "video")
        self.assertIn("PUMP FAULT", rec["fields"]["on_screen_text"])

    def test_image_routes_like_intake(self):
        svc = self.make_service()
        code, plan = svc.intake_file("photo.png", "image/png",
                                     B64(FAKE_PNG), "", "c")
        self.assertEqual(code, 200)
        self.assertEqual([s["name"] for s in plan["steps"]], ["file_intake"])

    def test_text_routes_to_document_report(self):
        svc = self.make_service()
        code, plan = svc.intake_file("server.log", "text/plain",
                                     B64(FAKE_TXT), "", "c")
        self.assertEqual(code, 200)
        self.assertEqual([s["name"] for s in plan["steps"]],
                         ["file_document_report"])

    def test_extension_fallback(self):
        svc = self.make_service()
        code, plan = svc.intake_file("invoice.pdf", "application/octet-stream",
                                     B64(FAKE_PDF), "", "c")
        self.assertEqual(code, 200)
        self.assertEqual([s["name"] for s in plan["steps"]], ["file_intake"])

    def test_approve_files_record(self):
        svc = self.make_service()
        code, plan = svc.intake_file("note.mp3", "audio/mpeg",
                                     B64(FAKE_WAV), "", "c")
        self.assertEqual(code, 200)
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        self.assertIn("audio_note", done["final_text"])
        self.assertTrue(done["next_steps"] or done["automate_command"])


class TestValidation(UiTestCase):
    def test_unsupported_type(self):
        svc = self.make_service()
        for name, mt in (("clip.mov", "video/quicktime"),
                         ("voice.m4a", "audio/mp4"),
                         ("movie.webm", "video/webm"),
                         ("data.bin", "application/octet-stream")):
            code, body = svc.intake_file(name, mt, B64(b"x"), "", "c")
            self.assertEqual(code, 400, (name, body))
            self.assertIn("supported", body["error"])

    def test_bad_base64(self):
        svc = self.make_service()
        code, body = svc.intake_file("invoice.pdf", "application/pdf",
                                     "!!!not-base64!!!", "", "c")
        self.assertEqual(code, 400)
        self.assertIn("base64", body["error"])

    def test_empty_file(self):
        svc = self.make_service()
        code, body = svc.intake_file("invoice.pdf", "application/pdf",
                                     "", "", "c")
        self.assertEqual(code, 400)

    def test_size_caps(self):
        svc = self.make_service()
        big_pdf = b"x" * (serve.PDF_MAX_BYTES + 1)
        code, body = svc.intake_file("big.pdf", "application/pdf",
                                     B64(big_pdf), "", "c")
        self.assertEqual(code, 413)
        self.assertIn("too large", body["error"])
        big_vid = b"x" * (serve.VIDEO_MAX_BYTES + 1)
        code, _ = svc.intake_file("big.mp4", "video/mp4",
                                  B64(big_vid), "", "c")
        self.assertEqual(code, 413)
        big_txt = ("x" * (serve.DOCUMENT_MAX_CHARS + 1)).encode()
        code, _ = svc.intake_file("big.log", "text/plain",
                                  B64(big_txt), "", "c")
        self.assertEqual(code, 413)

    def test_auth_required(self):
        svc = self.make_service(user=None)
        code, _ = svc.intake_file("invoice.pdf", "application/pdf",
                                  B64(FAKE_PDF), "", "")
        self.assertEqual(code, 403)
        svc = self.make_service(user=VIEWER)
        code, _ = svc.intake_file("invoice.pdf", "application/pdf",
                                  B64(FAKE_PDF), "", "c")
        self.assertEqual(code, 403)

    def test_recorded_only_mode(self):
        svc = self.make_service(live=False)
        code, _ = svc.intake_file("invoice.pdf", "application/pdf",
                                  B64(FAKE_PDF), "", "c")
        self.assertEqual(code, 503)


class TestScrub(UiTestCase):
    def test_binary_parts_scrubbed_from_plan(self):
        svc = self.make_service()
        code, plan = svc.intake_file("fault.mp4", "video/mp4",
                                     B64(FAKE_MP4), "", "c")
        self.assertEqual(code, 200)
        stored = svc.orch.plans[plan["id"]]
        blob = str(stored["messages"])
        self.assertNotIn("data:video/mp4;base64", blob)
        self.assertIn("[video attached", blob)


class TestPage(unittest.TestCase):
    def test_attach_anything_ui(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            page = f.read()
        self.assertIn("Attach anything", page)
        self.assertIn('id="anyFile"', page)
        self.assertIn("/api/intake-file", page)
        for key in ("pdf-intake", "audio-intake", "video-intake"):
            self.assertIn(key, page)

    def test_paste_anything_ui(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            page = f.read()
        # Renamed control + helper text covering every media type.
        self.assertIn("Paste Anything", page)
        self.assertNotIn("Paste text", page)
        self.assertIn("Paste anything", page)
        # Paste handler reads clipboard files and routes them through
        # the shared intake path used by the Attach picker.
        self.assertIn('addEventListener("paste"', page)
        self.assertIn("clipboardData", page)
        self.assertIn("intakeFile", page)
        self.assertIn("intakeDroppedFiles", page)
        # Drag-and-drop routes the same way.
        self.assertIn('addEventListener("drop"', page)
        self.assertIn("dataTransfer", page)
        # The Attach picker and the text zone both stay.
        self.assertIn('id="anyFile"', page)
        self.assertIn('id="docText"', page)
        self.assertIn("/api/intake-file", page)

    def test_stage_rail_fit_ui(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            page = f.read()
        # All six stages present, in order, with their labels.
        for stage in range(6):
            self.assertIn(f'data-stage="{stage}"', page)
        for label in ("HUMAN", "EMBER", "APPROVE", "METATRON", "NEXT"):
            self.assertIn(f'class="rlab">{label}<', page)
        # Nodes shrink instead of overflowing; labels are capped to their box.
        self.assertIn("flex: 0 1 74px; min-width: 0", page)
        self.assertIn("max-width: 100%; white-space: nowrap", page)
        # Phone widths: still ONE line — smaller icons + fluid labels, no grid.
        self.assertNotIn("grid-template-columns: repeat(3, minmax(0, 1fr))", page)
        self.assertNotIn(".rconn { display: none; }", page)
        self.assertIn(".rnode { flex: 1 1 0; gap: 3px; }", page)
        self.assertIn(".rnode .ric { width: 27px; height: 27px; }", page)
        self.assertIn(".rnode .ric svg { width: 13px; height: 13px; }", page)
        self.assertIn("font-size: clamp(7.5px, 2.3vw, 9px)", page)
        self.assertIn(".rconn { flex: 0 0 3px; min-width: 3px; margin-top: 13px; }", page)
        # Active/done stage styling is intact.
        self.assertIn(".rnode.active .ric", page)
        self.assertIn(".rnode.done .ric", page)
        self.assertIn("function setStage(", page)


if __name__ == "__main__":
    unittest.main()
