"""Ember clients.

The orchestrator talks to Muse Ember ONLY through this adapter
(hackathon rule: the project's AI runs on Muse Ember via the Meta Model API).

  - StubEmberClient  — deterministic, $0. Routes a user request to a tool
    plan, then composes the final answer from real tool results. Lets the
    whole system be built and rehearsed before any credits exist.
  - MetaEmberClient  — live Muse Ember via the Meta Model API (Phase 1).
    OpenAI-compatible Chat Completions at {base}/chat/completions, Bearer
    auth. Config via env: MODEL_API_KEY (or META_MODEL_API_KEY),
    EMBER_MODEL_ID (default muse-ember-1.0), EMBER_BASE_URL (default
    https://api.meta.ai/v1), EMBER_MAX_TOKENS (default 512).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    name: str
    arguments: dict = field(default_factory=dict)
    id: str = ""  # API tool_call id; the compose turn must replay it


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

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str = "auto") -> EmberResponse:
        raise NotImplementedError


class NotConfiguredError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects on an authenticated request."""

    def redirect_request(self, *args):
        raise RuntimeError("redirect refused on authenticated Ember call")


def _openai_tools(tools: list[dict]) -> list[dict]:
    """Map the catalog's {name, description, parameters} onto the
    OpenAI function-calling shape the Meta Model API accepts."""
    out = []
    for t in tools or []:
        out.append({"type": "function", "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("parameters") or {"type": "object",
                                                  "properties": {}},
        }})
    return out


def _normalize_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        msg = dict(m)
        if not isinstance(msg.get("content"), str):
            msg["content"] = json.dumps(msg.get("content"))
        if msg.get("role") == "tool" and not msg.get("tool_call_id"):
            msg["tool_call_id"] = msg.get("name") or "tool"
        out.append(msg)
    return out


class MetaEmberClient(EmberClient):
    """Live Muse Ember via the Meta Model API (Phase 1)."""

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, max_tokens: int | None = None,
                 timeout: int = 60, reasoning_effort: str | None = None):
        self.api_key = (api_key or os.environ.get("MODEL_API_KEY")
                        or os.environ.get("META_MODEL_API_KEY"))
        self.model = (model or os.environ.get("EMBER_MODEL_ID")
                      or os.environ.get("EMBER_MODEL") or "muse-ember-1.0")
        self.base_url = (base_url or os.environ.get("EMBER_BASE_URL")
                         or "https://api.meta.ai/v1").rstrip("/")
        if max_tokens is None:
            max_tokens = int(os.environ.get("EMBER_MAX_TOKENS", "512"))
        self.max_tokens = max_tokens
        self.timeout = timeout
        # Ember always reasons (reasoning_effort "none" -> HTTP 400); the
        # API default is "max". "minimal" is the lowest usable setting and
        # keeps tool turns cheap (dev.meta.ai/docs/protocols/chat-completions).
        self.reasoning_effort = (reasoning_effort
                                 or os.environ.get("EMBER_REASONING_EFFORT")
                                 or "minimal")
        self.last_usage: dict = {}
        if not self.api_key:
            raise NotConfiguredError(
                "MODEL_API_KEY (or META_MODEL_API_KEY) is not set. Source "
                "the Cube's ember.env before running the live client.")

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str = "auto") -> EmberResponse:
        payload: dict = {
            "model": self.model,
            "messages": _normalize_messages(messages),
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
        }
        oai_tools = _openai_tools(tools)
        if oai_tools:
            payload["tools"] = oai_tools
            payload["tool_choice"] = tool_choice
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                    request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(
                f"Meta Model API HTTP {err.code}: {detail}") from err
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            if fn.get("name"):
                calls.append(ToolCall(fn["name"], args, tc.get("id") or ""))
        usage = data.get("usage") or {}
        self.last_usage = usage
        text = message.get("content") or ""
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if prompt_tokens is None:  # never meter a real call as zero
            prompt_tokens = _est_tokens(json.dumps(payload))
        if completion_tokens is None:
            completion_tokens = _est_tokens(text)
        return EmberResponse(text=text, tool_calls=calls,
                             prompt_tokens=int(prompt_tokens),
                             completion_tokens=int(completion_tokens))


# Deterministic canned triage for the stub client (Phase 0 fixtures).
_STUB_TRIAGE = {
    "TKT-001": ("Network", "P2", "Network team",
                "Thanks for flagging this — afternoon VPN drops for the whole "
                "office sound like a capacity or ISP issue. We're "
                "investigating now and will update you within the hour."),
    "TKT-002": ("Billing", "P3", "Finance systems",
                "Sorry about the blank invoice page. We're checking the PDF "
                "service; meanwhile we can email you the invoice directly "
                "for month-end close."),
    "TKT-003": ("Access", "P3", "IT provisioning",
                "We'll provision the new starter with the same access as the "
                "other shift leads, ready for Monday."),
    "TKT-004": ("Hardware", "P3", "Site support",
                "Thanks — we're checking the shipping label printer and its "
                "print queue after the power flicker."),
    "TKT-005": ("Account", "P2", "Service desk",
                "We're resetting the shared mailbox password now and will "
                "confirm as soon as the orders inbox is accessible again."),
}


def _stub_triage() -> list[dict]:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "fixtures", "tickets.json")) as f:
            tickets = json.load(f)["tickets"]
    except (OSError, ValueError, KeyError):
        tickets = [{"id": k} for k in _STUB_TRIAGE]
    out = []
    for t in tickets:
        cat, pri, route, draft = _STUB_TRIAGE.get(
            t["id"], ("Other", "P3", "Service desk",
                      "Thanks for your message — we're looking into it."))
        out.append({"id": t["id"], "category": cat, "priority": pri,
                    "draft_reply": draft, "route": route})
    return out


class StubEmberClient(EmberClient):
    """Deterministic stand-in for Ember. No network, no cost.

    Turn 1 (no tool results in the conversation yet): read the user's words
    and propose a tool plan.
    Turn 2 (tool results present): compose the final answer from them.
    """

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str = "auto") -> EmberResponse:
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
        elif "triage" in u or "ticket" in u or "help-desk" in u or "helpdesk" in u:
            calls = [ToolCall("file_tickets",
                              {"tickets": _stub_triage()})]
            text = ("Plan: classify the 5 fixture tickets (category + "
                    "priority), draft a reply for each, and file them to the "
                    "board on your approval. Nothing is sent to requesters.")
        elif "handover" in u or "shift" in u:
            calls = [ToolCall("get_watcher_status")]
            text = ("Plan: read the live Watcher status and open incidents, "
                    "then I will draft the shift handover brief for your approval.")
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
        if "triage" in u or "ticket" in u:
            filed = payload.get("file_tickets") or {}
            if filed.get("error"):
                text = f"Filing failed: {filed['error']}"
            else:
                text = (f"Filed {filed.get('filed', 0)} tickets to the board "
                        f"({', '.join(filed.get('ticket_ids', []))}). Board "
                        f"total: {filed.get('board_total', 0)}. Draft replies "
                        "are stored as drafts — a human sends them.")
        elif "handover" in u or "shift" in u:
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
        elif status_line:
            text = f"Watcher status: {status_line}"
        else:
            text = "Here is what the tools returned: " + json.dumps(payload)[:600]
        return EmberResponse(text=text,
                             prompt_tokens=_est_tokens(prompt_text),
                             completion_tokens=_est_tokens(text))
