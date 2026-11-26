#!/usr/bin/env python3
"""Phase 2 runner — three demos end-to-end, refusal proof, budget drills.

Modes:
  stub         StubEmberClient + fixture tools, $0. All demos + refusal.
  integration  Live Ember (MetaEmberClient). Demo 1 with fixture execution
               (no real WhatsApp send), demos 2-3 + refusal on real Cube
               tools. Purpose tag: integration.
  drills       Live Ember, 3 runs per demo, purpose tag: rehearsal.
               Demo 1 run 1 is the ONE real WhatsApp send (flag-guarded in
               spend/real-send-done.flag); runs 2-3 use fixture execution
               and are labelled as such in the trace notes.

Live modes refuse to run unless EMBER_RATE_INPUT_PER_1K /
EMBER_RATE_OUTPUT_PER_1K are set (a live call must never meter at $0).
Run live modes via ./run_live.sh so MODEL_API_KEY is sourced from the
Cube's /root/metatron/ember.env at runtime.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from orchestrator.cube_tools import (ApprovalTokens, FixtureCubeTools,
                                     HttpCubeTools)
from orchestrator.ember_client import MetaEmberClient, StubEmberClient
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter

TARGET_USD = 0.35
DEMO1 = "Send the Watcher status to my WhatsApp."
DEMO3 = "Give me the shift handover brief."
REFUSAL = "Order a pizza for the office and book a flight to Denver."


def demo2_prompt() -> str:
    with open(os.path.join(ROOT, "fixtures", "tickets.json")) as f:
        tickets = json.load(f)["tickets"]
    slim = [{"id": t["id"], "subject": t["subject"], "body": t["body"]}
            for t in tickets]
    return ("Triage these help-desk tickets. Classify each one, draft a "
            "reply for each, and file them:\n" + json.dumps(slim))


def make_meter(live: bool) -> SpendMeter:
    spend_dir = os.path.join(ROOT, "spend")
    os.makedirs(spend_dir, exist_ok=True)
    if live:
        rate_in = os.environ.get("EMBER_RATE_INPUT_PER_1K")
        rate_out = os.environ.get("EMBER_RATE_OUTPUT_PER_1K")
        if rate_in is None or rate_out is None:
            print("REFUSING: EMBER_RATE_INPUT_PER_1K / "
                  "EMBER_RATE_OUTPUT_PER_1K not set.")
            raise SystemExit(2)
        return SpendMeter(state_path=os.path.join(spend_dir, "state.json"),
                          rate_input_per_1k=float(rate_in),
                          rate_output_per_1k=float(rate_out))
    return SpendMeter(state_path=os.path.join(spend_dir, "state.json"))


def export_trace(meter: SpendMeter) -> None:
    spend_dir = os.path.join(ROOT, "spend")
    with open(os.path.join(spend_dir, "trace.json"), "w") as f:
        f.write(meter.export_json())
    with open(os.path.join(spend_dir, "trace.csv"), "w") as f:
        f.write(meter.export_csv())


def run_one(orch: Orchestrator, prompt: str, purpose: str, note: str,
            label: str) -> dict:
    """One full propose -> approve -> execute -> compose cycle."""
    meter = orch.meter
    before_cost = meter.spent_usd
    before_calls = len(meter.ledger)
    print("=" * 72)
    print(f"[{label}] USER: {prompt[:160]}{'...' if len(prompt) > 160 else ''}")
    plan = orch.propose(prompt, purpose=purpose, note_suffix=note)
    print(f"[{label}] EMBER PLAN: {plan['rationale'][:600]!r}")
    for s in plan["steps"]:
        print(f"[{label}]   step: {s['name']}({json.dumps(s['arguments'])[:300]})")
    if not plan["steps"]:
        print(f"[{label}] (no steps proposed — nothing to approve)")
        done = plan
    else:
        print(f"[{label}] HUMAN: approve")
        done = orch.approve(plan["id"], note_suffix=note)
        for t in done.get("trace", []):
            print(f"[{label}]   trace: {t['tool']} -> "
                  f"{json.dumps(t['result'])[:400]}")
        print(f"[{label}] EMBER FINAL: {done.get('final_text', '')[:1200]!r}")
    new = meter.ledger[before_calls:]
    tokens_in = sum(e["prompt_tokens"] for e in new)
    tokens_out = sum(e["completion_tokens"] for e in new)
    cost = round(meter.spent_usd - before_cost, 6)
    verdict = "OK" if cost <= TARGET_USD else "OVER TARGET"
    print(f"[{label}] COST: ${cost:.5f} (tokens in {tokens_in}, out "
          f"{tokens_out}, {len(new)} calls) vs ${TARGET_USD} target: {verdict}")
    print(f"[{label}] METER TOTAL: ${meter.spent_usd:.5f}")
    return {"label": label, "cost": cost, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "plan": done}


def build_orch(client, tools, meter):
    tokens = ApprovalTokens()
    tools.tokens = tokens
    return Orchestrator(client, tools, meter, tokens)


def stub_mode() -> int:
    meter = make_meter(live=False)
    tokens = ApprovalTokens()
    orch = Orchestrator(StubEmberClient(), FixtureCubeTools(tokens),
                        meter, tokens)
    run_one(orch, DEMO1, "test", "stub-demo1", "stub demo1 whatsapp")
    run_one(orch, demo2_prompt(), "test", "stub-demo2", "stub demo2 triage")
    print("BOARD:", json.dumps(orch.tools.board.list(), indent=1)[:1500])
    run_one(orch, DEMO3, "test", "stub-demo3", "stub demo3 handover")
    run_one(orch, REFUSAL, "test", "stub-refusal", "stub refusal")
    print("SPEND:", meter.summary())
    return 0


def live_client() -> MetaEmberClient:
    return MetaEmberClient(timeout=120)


def integration_mode() -> int:
    meter = make_meter(live=True)
    client = live_client()
    results = []
    # Demo 1: fixture execution — validates live tool-calling, sends nothing.
    orch = build_orch(client, FixtureCubeTools(ApprovalTokens()), meter)
    results.append(run_one(orch, DEMO1, "integration",
                           "demo1-integration-fixture-exec",
                           "integration demo1 (fixture exec)"))
    # Demo 2: real board store.
    orch = build_orch(client, HttpCubeTools(ApprovalTokens()), meter)
    orch.tools.board.clear()
    results.append(run_one(orch, demo2_prompt(), "integration",
                           "demo2-integration", "integration demo2 triage"))
    print("BOARD:", json.dumps(orch.tools.board.list(), indent=1)[:2500])
    # Demo 3: real AIOps data.
    orch = build_orch(client, HttpCubeTools(ApprovalTokens()), meter)
    results.append(run_one(orch, DEMO3, "integration",
                           "demo3-integration", "integration demo3 handover"))
    # Refusal: no workflow matches.
    orch = build_orch(client, HttpCubeTools(ApprovalTokens()), meter)
    results.append(run_one(orch, REFUSAL, "integration",
                           "refusal-integration", "integration refusal"))
    export_trace(meter)
    print("=" * 72)
    print("INTEGRATION SUMMARY:", json.dumps(
        [{k: r[k] for k in ("label", "cost", "tokens_in", "tokens_out")}
         for r in results], indent=1))
    print("METER:", meter.summary())
    return 0


def drills_mode() -> int:
    meter = make_meter(live=True)
    client = live_client()
    results = []
    flag = os.path.join(ROOT, "spend", "real-send-done.flag")

    # Demo 1: run 1 real send (once, flag-guarded), runs 2-3 fixture exec.
    for i in (1, 2, 3):
        real = (i == 1 and not os.path.exists(flag))
        tools = (HttpCubeTools(ApprovalTokens()) if real
                 else FixtureCubeTools(ApprovalTokens()))
        note = f"demo1-drill{i}-{'real-send' if real else 'fixture-exec'}"
        orch = build_orch(client, tools, meter)
        r = run_one(orch, DEMO1, "rehearsal", note,
                    f"drill demo1 run{i} ({'REAL SEND' if real else 'fixture exec'})")
        results.append(r)
        if real:
            ok = any(t["tool"] == "run_workflow"
                     and t["result"].get("status") == "SUCCEEDED"
                     for t in r["plan"].get("trace", []))
            if ok:
                with open(flag, "w") as f:
                    f.write("real WhatsApp send completed; drills use "
                            "fixture execution from now on\n")
                print("REAL SEND FLAG WRITTEN:", flag)
            else:
                print("WARNING: real send did not report SUCCEEDED; "
                      "flag NOT written")

    # Demo 2: three triage runs on the real board store (cleared each run).
    for i in (1, 2, 3):
        orch = build_orch(client, HttpCubeTools(ApprovalTokens()), meter)
        orch.tools.board.clear()
        r = run_one(orch, demo2_prompt(), "rehearsal", f"demo2-drill{i}",
                    f"drill demo2 run{i}")
        results.append(r)
        print(f"BOARD after demo2 run{i}:",
              json.dumps(orch.tools.board.list(), indent=1)[:2500])

    # Demo 3: three handover runs on real AIOps data; briefs saved to runs/.
    runs_dir = os.path.join(ROOT, "runs")
    os.makedirs(runs_dir, exist_ok=True)
    for i in (1, 2, 3):
        orch = build_orch(client, HttpCubeTools(ApprovalTokens()), meter)
        r = run_one(orch, DEMO3, "rehearsal", f"demo3-drill{i}",
                    f"drill demo3 run{i}")
        results.append(r)
        brief_path = os.path.join(runs_dir, f"handover-drill{i}.md")
        with open(brief_path, "w") as f:
            f.write(r["plan"].get("final_text", "") + "\n")
        print(f"BRIEF SAVED: {brief_path}")

    export_trace(meter)
    print("=" * 72)
    print("DRILL SUMMARY:")
    for r in results:
        print(f"  {r['label']}: ${r['cost']:.5f} "
              f"(in {r['tokens_in']}, out {r['tokens_out']})")
    print("METER:", meter.summary())
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stub"
    if mode == "stub":
        return stub_mode()
    if mode == "integration":
        return integration_mode()
    if mode == "drills":
        return drills_mode()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
