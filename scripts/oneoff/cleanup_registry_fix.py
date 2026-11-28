#!/usr/bin/env python3
"""Inspect registry structure, remove test flow entry from nested flows."""
import json
import os

TEST_FLOW = "ttNrwbm5AmEFaCaK8w1u6"
ROM_FLOW = "G0eS7Pl1gEzko858Lv7PJ"
REGISTRY = "/opt/cube/ember-orchestrator/board/agent_flows.json"

with open(REGISTRY) as f:
    reg = json.load(f)

print(f"Top-level type: {type(reg).__name__}")
if isinstance(reg, dict):
    print(f"Top-level keys: {list(reg.keys())}")
    flows = reg.get("flows")
    print(f"flows type: {type(flows).__name__}")
    if isinstance(flows, list):
        print(f"flows count before: {len(flows)}")
        for e in flows:
            print(f"  entry keys: {sorted(e.keys()) if isinstance(e, dict) else type(e)} "
                  f"-> flow_id={e.get('flow_id') if isinstance(e, dict) else None} "
                  f"id={e.get('id') if isinstance(e, dict) else None} "
                  f"name={e.get('name') if isinstance(e, dict) else None}")
        before = len(flows)
        flows = [e for e in flows
                 if not (isinstance(e, dict)
                         and (e.get("flow_id") or e.get("id")) == TEST_FLOW)]
        reg["flows"] = flows
        print(f"Removed: {before - len(flows)}; count after: {len(flows)}")
    elif isinstance(flows, dict):
        print(f"flows keys before: {list(flows.keys())}")
        removed = flows.pop(TEST_FLOW, None)
        print(f"Removed entry: {removed is not None}")
        reg["flows"] = flows
    else:
        raise SystemExit("Unexpected flows shape")
else:
    raise SystemExit("Unexpected registry shape")

tmp = REGISTRY + ".tmp"
with open(tmp, "w") as f:
    json.dump(reg, f, indent=2)
os.replace(tmp, REGISTRY)

# Re-read and report final state.
with open(REGISTRY) as f:
    final = json.load(f)
flows = final.get("flows") if isinstance(final, dict) else final
if isinstance(flows, list):
    summary = [(e.get("flow_id") or e.get("id"), e.get("name"),
                e.get("enabled"), e.get("status"))
               for e in flows if isinstance(e, dict)]
else:
    summary = list(flows.keys())
print(f"FINAL registry entries: {summary}")
print(f"Only Rom draft remains: {summary == [(ROM_FLOW, summary[0][1], summary[0][2], summary[0][3])] if len(summary) == 1 and isinstance(summary[0], tuple) and summary[0][0] == ROM_FLOW else (len(summary) == 1)}")
