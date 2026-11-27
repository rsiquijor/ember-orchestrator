#!/usr/bin/env python3
"""Record the four Phase 4 demo replays from REAL live Ember runs.

Runs each new demo once end-to-end (propose -> approve -> execute ->
summarize) with UiCubeTools (real Cube reads + real execution),
metered as purpose=integration with notes ui-record-<key>, and
merges the transcripts into demos/recorded.json (existing entries
kept; same-key entries replaced).

Demos:
  agent-builder  "Build a new automated agentic workflow..." -> create_agent_flow ->
                 real DISABLED RPA draft, confirmed via the AP API,
                 then DELETED after capture (proof, not clutter).
  intake         fixtures/sample-invoice.png -> Ember reads the
                 image itself -> file_intake.
  fullpicture    one prompt -> 5 chained read tools -> one brief.
  document       fixtures/sample-server.log (5 planted anomalies)
                 -> file_document_report; findings checked against
                 fixtures/sample-server.groundtruth.json.

Run on the Cube via ./run_live.sh record_phase4.py so MODEL_API_KEY
is sourced from /root/metatron/ember.env at runtime. Refuses to run
unless meter rates are set (a live call must never meter at $0).
"""
import base64
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from orchestrator.cube_tools import ApprovalTokens
from orchestrator.ember_client import MetaEmberClient
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter
from record_demos import trim_result
from serve import UiCubeTools

AP_BASE = os.environ.get("AP_API_BASE", "http://127.0.0.1:8085/api/v1")


def ap_call(method, path, body=None):
    tok_path = os.environ.get("AP_TOKEN_PATH",
                              "/root/metatron/.ap-admin-token")
    with open(tok_path) as f:
        token = f.read().strip()
    req = urllib.request.Request(
        AP_BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json",
                 "User-Agent": "ember-orchestrator/0.4"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else {})


def delete_flow(flow_id: str) -> int:
    status, _ = ap_call("DELETE", "/flows/" + flow_id, {})
    return status


