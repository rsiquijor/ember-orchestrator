"""Live verification of the 3-actions NEXT STEPS restructure.
Runs ON the box against loopback :7860 (auth) + :8023 (Ember UI API).
Creds come from /tmp/ember-3actions/creds.json (deleted by caller).
Prints no secrets."""
import http.client
import json


def call(host, port, method, path, body=None, cookie=None):
    conn = http.client.HTTPConnection(host, port, timeout=120)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path,
                 body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    # Login sends TWO Set-Cookie headers (expire the legacy host-only
    # cookie, then set the domain-wide one) — take the last
    # cube_session with a real value.
    set_cookie = ""
    for name, value in resp.getheaders():
        if name.lower() == "set-cookie" and value.startswith("cube_session="):
            if len(value.split(";", 1)[0]) > len("cube_session=") + 10:
                set_cookie = value
    conn.close()
    try:
        parsed = json.loads(data)
    except ValueError:
        parsed = {"raw": data[:300]}
    return resp.status, parsed, set_cookie


creds = json.load(open("/tmp/ember-3actions/creds.json"))
code, body, sc = call("127.0.0.1", 7860, "POST", "/api/auth/login", creds)
assert code == 200, f"login failed: {code} {body}"
cookie = sc.split(";")[0]
print("login OK as", body.get("email"), body.get("role"))

code, who, _ = call("127.0.0.1", 8023, "GET", "/api/whoami", cookie=cookie)
print("whoami:", code, "can_run_live=", who.get("can_run_live"))
assert who.get("can_run_live")

# 1) Capacity run -> NEXT STEPS payload
code, plan, _ = call("127.0.0.1", 8023, "POST", "/api/propose",
                     {"command": "How is our capacity looking?"},
                     cookie=cookie)
assert code == 200 and plan.get("steps"), (code, plan)
print("capacity plan steps:", [s["name"] for s in plan["steps"]])
code, done, _ = call("127.0.0.1", 8023, "POST", "/api/approve",
                     {"plan_id": plan["id"]}, cookie=cookie)
assert code == 200, (code, done)
print("capacity trace tools:", [t["tool"] for t in done["trace"]])
print("next_steps:", json.dumps(done["next_steps"]))
print("default_action:", json.dumps(done.get("default_action")))
print("automate_command:", done["automate_command"])
assert "default_action" in done

# 2) Take action: Ember's first kind=action, else the default
actions = [s for s in done["next_steps"] if s["kind"] != "automate"]
pick = actions[0] if actions else done["default_action"]
assert pick, "no action available for Take action"
print("TAKE ACTION pick:", pick["label"], "=>", pick["command"])
code, plan2, _ = call("127.0.0.1", 8023, "POST", "/api/propose",
                      {"command": pick["command"]}, cookie=cookie)
assert code == 200 and plan2.get("steps"), (code, plan2)
print("action plan steps:", [s["name"] for s in plan2["steps"]])
code, done2, _ = call("127.0.0.1", 8023, "POST", "/api/approve",
                      {"plan_id": plan2["id"]}, cookie=cookie)
assert code == 200, (code, done2)
print("action trace tools:", [t["tool"] for t in done2["trace"]],
      "status:", done2["status"])
print("action-run next_steps:", json.dumps(done2["next_steps"]))
print("action-run default_action:", json.dumps(done2.get("default_action")))

# 3) Automate: Ember's automate suggestion first, else prefill -> propose -> REJECT
autos = [s for s in done["next_steps"] if s["kind"] == "automate"]
acmd = autos[0]["command"] if autos else done["automate_command"]
print("AUTOMATE pick:", acmd)
code, plan3, _ = call("127.0.0.1", 8023, "POST", "/api/propose",
                      {"command": acmd}, cookie=cookie)
assert code == 200, (code, plan3)
print("automate plan steps:", [s["name"] for s in plan3.get("steps", [])])
code, rej, _ = call("127.0.0.1", 8023, "POST", "/api/reject",
                    {"plan_id": plan3["id"]}, cookie=cookie)
print("automate plan rejected:", code, rej)

code, status, _ = call("127.0.0.1", 8023, "GET", "/api/status")
print("meter:", json.dumps(status["meter"]))
print("VERIFY DONE")
