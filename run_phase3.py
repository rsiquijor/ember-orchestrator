#!/usr/bin/env python3
"""Phase 3 runner — four read-only report demos over the MCP Bus.

Demos:
  5. FinOps summary      "What is the Cube costing us this month?"
  6. Blast radius        "What breaks if the brain goes down?"
  7. CVE exposure        "Any critical CVEs we should worry about?"
  8. Capacity outlook    "How is our capacity looking?"

Modes:
  stub         StubEmberClient + fixture tools, $0. All demos + refusal.
  integration  Live Ember, each demo once end-to-end on real Cube tools
               (purpose=integration), plus one live refusal check.
  drills       Live Ember, 2 rehearsal runs per demo (purpose=rehearsal).

All four demos are pure reads: Ember proposes, a human approves, the
deterministic MCP Bus fetches, Ember summarizes from the returned facts.
No Cube-side model runs in this path.

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
FLAG_USD = 0.05
DEMOS = [
    ("finops", "What is the Cube costing us this month?"),
    ("blast", "What breaks if the brain goes down?"),
    ("cve", "Any critical CVEs we should worry about?"),
    ("capacity", "How is our capacity looking?"),
]
REFUSAL = "Order a pizza for the office and book a flight to Denver."


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
                  f"{json.dumps(t['result'])[:500]}")
        print(f"[{label}] EMBER FINAL: {done.get('final_text', '')[:1500]!r}")
    new = meter.ledger[before_calls:]
    tokens_in = sum(e["prompt_tokens"] for e in new)
    tokens_out = sum(e["completion_tokens"] for e in new)
    cost = round(meter.spent_usd - before_cost, 6)
    verdict = "OK" if cost <= TARGET_USD else "OVER TARGET"
    flag = "  <-- FLAG >$0.05" if cost > FLAG_USD else ""
    print(f"[{label}] COST: ${cost:.5f} (tokens in {tokens_in}, out "
          f"{tokens_out}, {len(new)} calls) vs ${TARGET_USD} target: "
          f"{verdict}{flag}")
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
    for key, prompt in DEMOS:
        run_one(orch, prompt, "test", f"stub-{key}", f"stub {key}")
    run_one(orch, REFUSAL, "test", "stub-refusal", "stub refusal")
    print("SPEND:", meter.summary())
    return 0


def live_client() -> MetaEmberClient:
    return MetaEmberClient(timeout=120)


def integration_mode() -> int:
    meter = make_meter(live=True)
    client = live_client()
    results = []
    for key, prompt in DEMOS:
        orch = build_orch(client, HttpCubeTools(ApprovalTokens()), meter)
        results.append(run_one(orch, prompt, "integration",
                               f"{key}-integration",
                               f"integration {key}"))
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
    for key, prompt in DEMOS:
        for i in (1, 2):
            orch = build_orch(client, HttpCubeTools(ApprovalTokens()), meter)
            results.append(run_one(orch, prompt, "rehearsal",
                                   f"{key}-drill{i}",
                                   f"drill {key} run{i}"))
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
