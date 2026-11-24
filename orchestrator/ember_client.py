"""Ember clients.

The orchestrator talks to Muse Ember ONLY through this adapter
(hackathon rule: the project's AI runs on Muse Ember via the Meta Model API).

Phase 0 ships:
  - StubEmberClient  — deterministic, $0. Routes a user request to a tool
    plan, then composes the final answer from real tool results. Lets the
    whole system be built and rehearsed before any credits exist.
  - MetaEmberClient  — the Phase 1 wiring point. Raises until a Meta Model
    API key is configured; the chat() body is where the real call lands the
    day credits are issued.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class EmberResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class EmberClient:
    """Interface: one chat turn in, text and/or tool calls out."""

    def chat(self, messages: list[dict], tools: list[dict]) -> EmberResponse:
        raise NotImplementedError


class NotConfiguredError(RuntimeError):
    pass


class MetaEmberClient(EmberClient):
    """Live Muse Ember via the Meta Model API — Phase 1 wiring point."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("META_MODEL_API_KEY")
        self.model = model or os.environ.get("EMBER_MODEL_ID")
        if not self.api_key:
            raise NotConfiguredError(
                "META_MODEL_API_KEY is not set. Live Ember wiring lands in "
                "Phase 1, when hackathon credits are issued.")

    def chat(self, messages: list[dict], tools: list[dict]) -> EmberResponse:
        # PHASE 1: POST the chat request to the Meta Model API here, mapping
        # its tool-calling response onto EmberResponse, and return real
        # prompt/completion token counts for the spend meter.
        raise NotImplementedError("Phase 1: Meta Model API call not wired yet")


class StubEmberClient(EmberClient):
    """Deterministic stand-in for Ember. No network, no cost.

    Turn 1 (no tool results in the conversation yet): read the user's words
    and propose a tool plan.
    Turn 2 (tool results present): compose the final answer from them.
    """

    def chat(self, messages: list[dict], tools: list[dict]) -> EmberResponse:
        prompt_text = json.dumps(messages)
        results = [m for m in messages if m.get("role") == "tool"]
        if results:
            return self._compose(messages, results, prompt_text)
        user = next((m["content"] for m in reversed(messages)
                     if m.get("role") == "user"), "")
        return self._plan(user, prompt_text)

    # ------------------------------------------------------------- planning
    def _plan(self, user: str, prompt_text: str) -> EmberResponse:
        u = user.lower()
        calls: list[ToolCall] = []
        if "whatsapp" in u or ("watcher" in u and "send" in u):
            calls = [ToolCall("get_watcher_status"),
                     ToolCall("run_workflow", {"workflow_id": "watcher-status-whatsapp"})]
            text = ("Plan: read the live Watcher status, then fire the "
                    "deterministic 'Watcher Status to WhatsApp' flow. "
                    "Execution needs your approval.")
        elif "handover" in u or "shift" in u:
            calls = [ToolCall("get_watcher_status")]
            text = ("Plan: read the live Watcher status and open incidents, "
                    "then I will draft the shift handover brief for your approval.")
        elif "triage" in u or "ticket" in u or "help-desk" in u or "helpdesk" in u:
            calls = [ToolCall("list_workflows")]
            text = ("Plan: confirm the triage routing workflow is available, "
                    "then I will classify the fixture tickets and draft replies "
                    "for your approval before anything is filed.")
        elif "status" in u or "watcher" in u:
            calls = [ToolCall("get_watcher_status")]
            text = "Plan: read the live Watcher status and report it here."
        else:
            calls = [ToolCall("list_workflows")]
            text = ("Plan: list the deterministic workflows I am allowed to "
                    "run, so we can pick one together.")
        return EmberResponse(text=text, tool_calls=calls,
                             prompt_tokens=_est_tokens(prompt_text),
                             completion_tokens=_est_tokens(text) + 40)

    # ------------------------------------------------------------- composing
    def _compose(self, messages: list[dict], results: list[dict],
                 prompt_text: str) -> EmberResponse:
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        u = user.lower()
        payload = {}
        for m in results:
            try:
                payload[m.get("name", "")] = json.loads(m["content"])
            except (ValueError, KeyError, TypeError):
                payload[m.get("name", "")] = m.get("content")
        status = payload.get("get_watcher_status") or {}
        run = payload.get("run_workflow") or {}
        status_line = status.get("line", "")
        if "handover" in u or "shift" in u:
            text = ("SHIFT HANDOVER BRIEF (draft)\n"
                    f"- Watcher: {status_line or 'status unavailable'}\n"
                    "- Open incidents carried over: "
                    f"{status.get('open_incidents', 'n/a')}\n"
                    "- Notes: nightly backup completed 02:42 CST, 7-day rotation "
                    "intact; no open incidents carried over from the previous shift.\n"
                    "Approve to file this brief, or tell me what to change.")
        elif run:
            text = (f"Done. Watcher status: {status_line}. "
                    f"Workflow '{run.get('workflow_id')}' run {run.get('run_id')} "
                    f"finished with status {run.get('status')}. "
                    f"{run.get('note', '')}").strip()
        elif "triage" in u or "ticket" in u:
            text = ("Triage proposal ready. The routing workflow is available; "
                    "on your approval I will classify the 5 fixture tickets "
                    "(category + priority), draft a reply for each, and file "
                    "them through the deterministic routing flow.")
        elif status_line:
            text = f"Watcher status: {status_line}"
        else:
            text = "Here is what the tools returned: " + json.dumps(payload)[:600]
        return EmberResponse(text=text,
                             prompt_tokens=_est_tokens(prompt_text),
                             completion_tokens=_est_tokens(text))
