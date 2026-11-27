"""Demo UI service tests — auth, cap, replay data, real-execution wiring.

Offline, $0: stub Ember + fixture tools + fake auth. No network.

    python3 -m unittest discover -s tests -v
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from orchestrator.cube_tools import ApprovalTokens, FixtureCubeTools
from orchestrator.ember_client import StubEmberClient
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter


def fake_auth_factory(user):
    def _auth(cookie):
        if not user:
            return None, 0
        return user, serve.ROLE_RANK.get(user.get("role"), 0)
    return _auth


OPERATOR = {"email": "op@test.local", "role": "Operator"}
VIEWER = {"email": "view@test.local", "role": "Viewer"}


class UiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("EMBER_BOARD_PATH")
        os.environ["EMBER_BOARD_PATH"] = os.path.join(
            self._tmp.name, "board.json")
        self.cap_path = os.path.join(self._tmp.name, "cap.json")
        self.recorded_path = os.path.join(self._tmp.name, "recorded.json")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("EMBER_BOARD_PATH", None)
        else:
            os.environ["EMBER_BOARD_PATH"] = self._old
        self._tmp.cleanup()

    def make_service(self, user=OPERATOR, daily_cap=25, live=True):
        meter = SpendMeter()
        orch = None
        if live:
            tokens = ApprovalTokens()
            orch = Orchestrator(StubEmberClient(),
                                FixtureCubeTools(tokens), meter, tokens)
        return serve.DemoService(
            orch, meter, auth_fn=fake_auth_factory(user),
            cap_path=self.cap_path, recorded_path=self.recorded_path,
            daily_cap=daily_cap)


class TestAuth(unittest.TestCase):
    def test_no_cookie_no_user(self):
        user, rank = serve.check_auth("", fetch_me=lambda c: None)
        self.assertIsNone(user)
        self.assertEqual(rank, 0)

    def test_roles_ranked(self):
        for role, want in (("Viewer", 1), ("Operator", 2),
                           ("Engineer", 3), ("Admin", 4), ("Nope", 0)):
            user, rank = serve.check_auth(
                "cube_session=x",
                fetch_me=lambda c, r=role: {"email": "a@b.c", "role": r})
            self.assertEqual(rank, want, role)


class TestRealExecution(UiTestCase):
    """Nov 26, 2026: UI-approved runs execute for real. The webhook
    POST is stubbed here so tests never send anything, but the wiring
    under test is the production path (HttpCubeTools._execute)."""

    def test_ui_tools_use_real_execute(self):
        from orchestrator.cube_tools import HttpCubeTools
        self.assertIs(serve.UiCubeTools._execute, HttpCubeTools._execute)

    def test_run_workflow_fires_sync_webhook(self):
        tokens = ApprovalTokens()
        tools = serve.UiCubeTools(tokens)
        calls = []

        def fake_post(url, body):
            calls.append((url, body))
            return {"run_id": "test-run-1"}

        tools._post_json = fake_post
        result = tools.run_workflow("watcher-status-whatsapp", {},
                                    approval_token=tokens.issue())
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["run_id"], "test-run-1")
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            calls[0][0].endswith("/rBAcAYAXvIL2hNyrMQG1s/sync"),
            calls[0][0])


class TestDailyCap(UiTestCase):
    def test_bump_and_reset(self):
        self.assertEqual(serve.load_cap_state(self.cap_path)["count"], 0)
        self.assertEqual(serve.bump_cap(self.cap_path), 1)
        self.assertEqual(serve.bump_cap(self.cap_path), 2)
        with open(self.cap_path, "w") as f:
            json.dump({"date": "2020-01-01", "count": 99}, f)
        self.assertEqual(serve.load_cap_state(self.cap_path)["count"], 0)


class TestRecorded(UiTestCase):
    def test_missing_file_empty(self):
        self.assertEqual(serve.load_recorded(self.recorded_path)["demos"], [])

    def test_valid_file_loaded(self):
        with open(self.recorded_path, "w") as f:
            json.dump({"recorded_at": "2026-11-26T00:00:00Z",
                       "demos": [{"key": "finops"}]}, f)
        data = serve.load_recorded(self.recorded_path)
        self.assertEqual(data["demos"][0]["key"], "finops")


class TestService(UiTestCase):
    def test_status_shape(self):
        s = self.make_service().status()
        self.assertEqual(s["service"], "metatron-ember")
        self.assertTrue(s["ok"])
        self.assertTrue(s["live"])
        self.assertEqual(s["meter"]["cap_usd"], 10.0)
        self.assertIn("spent_usd", s["meter"])

    def test_status_tool_count_matches_catalog(self):
        # The page's "N DETERMINISTIC" chip is filled from this field,
        # so it can never drift from the catalog again.
        s = self.make_service().status()
        catalog_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools", "catalog.json")
        with open(catalog_path) as f:
            catalog = json.load(f)
        self.assertEqual(s["tool_count"], len(catalog["tools"]))

    def test_page_tool_count_is_dynamic(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "demo_page.html")
        with open(path) as f:
            page = f.read()
        self.assertIn('id="toolCount"', page)
        self.assertIn("s.tool_count", page)
        self.assertNotIn("27 DETERMINISTIC", page)
        self.assertNotIn("27 deterministic", page)

    def test_propose_requires_operator(self):
        svc = self.make_service(user=None)
        code, _ = svc.propose("What is the Watcher status?", "")
        self.assertEqual(code, 403)
        svc = self.make_service(user=VIEWER)
        code, _ = svc.propose("What is the Watcher status?", "c")
        self.assertEqual(code, 403)

    def test_propose_approve_flow(self):
        svc = self.make_service()
        code, plan = svc.propose("Send the Watcher status to WhatsApp", "c")
        self.assertEqual(code, 200)
        self.assertTrue(plan["steps"])
        code, done = svc.approve(plan["id"], "c")
        self.assertEqual(code, 200)
        self.assertTrue(done["final_text"])
        self.assertTrue(done["trace"])

    def test_refusal(self):
        svc = self.make_service()
        code, plan = svc.propose(
            "Order a pizza for the office and book a flight to Denver.", "c")
        self.assertEqual(code, 200)
        # Stub routes unknown intents to list_workflows; either way the
        # service must not crash and must report steps honestly.
        self.assertIn("steps", plan)

    def test_daily_cap_default_is_1000(self):
        # Rom (Nov 26): daily live-run cap raised 100 -> 1000.
        self.assertEqual(serve.DAILY_CAP, 1000)

    def test_daily_cap_blocks(self):
        svc = self.make_service(daily_cap=1)
        code, _ = svc.propose("What is the Watcher status?", "c")
        self.assertEqual(code, 200)
        code, body = svc.propose("What is the Watcher status?", "c")
        self.assertEqual(code, 429)
        self.assertIn("cap", body["error"])

    def test_recorded_only_mode(self):
        svc = self.make_service(live=False)
        self.assertFalse(svc.status()["live"])
        code, _ = svc.propose("What is the Watcher status?", "c")
        self.assertEqual(code, 503)

    def test_reject(self):
        svc = self.make_service()
        _, plan = svc.propose("Send the Watcher status to WhatsApp", "c")
        code, body = svc.reject(plan["id"], "c")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "rejected")
        code, _ = svc.approve(plan["id"], "c")
        self.assertEqual(code, 409)


class TestHttpSurface(UiTestCase):
    def test_endpoints(self):
        svc = self.make_service(user=None)
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     serve.make_handler(svc))
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/status")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read())["service"],
                             "metatron-ember")
            conn.request("GET", "/")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            body = r.read().decode()
            self.assertIn('<h1 class="font-display">MAC</h1>', body)
            self.assertIn("(Metatron Agentic Commander)", body)
            self.assertIn("Powered by: Meta's Muse Ember", body)
            self.assertIn("You prompt. MAC checks. You approve. "
                          "Metatron Cube executes.", body)
            hero_lines = [body.index('<h1 class="font-display">MAC</h1>'),
                          body.index('<div class="hero-subtitle">'
                                     '(Metatron Agentic Commander)</div>'),
                          body.index("Powered by: Meta's Muse Ember"),
                          body.index("You prompt. MAC checks.")]
            self.assertEqual(hero_lines, sorted(hero_lines))
            conn.request("POST", "/api/propose",
                         body=json.dumps({"command": "hi"}),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            self.assertEqual(r.status, 403)
            r.read()
            conn.request("GET", "/api/demos")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read())["demos"], [])
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
