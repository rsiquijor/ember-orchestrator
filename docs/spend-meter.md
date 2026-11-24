# Spend meter + trace ledger (Phase 0 spec)

**Purpose:** prove the whole system runs inside the $10 Meta Model API hard cap, and produce
the submission's required prompt/token trace as a by-product.

## Rules

- Every Ember call passes through the meter **before** it is made: log purpose tag, prompt +
  completion tokens, cost at the configured rate, running total.
- Budget gates: **warn at $7.00**, non-essential calls halted at **$9.00**, absolute stop at **$9.75**.
- Purpose tags: `experiment` / `test` / `rehearsal` / `final`.
- The rate is a **single config value**, set from Meta Model API docs at first credited access
  (per-token pricing is an open question until then — plan §11). The plan holds whatever the rate is.
- Ledger exports as **JSON + CSV** — that export IS the submission's prompt/token trace.

## Budget buckets (plan §7)

| Bucket | Allocation |
|---|---|
| Experimentation & prompt tuning | $3.00 |
| Integration testing | $2.00 |
| Demo rehearsals | $1.50 |
| Final recorded submission runs | $1.00 |
| Reserve (untouched unless needed) | $2.50 |
| **Total** | **$10.00** |

## Cost discipline

- Stub-first development: all debugging that doesn't need the real model runs against the stub
  `EmberClient` with recorded fixtures — $0.
- System prompt ≤ ~600 tokens; step cap 8 per task; payloads trimmed deterministically
  Cube-side (e.g. incidents capped at the most recent N).
- Target ≤ **$0.35 per full demo run**, measured in rehearsals. A workflow that can't hit its
  target gets simplified, not re-run endlessly.

## Meter state (to be implemented in Phase 0)

```json
{
  "rate_config": { "input_per_1k": null, "output_per_1k": null, "source": "TBD at first credited access" },
  "totals": { "spent_usd": 0.0, "by_bucket": { "experiment": 0.0, "test": 0.0, "rehearsal": 0.0, "final": 0.0 } },
  "gates": { "warn_at": 7.0, "halt_nonessential_at": 9.0, "absolute_stop_at": 9.75 },
  "ledger": []
}
```
