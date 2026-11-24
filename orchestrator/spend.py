"""Spend meter + trace ledger for the Ember Orchestrator.

Every Ember call passes through the meter BEFORE it is made and is recorded
after it returns. The ledger export (JSON + CSV) doubles as the hackathon
submission's required prompt/token trace.

Budget gates (hard cap $10 of Meta Model API credit):
  - warn at $7.00
  - non-essential purposes halt at $9.00  (experiment / test / rehearsal)
  - absolute stop at $9.75                (every purpose, including final)

The per-token rate is a single config value, set from Meta Model API docs at
first credited access (Phase 1). Until then it is None and recorded costs are
$0 — development runs against the stub client cost nothing anyway.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time

WARN_AT = 7.00
HALT_NONESSENTIAL_AT = 9.00
ABSOLUTE_STOP_AT = 9.75
ESSENTIAL_PURPOSES = {"final"}  # everything else is non-essential
PURPOSES = {"experiment", "test", "rehearsal", "final"}


class BudgetHalt(Exception):
    """Non-essential spend is halted at $9.00."""


class BudgetStop(Exception):
    """Absolute stop at $9.75 — no further Ember calls, any purpose."""


class SpendMeter:
    def __init__(self, state_path: str | None = None,
                 rate_input_per_1k: float | None = None,
                 rate_output_per_1k: float | None = None):
        self.state_path = state_path
        self.rate_input_per_1k = rate_input_per_1k
        self.rate_output_per_1k = rate_output_per_1k
        self.ledger: list[dict] = []
        self.spent_usd = 0.0
        self.by_bucket: dict[str, float] = {p: 0.0 for p in PURPOSES}
        if state_path and os.path.exists(state_path):
            self._load()

    # ------------------------------------------------------------- costing
    def cost_of(self, prompt_tokens: int, completion_tokens: int) -> float:
        if self.rate_input_per_1k is None or self.rate_output_per_1k is None:
            return 0.0
        return (prompt_tokens / 1000 * self.rate_input_per_1k
                + completion_tokens / 1000 * self.rate_output_per_1k)

    # ------------------------------------------------------------- gates
    def authorize(self, purpose: str, est_cost: float = 0.0) -> None:
        """Call BEFORE every Ember request. Raises on a closed gate."""
        if purpose not in PURPOSES:
            raise ValueError(f"unknown purpose tag: {purpose!r}")
        projected = self.spent_usd + est_cost
        if projected >= ABSOLUTE_STOP_AT:
            raise BudgetStop(
                f"absolute stop: projected ${projected:.2f} >= ${ABSOLUTE_STOP_AT:.2f}")
        if projected >= HALT_NONESSENTIAL_AT and purpose not in ESSENTIAL_PURPOSES:
            raise BudgetHalt(
                f"non-essential halt: projected ${projected:.2f} >= "
                f"${HALT_NONESSENTIAL_AT:.2f} (purpose={purpose})")

    @property
    def warning(self) -> bool:
        return self.spent_usd >= WARN_AT

    # ------------------------------------------------------------- ledger
    def record(self, purpose: str, prompt_tokens: int, completion_tokens: int,
               note: str = "") -> dict:
        cost = self.cost_of(prompt_tokens, completion_tokens)
        self.spent_usd = round(self.spent_usd + cost, 6)
        self.by_bucket[purpose] = round(self.by_bucket.get(purpose, 0.0) + cost, 6)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "purpose": purpose,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": round(cost, 6),
            "total_after_usd": self.spent_usd,
            "note": note,
        }
        self.ledger.append(entry)
        self._save()
        return entry

    # ------------------------------------------------------------- export
    def export_json(self) -> str:
        return json.dumps({
            "rate_config": {"input_per_1k": self.rate_input_per_1k,
                            "output_per_1k": self.rate_output_per_1k,
                            "source": "TBD at first credited access" if self.rate_input_per_1k is None else "configured"},
            "totals": {"spent_usd": self.spent_usd, "by_bucket": self.by_bucket},
            "gates": {"warn_at": WARN_AT, "halt_nonessential_at": HALT_NONESSENTIAL_AT,
                      "absolute_stop_at": ABSOLUTE_STOP_AT},
            "ledger": self.ledger,
        }, indent=1)

    def export_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts", "purpose", "prompt_tokens", "completion_tokens",
                    "cost_usd", "total_after_usd", "note"])
        for e in self.ledger:
            w.writerow([e["ts"], e["purpose"], e["prompt_tokens"],
                        e["completion_tokens"], e["cost_usd"],
                        e["total_after_usd"], e["note"]])
        return buf.getvalue()

    def summary(self) -> dict:
        return {"spent_usd": self.spent_usd, "by_bucket": dict(self.by_bucket),
                "calls": len(self.ledger), "warning": self.warning,
                "rate_configured": self.rate_input_per_1k is not None}

    # ------------------------------------------------------------- state
    def _save(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(self.export_json())
        os.replace(tmp, self.state_path)

    def _load(self) -> None:
        with open(self.state_path) as f:
            state = json.load(f)
        self.ledger = state.get("ledger", [])
        totals = state.get("totals", {})
        self.spent_usd = totals.get("spent_usd", 0.0)
        self.by_bucket.update(totals.get("by_bucket", {}))
