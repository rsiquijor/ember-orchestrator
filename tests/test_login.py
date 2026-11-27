"""In-page sign-in tests — /api/login + /api/logout proxy behavior.

Offline, $0: the Cube interface auth endpoints are stubbed via
auth_post_fn. No network, no real credentials. Asserts cookie
re-issue attributes, failure passthrough, and that the password
never appears in any response.

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
from orchestrator.spend import SpendMeter

# The interface's exact Set-Cookie pair on login (server.js
# handleAuthLogin): host-only clear + Domain session cookie.
IFACE_LOGIN_COOKIES = [
    "cube_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax; Secure",
    "cube_session=deadbeefcafe; HttpOnly; Path=/; Max-Age=604800; "
    "SameSite=Lax; Secure; Domain=.metatroncube.online",
]
IFACE_LOGOUT_COOKIES = [
    "cube_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax; Secure",
    "cube_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax; Secure; "
    "Domain=.metatroncube.online",
]
PASSWORD = "test-password-never-logged"


class StubAuthPost:
    """Records calls; returns a canned (status, data, cookies)."""

    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, url, payload, cookie_header=""):
        self.calls.append((url, payload, cookie_header))
        if self.raises:
            raise self.raises
        return self.result


def make_service(stub):
    tmp = tempfile.mkdtemp()
    return serve.DemoService(
        None, SpendMeter(), auth_fn=lambda c: (None, 0),
        cap_path=os.path.join(tmp, "cap.json"),
        recorded_path=os.path.join(tmp, "recorded.json"),
        auth_post_fn=stub)


class TestLogin(unittest.TestCase):
    def test_success_reissues_interface_cookies_verbatim(self):
        stub = StubAuthPost((200, {"outcome": "ok", "user": {
            "email": "admin@test.local", "role": "Admin"}},
            list(IFACE_LOGIN_COOKIES)))
        svc = make_service(stub)
        code, body, cookies = svc.login("admin@test.local", PASSWORD)
        self.assertEqual(code, 200)
        self.assertEqual(body, {"ok": True,
                                "email": "admin@test.local",
                                "role": "Admin"})
        self.assertEqual(cookies, IFACE_LOGIN_COOKIES)
        # Session cookie keeps the interface's exact attributes.
        session = cookies[1]
        for attr in ("Domain=.metatroncube.online", "HttpOnly",
                     "Path=/", "Max-Age=604800", "SameSite=Lax",
                     "Secure"):
            self.assertIn(attr, session)
        # Forwarded to the interface login URL with the credentials.
        self.assertEqual(stub.calls[0][0], serve.AUTH_LOGIN_URL)
        self.assertEqual(stub.calls[0][1],
                         {"email": "admin@test.local",
                          "password": PASSWORD})

    def test_token_and_password_never_in_body(self):
        stub = StubAuthPost((200, {"outcome": "ok", "user": {
            "email": "a@b.c", "role": "Operator"}},
            list(IFACE_LOGIN_COOKIES)))
        svc = make_service(stub)
        _, body, _ = svc.login("a@b.c", PASSWORD)
        blob = json.dumps(body)
        self.assertNotIn(PASSWORD, blob)
        self.assertNotIn("deadbeefcafe", blob)

    def test_bad_credentials_passthrough_no_cookie(self):
        stub = StubAuthPost((401, {"outcome": "error", "detail":
                                   "Invalid email or password."}, []))
        svc = make_service(stub)
        code, body, cookies = svc.login("a@b.c", "wrong")
        self.assertEqual(code, 401)
        self.assertEqual(body["error"], "Invalid email or password.")
        self.assertEqual(cookies, [])

    def test_rate_limit_passthrough(self):
        stub = StubAuthPost((429, {"outcome": "error", "detail":
                                   "Too many failed attempts — wait a "
                                   "few minutes and try again."}, []))
        svc = make_service(stub)
        code, body, cookies = svc.login("a@b.c", "wrong")
        self.assertEqual(code, 429)
        self.assertIn("Too many", body["error"])
        self.assertEqual(cookies, [])

    def test_missing_fields_never_reach_interface(self):
        stub = StubAuthPost((200, {}, []))
        svc = make_service(stub)
        for email, pw in (("", "x"), ("a@b.c", ""), ("", "")):
            code, _, cookies = svc.login(email, pw)
            self.assertEqual(code, 400)
            self.assertEqual(cookies, [])
        self.assertEqual(stub.calls, [])

    def test_interface_unreachable_502(self):
        stub = StubAuthPost(raises=OSError("connection refused"))
        svc = make_service(stub)
        code, body, cookies = svc.login("a@b.c", PASSWORD)
        self.assertEqual(code, 502)
        self.assertEqual(cookies, [])

    def test_success_without_cookie_is_502(self):
        stub = StubAuthPost((200, {"outcome": "ok", "user": {
            "email": "a@b.c", "role": "Admin"}}, []))
        svc = make_service(stub)
        code, _, cookies = svc.login("a@b.c", PASSWORD)
        self.assertEqual(code, 502)
        self.assertEqual(cookies, [])


class TestLogout(unittest.TestCase):
    def test_forwards_cookie_and_clear_pair(self):
        stub = StubAuthPost((200, {"outcome": "ok"},
                             list(IFACE_LOGOUT_COOKIES)))
        svc = make_service(stub)
        code, body, cookies = svc.logout("cube_session=deadbeefcafe")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(cookies, IFACE_LOGOUT_COOKIES)
        self.assertEqual(stub.calls[0][0], serve.AUTH_LOGOUT_URL)
        self.assertEqual(stub.calls[0][2], "cube_session=deadbeefcafe")
        for c in cookies:
            self.assertIn("Max-Age=0", c)

    def test_unreachable_interface_still_clears(self):
        stub = StubAuthPost(raises=OSError("down"))
        svc = make_service(stub)
        code, body, cookies = svc.logout("cube_session=x")
        self.assertEqual(code, 200)
        self.assertEqual(cookies, serve.CLEAR_COOKIES)


class TestLoginHttpSurface(unittest.TestCase):
    """Through the real handler: Set-Cookie headers on the wire."""

    def _serve(self, stub):
        svc = make_service(stub)
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     serve.make_handler(svc))
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server, server.server_address[1]

    def test_login_and_logout_headers(self):
        def auth_post(url, payload, cookie_header=""):
            if url.endswith("/login"):
                return 200, {"outcome": "ok", "user": {
                    "email": "admin@test.local", "role": "Admin"}}, \
                    list(IFACE_LOGIN_COOKIES)
            return 200, {"outcome": "ok"}, list(IFACE_LOGOUT_COOKIES)

        server, port = self._serve(auth_post)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port,
                                              timeout=10)
            conn.request("POST", "/api/login",
                         body=json.dumps({"email": "admin@test.local",
                                          "password": PASSWORD}),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            body = r.read().decode()
            self.assertEqual(r.status, 200)
            set_cookies = r.headers.get_all("Set-Cookie") or []
            self.assertEqual(set_cookies, IFACE_LOGIN_COOKIES)
            self.assertNotIn(PASSWORD, body)
            self.assertNotIn("deadbeefcafe", body)

            conn.request("POST", "/api/logout", body="{}",
                         headers={"Content-Type": "application/json",
                                  "Cookie": "cube_session=deadbeefcafe"})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
            set_cookies = r.headers.get_all("Set-Cookie") or []
            self.assertEqual(set_cookies, IFACE_LOGOUT_COOKIES)
        finally:
            server.shutdown()

    def test_login_failure_has_no_set_cookie(self):
        stub = StubAuthPost((401, {"outcome": "error", "detail":
                                   "Invalid email or password."}, []))
        server, port = self._serve(stub)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port,
                                              timeout=10)
            conn.request("POST", "/api/login",
                         body=json.dumps({"email": "a@b.c",
                                          "password": "wrong"}),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 401)
            self.assertFalse(r.headers.get_all("Set-Cookie"))
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
