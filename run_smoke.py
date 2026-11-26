#!/usr/bin/env python3
"""Phase 1 live smoke test — exactly ONE tiny Ember call, fully metered.

Refuses to run unless the meter rates are configured (a live call recorded
at $0 would defeat the budget gates). Rates come from the environment:
  EMBER_RATE_INPUT_PER_1K, EMBER_RATE_OUTPUT_PER_1K   (USD per 1K tokens)
The API key comes from MODEL_API_KEY (sourced by run_live.sh from the
Cube's /root/metatron/ember.env — never copied into this repo).

    ./run_live.sh run_smoke.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from orchestrator.ember_client import MetaEmberClient
from orchestrator.spend import SpendMeter


def main() -> int:
    rate_in = os.environ.get("EMBER_RATE_INPUT_PER_1K")
    rate_out = os.environ.get("EMBER_RATE_OUTPUT_PER_1K")
    if rate_in is None or rate_out is None:
        print("REFUSING: EMBER_RATE_INPUT_PER_1K / EMBER_RATE_OUTPUT_PER_1K "
              "not set — a live call must never be metered at $0.")
        return 2

    spend_dir = os.path.join(ROOT, "spend")
    os.makedirs(spend_dir, exist_ok=True)
    meter = SpendMeter(state_path=os.path.join(spend_dir, "state.json"),
                       rate_input_per_1k=float(rate_in),
                       rate_output_per_1k=float(rate_out))
    client = MetaEmberClient(max_tokens=16)

    meter.authorize("experiment", est_cost=0.01)
    resp = client.chat(
        [{"role": "system", "content": "You are a test."},
         {"role": "user", "content": "Reply with exactly: EMBER ONLINE"}],
        tools=[])
    entry = meter.record("experiment", resp.prompt_tokens,
                         resp.completion_tokens, note="phase1-smoke")

    print(f"model: {client.model}")
    print(f"response text: {resp.text!r}")
    print(f"prompt_tokens: {resp.prompt_tokens}")
    print(f"completion_tokens: {resp.completion_tokens}")
    print(f"raw usage: {json.dumps(client.last_usage)}")
    print(f"cost_usd: {entry['cost_usd']}")
    print(f"meter total_after_usd: {entry['total_after_usd']}")
    print(f"meter summary: {json.dumps(meter.summary())}")

    json_path = os.path.join(spend_dir, "trace.json")
    csv_path = os.path.join(spend_dir, "trace.csv")
    with open(json_path, "w") as f:
        f.write(meter.export_json())
    with open(csv_path, "w") as f:
        f.write(meter.export_csv())

    # Verify the exports by re-reading them.
    with open(json_path) as f:
        exported = json.load(f)
    assert exported["ledger"], "JSON export has no ledger entries"
    assert exported["ledger"][-1]["purpose"] == "experiment"
    assert exported["ledger"][-1]["note"] == "phase1-smoke"
    with open(csv_path) as f:
        csv_lines = f.read().splitlines()
    assert csv_lines[0].startswith("ts,purpose,prompt_tokens"), csv_lines[0]
    assert any("phase1-smoke" in line for line in csv_lines[1:])
    print(f"exports verified: {json_path} ({len(exported['ledger'])} entries), "
          f"{csv_path} ({len(csv_lines) - 1} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
