#!/usr/bin/env python3
"""Record the seven demo replays (+ refusal) from REAL live Ember runs.

Runs each demo once end-to-end (propose -> approve -> execute ->
summarize) with UiCubeTools (real Cube reads; workflow execution
simulated — no WhatsApp send), metered as purpose=integration with
notes ui-record-<key>, and writes full transcripts to
demos/recorded.json for the public demo page (serve.py GET /api/demos).

Run on the Cube via ./run_live.sh record_demos.py so MODEL_API_KEY is
sourced from /root/metatron/ember.env at runtime. Refuses to run
unless meter rates are set (a live call must never meter at $0).
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from orchestrator.cube_tools import ApprovalTokens
from orchestrator.ember_client import MetaEmberClient
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter
from serve import UiCubeTools


def triage_prompt() -> str:
    with open(os.path.join(ROOT, "fixtures", "tickets.json")) as f:
        tickets = json.load(f)["tickets"]
    slim = [{"id": t["id"], "subject": t["subject"], "body": t["body"]}
            for t in tickets]
    return ("Triage these help-desk tickets. Classify each one, draft a "
            "reply for each, and file them:\n" + json.dumps(slim))


DEMOS = [
    ("whatsapp", "Send the Watcher status to my WhatsApp.",
     "Send the Watcher status to my WhatsApp."),
    ("handover", "Give me the shift handover brief.",
     "Give me the shift handover brief."),
    ("triage", None,  # prompt built from fixtures
     "Triage these help-desk tickets. Classify each one, draft a reply "
     "for each, and file them."),
    ("finops", "What is the Cube costing us this month?",
     "What is the Cube costing us this month?"),
    ("cve", "Any critical CVEs we should worry about?",
     "Any critical CVEs we should worry about?"),
    ("blast", "What breaks if the brain goes down?",
     "What breaks if the brain goes down?"),
    ("capacity", "How is our capacity looking?",
     "How is our capacity looking?"),
    ("refusal", "Order a pizza for the office and book a flight to Denver.",
     "Order a pizza for the office and book a flight to Denver."),
]


def trim_result(result) -> object:
    """Cap recorded tool results so the replay file stays lean."""
    text = json.dumps(result)
    if len(text) <= 6000:
        return result
    return {"_trimmed_for_replay": True, "preview": text[:6000]}


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
    client = MetaEmberClient(timeout=120)
    print(f"model {client.model} · meter before: ${meter.spent_usd:.6f} "
          f"({len(meter.ledger)} calls)")

    recorded = []
    for key, prompt, display in DEMOS:
        if prompt is None:
            prompt = triage_prompt()
        tokens = ApprovalTokens()
        tools = UiCubeTools(tokens)
        if key == "triage":
            tools.board.clear()
        orch = Orchestrator(client, tools, meter, tokens)
        before_calls = len(meter.ledger)
        before_cost = meter.spent_usd
        note = f"ui-record-{key}"
        plan = orch.propose(prompt, purpose="integration", note_suffix=note)
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
        recorded.append(entry)
        print(f"[{key}] steps={len(entry['steps'])} "
              f"cost=${entry['cost_usd']:.6f} "
              f"final={entry['final_text'][:90]!r}")

    out = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": client.model,
        "note": ("Recorded from real live runs on the production Cube. "
                 "Workflow execution is simulated in the public demo "
                 "(no WhatsApp send); all reads are real Cube data."),
        "demos": recorded,
    }
    demos_dir = os.path.join(ROOT, "demos")
    os.makedirs(demos_dir, exist_ok=True)
    path = os.path.join(demos_dir, "recorded.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, path)
    # Refresh the submission trace exports too.
    with open(os.path.join(spend_dir, "trace.json"), "w") as f:
        f.write(meter.export_json())
    with open(os.path.join(spend_dir, "trace.csv"), "w") as f:
        f.write(meter.export_csv())
    print(f"WROTE {path} ({len(recorded)} demos)")
    print(f"meter after: ${meter.spent_usd:.6f} ({len(meter.ledger)} calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
