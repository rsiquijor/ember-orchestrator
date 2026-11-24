#!/usr/bin/env python3
"""Scripted Phase 0 demo — stub Ember + fixture Cube tools, $0 spend.

Runs two of the three hackathon demos end to end:
  1. Watcher status -> WhatsApp (propose, approve, execute, summarize)
  2. Shift handover brief
and prints the spend summary. Nothing leaves the machine.

    python3 run_demo.py
"""
from orchestrator.cube_tools import ApprovalTokens, FixtureCubeTools
from orchestrator.ember_client import StubEmberClient
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import SpendMeter


def run(orch: Orchestrator, prompt: str) -> None:
    print("=" * 72)
    print("USER:", prompt)
    plan = orch.propose(prompt, purpose="rehearsal")
    print("EMBER PLAN:", plan["rationale"])
    for s in plan["steps"]:
        print("  step:", s["name"], s["arguments"])
    print("HUMAN: approve")
    done = orch.approve(plan["id"])
    for t in done["trace"]:
        print("  trace:", t["tool"], "->", t["result"])
    print("EMBER:", done["final_text"])


def main() -> None:
    meter = SpendMeter()
    tokens = ApprovalTokens()
    orch = Orchestrator(StubEmberClient(), FixtureCubeTools(tokens), meter, tokens)
    run(orch, "Send the Watcher status to WhatsApp")
    run(orch, "Draft the shift handover brief")
    print("=" * 72)
    print("SPEND:", meter.summary())
    print("Ledger entries:", len(meter.ledger), "(all $0 — stub client, rate unset)")


if __name__ == "__main__":
    main()
