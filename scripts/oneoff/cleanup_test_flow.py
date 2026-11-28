#!/usr/bin/env python3
"""Cleanup: delete test RPA flow ttNrwbm5AmEFaCaK8w1u6 and its registry entry."""
import glob
import json
import os
import shutil
import urllib.request

TEST_FLOW = "ttNrwbm5AmEFaCaK8w1u6"
ROM_FLOW = "G0eS7Pl1gEzko858Lv7PJ"
REGISTRY = "/opt/cube/ember-orchestrator/board/agent_flows.json"
AP_BASE = "http://127.0.0.1:8085/api/v1"

# Token read at runtime via glob (never printed).
token_path = glob.glob("/root/metatron/.ap-*")[0]
with open(token_path) as f:
    token = f.read().strip()


def ap(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        AP_BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, raw


# 1. Delete the test flow (expect 204), verify GET -> 404.
status, _ = ap("DELETE", f"/flows/{TEST_FLOW}", {})
print(f"DELETE {TEST_FLOW} -> HTTP {status}")
status, _ = ap("GET", f"/flows/{TEST_FLOW}")
print(f"GET {TEST_FLOW} after delete -> HTTP {status} (expect 404)")

# 2. Remove its registry entry (backup first).
backup = REGISTRY + ".bak-nov26-cleanup"
shutil.copy2(REGISTRY, backup)
print(f"Registry backed up to {backup}")
with open(REGISTRY) as f:
    reg = json.load(f)

# Registry shape: list of entries, or dict keyed by flow id.
if isinstance(reg, list):
    before = len(reg)
    reg = [e for e in reg
           if (e.get("flow_id") or e.get("id")) != TEST_FLOW]
    removed = before - len(reg)
elif isinstance(reg, dict):
    removed = 1 if reg.pop(TEST_FLOW, None) is not None else 0
else:
    raise SystemExit(f"Unexpected registry shape: {type(reg)}")
print(f"Registry entries removed: {removed}")

tmp = REGISTRY + ".tmp"
with open(tmp, "w") as f:
    json.dump(reg, f, indent=2)
os.replace(tmp, REGISTRY)

# 3. Confirm registry holds ONLY Rom's draft, still DISABLED in AP.
ids = []
if isinstance(reg, list):
    ids = [(e.get("flow_id") or e.get("id")) for e in reg]
else:
    ids = list(reg.keys())
print(f"Registry flow ids after cleanup: {ids}")

status, flow = ap("GET", f"/flows/{ROM_FLOW}")
if status == 200 and isinstance(flow, dict):
    version = flow.get("version") or {}
    print(f"Rom draft {ROM_FLOW}: HTTP {status}, "
          f"status={flow.get('status')}, "
          f"displayName={version.get('displayName')}, "
          f"publishedVersionId={flow.get('publishedVersionId')}")
else:
    print(f"Rom draft {ROM_FLOW}: HTTP {status} (UNEXPECTED)")

# 4. Ember service health: /api/status via loopback.
try:
    with urllib.request.urlopen("http://127.0.0.1:8023/api/status",
                                timeout=10) as r:
        body = json.loads(r.read().decode())
        print(f"Ember /api/status -> HTTP {r.status}, "
              f"model={body.get('model')}, mode={body.get('mode')}")
except Exception as e:
    print(f"Ember /api/status FAILED: {e}")