def run_demo(orch, meter, key, propose_fn, display):
    before_calls = len(meter.ledger)
    before_cost = meter.spent_usd
    note = f"ui-record-{key}"
    plan = propose_fn(note)
    done = plan
    if plan["steps"]:
        done = orch.approve(plan["id"], note_suffix=note)
    new = meter.ledger[before_calls:]
    entry = {
        "key": key,
        "command": display,
        "rationale": plan["rationale"],
        "steps": [{"name": s["name"], "arguments": s["arguments"]}
                  for s in plan["steps"]],
        "trace": [{"tool": t["tool"], "arguments": t["arguments"],
                   "result": trim_result(t["result"])}
                  for t in done.get("trace", [])],
        "final_text": done.get("final_text") or plan["rationale"],
        "refusal": len(plan["steps"]) == 0,
        "tokens_in": sum(e["prompt_tokens"] for e in new),
        "tokens_out": sum(e["completion_tokens"] for e in new),
        "calls": len(new),
        "cost_usd": round(meter.spent_usd - before_cost, 6),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(f"[{key}] steps={len(entry['steps'])} "
          f"cost=${entry['cost_usd']:.6f} "
          f"final={entry['final_text'][:110]!r}")
    return entry, done


def main() -> int:
    rate_in = os.environ.get("EMBER_RATE_INPUT_PER_1K")
    rate_out = os.environ.get("EMBER_RATE_OUTPUT_PER_1K")
    if rate_in is None or rate_out is None:
        print("REFUSING: EMBER_RATE_INPUT_PER_1K / EMBER_RATE_OUTPUT_PER_1K "
              "not set.")
        return 2
    spend_dir = os.path.join(ROOT, "spend")
    os.makedirs(spend_dir, exist_ok=True)
    meter = SpendMeter(state_path=os.path.join(spend_dir, "state.json"),
                       rate_input_per_1k=float(rate_in),
                       rate_output_per_1k=float(rate_out))
    client = MetaEmberClient(timeout=180)
    print(f"model {client.model} · meter before: ${meter.spent_usd:.6f} "
          f"({len(meter.ledger)} calls)")

    tokens = ApprovalTokens()
    tools = UiCubeTools(tokens)
    orch = Orchestrator(client, tools, meter, tokens)
    recorded = []

    # ── 1. Agent Builder ─────────────────────────────────────────────
    cmd = "Build a new automated agentic workflow."
    entry, done = run_demo(
        orch, meter, "agent-builder",
        lambda note: orch.propose(cmd, purpose="integration",
                                  note_suffix=note), cmd)
    recorded.append(entry)
    flow_id = None
    for t in done.get("trace", []):
        if t["tool"] == "create_agent_flow":
            flow_id = (t["result"] or {}).get("flow_id")
    if flow_id:
        status, flow = ap_call("GET", "/flows/" + flow_id)
        print(f"[agent-builder] AP confirm: flow {flow_id} "
              f"status={flow.get('status')} (HTTP {status})")
        dstatus = delete_flow(flow_id)
        print(f"[agent-builder] test flow deleted (HTTP {dstatus})")
    else:
        print("[agent-builder] WARNING: no flow_id in trace")

    # ── 2. Multimodal Intake ─────────────────────────────────────────
    with open(os.path.join(ROOT, "fixtures", "sample-invoice.png"),
              "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()
    instruction = (
        "Read the attached image yourself and extract its fields. "
        "Call file_intake once with kind and the fields you can see "
        "(for an invoice: vendor, invoice_number, date, po_number, "
        "bill_to, line_items, subtotal, total_due). Report only what "
        "is visible: include amount_paid and balance_due only when "
        "explicitly printed (omit blank or absent fields; never copy "
        "another field's value). Notes are for factual observations "
        "only — invoice totals are verified deterministically by "
        "the filing tool. A human approves before anything is "
        "filed.")
    content = [
        {"type": "text", "text": instruction},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + img_b64}},
    ]
    entry, done = run_demo(
        orch, meter, "intake",
        lambda note: orch.propose_content(
            content,
            "Image attached (sample-invoice.png) — read it and file "
            "the extracted fields.",
            purpose="integration", note_suffix=note),
        "Attach an invoice photo — Ember reads it and files the "
        "extracted fields.")
    recorded.append(entry)
    for t in done.get("trace", []):
        if t["tool"] == "file_intake":
            print("[intake] extracted fields:",
                  json.dumps((t["result"] or {}).get("fields"),
                             indent=1)[:1200])

    # ── 3. Full Picture Brief ────────────────────────────────────────
    cmd = ("Give me the full picture on the Cube: overall status, "
           "what breaks if the brain goes down, CVE exposure, "
           "failure predictions, and this month's costs.")
    entry, done = run_demo(
        orch, meter, "fullpicture",
        lambda note: orch.propose(cmd, purpose="integration",
                                  note_suffix=note), cmd)
    recorded.append(entry)
    print(f"[fullpicture] chained tools: "
          f"{[s['name'] for s in entry['steps']]}")

    # ── 4. Document Intelligence ─────────────────────────────────────
    with open(os.path.join(ROOT, "fixtures", "sample-server.log")) as f:
        log_text = f.read()
    numbered = "\n".join(f"{i}: {line}" for i, line in
                         enumerate(log_text.splitlines(), start=1))
    instruction = (
        "Analyze the attached document. Lines are numbered. Call "
        "file_document_report once with a title, a short summary, "
        "and findings for anomalies or problems you find (line = "
        "the number shown, severity, detail). Cite only lines that "
        "exist. A human approves before anything is filed.\n\n"
        + numbered)
    entry, done = run_demo(
        orch, meter, "document",
        lambda note: orch.propose_content(
            instruction,
            "Document attached (sample-server.log) — analyze it and "
            "file a findings report.",
            purpose="integration", note_suffix=note),
        "Paste a log file — Ember finds the anomalies and files a "
        "report.")
    recorded.append(entry)
    with open(os.path.join(ROOT, "fixtures",
                           "sample-server.groundtruth.json")) as f:
        gt = json.load(f)["anomalies"]
    findings = []
    for t in done.get("trace", []):
        if t["tool"] == "file_document_report":
            rep = (t["arguments"] or {}).get("report") or {}
            findings = rep.get("findings") or []
    found_lines = {f.get("line") for f in findings}
    hits = [a for a in gt if a["line"] in found_lines]
    print(f"[document] planted anomalies found: {len(hits)}/{len(gt)} "
          f"(lines {sorted(found_lines)})")
    for a in gt:
        mark = "HIT " if a["line"] in found_lines else "MISS"
        print(f"  {mark} line {a['line']}: {a['kind']}")

    # ── Merge into recorded.json ─────────────────────────────────────
    demos_dir = os.path.join(ROOT, "demos")
    os.makedirs(demos_dir, exist_ok=True)
    path = os.path.join(demos_dir, "recorded.json")
    try:
        with open(path) as f:
            out = json.load(f)
    except (OSError, ValueError):
        out = {"demos": []}
    by_key = {d["key"]: d for d in out.get("demos", [])}
    order = [d["key"] for d in out.get("demos", [])]
    for e in recorded:
        if e["key"] not in by_key:
            order.append(e["key"])
        by_key[e["key"]] = e
    out["demos"] = [by_key[k] for k in order]
    out["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out["model"] = client.model
    out["note"] = ("Recorded from real live runs on the production "
                   "Cube. Live runs execute for real (the WhatsApp "
                   "demo sends); Agent Builder recordings created a "
                   "real DISABLED draft that was deleted after "
                   "capture. All reads are real Cube data.")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, path)
    with open(os.path.join(spend_dir, "trace.json"), "w") as f:
        f.write(meter.export_json())
    with open(os.path.join(spend_dir, "trace.csv"), "w") as f:
        f.write(meter.export_csv())
    print(f"WROTE {path} ({len(out['demos'])} demos)")
    print(f"meter after: ${meter.spent_usd:.6f} ({len(meter.ledger)} calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
