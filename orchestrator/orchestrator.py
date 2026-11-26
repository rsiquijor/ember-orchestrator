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

from .cube_tools import GATED_TOOLS, ApprovalTokens, CubeTools, load_catalog
from .ember_client import EmberClient
from .spend import SpendMeter

STEP_CAP = 8

SYSTEM_PROMPT = (
    "You are the Ember Orchestrator for the Metatron Cube. You plan work "
    "over a small catalog of deterministic workflows. You never execute "
    "anything yourself: you propose a plan, a human approves it, and only "
    "then do tools run. Never claim an action has happened before approval.\n"
    "Express your plan as tool calls, never as prose describing tool calls. "
    "Calling a tool in your plan does not execute it — a human approves the "
    "plan first, and only then do the calls run.\n"
    "Rules:\n"
    "- Use the provided tools. For Watcher status, call get_watcher_status. "
    "To send the status to WhatsApp, call get_watcher_status and "
    "run_workflow with workflow_id 'watcher-status-whatsapp' in the same "
    "turn.\n"
    "- For help-desk triage, classify every ticket (category: Network, "
    "Billing, Access, Hardware, Account, Software, or Other; priority P1 "
    "highest to P4 lowest), draft a short reply for each, and call "
    "file_tickets once with all tickets. A human approves before anything "
    "is filed or sent.\n"
    "- For a shift handover brief, call get_watcher_status and write the "
    "brief only from the returned facts.\n"
    "- If a request matches no tool or workflow, say so plainly, name what "
    "you can do (Watcher status, WhatsApp status send, ticket triage, "
    "handover brief), and do not invent capabilities, data, or results.\n"
    "- Ground every statement in tool results. If data is missing, say it "
    "is missing.\n"
    "- Keep answers short and factual.")


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
        self.system = SYSTEM_PROMPT

    # ------------------------------------------------------------- propose
    def propose(self, user_text: str, purpose: str = "experiment",
                note_suffix: str = "") -> dict:
        """Ask Ember for a plan. Executes NOTHING."""
        self.meter.authorize(purpose)
        messages = [{"role": "system", "content": self.system},
                    {"role": "user", "content": user_text}]
        # tool_choice "required" is not supported by the API (only "auto"),
        # so if Ember narrates a plan instead of calling tools, nudge once.
        resp = self.ember.chat(messages, self.tool_schemas)
        self.meter.record(purpose, resp.prompt_tokens, resp.completion_tokens,
                          note="propose" + (f":{note_suffix}" if note_suffix else ""))
        if not resp.tool_calls:
            self.meter.authorize(purpose)
            retry = messages + [
                {"role": "assistant", "content": resp.text},
                {"role": "user", "content":
                 "Express that plan as tool calls now. Do not describe the "
                 "calls — make them. If no tool fits the request, reply "
                 "with plain text saying so and make no calls."}]
            resp = self.ember.chat(retry, self.tool_schemas)
            self.meter.record(purpose, resp.prompt_tokens,
                              resp.completion_tokens,
                              note="propose-retry"
                              + (f":{note_suffix}" if note_suffix else ""))
        plan_id = "plan-" + secrets.token_hex(4)
        plan = {
            "id": plan_id,
            "user_text": user_text,
            "purpose": purpose,
            "rationale": resp.text,
            "steps": [{"name": c.name, "arguments": c.arguments, "id": c.id}
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
    def approve(self, plan_id: str, note_suffix: str = "") -> dict:
        """Human approval: mint a fresh single-use token per gated step,
        execute the plan's tool calls in order, then let Ember compose the
        final answer from the results."""
        plan = self.plans[plan_id]
        if plan["status"] != "proposed":
            raise ValueError(f"plan {plan_id} is {plan['status']}, not proposed")
        plan["status"] = "approved"
        trace = []
        messages = list(plan["messages"])
        # Replay the assistant turn WITH its tool_calls: the API rejects
        # tool results that have no matching assistant tool_calls entry.
        assistant: dict = {"role": "assistant", "content": plan["rationale"]}
        if plan["steps"]:
            assistant["tool_calls"] = [
                {"id": s.get("id") or f"call_{i}_{s['name']}",
                 "type": "function",
                 "function": {"name": s["name"],
                              "arguments": json.dumps(s["arguments"])}}
                for i, s in enumerate(plan["steps"])]
        messages.append(assistant)
        for i, step in enumerate(plan["steps"]):
            args = dict(step["arguments"])
            if step["name"] in GATED_TOOLS:
                args["approval_token"] = self.tokens.issue()
            if step["name"] == "file_tickets" and note_suffix:
                args["run_tag"] = note_suffix
            try:
                result = self.tools.call(step["name"], args)
            except Exception as e:  # noqa: BLE001 — feed back, don't crash
                result = {"error": f"{type(e).__name__}: {e}"}
            trace.append({"tool": step["name"], "arguments": step["arguments"],
                          "result": result})
            messages.append({"role": "tool", "name": step["name"],
                             "tool_call_id": step.get("id")
                             or f"call_{i}_{step['name']}",
                             "content": json.dumps(result)})
        self.meter.authorize(plan["purpose"])
        resp = self.ember.chat(messages, self.tool_schemas)
        self.meter.record(plan["purpose"], resp.prompt_tokens,
                          resp.completion_tokens,
                          note="compose-final"
                          + (f":{note_suffix}" if note_suffix else ""))
        plan["status"] = "executed"
        plan["trace"] = trace
        plan["final_text"] = resp.text
        return plan

    def reject(self, plan_id: str) -> dict:
        plan = self.plans[plan_id]
        plan["status"] = "rejected"
        return plan
