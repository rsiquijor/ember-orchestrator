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
    # True when the API's arguments JSON did not parse (response cut off
    # mid-call at max_tokens) — arguments is then unreliable/empty.
    args_truncated: bool = False


@dataclass
class EmberResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # API finish_reason ("stop", "length", "tool_calls", ...). "length"
    # means the reply was truncated at max_tokens — Ember is a reasoning
    # model, so reasoning can eat the whole budget before any tool call.
    finish_reason: str = ""


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class EmberClient:
    """Interface: one chat turn in, text and/or tool calls out."""

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str = "auto",
             max_tokens: int | None = None) -> EmberResponse:
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
        content = msg.get("content")
        if isinstance(content, list):
            # Multimodal content parts (Phase 4 intake): pass through in
            # the OpenAI shape — {"type": "text", ...} /
            # {"type": "image_url", "image_url": {"url": "data:..."}}.
            msg["content"] = content
        elif not isinstance(content, str):
            msg["content"] = json.dumps(content)
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
             tool_choice: str = "auto",
             max_tokens: int | None = None) -> EmberResponse:
        payload: dict = {
            "model": self.model,
            "messages": _normalize_messages(messages),
            "max_tokens": self.max_tokens if max_tokens is None
            else int(max_tokens),
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
            truncated = False
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                # Arguments JSON cut off mid-call (max_tokens) — flag it
                # so the orchestrator can retry with more headroom
                # instead of executing an empty-arguments call.
                args = {}
                truncated = True
            if fn.get("name"):
                calls.append(ToolCall(fn["name"], args, tc.get("id") or "",
                                      args_truncated=truncated))
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
                             completion_tokens=int(completion_tokens),
                             finish_reason=str(
                                 choice.get("finish_reason") or ""))


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
             tool_choice: str = "auto",
             max_tokens: int | None = None) -> EmberResponse:
        prompt_text = json.dumps(messages)
        results = [m for m in messages if m.get("role") == "tool"]
        if results:
            return self._compose(messages, results, prompt_text)
        user = next((m["content"] for m in reversed(messages)
                     if m.get("role") == "user"), "")
        return self._plan(user, prompt_text)

    # ------------------------------------------------------------- planning
    @staticmethod
    def _stub_export_params(text: str) -> dict | None:
        """Deterministic format/source sniff for export requests."""
        t = str(text).lower()
        if "excel" in t or "xlsx" in t or "spreadsheet" in t:
            fmt = "xlsx"
        elif "csv" in t:
            fmt = "csv"
        elif "word file" in t or "word document" in t or "docx" in t:
            fmt = "docx"
        elif "json" in t:
            fmt = "json"
        elif "txt" in t or "text file" in t:
            fmt = "txt"
        else:
            return None
        if "plc" in t or "converted code" in t:
            source = "plc"
        elif "legal" in t or "paralegal" in t:
            source = "legal"
        elif "medical" in t:
            source = "medical"
        elif "ticket" in t or "board" in t:
            source = "board"
        elif "finding" in t or "document" in t or "report" in t:
            source = "document"
        elif "last run" in t or "results" in t:
            source = "last_run"
        else:
            source = "intake"
        return {"format": fmt, "source": source}

    @staticmethod
    def _stub_agent_params(text: str) -> dict:
        """Deterministic agentic-process params for the stub: source
        sniffed from the words, schedule from 'at H', actions from
        whatsapp/board mentions, and a condition when the request
        implies alerting only on a threshold ('only if', 'when',
        'alert'). Mirrors what live Ember is instructed to emit."""
        t = str(text).lower()
        if "capacit" in t:
            source, name = "capacity", "Morning Capacity Report"
            cond = {"field": "disk_pct", "op": ">", "value": 80}
        elif "cost" in t or "finops" in t or "budget" in t:
            source, name = "finops", "Daily Cost Check"
            cond = {"field": "budget_pct", "op": ">", "value": 80}
        elif "cve" in t or "secops" in t or "security" in t:
            source, name = "secops", "Daily CVE Check"
            cond = {"field": "critical_cves", "op": ">", "value": 0}
        elif "prediction" in t or "failure" in t:
            source, name = "predictions", "Failure Risk Watch"
            cond = {"field": "high_count", "op": ">", "value": 0}
        else:
            source, name = "watcher_overview", "Morning Watcher WhatsApp"
            cond = {"field": "components_down", "op": ">", "value": 0}
        actions = []
        if "whatsapp" in t:
            actions.append("whatsapp")
        if "board" in t:
            actions.append("board")
        if not actions:
            actions = ["whatsapp"] if source == "watcher_overview" \
                else ["board"]
        hour = 6
        import re as _re
        m = _re.search(r"\bat\s+(\d{1,2})\b", t)
        if m and 0 <= int(m.group(1)) <= 23:
            hour = int(m.group(1))
        params = {"name": name, "trigger": "schedule",
                  "cron": f"0 {hour} * * *", "source": source,
                  "actions": actions}
        if "only if" in t or "only when" in t or "alert" in t \
                or "if a component is down" in t \
                or "if something is down" in t:
            params["condition"] = cond
        return params

    @staticmethod
    def _stub_dashboard_params(text: str) -> dict:
        """Deterministic dashboard spec for the stub: endpoints
        sniffed from the words, KPIs/charts referencing the
        FixtureCubeTools canned profiles (prefixed names when
        multi-source, exactly like the live generator)."""
        t = str(text).lower()
        endpoints = []
        if "capacit" in t or "disk" in t or "headroom" in t:
            endpoints.append("capacity")
        if "cost" in t or "finops" in t or "budget" in t \
                or "spend" in t:
            endpoints.append("finops")
        if "prediction" in t or "failure" in t or "risk" in t:
            endpoints.append("predictions")
        if "watcher" in t or "estate" in t or "health" in t:
            endpoints.append("watcher-overview")
        if "sentiment" in t or "frustrat" in t:
            endpoints.append("sentiment")
        if "lease" in t or "acquisition" in t:
            endpoints.append("leases")
        if "finclose" in t or "month-end" in t or "month end" in t:
            endpoints.append("finclose")
        if "bid" in t or "dot" in t:
            endpoints.append("dotkrc-status")
        if not endpoints:
            endpoints = ["watcher-overview"]
        endpoints = endpoints[:3]
        multi = len(endpoints) > 1

        def p(ep, name):
            return (ep.replace("-", "_") + "_" + name) if multi else name

        kpis, charts, table = [], [], None
        for ep in endpoints:
            if ep == "capacity":
                kpis += [
                    {"label": "Disk %", "field": p(ep, "host_disk_pct"),
                     "format": "pct"},
                    {"label": "Memory %", "field": p(ep, "host_mem_pct"),
                     "format": "pct"},
                    {"label": "Days to disk 90%",
                     "field": p(ep, "disk_days_to_90"), "format": "int"},
                ]
                charts.append({
                    "type": "bar", "title": "Component uptime (7d %)",
                    "from": p(ep, "components"),
                    "label_field": "component",
                    "value_field": "uptime_7d_pct"})
                table = {"title": "Components",
                         "from": p(ep, "components"),
                         "columns": ["component", "up",
                                     "uptime_7d_pct", "incidents_7d"]}
            elif ep == "finops":
                kpis += [
                    {"label": "Cost today",
                     "field": p(ep, "costs_today_usd"), "format": "money"},
                    {"label": "Projected month",
                     "field": p(ep, "costs_month_projected_usd"),
                     "format": "money"},
                    {"label": "Budget % (projected)",
                     "field": p(ep, "budget_projected_pct"),
                     "format": "pct"},
                ]
            elif ep == "watcher-overview":
                kpis += [
                    {"label": "Components up",
                     "field": p(ep, "totals_up"), "format": "int"},
                    {"label": "Open incidents",
                     "field": p(ep, "totals_open_incidents"),
                     "format": "int"},
                    {"label": "Disk %", "field": p(ep, "host_disk_pct"),
                     "format": "pct"},
                ]
                charts.append({
                    "type": "status-list", "title": "Component health",
                    "from": p(ep, "components"),
                    "label_field": "component", "status_field": "up"})
            elif ep == "predictions":
                kpis += [
                    {"label": "Max risk score",
                     "field": p(ep, "risk_max"), "format": "int"},
                    {"label": "High-risk components",
                     "field": p(ep, "high_count"), "format": "int"},
                ]
                charts.append({
                    "type": "bar", "title": "Risk by component",
                    "from": p(ep, "predictions"),
                    "label_field": "name", "value_field": "risk_score"})
            elif ep == "sentiment":
                kpis.append({"label": "Frustrated users",
                             "field": p(ep, "frustrated_count"),
                             "format": "int"})
            elif ep == "leases":
                kpis.append({"label": "Pipeline value",
                             "field": p(ep, "pipeline_value_usd"),
                             "format": "money"})
                charts.append({
                    "type": "share", "title": "Pipeline by stage",
                    "from": p(ep, "companies"),
                    "label_field": "stage",
                    "value_field": "est_value_usd"})
            elif ep == "finclose":
                kpis += [
                    {"label": "Revenue (DEMO)",
                     "field": p(ep, "kpis_revenue"), "format": "money"},
                    {"label": "EBITDA (DEMO)",
                     "field": p(ep, "kpis_ebitda"), "format": "money"},
                ]
            elif ep == "dotkrc-status":
                kpis.append({"label": "Projects tracked",
                             "field": p(ep, "projects"), "format": "int"})
        labels = {"capacity": "Capacity", "finops": "Costs",
                  "watcher-overview": "Estate Health",
                  "predictions": "Failure Predictions",
                  "sentiment": "Sentiment", "leases": "Leases",
                  "finclose": "Month-End Close",
                  "dotkrc-status": "DOT Bids"}
        title = " & ".join(labels[e] for e in endpoints)
        params = {"title": title, "endpoints": endpoints,
                  "kpis": kpis[:8], "charts": charts[:4]}
        if table:
            params["table"] = table
        return params

    def _plan(self, user, prompt_text: str) -> EmberResponse:
        # Multimodal intake: content parts with an image -> Ember (stub)
        # "reads" it and proposes filing the extracted fields.
        if isinstance(user, list):
            part_types = {p.get("type") for p in user if isinstance(p, dict)}
            has_image = "image_url" in part_types
            text = " ".join(str(p.get("text") or "") for p in user
                            if isinstance(p, dict))
            if "input_audio" in part_types:
                calls = [ToolCall("file_intake", {"record": {
                    "kind": "audio_note",
                    "fields": {
                        "transcript": ("Work order seven seven one two. "
                                       "Replace the hydraulic pump on "
                                       "line three. Priority high. "
                                       "Due Friday."),
                        "work_order": "7712",
                        "task": "Replace the hydraulic pump",
                        "line": "3",
                        "priority": "high",
                        "due": "Friday",
                    },
                    "notes": "Stub extraction from audio.",
                }})]
                exp = self._stub_export_params(text)
                if exp:
                    calls.append(ToolCall(
                        "export_file", {**exp, "source": "intake"}))
                plan_text = ("Plan: I listened to the audio clip and "
                             "transcribed it. On your approval I file "
                             "the transcript and extracted items to the "
                             "intake board — nothing is sent.")
                return EmberResponse(
                    text=plan_text, tool_calls=calls,
                    prompt_tokens=_est_tokens(prompt_text),
                    completion_tokens=_est_tokens(plan_text) + 40)
            if "video_url" in part_types or "input_video" in part_types:
                calls = [ToolCall("file_intake", {"record": {
                    "kind": "video",
                    "fields": {
                        "on_screen_text": ("PUMP FAULT - LINE 3; "
                                           "PRESSURE 42 PSI"),
                        "summary": ("Static alert screen reporting a pump "
                                    "fault on line 3 at 42 PSI."),
                        "transcript": "",
                    },
                    "notes": "Stub extraction from video.",
                }})]
                exp = self._stub_export_params(text)
                if exp:
                    calls.append(ToolCall(
                        "export_file", {**exp, "source": "intake"}))
                plan_text = ("Plan: I watched the video and extracted "
                             "the on-screen text. On your approval I "
                             "file it to the intake board — nothing is "
                             "sent.")
                return EmberResponse(
                    text=plan_text, tool_calls=calls,
                    prompt_tokens=_est_tokens(prompt_text),
                    completion_tokens=_est_tokens(plan_text) + 40)
            if "file" in part_types or has_image:
                calls = [ToolCall("file_intake", {"record": {
                    "kind": "invoice",
                    "fields": {
                        "vendor": "Hydraulic Supply Co.",
                        "invoice_number": "INV-88410",
                        "date": "11/18/2026",
                        "po_number": "PO-2214",
                        "bill_to": "Demo Plant - Accounts Payable",
                        "line_items": [
                            "Hydraulic hose, 3/4 in x 6 ft x2 @ 86.50 = 173.00",
                            "Hydraulic oil ISO 46, 55 gal drum x1 @ 812.00 = 812.00",
                            "Manifold fitting kit x1 @ 257.50 = 257.50"],
                        "subtotal": "1,242.50",
                        "total_due": "1,250.00",
                    },
                    "notes": "Stub extraction.",
                }})]
                exp = self._stub_export_params(text)
                if exp:
                    calls.append(ToolCall(
                        "export_file", {**exp, "source": "intake"}))
                plan_text = ("Plan: I read the invoice image and extracted "
                             "the fields below. On your approval I file "
                             "them to the intake board — nothing is paid "
                             "or sent.")
                return EmberResponse(
                    text=plan_text, tool_calls=calls,
                    prompt_tokens=_est_tokens(prompt_text),
                    completion_tokens=_est_tokens(plan_text) + 40)
            user = text
        u = str(user).lower()
        calls: list[ToolCall] = []
        if "enable" in u and ("flow" in u or "automation" in u):
            import re as _re
            m = _re.search(r"(sim-flow-\d+|[A-Za-z0-9]{20,24})", str(user))
            fid = m.group(1) if m else ""
            calls = [ToolCall("enable_agent_flow", {"flow_id": fid})]
            text = (f"Plan: enable the Agent Builder draft {fid} — "
                    "publish and switch it on in Metatron RPA. Only "
                    "builder-created drafts can be enabled, and only "
                    "after your approval.")
        elif ("agentic workflow" in u or "create an agent" in u
                or "build an agent" in u
                or ("agent" in u and "build" in u)):
            calls = [ToolCall("create_agent_flow",
                              self._stub_agent_params(u))]
            text = ("Plan: build a deterministic RPA automated "
                    "agentic workflow — "
                    "on its schedule it fetches the source, composes a "
                    "report, and runs the action(s). Created as a "
                    "DISABLED draft for your review; no AI steps "
                    "inside the flow.")
        elif "dashboard" in u and ("build" in u or "create" in u
                                    or "make" in u or "design" in u
                                    or "new dashboard" in u):
            params = self._stub_dashboard_params(u)
            calls = [ToolCall("create_dashboard", params)]
            text = (f"Plan: build a dashboard "
                    f"“{params['title']}” — "
                    f"{len(params['kpis'])} KPI cards + "
                    f"{len(params['charts'])} charts from "
                    f"{', '.join(params['endpoints'])}. I design "
                    "the spec from the live data profile; Metatron "
                    "renders it deterministically after your "
                    "approval.")
        elif "plc" in u and ("convert" in u or "translate" in u) \
                and not (self._stub_export_params(u) is not None
                         and ("save" in u or "export" in u
                              or "download" in u)):
            from .plc_converter import (DIRECTION_LABELS,
                                        SAMPLE_CONVERSIONS)
            if ("to allen" in u or "to studio" in u or "to ab" in u
                    or "siemens to" in u or "scl to" in u):
                direction = "siemens_to_ab"
            else:
                direction = "ab_to_siemens"
            conv = SAMPLE_CONVERSIONS[direction]
            calls = [ToolCall("convert_plc_code", {
                "direction": direction,
                "program_name": conv["program_name"],
                "source_code": conv["source_code"],
                "converted_code": conv["converted_code"],
                "coverage": conv["coverage"],
                "notes": ("Stub conversion of the sample program; "
                          "timer/counter instances must be "
                          "declared in the target project."),
            })]
            text = (f"Plan: convert “{conv['program_name']}” "
                    f"({DIRECTION_LABELS[direction]}) using the "
                    "vendor manuals as reference. I write the "
                    "converted code plus a per-instruction "
                    "coverage report; unsupported instructions "
                    "are flagged, never silently replaced. The "
                    "result is a DRAFT for qualified-engineer "
                    "review — nothing deploys.")
        elif any(w in u for w in (
                "paralegal", "legal", "u.s.c", "statute",
                "constitution", "federal rules", "discovery",
                "jurisdiction", "hearsay", "cfr", "amendment",
                "lawsuit", "regulation", "court rule")) \
                and not (self._stub_export_params(u) is not None
                         and ("save" in u or "export" in u
                              or "download" in u)):
            calls = [ToolCall("ask_paralegal",
                              {"question": str(user).strip()[:300]})]
            text = ("Plan: ask the paralegal assistant — "
                    "deterministic keyword retrieval over the "
                    "U.S. Code, Federal Rules, Constitution, "
                    "and CFR returns the relevant sections as "
                    "long-context reference, and I answer only "
                    "from those passages, citing per "
                    "section/rule. If the sources do not cover "
                    "it, I say so. Legal information — not "
                    "legal advice.")
        elif any(w in u for w in (
                "manual say", "merck", "medical", "symptom",
                "diagnos", "treatment", "disease", "infection",
                "hepatitis", "diabetes", "cancer", "virus",
                "bacterial", "fever", "blood pressure",
                "cholesterol", "vaccine", "antibiotic",
                "syndrome", "disorder", "illness", "patient")) \
                and not (self._stub_export_params(u) is not None
                         and ("save" in u or "export" in u
                              or "download" in u)):
            calls = [ToolCall("ask_medical_manual",
                              {"question": str(user).strip()[:300]})]
            text = ("Plan: ask The Merck Manual (19th Ed.) — "
                    "deterministic keyword retrieval over the "
                    "chunked manual returns the relevant "
                    "chapters as long-context reference, and I "
                    "answer only from those passages, citing "
                    "chapter, section, and pages. If the manual "
                    "does not cover it, I say so. Demo "
                    "reference — not medical advice.")
        elif self._stub_export_params(u) is not None and (
                "save" in u or "export" in u or "download" in u
                or "convert" in u or "spit out" in u or "give me" in u
                or "as excel" in u or "as csv" in u or "as json" in u
                or "as txt" in u or "as a word" in u
                or "as a text" in u):
            params = self._stub_export_params(u)
            calls = [ToolCall("export_file", params)]
            text = (f"Plan: export the {params['source']} data as a "
                    f"{params['format'].upper()} file. Generation is "
                    "deterministic ($0); the file is created only "
                    "after your approval.")
        elif "document" in u and ("finding" in u or "anomal" in u
                                  or "analy" in u or "report" in u):
            calls = [ToolCall("file_document_report", {"report": {
                "title": "Stub document analysis",
                "summary": ("Stub scan of the attached document: 2 "
                            "anomalies flagged for review."),
                "findings": [
                    {"line": 1, "severity": "medium",
                     "detail": "Stub finding — live Ember cites real lines."},
                    {"line": 2, "severity": "low",
                     "detail": "Stub finding — live Ember cites real lines."},
                ]}})]
            text = ("Plan: I parsed the document and will file a findings "
                    "report to the board on your approval.")
        elif "full picture" in u or "full health" in u:
            calls = [ToolCall("cube_status"),
                     ToolCall("graph_dependents",
                              {"component": "model-endpoint"}),
                     ToolCall("secops_status"),
                     ToolCall("predictions"),
                     ToolCall("finops_summary")]
            text = ("Plan: chain five read-only reports — estate status, "
                    "brain blast radius, CVE posture, failure "
                    "predictions, and FinOps costs — into one brief.")
        elif "whatsapp" in u or ("watcher" in u and "send" in u):
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
        elif ("cost" in u or "finops" in u or "spending" in u
              or "budget" in u or "burn" in u):
            calls = [ToolCall("finops_summary")]
            text = ("Plan: read the FinOps summary (costs today/month/"
                    "projected, budget %, alerts) and report it. Read-only; "
                    "runs on your approval.")
        elif ("cve" in u or "vulnerab" in u or "secops" in u
              or "security" in u or "exposed" in u or "threat" in u
              or "kev" in u):
            calls = [ToolCall("secops_status")]
            text = ("Plan: read the SecOps posture (CVE/KEV/EPSS counts and "
                    "top priority CVEs) and summarize. Read-only data — no "
                    "investigation runs.")
        elif ("capacity" in u or "headroom" in u or "forecast" in u
              or "saturat" in u or "disk" in u):
            calls = [ToolCall("capacity_report"), ToolCall("predictions")]
            text = ("Plan: read the capacity report and failure predictions, "
                    "then summarize headroom, the disk forecast, and top "
                    "risks. Read-only.")
        elif ("break" in u or "blast" in u or "depends" in u
              or "depend" in u or "impact" in u or "goes down" in u
              or "go down" in u or "downstream" in u):
            if "stt" in u or "speech" in u:
                comp = "stt-service"
            elif "ocr" in u:
                comp = "ocr-service"
            elif "rpa" in u or "activepieces" in u:
                comp = "rpa-sandbox"
            else:  # brain / llm / model / default
                comp = "model-endpoint"
            calls = [ToolCall("graph_dependents", {"component": comp})]
            text = (f"Plan: query the knowledge graph for everything that "
                    f"depends on '{comp}' and explain the blast radius. "
                    "Read-only.")
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
        if isinstance(user, list):
            user = " ".join(str(p.get("text") or "") for p in user
                            if isinstance(p, dict))
        u = str(user).lower()
        payload = {}
        for m in results:
            try:
                payload[m.get("name", "")] = json.loads(m["content"])
            except (ValueError, KeyError, TypeError):
                payload[m.get("name", "")] = m.get("content")
        status = payload.get("get_watcher_status") or {}
        run = payload.get("run_workflow") or {}
        status_line = status.get("line", "")
        _reads = [k for k in payload if k in (
            "cube_status", "graph_dependents", "secops_status",
            "predictions", "finops_summary", "capacity_report")]
        if len(_reads) >= 3:
            parts = []
            cs = payload.get("cube_status") or {}
            if cs:
                parts.append(
                    f"Estate: {cs.get('components_up', 'n/a')}/"
                    f"{cs.get('components_total', 'n/a')} up, "
                    f"{cs.get('open_incidents', 'n/a')} open incidents.")
            gd = payload.get("graph_dependents") or {}
            if gd:
                parts.append(
                    f"Blast radius: {gd.get('count', 'n/a')} dependents "
                    f"on '{gd.get('component')}'.")
            sec = payload.get("secops_status") or {}
            if sec:
                kpis = sec.get("kpis") or {}
                parts.append(
                    f"Security: {kpis.get('critical', 'n/a')} critical / "
                    f"{kpis.get('high', 'n/a')} high CVEs.")
            pred = payload.get("predictions") or {}
            if pred:
                parts.append(
                    f"Predictions: {pred.get('high_count', 'n/a')} high, "
                    f"{pred.get('watch_count', 'n/a')} watch.")
            fin = payload.get("finops_summary") or {}
            if fin:
                costs = fin.get("costs") or {}
                parts.append(
                    f"Costs: month projected "
                    f"${costs.get('month_projected_usd', 'n/a')}.")
            text = "Full picture: " + " ".join(parts)
        elif "finops_summary" in payload:
            fin = payload["finops_summary"] or {}
            costs = fin.get("costs") or {}
            budget = fin.get("budget") or {}
            alerts = fin.get("alerts") or []
            text = ("Cube costs (FinOps): "
                    f"today ${costs.get('today_usd', 'n/a')}, "
                    f"month so far ${costs.get('month_so_far_usd', 'n/a')}, "
                    f"month projected ${costs.get('month_projected_usd', 'n/a')} "
                    f"({budget.get('projected_pct', 'n/a')}% of the "
                    f"${budget.get('monthly_usd', 'n/a')} monthly budget), "
                    f"daily burn ${costs.get('daily_burn_usd', 'n/a')}. "
                    + (f"Alerts: {'; '.join(str(a) for a in alerts)}."
                       if alerts else "No alerts."))
        elif "secops_status" in payload:
            sec = payload["secops_status"] or {}
            kpis = sec.get("kpis") or {}
            top = (sec.get("priority_top5") or [{}])[0]
            text = ("Security posture (SecOps): "
                    f"{kpis.get('critical', 'n/a')} critical and "
                    f"{kpis.get('high', 'n/a')} high CVEs in the current set, "
                    f"{kpis.get('kev_in_set', 'n/a')} of them in CISA KEV "
                    f"(catalog total {kpis.get('kev_total', 'n/a')}, "
                    f"{kpis.get('kev_recent', 'n/a')} added recently). "
                    + (f"Top priority: {top.get('id')} "
                       f"({top.get('severity')}, CVSS {top.get('cvss')}) "
                       f"in {top.get('product')}. " if top.get("id") else "")
                    + "Read-only data — no investigation was run.")
        elif "capacity_report" in payload or "predictions" in payload:
            cap = payload.get("capacity_report") or {}
            pred = payload.get("predictions") or {}
            cur = cap.get("host_current") or {}
            worst = (cap.get("worst_components") or [{}])[0]
            top = (pred.get("top_risks") or [{}])[0]
            text = ("Capacity outlook: "
                    f"CPU {cur.get('cpu_pct', 'n/a')}%, "
                    f"memory {cur.get('mem_pct', 'n/a')}%, "
                    f"disk {cur.get('disk_pct', 'n/a')}% "
                    f"({cur.get('disk_used_gb', 'n/a')}/"
                    f"{cur.get('disk_total_gb', 'n/a')} GB), "
                    f"disk reaches 90% in ~{cap.get('disk_days_to_90', 'n/a')} "
                    "days at the current trend. "
                    f"{cap.get('components_up', 'n/a')}/"
                    f"{cap.get('components_total', 'n/a')} components up. "
                    + (f"Weakest component: {worst.get('name')} "
                       f"({worst.get('uptime_7d_pct')}% 7-day uptime). "
                       if worst.get("name") else "")
                    + (f"Top predicted risk: {top.get('name')} "
                       f"(score {top.get('risk_score')}, {top.get('band')}) — "
                       f"{top.get('predicted_window')}. "
                       if top.get("name") else "")
                    + f"High-risk components: {pred.get('high_count', 'n/a')}, "
                    f"watch: {pred.get('watch_count', 'n/a')}.")
        elif "graph_dependents" in payload:
            gd = payload["graph_dependents"] or {}
            deps = gd.get("dependents") or []
            labels = [str(d.get("label")) for d in deps[:6]]
            text = (f"Blast radius for '{gd.get('component')}': "
                    f"{gd.get('count', len(deps))} things depend on it"
                    + (f", including {', '.join(labels)}" if labels else "")
                    + ". If it goes down, those lose their dependency — "
                    "plan maintenance accordingly.")
        elif "triage" in u or "ticket" in u:
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
        elif "create_dashboard" in payload:
            dash = payload["create_dashboard"] or {}
            if dash.get("error"):
                text = f"Dashboard build failed: {dash['error']}"
            else:
                text = (f"Dashboard '{dash.get('title')}' is live "
                        f"at {dash.get('url')} — "
                        f"{dash.get('kpi_count')} KPI cards + "
                        f"{dash.get('chart_count')} charts from "
                        f"{', '.join(dash.get('source_labels') or [])}. "
                        "Designed by Ember, rendered "
                        "deterministically by Metatron — no "
                        "Cube-side model was involved.")
        elif "convert_plc_code" in payload:
            conv = payload["convert_plc_code"] or {}
            if conv.get("error"):
                text = f"PLC conversion failed: {conv['error']}"
            else:
                st = conv.get("stats") or {}
                text = (f"Converted '{conv.get('program_name')}' "
                        f"({conv.get('direction_label')}) — "
                        f"{st.get('mapped', 0)} of "
                        f"{st.get('source_instructions', 0)} "
                        "source instructions mapped"
                        + (f"; flagged unsupported: "
                           + ", ".join(st.get("flagged") or [])
                           if st.get("flagged") else "")
                        + ". Stored as a DRAFT for "
                          "qualified-engineer review — nothing "
                          "was deployed.")
        elif "ask_medical_manual" in payload:
            med = payload["ask_medical_manual"] or {}
            if med.get("error"):
                text = f"Medical manual lookup failed: {med['error']}"
            elif not med.get("covered"):
                text = ("The Merck Manual (19th Ed.) does not "
                        "cover that question — no passages "
                        "matched, so I won't answer from general "
                        "knowledge. Try asking about a condition, "
                        "its symptoms, diagnosis, or treatment "
                        "as described in the manual.")
            else:
                # Per-chapter summary blocks (medical
                # practitioner assistant): one block per
                # relevant chapter in the result's chapter
                # order, each a short deterministic extract
                # with that chapter's citations, plus a
                # cross-chapter synthesis line when several
                # chapters apply.
                passages = med.get("passages") or []
                order = [c.get("chapter", "")
                         for c in (med.get("chapters") or [])
                         if c.get("chapter")]
                for p in passages:
                    ch = p.get("chapter") or ""
                    if ch and ch not in order:
                        order.append(ch)
                blocks = []
                for ch in order[:5]:
                    mine = [p for p in passages
                            if (p.get("chapter") or "") == ch]
                    if not mine:
                        continue
                    body = " ".join(p.get("text", "")
                                    for p in mine)
                    sents = [s.strip() for s in body.split(". ")
                             if s.strip()]
                    summ = ". ".join(sents[:2])
                    if len(summ) > 420:
                        summ = summ[:420].rsplit(" ", 1)[0]
                    if summ and not summ.endswith("."):
                        summ += "."
                    cites = []
                    for p in mine:
                        cite = ch
                        if p.get("section"):
                            cite += f" — {p['section']}"
                        if p.get("pages"):
                            cite += f", {p['pages']}"
                        if cite not in cites:
                            cites.append(cite)
                    blocks.append(
                        f"## {ch}\n{summ} "
                        f"({'; '.join(cites[:3])})")
                text = ("Per The Merck Manual (19th Ed.), by "
                        "chapter:\n\n" + "\n\n".join(blocks))
                if len(blocks) > 1:
                    text += ("\n\nAcross these chapters, the "
                             "manual covers this topic in "
                             + "; ".join(order[:5]) + ".")
                text += (f"\n\n({len(passages)} passages "
                         f"retrieved, "
                         f"~{med.get('reference_tokens_approx', 0):,} "
                         "tokens of reference.)")
        elif "ask_paralegal" in payload:
            leg = payload["ask_paralegal"] or {}
            if leg.get("error"):
                text = f"Paralegal lookup failed: {leg['error']}"
            elif not leg.get("covered"):
                text = ("These sources (U.S. Code, Federal "
                        "Rules, Constitution, CFR) do not cover "
                        "that question — no passages matched, so "
                        "I won't answer from general knowledge. "
                        "Try asking about a statute, a federal "
                        "court rule, a constitutional provision, "
                        "or a regulation.")
            else:
                # Per-source summary blocks (paralegal
                # assistant): one block per relevant
                # source/title in the result's group order,
                # each a short deterministic extract with
                # that group's citations, plus a
                # cross-source synthesis line when several
                # groups apply.
                passages = leg.get("passages") or []
                order = [g.get("group", "")
                         for g in (leg.get("groups") or [])
                         if g.get("group")]
                for p in passages:
                    gname = p.get("group") or ""
                    if gname and gname not in order:
                        order.append(gname)
                blocks = []
                for gname in order[:5]:
                    mine = [p for p in passages
                            if (p.get("group") or "") == gname]
                    if not mine:
                        continue
                    body = " ".join(p.get("text", "")
                                    for p in mine)
                    sents = [s.strip() for s in body.split(". ")
                             if s.strip()]
                    summ = ". ".join(sents[:2])
                    if len(summ) > 420:
                        summ = summ[:420].rsplit(" ", 1)[0]
                    if summ and not summ.endswith("."):
                        summ += "."
                    cites = []
                    for p in mine:
                        cite = p.get("citation") or ""
                        if cite and cite not in cites:
                            cites.append(cite)
                    blocks.append(
                        f"## {gname}\n{summ} "
                        f"({'; '.join(cites[:3])})")
                text = ("Per the US public-domain legal "
                        "sources, by source:\n\n"
                        + "\n\n".join(blocks))
                if len(blocks) > 1:
                    text += ("\n\nAcross these sources, the law "
                             "covers this topic in "
                             + "; ".join(order[:5]) + ".")
                text += (f"\n\n({len(passages)} passages "
                         f"retrieved, "
                         f"~{leg.get('reference_tokens_approx', 0):,} "
                         "tokens of reference.)")
        elif "create_agent_flow" in payload:
            agent = payload["create_agent_flow"] or {}
            if agent.get("error"):
                text = f"Agent build failed: {agent['error']}"
            else:
                acts = agent.get("actions") or [agent.get("action")]
                cond = agent.get("condition") or {}
                cond_txt = (f"; only if {cond.get('field')} "
                            f"{cond.get('op')} {cond.get('value')}"
                            if cond else "")
                text = (f"Automated agentic workflow "
                        f"'{agent.get('name')}' created "
                        f"in Metatron RPA as flow {agent.get('flow_id')} "
                        f"— status {agent.get('status')}. Trigger: "
                        f"{agent.get('trigger')}"
                        + (f" ({agent.get('cron')})" if agent.get("cron")
                           else "")
                        + f"; source: {agent.get('source_label') or agent.get('source')}; "
                        f"actions: {', '.join(a for a in acts if a)}"
                        f"{cond_txt}. It will not run until a human "
                        "enables it — use the Enable button on this "
                        "page.")
        elif "enable_agent_flow" in payload:
            en = payload["enable_agent_flow"] or {}
            if en.get("error"):
                text = f"Enable failed: {en['error']}"
            else:
                sched = (f"runs on schedule {en.get('cron')} "
                         "(America/Chicago)"
                         if en.get("cron") else "runs on its webhook")
                text = (f"Enabled ✓ — '{en.get('name')}' "
                        f"(flow {en.get('flow_id')}) is now "
                        f"{en.get('status')} and {sched}.")
        elif "export_file" in payload:
            ex = payload["export_file"] or {}
            if ex.get("error"):
                text = f"Export failed: {ex['error']}"
            elif not ex.get("exported"):
                text = ex.get("message") or "Nothing to export yet."
            else:
                filed_note = ("Filed the extraction, then exported it. "
                              if "file_intake" in payload else "")
                text = (f"{filed_note}Exported the {ex.get('source')} "
                        f"data to {ex.get('filename')} "
                        f"({str(ex.get('format') or '').upper()}, "
                        f"{ex.get('size_bytes')} bytes) — generation "
                        "was deterministic ($0). Use the Download "
                        "button on the card to save the file.")
        elif "file_intake" in payload:
            filed = payload["file_intake"] or {}
            if filed.get("error"):
                text = f"Intake filing failed: {filed['error']}"
            else:
                fields = filed.get("fields") or {}
                verdict = filed.get("totals_check") or {}
                verdict_note = (f" Totals check ({verdict.get('status')}): "
                                f"{verdict.get('detail')}"
                                if verdict.get("detail") else "")
                text = (f"Filed the {filed.get('kind')} to the intake "
                        f"board (record #{filed.get('intake_total')}): "
                        + ", ".join(f"{k}={v}" for k, v in
                                    list(fields.items())[:6])
                        + "." + verdict_note
                        + " Awaiting human review — nothing was paid "
                        "or sent.")
        elif "file_document_report" in payload:
            filed = payload["file_document_report"] or {}
            if filed.get("error"):
                text = f"Report filing failed: {filed['error']}"
            else:
                text = (f"Filed document report '{filed.get('title')}' "
                        f"with {filed.get('findings_count')} findings "
                        f"(report #{filed.get('reports_total')}). "
                        "Awaiting human review.")
        elif run:
            text = (f"Done. Watcher status: {status_line}. "
                    f"Workflow '{run.get('workflow_id')}' run {run.get('run_id')} "
                    f"finished with status {run.get('status')}. "
                    f"{run.get('note', '')}").strip()
        elif status_line:
            text = f"Watcher status: {status_line}"
        else:
            text = "Here is what the tools returned: " + json.dumps(payload)[:600]
        # Mirror the live compose contract: end with a NEXT_STEPS line
        # (the orchestrator parses + strips it). Deterministic, keyed
        # on which tools ran.
        ns = self._stub_next_steps(payload)
        if ns:
            text += "\nNEXT_STEPS: " + json.dumps(ns)
        return EmberResponse(text=text,
                             prompt_tokens=_est_tokens(prompt_text),
                             completion_tokens=_est_tokens(text))

    @staticmethod
    def _stub_next_steps(payload: dict) -> list[dict]:
        """Canned next-step suggestions for the stub, mirroring what
        live Ember is instructed to emit: 2-4 items, <=1 automate."""
        cap_agent = {"label": "Automate a daily capacity check",
                     "kind": "automate",
                     "command": "Build a new automated agentic workflow."}
        watcher_agent = {"label": "Automate the morning status",
                         "kind": "automate",
                         "command": "Build a new automated agentic workflow."}
        if "file_document_report" in payload:
            return [{"label": "Check capacity headroom", "kind": "action",
                     "command": "How is our capacity looking?"}, cap_agent]
        if "capacity_report" in payload or "predictions" in payload:
            return [{"label": "Give me the full picture", "kind": "action",
                     "command": ("Give me the full picture on the Cube: "
                                 "overall status, what breaks if the brain "
                                 "goes down, CVE exposure, failure "
                                 "predictions, and this month's costs.")},
                    cap_agent]
        if "finops_summary" in payload:
            return [{"label": "Check capacity headroom", "kind": "action",
                     "command": "How is our capacity looking?"},
                    {"label": "Automate a daily cost check",
                     "kind": "automate",
                     "command": "Build a new automated agentic workflow."}]
        if "secops_status" in payload:
            return [{"label": "What breaks if the brain goes down?",
                     "kind": "action",
                     "command": "What breaks if the brain goes down?"},
                    {"label": "Automate a daily CVE check",
                     "kind": "automate",
                     "command": "Build a new automated agentic workflow."}]
        if "file_intake" in payload and "export_file" not in payload:
            return [{"label": "Export this extraction as Excel",
                     "kind": "action",
                     "command": "Save the last extraction as Excel."},
                    watcher_agent]
        if "create_agent_flow" in payload:
            return [{"label": "List the RPA flows", "kind": "action",
                     "command": "List the Metatron RPA flows."}]
        if "create_dashboard" in payload:
            return [{"label": "List the dashboards", "kind": "action",
                     "command": "List the dashboards."},
                    cap_agent]
        if "convert_plc_code" in payload:
            return [{"label": "Export the converted code as TXT",
                     "kind": "action",
                     "command": "Save the converted PLC code as TXT."},
                    {"label": "Convert the other direction",
                     "kind": "action",
                     "command": "Convert the sample Siemens "
                                "conveyor program to Allen-Bradley."}]
        if "get_watcher_status" in payload:
            return [{"label": "Check failure predictions", "kind": "action",
                     "command": "What are the current failure predictions?"},
                    watcher_agent]
        return []
