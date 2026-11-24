"""The Ember Orchestrator loop.

Pattern (inherited from the Cube's Axion agent): Ember proposes, a human
approves, deterministic flows execute. Nothing with side effects runs
before approval — run_workflow additionally demands the single-use token
that only the approval step can mint.

Every Ember call is authorized and recorded by the spend meter, so the
trace ledger is a by-product of running, not an afterthought.
"""
from __future__ import annotations

import json
import secrets

from .cube_tools import ApprovalTokens, CubeTools, load_catalog
from .ember_client import EmberClient
from .spend import SpendMeter

STEP_CAP = 8


class Orchestrator:
    def __init__(self, ember: EmberClient, tools: CubeTools,
                 meter: SpendMeter, tokens: ApprovalTokens):
        self.ember = ember
        self.tools = tools
        self.meter = meter
        self.tokens = tokens
        self.plans: dict[str, dict] = {}
        catalog = load_catalog()
        self.tool_schemas = [
            {"name": t["name"], "description": t["description"],
             "parameters": t["parameters"]} for t in catalog["tools"]]
        self.system = (
            "You are the Ember Orchestrator for the Metatron Cube. You plan "
            "work over a small set of deterministic workflows and propose "
            "every consequential action for human approval before it runs. "
            "Keep answers short and factual.")

    # ------------------------------------------------------------- propose
    def propose(self, user_text: str, purpose: str = "experiment") -> dict:
        """Ask Ember for a plan. Executes NOTHING."""
        self.meter.authorize(purpose)
        messages = [{"role": "system", "content": self.system},
                    {"role": "user", "content": user_text}]
        resp = self.ember.chat(messages, self.tool_schemas)
        self.meter.record(purpose, resp.prompt_tokens, resp.completion_tokens,
                          note="propose")
        plan_id = "plan-" + secrets.token_hex(4)
        plan = {
            "id": plan_id,
            "user_text": user_text,
            "purpose": purpose,
            "rationale": resp.text,
            "steps": [{"name": c.name, "arguments": c.arguments}
                      for c in resp.tool_calls],
            "status": "proposed",
            "messages": messages,
        }
        if len(plan["steps"]) > STEP_CAP:
            plan["steps"] = plan["steps"][:STEP_CAP]
            plan["rationale"] += f" (Plan trimmed to the {STEP_CAP}-step cap.)"
        self.plans[plan_id] = plan
        return plan

    # ------------------------------------------------------------- approve
    def approve(self, plan_id: str) -> dict:
        """Human approval: mint the token, execute the plan's tool calls in
        order, then let Ember compose the final answer from the results."""
        plan = self.plans[plan_id]
        if plan["status"] != "proposed":
            raise ValueError(f"plan {plan_id} is {plan['status']}, not proposed")
        plan["status"] = "approved"
        token = self.tokens.issue()
        trace = []
        messages = list(plan["messages"])
        messages.append({"role": "assistant", "content": plan["rationale"]})
        for step in plan["steps"]:
            args = dict(step["arguments"])
            if step["name"] == "run_workflow":
                args["approval_token"] = token
            result = self.tools.call(step["name"], args)
            trace.append({"tool": step["name"], "arguments": step["arguments"],
                          "result": result})
            messages.append({"role": "tool", "name": step["name"],
                             "content": json.dumps(result)})
        self.meter.authorize(plan["purpose"])
        resp = self.ember.chat(messages, self.tool_schemas)
        self.meter.record(plan["purpose"], resp.prompt_tokens,
                          resp.completion_tokens, note="compose-final")
        plan["status"] = "executed"
        plan["trace"] = trace
        plan["final_text"] = resp.text
        return plan

    def reject(self, plan_id: str) -> dict:
        plan = self.plans[plan_id]
        plan["status"] = "rejected"
        return plan
