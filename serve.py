#!/usr/bin/env python3
"""Ember Orchestrator demo service — ember.metatroncube.online (port 8023).

Public surface:
  GET  /                the demo page (demo_page.html, self-contained)
  GET  /assets/<file>   whitelisted static assets (face artwork)
  GET  /api/status      service + spend meter summary (watchdog probe)
  GET  /api/demos       recorded live-run replays ($0, public)
  GET  /api/whoami      Cube login state for the page UI
  POST /api/login       {"email": "...", "password": "..."} -> in-page
                        sign-in against the Cube interface accounts
                        (proxied to 127.0.0.1:7860; the interface's
                        Set-Cookie headers pass through verbatim, so
                        the session cookie keeps its exact attributes:
                        Domain=.metatroncube.online, HttpOnly, Path=/,
                        Max-Age=604800, SameSite=Lax, Secure). The
                        password is forwarded only — never logged or
                        stored. On the apex door the page posts to the
                        interface's own /api/auth/login instead (the
                        /ember proxy does not forward Set-Cookie).
  POST /api/logout      forward to the interface logout + clear cookies

Live surface (Cube session cookie, Operator+ role, validated against
the interface /api/auth/me on 127.0.0.1:7860):
  POST /api/propose     {"command": "..."}  -> Ember plan (metered)
  POST /api/intake      {"image_base64": "...", "media_type": "image/png"}
                        -> Ember reads the image, proposes file_intake
  POST /api/document    {"text": "...", "title": "..."}
                        -> Ember parses, proposes file_document_report
  POST /api/intake-file {"filename": "...", "media_type": "...",
                         "data_base64": "..."} -> unified multimodal
                        intake: image / PDF / audio (mp3,wav) / video
                        (mp4) / text file, routed by type to the
                        Ember input format the API accepts
  POST /api/approve     {"plan_id": "..."}  -> execute + Ember summary
                        + next_steps (Ember's suggested follow-ups from
                        the same compose call) + automate_command
                        (deterministic Agent Builder prefill) +
                        default_action (deterministic one-time
                        follow-up when Ember proposed no action)
  POST /api/reject      {"plan_id": "..."}

Ember-native Agent Builder Wizard (Nov 26, 2026 — replaces the
external Agents-app handoff as the Automate default; Ember-only AI,
deterministic assembly, no Axion anywhere in the path):
  GET  /api/agent-wizard/options  allowlisted sources/actions/
                        triggers + schedule presets (form truth)
  POST /api/agent-wizard/design   {"description": "..."} -> Ember
                        proposes create_agent_flow params (metered);
                        returns plan_id + proposed params + their
                        deterministic validation result
  POST /api/agent-wizard/template {"template_id": "..."} -> a
                        pre-built template becomes a proposed plan
                        deterministically ($0, no Ember call); same
                        response shape as design
  POST /api/agent-wizard/scratch  {"answers": {...}} -> guided-Q&A
                        chip answers assemble into params
                        deterministically ($0, no Ember call); same
                        response shape as design (Nov 27, 2026)
  POST /api/agent-wizard/validate {"params": {...}} -> deterministic
                        allowlist check only ($0, no Ember call)
  POST /api/agent-wizard/create   {"plan_id": "...", "params": {...}}
                        -> the FINAL edited params are re-validated
                        and executed through the gated approval path
                        (single-use token); DISABLED draft created

Built-automation test (Nov 26, 2026 — per-automation Test button):
  POST /api/agent-flows/test  {"flow_id": "..."} -> deterministic
                        dry-run replay from the registry params:
                        next schedule fire (America/Chicago) or a
                        simulated trigger, a live GET to the flow's
                        allowlisted source, and the action payload
                        CAPTURED — never sent, never filed, no RPA
                        run created. $0 (no Ember call), registry
                        ids only. The Built Automations panel also
                        carries a per-flow "Edit flow" link into the
                        Metatron RPA editor (editing is not an
                        approval; enable/disable stay in-screen).

Internal surface (X-API-Key == CUBE_SERVICES_API_KEY; used by
Agent-Builder flows running inside RPA, which have no session):
  POST /api/board/file  {"title": "...", "summary": "...", "source": "..."}
                        -> append to the reports board (deterministic)

MCP Bus surface (X-API-Key == CUBE_SERVICES_API_KEY; the Metatron MCP
Bus proxies its ember_* tools here — Nov 26, 2026, Direction 2):
  GET  /api/mcp/status      service + meter + discovery state
  GET  /api/mcp/automations Agent Builder registry (read-only)
  POST /api/mcp/propose     {"command": "..."} -> Ember plan,
                        PROPOSE-ONLY (metered, capped, ledger-tagged
                        "mcp-bus"). Approval/execution are NOT
                        exposed here: the single-use approval token
                        is minted only by the UI approval step, so
                        nothing on this surface can bypass human
                        approval. The service key is Operator-
                        equivalent for these routes only.

Guardrails:
  - Every live Ember call goes through the SpendMeter (gates $7/$9/$9.75).
  - A per-day live-run cap (default 100) bounds public exposure.
  - Approved live runs execute for real: UiCubeTools inherits
    HttpCubeTools._execute, so an approved run_workflow fires the
    production flow's sync webhook (the WhatsApp demo sends a real
    WhatsApp to the configured number). Changed Nov 26, 2026 at Rom's
    direction — the UI originally simulated execution, so his first
    live demo sent nothing. The Phase 2 CLI drill flag
    (spend/real-send-done.flag, consulted only by run_phase2.py) is
    separate and untouched: drills still use fixture execution after
    their one real send, UI-approved runs are always real.
  - If MODEL_API_KEY / meter rates are missing, the service runs in
    recorded-only mode and live endpoints answer 503.

Run on the Cube via /root/metatron/start-ember.sh (sources
/root/metatron/ember.env + mcp.env + ember-rates.env at runtime).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, ROOT)

from orchestrator.agent_builder import (ACTIONS, AUTONOMY_LABELS,
                                        AUTONOMY_LEVELS,
                                        CONDITION_FIELDS,
                                        CONDITION_OPS,
                                        SCRATCH_ACTION_TOOLS,
                                        SCRATCH_BUILDER_TOOL,
                                        SCRATCH_CONDITION_SUGGESTIONS,
                                        SCRATCH_QUESTIONS,
                                        SCRATCH_SOURCES,
                                        SOURCES, TEMPLATES, TRIGGERS,
                                        chain_summary,
                                        compose_report_text,
                                        condition_label,
                                        evaluate_condition,
                                        next_cron_fire,
                                        scratch_params_from_answers,
                                        template_by_id,
                                        validate_agent_params)
from orchestrator.cube_tools import (GATED_TOOLS, ApprovalTokens,
                                     HttpCubeTools, ReportStore,
                                     load_catalog)
from orchestrator.dashboard_builder import (SOURCES as DASH_SOURCES,
                                            validate_dashboard_params)
from orchestrator.plc_converter import (DIRECTION_LABELS,
                                        PLATFORM_LABELS, SAMPLES,
                                        SOURCE_PLATFORM,
                                        TARGET_PLATFORM,
                                        load_manual_context,
                                        parse_source_instructions,
                                        validate_conversion)
from orchestrator.ember_client import MetaEmberClient, NotConfiguredError
from orchestrator.orchestrator import Orchestrator
from orchestrator.spend import BudgetHalt, BudgetStop, SpendMeter

PORT = int(os.environ.get("EMBER_UI_PORT", "8023"))
AUTH_ME_URL = os.environ.get("EMBER_AUTH_ME_URL",
                             "http://127.0.0.1:7860/api/auth/me")
AUTH_LOGIN_URL = os.environ.get("EMBER_AUTH_LOGIN_URL",
                                "http://127.0.0.1:7860/api/auth/login")
AUTH_LOGOUT_URL = os.environ.get("EMBER_AUTH_LOGOUT_URL",
                                 "http://127.0.0.1:7860/api/auth/logout")
# The interface's own clear-cookie pair (server.js handleAuthLogout),
# synthesized locally only if the interface is unreachable at logout.
CLEAR_COOKIES = [
    "cube_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax; Secure",
    "cube_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax; Secure; "
    "Domain=.metatroncube.online",
]
# Metatron RPA editor link (Built Automations "Edit flow"). The URL
# pattern mirrors the Agents service's proven admin_url construction
# (/opt/cube/agents/serve.py): <base>/projects/<project>/flows/<id>.
# The project id default matches HttpCubeTools.ap_project (the
# project builder-created flows live in).
RPA_PUBLIC_BASE = os.environ.get("RPA_PUBLIC_BASE",
                                 "https://rpa.metatroncube.online")
AP_PROJECT_ID = os.environ.get("AP_PROJECT_ID", "R4KLbLXTv040pofVBOvkz")


def rpa_edit_url(flow_id: str) -> str:
    return (f"{RPA_PUBLIC_BASE.rstrip('/')}/projects/{AP_PROJECT_ID}"
            f"/flows/{flow_id}")


def _fetch_source_json(url: str, timeout: int = 15):
    """Live GET to an allowlisted Cube read endpoint (dry-run Test).
    Returns parsed JSON, or the raw text if it is not JSON. Read-only
    by construction — the allowlist in agent_builder.SOURCES holds
    GET endpoints only."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "ember-orchestrator-test/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(1_000_000).decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


ROLE_RANK = {"Viewer": 1, "Operator": 2, "Engineer": 3, "Admin": 4}
MIN_LIVE_RANK = ROLE_RANK["Operator"]
DAILY_CAP = int(os.environ.get("EMBER_UI_DAILY_CAP", "1000"))
CAP_PATH = os.environ.get(
    "EMBER_UI_CAP_PATH", os.path.join(ROOT, "spend", "ui-live-runs.json"))
RECORDED_PATH = os.environ.get(
    "EMBER_RECORDED_PATH", os.path.join(ROOT, "demos", "recorded.json"))
PAGE_PATH = os.path.join(ROOT, "demo_page.html")
ASSETS_DIR = os.path.join(ROOT, "assets")
ASSET_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".svg": "image/svg+xml",
               ".ico": "image/x-icon"}
CAP_USD = 10.0
INTAKE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
INTAKE_MAX_BYTES = 5_000_000          # decoded image cap (5 MB)
DOCUMENT_MAX_CHARS = 200_000          # ~50K tokens of document text
PDF_MAX_BYTES = 10_000_000            # decoded PDF cap (10 MB)
AUDIO_MAX_BYTES = 10_000_000          # decoded audio cap (10 MB)
VIDEO_MAX_BYTES = 20_000_000          # decoded video cap (20 MB)

# Unified multimodal intake (Nov 26, 2026): one endpoint routes by
# media type to the Ember input format the Meta Model API accepts on
# Chat Completions (probed live Nov 26: file / input_audio /
# video_url-object all verified with muse-ember-1.0). The API accepts
# MP4 video and MP3/WAV audio only — m4a/mov/webm get a clear error.
INTAKE_FILE_TYPES = {
    "image/png": ("image", INTAKE_MAX_BYTES),
    "image/jpeg": ("image", INTAKE_MAX_BYTES),
    "image/webp": ("image", INTAKE_MAX_BYTES),
    "image/gif": ("image", INTAKE_MAX_BYTES),
    "application/pdf": ("pdf", PDF_MAX_BYTES),
    "audio/mpeg": ("audio", AUDIO_MAX_BYTES),
    "audio/wav": ("audio", AUDIO_MAX_BYTES),
    "audio/x-wav": ("audio", AUDIO_MAX_BYTES),
    "video/mp4": ("video", VIDEO_MAX_BYTES),
    "text/plain": ("text", None),
    "application/json": ("text", None),
}
INTAKE_EXT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
    ".pdf": "application/pdf", ".mp3": "audio/mpeg",
    ".wav": "audio/wav", ".mp4": "video/mp4",
    ".txt": "text/plain", ".log": "text/plain", ".md": "text/plain",
    ".json": "application/json",
}
INTAKE_SUPPORTED_MSG = (
    "supported: images (PNG/JPEG/WebP/GIF), PDF, audio (MP3/WAV), "
    "video (MP4), text files (.txt/.log/.md/.json)")

# Ember-native Agent Builder Wizard form truth. The allowlists come
# from agent_builder.py (single source); only display labels and the
# schedule presets live here.
WIZARD_ACTION_LABELS = {"whatsapp": "WhatsApp status",
                        "board": "File to board"}
WIZARD_ACTION_HINTS = {
    "whatsapp": "Send the composed report to your WhatsApp via "
                "the proven Watcher flow.",
    "board": "File the composed report to the triage board on "
             "this page.",
}
WIZARD_TRIGGER_LABELS = {"schedule": "Schedule", "webhook": "Webhook"}
WIZARD_TRIGGER_HINTS = {
    "schedule": "Runs on a timer (America/Chicago) — the usual "
                "choice, e.g. a 6 AM morning digest.",
    "webhook": "Runs when something calls its URL — advanced; "
               "pick Schedule unless another system triggers it.",
}
SCHEDULE_PRESETS = [
    {"id": "hourly", "label": "Hourly", "cron": "0 * * * *"},
    {"id": "daily-6", "label": "Daily 06:00", "cron": "0 6 * * *"},
    {"id": "daily-7", "label": "Daily 07:00", "cron": "0 7 * * *"},
    {"id": "weekdays-6", "label": "Weekdays 06:00",
     "cron": "0 6 * * 1-5"},
    {"id": "weekly-mon-6", "label": "Weekly Mon 06:00",
     "cron": "0 6 * * 1"},
]

# "Next steps" stage: the approve response carries Ember's suggested
# next steps (parsed from the compose-final call by the orchestrator)
# plus a deterministic Automate prefill derived from the tools that
# ran — so the page's Automate button always works even when Ember's
# suggestion block is absent or malformed. The prefill is a UI
# affordance, not Ember output; it goes through the normal propose
# path (Ember plans it, a human approves) like any other command.
# Rom (Nov 26): the prefill is the bare phrase — no context tail.
# The wizard designs the specifics from the run context (chips +
# server-side context assembly), so every tool maps to the same
# command.
_AUTOMATE_PHRASE = "Build a new automated agentic workflow."
_AUTOMATE_BY_TOOL = [
    ("capacity_report", _AUTOMATE_PHRASE),
    ("predictions", _AUTOMATE_PHRASE),
    ("file_document_report", _AUTOMATE_PHRASE),
    ("secops_status", _AUTOMATE_PHRASE),
    ("finops_summary", _AUTOMATE_PHRASE),
    ("file_intake", _AUTOMATE_PHRASE),
    ("get_watcher_status", _AUTOMATE_PHRASE),
    ("cube_status", _AUTOMATE_PHRASE),
]
_AUTOMATE_DEFAULT = _AUTOMATE_PHRASE


def derive_automate_command(trace: list[dict]) -> str:
    """Deterministic Agent Builder prefill from the tools a run used."""
    tools = [t.get("tool", "") for t in (trace or [])]
    for tool, command in _AUTOMATE_BY_TOOL:
        if tool in tools:
            return command
    return _AUTOMATE_DEFAULT


# Deterministic one-time "Take action" default for the NEXT STEPS
# beat, used only when Ember proposed no kind=action suggestion.
# Keyed on the tools that ran (first match wins, table order), and
# mirroring the recordings' hand-authored follow-ups — every command
# is answerable by real catalog tools and still goes through the
# normal propose -> approve -> execute loop. None = no sensible
# default (e.g. a refusal, which ran nothing).
_DEFAULT_ACTION_BY_TOOL = [
    ("ask_paralegal",
     {"label": "Export this answer as TXT",
      "command": "Save the legal answer as TXT."}),
    ("ask_medical_manual",
     {"label": "Export this answer as TXT",
      "command": "Save the medical answer as TXT."}),
    ("convert_plc_code",
     {"label": "Export the converted code as TXT",
      "command": "Save the converted PLC code as TXT."}),
    ("create_dashboard",
     {"label": "List the dashboards",
      "command": "List the dashboards."}),
    ("run_workflow",
     {"label": "Check failure predictions",
      "command": "What are the current failure predictions?"}),
    ("get_watcher_status",
     {"label": "Send the status to WhatsApp",
      "command": "Send the Watcher status to my WhatsApp."}),
    ("cube_status",
     {"label": "Send the status to WhatsApp",
      "command": "Send the Watcher status to my WhatsApp."}),
    ("file_tickets",
     {"label": "List current ITSM tickets",
      "command": "List the current ITSM tickets."}),
    ("file_document_report",
     {"label": "Check capacity headroom",
      "command": "How is our capacity looking?"}),
    ("file_intake",
     {"label": "Export this extraction as Excel",
      "command": "Save the last extraction as Excel."}),
    ("finops_summary",
     {"label": "Check capacity headroom",
      "command": "How is our capacity looking?"}),
    ("secops_status",
     {"label": "What breaks if the brain goes down?",
      "command": "What breaks if the brain goes down?"}),
    ("graph_dependents",
     {"label": "Check the CVE posture",
      "command": "Any critical CVEs we should worry about?"}),
    ("capacity_report",
     {"label": "Give me the full picture",
      "command": "Give me the full picture on the Cube: overall status, "
                 "what breaks if the brain goes down, CVE exposure, "
                 "failure predictions, and this month's costs."}),
    ("predictions",
     {"label": "Give me the full picture",
      "command": "Give me the full picture on the Cube: overall status, "
                 "what breaks if the brain goes down, CVE exposure, "
                 "failure predictions, and this month's costs."}),
    ("create_agent_flow",
     {"label": "List the RPA flows",
      "command": "List the Metatron RPA flows."}),
    ("enable_agent_flow",
     {"label": "List the RPA flows",
      "command": "List the Metatron RPA flows."}),
]


def derive_default_action(trace: list[dict]) -> dict | None:
    """Deterministic one-time follow-up from the tools a run used,
    or None when nothing sensible follows (empty/unknown trace)."""
    tools = [t.get("tool", "") for t in (trace or [])]
    for tool, action in _DEFAULT_ACTION_BY_TOOL:
        if tool in tools:
            return dict(action)
    return None


# Medical Manual Q&A (Nov 27, 2026): every answer that used
# ask_medical_manual carries this disclaimer, appended HERE by
# deterministic code — never left to the model. The tool
# answers "what does the manual say about X" from retrieved
# passages only; it is a demo reference, not medical advice.
MEDICAL_DISCLAIMER = (
    "\n\n⚕️ Demo — information from The Merck Manual of "
    "Diagnosis & Therapy (19th Ed.) only. Not medical advice; "
    "consult a qualified professional.")


def _with_medical_disclaimer(done: dict) -> str:
    """final_text with the medical disclaimer appended when
    the run used ask_medical_manual (idempotent)."""
    text = str(done.get("final_text") or "")
    tools = [t.get("tool", "") for t in (done.get("trace") or [])]
    if "ask_medical_manual" in tools \
            and "Not medical advice" not in text:
        text += MEDICAL_DISCLAIMER
    return text


# Paralegal Assistant (Nov 27, 2026): every answer that used
# ask_paralegal carries this disclaimer, appended HERE by
# deterministic code — never left to the model. The tool
# answers "what does the law say about X" from retrieved
# passages only; it is legal information, not legal advice.
LEGAL_DISCLAIMER = (
    "\n\n⚖️ Demo — legal information from US public-domain "
    "sources (U.S. Code, Federal Rules, US Constitution, "
    "CFR) only. Not legal advice; consult a licensed "
    "attorney.")


def _with_legal_disclaimer(done: dict) -> str:
    """final_text with the legal disclaimer appended when
    the run used ask_paralegal (idempotent)."""
    text = str(done.get("final_text") or "")
    tools = [t.get("tool", "") for t in (done.get("trace") or [])]
    if "ask_paralegal" in tools \
            and "Not legal advice" not in text:
        text += LEGAL_DISCLAIMER
    return text


def _with_disclaimers(done: dict) -> str:
    """final_text with the medical and/or legal disclaimers
    appended deterministically (idempotent)."""
    text = _with_medical_disclaimer(done)
    return _with_legal_disclaimer(
        {**done, "final_text": text})



# ── Capability table (Nov 26, 2026, Rom) ───────────────────────────
# "What can you do?" used to fall through to Ember, which refused
# ("No tool fits that request"). Instead, capability questions are
# answered deterministically from the tool catalog — $0, no Ember
# call, no daily-cap consumption. Detection is deliberately tight:
# the whole normalized command must be a capability ask, so
# "What can you do about the disk?" still proposes normally and
# out-of-scope requests still refuse exactly as before.
_CAPABILITY_ASKS = {
    "help", "menu", "capabilities",
    "what can you do", "what do you do", "what are you able to do",
    "what can you do for me", "what can you help with",
    "what are your capabilities", "what tools do you have",
    "what functions do you have", "list your tools",
    "list your functions", "show your tools", "show your capabilities",
    "list capabilities", "what can ember do", "show me what you can do",
}


def is_capability_question(command: str) -> bool:
    """True only when the whole command is a capability ask."""
    c = re.sub(r"\s+", " ", str(command or "").strip().lower())
    c = c.rstrip("?.!").strip()
    if c.startswith("please "):
        c = c[len("please "):].strip()
    return c in _CAPABILITY_ASKS


_AGENT_BUILD_MARKERS = ("agentic workflow", "create an agent",
                        "build an agent", "build me an agent",
                        "create an automation", "build an automation",
                        "build me an automation", "automate this")


_AGENT_SPECIFIC_WORDS = (
    "watcher", "capacity", "disk", "cost", "finops", "budget",
    "cve", "secops", "security", "prediction", "failure", "bid",
    "dot", "whatsapp", "board", "daily", "morning", "hourly",
    "weekly", "weekday", "every", "alert", "only if")


def _agent_specifics(command: str) -> bool:
    """True when an agent-build command names anything derivable
    (a source, a time, or an action) — then Ember derives the
    rest and no clarifying question is asked."""
    c = str(command or "").lower()
    if re.search(r"\bat\s+\d{1,2}\b", c):
        return True
    return any(w in c for w in _AGENT_SPECIFIC_WORDS)


def is_agent_build_command(command: str) -> bool:
    """True when the command asks to build an automated agentic
    workflow / agent / automation. Such commands (notably the bare
    "Build a new automated agentic workflow." prefill) carry no
    name/trigger/source, so the propose path appends the run
    context and Ember derives the parameters from it — mirroring
    the wizard design path (Rom's refusal, Nov 26, 2026)."""
    c = re.sub(r"\s+", " ", str(command or "").strip().lower())
    if any(m in c for m in _AGENT_BUILD_MARKERS):
        return True
    return "agent" in c and "build" in c


# ------------------------------------------------ Dashboard Builder
_DASHBOARD_BUILD_MARKERS = (
    "build a dashboard", "create a dashboard", "make a dashboard",
    "design a dashboard", "new dashboard", "dashboard showing",
    "dashboard for",
)


def is_dashboard_build_command(command: str) -> bool:
    """True only for build intents — 'List the dashboards' and
    other reads must NOT be hijacked into the dashboard path."""
    c = re.sub(r"\s+", " ", str(command or "").strip().lower())
    if "dashboard" not in c:
        return False
    if any(m in c for m in _DASHBOARD_BUILD_MARKERS):
        return True
    if ("build" in c or "create" in c or "make" in c) \
            and "dashboard" in c:
        return True
    return False


_DASHBOARD_SOURCE_KEYWORDS = (
    ("capacity", ("capacity", "disk", "headroom", "saturation",
                  "forecast")),
    ("finops", ("finops", "cost", "budget", "spend", "spending")),
    ("watcher-overview", ("watcher", "estate", "health", "uptime")),
    ("predictions", ("prediction", "failure", "risk", "fail")),
    ("sentiment", ("sentiment", "frustrated", "ticket mood")),
    ("leases", ("lease", "acquisition")),
    ("finclose", ("finclose", "month-end", "month end")),
    ("dotkrc-status", ("bid", "dot")),
    ("agents-status", ("agent status", "agents")),
)


def _dashboard_endpoints_for(command: str) -> list:
    c = str(command or "").lower()
    eps = []
    for ep, kws in _DASHBOARD_SOURCE_KEYWORDS:
        if any(k in c for k in kws):
            eps.append(ep)
    return eps[:3]


def _dashboard_profile_text(profile: dict) -> str:
    """Compact, Ember-readable rendering of a data profile."""
    parts = []
    facts = profile.get("facts") or []
    if facts:
        parts.append("Facts (name = sample value): " + ", ".join(
            f"{f.get('name')}={f.get('value')}" for f in facts))
    for lst in profile.get("lists") or []:
        cols = ", ".join(lst.get("columns") or [])
        parts.append(f"List '{lst.get('name')}' "
                     f"({lst.get('count')} records) columns: {cols}")
    return "\n".join(parts)


def _dashboard_context(orch, command: str) -> str:
    """Brief Ember with the live data profile for the dashboard
    sources the command implies, so the spec it authors references
    real fields only (Dashboard Builder, Nov 27, 2026)."""
    eps = _dashboard_endpoints_for(command)
    if not eps:
        eps = ["watcher-overview"]
    try:
        profile = orch.tools.dashboard_profile(eps)
    except Exception:  # noqa: BLE001
        return ""
    labels = ", ".join(f"{ep} ({DASH_SOURCES[ep]})" for ep in eps)
    text = (f"DASHBOARD DATA PROFILE for {labels}. Design ONE "
            "create_dashboard call. Reference ONLY these names "
            "exactly" +
            (" (multi-source: every name is already prefixed with "
             "its source)." if len(eps) > 1 else ".") +
            "\n" + _dashboard_profile_text(profile))
    return text[:3500]


# ------------------------------------------------ PLC Code Converter
_PLC_PLATFORM_WORDS = ("plc", "allen-bradley", "allen bradley",
                       "studio 5000", "logix", "siemens", "scl",
                       "tia portal", "s7-1200", "s7-1500")


def is_plc_convert_command(command: str) -> bool:
    """True only for PLC conversion intents — a platform word
    plus a convert/translate verb. Plain status reads that
    merely mention a PLC must NOT be hijacked."""
    c = re.sub(r"\s+", " ", str(command or "").strip().lower())
    if "convert" not in c and "translate" not in c:
        return False
    # "Save/export the converted PLC code as TXT" is an export,
    # not a conversion — don't inject 180K tokens of manuals.
    if c.startswith(("save", "export", "download")):
        return False
    return any(w in c for w in _PLC_PLATFORM_WORDS)


def _plc_direction_for(command: str) -> str:
    c = str(command or "").lower()
    if ("to allen" in c or "to studio" in c or "to ab" in c
            or "to logix" in c or "siemens to" in c
            or "scl to" in c):
        return "siemens_to_ab"
    return "ab_to_siemens"


def _plc_carries_code(command: str) -> bool:
    """Heuristic: the command itself contains program code
    (pasted), vs. asking to convert the built-in sample."""
    c = str(command or "")
    return (":=" in c or "TON(" in c.upper()
            or "END_IF" in c.upper() or "#RunDelay" in c)


def _plc_context(command: str) -> tuple[str, dict]:
    """Brief Ember for a PLC conversion: the vendor manuals as
    long context (deterministic file reads — no KB search, no
    Cube-side model) plus the sample program when the command
    carries no code of its own. Returns (context_text, stats)."""
    direction = _plc_direction_for(command)
    manuals_text, stats = load_manual_context(direction)
    stats = dict(stats)
    stats["direction"] = direction
    src_label = PLATFORM_LABELS[SOURCE_PLATFORM[direction]]
    tgt_label = PLATFORM_LABELS[TARGET_PLATFORM[direction]]
    if stats["manuals"]:
        loaded = ", ".join(m["file"] for m in stats["manuals"])
        manual_line = (
            f"Vendor instruction-reference manuals loaded as "
            f"context ({stats['approx_tokens']:,} approx tokens): "
            f"{loaded}. Convert using these references.")
    else:
        manual_line = ("No vendor manuals are loaded in the "
                       "corpus — convert from your own knowledge "
                       "and say so in notes.")
    parts = [
        f"PLC CONVERSION TASK: convert a {src_label} program to "
        f"{tgt_label} ({DIRECTION_LABELS[direction]}). "
        + manual_line,
        "Call convert_plc_code ONCE with: direction "
        f"'{direction}', program_name, source_code (the full "
        "source program), converted_code (the full converted "
        "program), coverage (one entry for EVERY instruction "
        "in the source — mapped with its target equivalent, or "
        "unsupported with a redesign note; unsupported "
        "instructions are flagged and left out of the converted "
        "code with an explanatory comment, never silently "
        "replaced), and notes. A deterministic validator checks "
        "coverage completeness and target-platform instructions "
        "before approval is offered. The result is a DRAFT for "
        "qualified-engineer review — never claim it is deployed "
        "or safety-certified.",
        "Coverage format rules (the validator is strict): each "
        "coverage entry's instruction field must be the exact "
        "source mnemonic ONLY (e.g. 'TON' — no parentheses, no "
        "prose, no combined names); one entry per instruction; "
        "the target field must be the exact target mnemonic "
        "only. converted_code must have balanced parentheses "
        "and use only target-platform instructions.",
    ]
    if not _plc_carries_code(command):
        name, sample = SAMPLES[direction]
        found = parse_source_instructions(
            sample, SOURCE_PLATFORM[direction])
        parts.append(
            f"The user asked for the built-in sample program. "
            f"Convert this program (program_name '{name}'):\n"
            + sample)
        if found:
            parts.append(
                "The source program uses these instructions — "
                "coverage must include one entry for each: "
                + ", ".join(found) + ".")
    else:
        found = parse_source_instructions(
            command, SOURCE_PLATFORM[direction])
        if found:
            parts.append(
                "The source program in the user's message uses "
                "these instructions — coverage must include one "
                "entry for each: " + ", ".join(found) + ".")
    if manuals_text:
        parts.append(manuals_text)
    return "\n\n".join(parts), stats


# Curated one-liners + example prompts per catalog tool, grouped by
# the demo-card areas. Gated (write) tools are labelled as needing
# approval; everything else is a read-only report. Any catalog tool
# missing here still appears (catalog description, IT Operations)
# so the table can never silently drop a tool.
_CAPABILITY_INFO = {
    "cube_status": ("IT Operations",
                    "Live estate status — components up/down, host vitals, open incidents (read-only).",
                    "Give me the overall Cube status."),
    "get_watcher_status": ("IT Operations",
                           "Watcher status from AIOps — components, incidents, host (read-only).",
                           "Give me the shift handover brief."),
    "run_workflow": ("IT Operations",
                     "Fire a deterministic workflow, e.g. the Watcher status to WhatsApp (needs your approval).",
                     "Send the Watcher status to my WhatsApp."),
    "get_run_status": ("IT Operations",
                       "Check the result of a workflow run (read-only).",
                       "Check the status of that workflow run."),
    "list_workflows": ("IT Operations",
                       "List the deterministic workflows Ember can call (read-only).",
                       "List the workflows you can run."),
    "brain_status": ("IT Operations",
                     "Which local brain is live — Axion or Qwen (read-only).",
                     "Which brain is live right now?"),
    "list_assets": ("IT Operations",
                    "List CMDB assets from SysOps (read-only).",
                    "List the CMDB assets."),
    "get_asset": ("IT Operations",
                  "Show one CMDB asset with its relationships (read-only).",
                  "Show me asset CI-0001."),
    "list_changes": ("IT Operations",
                     "Recent change-register entries (read-only).",
                     "Show recent changes."),
    "list_connectors": ("IT Operations",
                        "CMDB connectors configured in SysOps and their status (read-only).",
                        "Which CMDB connectors are configured?"),
    "list_dashboards": ("IT Operations",
                        "List dashboards from the Dashboard Generator (read-only).",
                        "List the dashboards."),
    "list_flows": ("IT Operations",
                   "List Metatron RPA flows with status (read-only).",
                   "List the Metatron RPA flows."),
    "graph_search": ("IT Operations",
                     "Search the knowledge graph by label (read-only).",
                     "Search the knowledge graph for the brain."),
    "list_rca": ("IT Operations",
                 "Root-cause and auto-heal records from ITSM (read-only).",
                 "List the RCA records."),
    "list_tickets": ("Help Desk",
                     "List ITSM tickets (read-only; never creates).",
                     "List the current ITSM tickets."),
    "file_tickets": ("Help Desk",
                     "File triaged tickets with draft replies to the board — nothing is sent (needs your approval).",
                     "Triage these help-desk tickets. Classify each one, draft a reply for each, and file them."),
    "sentiment_summary": ("Help Desk",
                          "Ticket sentiment — frustrated users and escalation risk (read-only).",
                          "How frustrated are our users?"),
    "finops_summary": ("Finance",
                       "Cube run costs — today, month-to-date, projected, budget % (read-only).",
                       "What is the Cube costing us this month?"),
    "finclose_summary": ("Finance",
                         "FinClose demo month-end KPIs — revenue, margins, EBITDA (read-only, simulated data).",
                         "Show the month-end close KPIs."),
    "list_leases": ("Finance",
                    "Lease & Acquisition summary — pipeline and expiring leases (read-only).",
                    "Show the lease and acquisition summary."),
    "secops_status": ("Security",
                      "CVE posture — critical/high counts, KEV exposure, top priorities (read-only; never investigates).",
                      "Any critical CVEs we should worry about?"),
    "graph_dependents": ("Risk & Impact",
                         "Blast radius — what depends on a component (read-only).",
                         "What breaks if the brain goes down?"),
    "predictions": ("Risk & Impact",
                    "Failure predictions — per-component risk scores and reasons (read-only).",
                    "What are the current failure predictions?"),
    "capacity_report": ("Capacity Planning",
                        "Capacity and headroom — host saturation and disk forecast (read-only).",
                        "How is our capacity looking?"),
    "create_agent_flow": ("Build & Automate",
                          "Build a deterministic RPA automated agentic workflow from plain words — created DISABLED (needs your approval).",
                          "Build a new automated agentic workflow."),
    "create_dashboard": ("Build & Automate",
                         "Build a live dashboard from Cube data — Ember designs it, Metatron renders it (needs your approval).",
                         "Build a dashboard showing capacity and FinOps costs."),
    "convert_plc_code": ("Build & Automate",
                         "Convert PLC code between Allen-Bradley and Siemens using vendor manuals as long context — DRAFT for engineer review, nothing deploys (needs your approval).",
                         "Convert the sample Allen-Bradley conveyor program to Siemens SCL."),
    "ask_medical_manual": ("Medical Reference",
                           "Ask The Merck Manual (19th Ed.) — answers grounded in retrieved passages with chapter/section/page citations; says so when the manual doesn't cover it. Demo reference, not medical advice (read-only).",
                           "What does the manual say about the symptoms of acute viral hepatitis?"),
    "ask_paralegal": ("Legal Reference",
                      "Ask the paralegal assistant — answers grounded in retrieved passages from the U.S. Code, Federal Rules, US Constitution, and CFR with per-section/rule citations; says so when the sources don't cover it. Legal information, not legal advice (read-only).",
                      "What does the law say about federal-question jurisdiction?"),
    "enable_agent_flow": ("Build & Automate",
                          "Publish and enable a DISABLED agent draft from the registry (needs your approval).",
                          "Enable the automation flow I just built."),
    "file_intake": ("Document Intake",
                    "File fields Ember extracted from an attached photo, PDF, audio or video (needs your approval).",
                    "Attach an invoice photo — Ember reads it and files the extracted fields."),
    "file_document_report": ("Document Intake",
                             "File a document-analysis report with line-referenced findings (needs your approval).",
                             "Paste a log file — Ember finds the anomalies and files a report."),
    "export_file": ("Document Intake",
                    "Export the last extraction, transcript, board, or document report as CSV, Excel, Word, TXT, or JSON — generated deterministically, $0 (needs your approval).",
                    "Save that transcript as a Word file."),
}
_CAPABILITY_AREA_ORDER = ["IT Operations", "Help Desk", "Finance",
                          "Security", "Risk & Impact",
                          "Capacity Planning", "Build & Automate",
                          "Document Intake", "Medical Reference",
                          "Legal Reference"]


def capabilities_payload(tools: list[dict] | None = None) -> dict:
    """The full deterministic capability table.

    `tools` is the effective Ember schema list (bus-discovered merged
    over the catalog when live); None = the static catalog. Either
    way the eligibility exclusions are already applied upstream —
    excluded bus tools never appear here."""
    if tools is None:
        tools = load_catalog()["tools"]
    by_area: dict[str, list] = {a: [] for a in _CAPABILITY_AREA_ORDER}
    for tool in tools:
        name = tool["name"]
        area, does, example = _CAPABILITY_INFO.get(
            name, ("IT Operations", tool["description"],
                   "Ask Ember to run " + name + "."))
        by_area[area].append({
            "name": name, "does": does, "example": example,
            "gated": name in GATED_TOOLS,
        })
    groups = [{"area": a, "tools": by_area[a]}
              for a in _CAPABILITY_AREA_ORDER if by_area[a]]
    return {"type": "capabilities", "groups": groups,
            "tool_count": sum(len(g["tools"]) for g in groups)}


def _services_key() -> str:
    """Cube services key (MCP Bus key) for the internal board endpoint.
    Env first, then the box env file at request time; never logged."""
    key = os.environ.get("CUBE_SERVICES_API_KEY", "")
    if not key:
        path = os.environ.get("MCP_ENV_PATH", "/root/metatron/mcp.env")
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("CUBE_SERVICES_API_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
        except OSError:
            key = ""
    return key


class UiCubeTools(HttpCubeTools):
    """Real Cube reads AND real workflow execution.

    Nov 26, 2026 (Rom's direction): an approved live run from this UI
    must actually execute — UiCubeTools previously overrode _execute
    to simulate, so Rom's first live Watcher→WhatsApp demo sent
    nothing. The override is gone; _execute is HttpCubeTools' real
    sync-webhook POST, exactly like the CLI orchestrator path that
    proved delivery on Nov 24/26. The class remains as the UI's named
    wiring point. Guardrails are unchanged: execution still requires
    an Operator+ session, a human approval (single-use token), the
    daily live-run cap, and the SpendMeter gates.

    Note: the Phase 2 drill flag (spend/real-send-done.flag) is
    consulted only by run_phase2.py when picking FixtureCubeTools vs
    HttpCubeTools for CLI drills. It is not shared with this UI path
    and was deliberately left as-is.
    """


# --------------------------------------------------------------- auth
def _fetch_me(cookie_header: str) -> dict | None:
    """Ask the Cube interface who this session cookie belongs to."""
    if not cookie_header:
        return None
    req = urllib.request.Request(
        AUTH_ME_URL, headers={"Cookie": cookie_header,
                              "User-Agent": "ember-orchestrator-ui/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            payload = json.load(r)
    except Exception:  # noqa: BLE001 — any failure = not authorized
        return None
    user = payload.get("user")
    return user if isinstance(user, dict) else None


def check_auth(cookie_header: str, fetch_me=_fetch_me) -> tuple[dict | None, int]:
    """Return (user, rank). rank 0 = anonymous / unverifiable."""
    user = fetch_me(cookie_header or "")
    if not user:
        return None, 0
    return user, ROLE_RANK.get(str(user.get("role") or ""), 0)


def _proxy_auth_post(url: str, payload: dict | None,
                     cookie_header: str = "") -> tuple[int, dict, list]:
    """POST JSON to a Cube interface auth endpoint (login/logout).

    Returns (status, json_dict, set_cookie_headers). HTTPError
    responses (401/429/500) are unwrapped the same way — the caller
    decides what passes through. Transport failures raise OSError.

    The payload may contain a password (login): it is forwarded to
    the interface over loopback only — never logged, never stored,
    never echoed in any response this service builds.
    """
    body = json.dumps(payload or {}).encode()
    headers = {"Content-Type": "application/json",
               "User-Agent": "ember-orchestrator-ui/1.0"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, e.headers, e.read()
    cookies = (hdrs.get_all("Set-Cookie") or []) if hdrs else []
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {}, cookies


# --------------------------------------------------------------- daily cap
def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def load_cap_state(path: str = CAP_PATH) -> dict:
    try:
        with open(path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if state.get("date") != _today():
        state = {"date": _today(), "count": 0}
    return state


def bump_cap(path: str = CAP_PATH) -> int:
    state = load_cap_state(path)
    state["count"] = int(state.get("count") or 0) + 1
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)
    return state["count"]


# --------------------------------------------------------------- recorded
def load_recorded(path: str = RECORDED_PATH) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("demos"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"recorded_at": None, "model": "muse-ember-1.0", "demos": []}


# --------------------------------------------------------------- service
class DemoService:
    def __init__(self, orch: Orchestrator | None, meter: SpendMeter,
                 auth_fn=check_auth, cap_path: str = CAP_PATH,
                 recorded_path: str = RECORDED_PATH,
                 daily_cap: int = DAILY_CAP, model: str = "muse-ember-1.0",
                 reports_path: str | None = None,
                 auth_post_fn=_proxy_auth_post,
                 source_fetch_fn=_fetch_source_json):
        self.orch = orch
        self.meter = meter
        self.auth_fn = auth_fn
        self.auth_post_fn = auth_post_fn
        # Dry-run Test source fetch (injectable for tests; the real
        # one GETs the allowlisted endpoint on the Cube).
        self.source_fetch_fn = source_fetch_fn
        self.cap_path = cap_path
        self.recorded_path = recorded_path
        self.daily_cap = daily_cap
        self.model = model
        self.lock = threading.Lock()
        # Scratch Q&A live values cache (60 s): current readings
        # for the suggested condition thresholds, so the chips can
        # say "now 69.9". Best-effort — a dead source just leaves
        # the static suggestion.
        self._scratch_live_cache: tuple[float, dict] = (0.0, {})
        # Board filings from Agent-Builder flows (file-based store;
        # same file the tools' ReportStore writes).
        self.reports = ReportStore(reports_path)

    # -- public reads -------------------------------------------------
    def status(self) -> dict:
        s = self.meter.summary()
        cap = load_cap_state(self.cap_path)
        return {
            "service": "metatron-ember",
            "ok": True,
            "model": self.model,
            "live": self.orch is not None,
            "meter": {
                "spent_usd": s["spent_usd"],
                "cap_usd": CAP_USD,
                "remaining_usd": round(CAP_USD - s["spent_usd"], 6),
                "calls": s["calls"],
                "by_bucket": s["by_bucket"],
                "warning": s["warning"],
                "rate_configured": s["rate_configured"],
            },
            "live_runs_today": int(cap.get("count") or 0),
            "live_run_cap": self.daily_cap,
            # Effective tool count: bus-discovered set when live
            # (merged over the catalog), static catalog otherwise.
            "tool_count": (len(self.orch.tool_schemas)
                           if self.orch is not None
                           else len(load_catalog()["tools"])),
            "mcp_bus": (self.orch.tools.discovery_state()
                        if self.orch is not None else
                        {"source": "recorded-only",
                         "discovered_tools": 0}),
        }

    def demos(self) -> dict:
        return load_recorded(self.recorded_path)

    def whoami(self, cookie: str) -> dict:
        user, rank = self.auth_fn(cookie)
        return {"user": user, "rank": rank,
                "can_run_live": bool(user) and rank >= MIN_LIVE_RANK
                and self.orch is not None}

    # -- in-page sign-in (proxied to the Cube interface) --------------
    def login(self, email: str, password: str) -> tuple[int, dict, list]:
        """Authenticate against the Cube interface accounts.

        On success the interface's Set-Cookie headers pass through
        verbatim (Domain=.metatroncube.online, HttpOnly, Path=/,
        Max-Age=604800, SameSite=Lax, Secure) and the body carries
        only {ok, email, role} — never the token. Failures pass the
        interface's status + detail through with NO cookie. The
        password is forwarded over loopback only; it is never logged
        or stored (this service logs nothing per-request).
        """
        email = str(email or "").strip()
        password = str(password or "")
        if not email or not password:
            return 400, {"error": "Email and password are required."}, []
        try:
            status, data, cookies = self.auth_post_fn(
                AUTH_LOGIN_URL, {"email": email, "password": password})
        except Exception:  # noqa: BLE001 — transport failure only
            return 502, {"error": "Cube sign-in unreachable — "
                                  "try again."}, []
        if status == 200 and data.get("outcome") == "ok":
            if not cookies:
                return 502, {"error": "Cube sign-in did not return a "
                                      "session — try again."}, []
            user = data.get("user") or {}
            return 200, {"ok": True,
                         "email": str(user.get("email") or email),
                         "role": str(user.get("role") or "")}, cookies
        detail = str(data.get("detail") or "Sign-in failed.")
        code = status if status in (401, 429, 500) else 502
        return code, {"error": detail}, []

    def logout(self, cookie: str) -> tuple[int, dict, list]:
        """End the Cube session (interface deletes the session row)
        and clear the cookie. If the interface is unreachable the
        cookie is still cleared locally with the same pair."""
        clear = list(CLEAR_COOKIES)
        try:
            _status, _data, cookies = self.auth_post_fn(
                AUTH_LOGOUT_URL, {}, cookie or "")
            if cookies:
                clear = cookies
        except Exception:  # noqa: BLE001 — clear locally regardless
            pass
        return 200, {"ok": True}, clear

    # -- live runs ----------------------------------------------------
    def _require_live(self, cookie: str) -> tuple[dict | None, tuple | None]:
        user, rank = self.auth_fn(cookie)
        if not user or rank < MIN_LIVE_RANK:
            return None, (403, {"error": "Cube login required (Operator+ role) "
                                        "for live Ember runs. Recorded "
                                        "replays are public."})
        if self.orch is None:
            return None, (503, {"error": "live Ember is not configured on "
                                        "this service (recorded-only mode)"})
        return user, None

    # -- MCP Bus service surface (Direction 2, Nov 26, 2026) ----------
    # Server-side credential path for the Metatron MCP Bus: the bus
    # authenticates with the Cube services key (X-API-Key), never a
    # user session. Scoped to propose + read routes only; approval
    # and execution stay in the Ember UI (the single-use approval
    # token is minted only by the UI approval step), so nothing here
    # can bypass human approval. Proposals made this way are metered
    # and ledger-tagged "mcp-bus" for audit.
    SERVICE_USER = {"email": "mcp-bus@metatroncube", "role": "Operator"}

    def _require_service(self, api_key: str) -> tuple | None:
        """None when the services key is valid, else an error tuple."""
        expected = _services_key()
        if not expected or not api_key or api_key != expected:
            return (401, {"error": "valid X-API-Key required"})
        if self.orch is None:
            return (503, {"error": "live Ember is not configured on "
                                   "this service (recorded-only mode)"})
        return None

    def status_via_service(self, api_key: str) -> tuple[int, dict]:
        err = self._require_service(api_key)
        if err:
            return err
        return 200, self.status()

    def automations_via_service(self, api_key: str) -> tuple[int, dict]:
        err = self._require_service(api_key)
        if err:
            return err
        return self.agent_flows("", api_key=api_key)

    def propose_via_service(self, command: str,
                            api_key: str) -> tuple[int, dict]:
        err = self._require_service(api_key)
        if err:
            return err
        return self.propose(command, "", api_key=api_key)

    def exports_list(self, cookie: str) -> tuple[int, dict]:
        """Generated export files (manifest) for the page's Exports
        panel. Operator+ only — exports carry filed data."""
        user, err = self._require_live(cookie)
        if err:
            return err
        exports = self.orch.tools.exports.list()
        return 200, {"exports": [
            {"id": e.get("id"), "filename": e.get("filename"),
             "format": e.get("format"), "source": e.get("source"),
             "created_at": e.get("created_at"),
             "size_bytes": e.get("size_bytes"),
             "download_path": f"/api/exports/{e.get('id')}"}
            for e in reversed(exports)]}

    def export_download(self, export_id: str,
                        cookie: str) -> tuple[int, object, dict]:
        """Serve one generated export file. Returns (code, payload,
        headers): payload is bytes on success, an error dict
        otherwise. Same Operator+ gate as every live route."""
        user, err = self._require_live(cookie)
        if err:
            return err[0], err[1], {}
        from orchestrator.exporter import CONTENT_TYPES
        export_id = str(export_id or "").strip()
        found = self.orch.tools.exports.read(export_id) \
            if export_id else None
        if found is None:
            return 404, {"error": f"unknown export: {export_id}"}, {}
        entry, payload = found
        headers = {
            "Content-Type": CONTENT_TYPES.get(entry["format"],
                                              "application/octet-stream"),
            "Content-Disposition":
                f'attachment; filename="{entry["filename"]}"',
        }
        return 200, payload, headers

    # -- PLC Code Converter: change approval + download -------------
    def plc_send_approval(self, conversion_id: str,
                          cookie: str) -> tuple[int, dict]:
        """Route a stored DRAFT conversion through the Metatron
        RPA change-approval flow (ITSM change record + approval
        waitpoint). Deterministic, Operator+, idempotent: a
        conversion already sent returns its existing RFC."""
        user, err = self._require_live(cookie)
        if err:
            return err
        conversion_id = str(conversion_id or "").strip()
        tools = self.orch.tools
        with self.lock:
            entry = tools.plc_conversions.get(conversion_id)
            if entry is None:
                return 404, {"error": f"unknown conversion: "
                                      f"{conversion_id}"}
            if entry.get("status") == "change_requested" \
                    and entry.get("rfc_ref"):
                return 200, {
                    "ok": True, "conversion_id": conversion_id,
                    "rfc_ref": entry["rfc_ref"],
                    "change_id": entry.get("change_id"),
                    "already_sent": True,
                    "note": "Already sent for change approval — "
                            "the RPA flow is waiting at its "
                            "approval waitpoint."}
            try:
                out = tools.send_plc_change_approval(entry)
            except Exception as e:  # noqa: BLE001
                return 502, {"error": f"change-approval routing "
                                      f"failed: {type(e).__name__}: {e}"}
            tools.plc_conversions.update(conversion_id, {
                "status": "change_requested",
                "rfc_ref": out.get("rfc_ref"),
                "change_id": out.get("change_id"),
                "approval_sent_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "approval_by": user.get("email") or "operator",
            })
        out["conversion_id"] = conversion_id
        out["audit"] = (f"plc change approval {conversion_id} "
                        f"by {user.get('email') or 'operator'} at "
                        + time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime()))
        return 200, out

    def plc_download(self, conversion_id: str,
                     cookie: str) -> tuple[int, object, dict]:
        """Download one conversion's converted code as a text
        file. Same Operator+ gate as every live route. The file
        carries the DRAFT banner — it is review material, not a
        deployable program."""
        user, err = self._require_live(cookie)
        if err:
            return err[0], err[1], {}
        conversion_id = str(conversion_id or "").strip()
        entry = self.orch.tools.plc_conversions.get(conversion_id) \
            if conversion_id else None
        if entry is None:
            return 404, {"error": f"unknown conversion: "
                                  f"{conversion_id}"}, {}
        stats = entry.get("stats") or {}
        banner = (
            "(* DRAFT PLC CONVERSION — generated by MAC "
            "(Metatron Agentic Commander) for qualified-engineer "
            "review. Not safety-certified. Do not deploy to any "
            "PLC without engineering review and change "
            "approval. *)\n"
            f"(* Program: {entry.get('program_name', '')} | "
            f"{entry.get('direction_label', '')} | "
            f"conversion {entry.get('id', '')} *)\n"
            f"(* Coverage: {stats.get('mapped', 0)} mapped, "
            f"{stats.get('unsupported', 0)} flagged unsupported"
            + (f" ({', '.join(stats.get('flagged') or [])})"
               if stats.get("flagged") else "")
            + " *)\n\n")
        payload = (banner
                   + str(entry.get("converted_code") or "")
                   ).encode()
        ext = "scl" if entry.get("direction") == "ab_to_siemens" \
            else "st"
        filename = (f"{entry.get('program_name', 'plc')}"
                    f"-converted.{ext}.txt")
        filename = re.sub(r"[^A-Za-z0-9._-]+", "-", filename)
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition":
                f'attachment; filename="{filename}"',
        }
        return 200, payload, headers

    def agent_flows(self, cookie: str,
                    api_key: str = "") -> tuple[int, dict]:
        """Builder-created automations (registry) for the page's
        drafts panel. Operator+ only — flow ids are not public.
        The MCP Bus reaches this via the services key instead of a
        session cookie (api_key path, read-only)."""
        if api_key:
            err = self._require_service(api_key)
            if err:
                return err
        else:
            user, err = self._require_live(cookie)
            if err:
                return err
        flows = self.orch.tools.agent_flows.list()
        return 200, {"flows": [
            {"id": f.get("id"), "name": f.get("name"),
             "created_at": f.get("created_at"),
             "trigger": f.get("trigger"), "cron": f.get("cron"),
             "source": f.get("source"),
             "source_label": SOURCES.get(f.get("source") or "",
                                         {}).get("label",
                                                 f.get("source")),
             "action": f.get("action"),
             "actions": f.get("actions")
             or ([f["action"]] if f.get("action") else []),
             "condition": f.get("condition"),
             "condition_label": (
                 condition_label(f.get("source") or "",
                                 f["condition"])
                 if f.get("condition") else None),
             "chain": f.get("chain"),
             "chain_summary": (chain_summary(f["chain"])
                               if f.get("chain") else None),
             "autonomy": f.get("autonomy") or "propose-only",
             "autonomy_label": AUTONOMY_LABELS.get(
                 f.get("autonomy") or "propose-only",
                 "Propose-only (default)"),
             "enabled": bool(f.get("enabled")),
             "enabled_at": f.get("enabled_at"),
             "edit_url": rpa_edit_url(f.get("id") or "")}
            for f in flows]}

    def agent_flow_test(self, flow_id: str,
                        cookie: str) -> tuple[int, dict]:
        """Dry-run Test for one builder-created automation.

        Deterministic replay from the registry params (AP 0.93 has no
        manual-run API for schedule flows, and a test must never
        create a real run anyway): trigger step (next fire in
        America/Chicago, or a simulated webhook), a LIVE GET to the
        flow's allowlisted source, then the action payload composed
        exactly as the flow would compose it and CAPTURED — never
        sent, never filed, no RPA run, no board write. $0: no Ember
        call, no daily-cap consumption. Registry ids only; works
        for DISABLED and ENABLED flows alike (test-before-enable)."""
        user, err = self._require_live(cookie)
        if err:
            return err
        flow_id = str(flow_id or "").strip()
        if not flow_id:
            return 400, {"error": "flow_id is required"}
        entry = self.orch.tools.agent_flows.get(flow_id)
        if entry is None:
            return 404, {"error": f"flow {flow_id!r} is not in the "
                                  "Agent Builder registry — only "
                                  "builder-created automations can be "
                                  "tested from here"}
        started = time.monotonic()
        steps = []
        # -- Step 1: trigger -----------------------------------------
        trigger = str(entry.get("trigger") or "")
        cron = str(entry.get("cron") or "")
        if trigger == "schedule" and cron:
            try:
                nxt = next_cron_fire(cron)
                steps.append({
                    "name": "trigger", "display": "Schedule trigger",
                    "status": "ok",
                    "detail": (f"cron {cron} (America/Chicago) — "
                               f"next fire {nxt.strftime('%a %b %d, %Y %H:%M %Z')}"),
                    "next_fire": nxt.isoformat(), "output_preview": ""})
            except Exception as e:  # noqa: BLE001
                steps.append({
                    "name": "trigger", "display": "Schedule trigger",
                    "status": "error",
                    "detail": f"could not compute next fire for cron "
                              f"{cron!r}: {e}",
                    "output_preview": ""})
        elif trigger == "webhook":
            steps.append({
                "name": "trigger", "display": "Webhook trigger",
                "status": "ok",
                "detail": "simulated trigger — the webhook was not "
                          "called",
                "output_preview": ""})
        else:
            steps.append({
                "name": "trigger", "display": "Trigger",
                "status": "error",
                "detail": f"unknown trigger {trigger!r} in the "
                          "registry entry",
                "output_preview": ""})
        # -- Chained processes (Nov 26): walk fetch -> condition ->
        #    action groups in order, capturing every action.
        chain = entry.get("chain")
        if chain:
            bodies = {}
            gate_ok = True
            current_source = None
            for st in chain:
                if st.get("kind") == "fetch":
                    current_source = st.get("source")
                    src = SOURCES.get(current_source or "")
                    if src is None:
                        steps.append({
                            "name": "fetch", "display": "Fetch source",
                            "status": "error",
                            "detail": f"source {current_source!r} "
                                      "not in the allowlist",
                            "output_preview": ""})
                        gate_ok = False
                        continue
                    try:
                        bodies[current_source] = \
                            self.source_fetch_fn(src["url"])
                        steps.append({
                            "name": "fetch",
                            "display": "Fetch " + src["label"],
                            "status": "ok",
                            "detail": f"GET {src['url']} — live "
                                      "response received",
                            "output_preview": json.dumps(
                                bodies[current_source])[:600]})
                        gate_ok = True
                    except Exception as e:  # noqa: BLE001
                        steps.append({
                            "name": "fetch",
                            "display": "Fetch " + src["label"],
                            "status": "error",
                            "detail": f"GET failed: "
                                      f"{type(e).__name__}: {e}",
                            "output_preview": ""})
                        gate_ok = False
                elif st.get("kind") == "condition":
                    cond = {k: st.get(k) for k in
                            ("field", "op", "value")}
                    label = condition_label(current_source or "",
                                            cond)
                    body = bodies.get(current_source)
                    if body is None:
                        gate_ok = False
                        steps.append({
                            "name": "condition",
                            "display": "Condition — " + label,
                            "status": "skipped",
                            "detail": "fetch failed — cannot "
                                      "evaluate",
                            "output_preview": ""})
                    else:
                        actual, passed, cond_err = \
                            evaluate_condition(body, current_source,
                                               cond)
                        gate_ok = passed and not cond_err
                        steps.append({
                            "name": "condition",
                            "display": "Condition — " + label,
                            "status": ("error" if cond_err else
                                       "ok" if passed else
                                       "skipped"),
                            "detail": (cond_err or
                                       f"{label} = {actual} — "
                                       + ("met" if passed else
                                          "NOT met; the chain "
                                          "stops here")),
                            "condition_value": actual,
                            "condition_passed": passed,
                            "output_preview": ""})
                elif st.get("kind") == "action":
                    act = st.get("action")
                    src = SOURCES.get(current_source or "")
                    body = bodies.get(current_source)
                    display = ("WhatsApp notify (via Watcher flow)"
                               if act == "whatsapp"
                               else "File report to board")
                    if not gate_ok or body is None or src is None:
                        steps.append({
                            "name": "action", "display": display,
                            "status": "skipped",
                            "detail": "earlier step failed or "
                                      "condition not met — "
                                      "nothing sent or filed",
                            "output_preview": ""})
                    else:
                        payload = {
                            "agent": entry.get("name"),
                            "source": current_source,
                            "summary": compose_report_text(
                                src["label"], body,
                                source=current_source)}
                        steps.append({
                            "name": "action", "display": display,
                            "status": "captured",
                            "detail": "CAPTURED — NOT SENT/FILED "
                                      "(dry-run).",
                            "output_preview": json.dumps(
                                payload, indent=1)[:1200],
                            "captured_payload": payload})
            return 200, {
                "ok": True, "mode": "dry-test",
                "flow": {"id": flow_id, "name": entry.get("name"),
                         "enabled": bool(entry.get("enabled")),
                         "trigger": trigger, "cron": cron or None,
                         "source": entry.get("source"),
                         "action": entry.get("action"),
                         "actions": entry.get("actions"),
                         "condition": entry.get("condition"),
                         "chain": chain,
                         "autonomy": entry.get("autonomy")
                         or "propose-only"},
                "steps": steps,
                "duration_ms": int(
                    (time.monotonic() - started) * 1000),
                "note": "Test run — nothing was sent or filed.",
            }
        # -- Step 2: source (live GET, allowlist only) ----------------
        source = str(entry.get("source") or "")
        src = SOURCES.get(source)
        body = None
        source_ok = False
        if src is None:
            steps.append({
                "name": "fetch", "display": "Fetch source",
                "status": "error",
                "detail": f"source {source!r} is not in the builder "
                          "allowlist — nothing fetched",
                "output_preview": ""})
        else:
            try:
                body = self.source_fetch_fn(src["url"])
                source_ok = True
                preview = (body[:600] if isinstance(body, str)
                           else json.dumps(body)[:600])
                steps.append({
                    "name": "fetch", "display": "Fetch " + src["label"],
                    "status": "ok",
                    "detail": f"GET {src['url']} — live response "
                              "received",
                    "output_preview": preview})
            except Exception as e:  # noqa: BLE001 — shown, not hidden
                steps.append({
                    "name": "fetch", "display": "Fetch " + src["label"],
                    "status": "error",
                    "detail": f"GET {src['url']} failed: "
                              f"{type(e).__name__}: {e}",
                    "output_preview": ""})
        # -- Step 3: condition gate (when the process has one) --------
        # Mirrors the flow's compose-step gate: evaluated against the
        # LIVE fetched body; a failed gate means the real run stops
        # before any action step.
        condition = entry.get("condition")
        gate_passed = True
        if condition:
            label = condition_label(source, condition) \
                if src is not None else str(condition)
            if not source_ok:
                gate_passed = False
                steps.append({
                    "name": "condition",
                    "display": "Condition — " + label,
                    "status": "skipped",
                    "detail": "source fetch failed — the condition "
                              "cannot be evaluated",
                    "output_preview": ""})
            else:
                actual, passed, cond_err = evaluate_condition(
                    body, source, condition)
                gate_passed = passed and not cond_err
                if cond_err:
                    steps.append({
                        "name": "condition",
                        "display": "Condition — " + label,
                        "status": "error",
                        "detail": cond_err + " (in a real run the "
                                  "flow stops at the condition "
                                  "gate)",
                        "output_preview": ""})
                else:
                    steps.append({
                        "name": "condition",
                        "display": "Condition — " + label,
                        "status": "ok" if passed else "skipped",
                        "detail": (
                            f"{label} = {actual} — "
                            + ("met; the action step(s) run"
                               if passed else
                               "NOT met; in a real run the flow "
                               "stops at the condition gate and "
                               "nothing is sent or filed")),
                        "condition_value": actual,
                        "condition_passed": passed,
                        "output_preview": ""})
        # -- Step 4+: action(s) (composed + captured, never executed) -
        actions = entry.get("actions") or (
            [entry["action"]] if entry.get("action") else [])
        action = str(entry.get("action") or "+".join(actions))
        if not source_ok:
            steps.append({
                "name": "action", "display": "Action",
                "status": "skipped",
                "detail": "source fetch failed — in a real run the "
                          "flow would stop at the fetch step",
                "output_preview": ""})
        elif condition and not gate_passed:
            for act in actions:
                steps.append({
                    "name": "action",
                    "display": ("WhatsApp notify (via Watcher flow)"
                                if act == "whatsapp"
                                else "File report to board"),
                    "status": "skipped",
                    "detail": "condition not met — in a real run "
                              "the flow stops at the condition "
                              "gate; nothing was sent or filed",
                    "output_preview": ""})
        else:
            for act in actions:
                if act == "whatsapp":
                    payload = {
                        "agent": entry.get("name"), "source": source,
                        "summary": compose_report_text(
                            src["label"], body, source=source,
                            condition=condition)}
                    steps.append({
                        "name": "action",
                        "display": "WhatsApp notify (via Watcher "
                                   "flow)",
                        "status": "captured",
                        "detail": "CAPTURED — NOT SENT. In a real "
                                  "run this payload POSTs to the "
                                  "Watcher WhatsApp flow's sync "
                                  "webhook.",
                        "output_preview":
                            json.dumps(payload, indent=1)[:1200],
                        "captured_payload": payload})
                elif act == "board":
                    payload = {
                        "title": entry.get("name"), "source": source,
                        "summary": compose_report_text(
                            src["label"], body, source=source,
                            condition=condition)}
                    steps.append({
                        "name": "action",
                        "display": "File report to board",
                        "status": "captured",
                        "detail": "CAPTURED — NOT FILED. In a real "
                                  "run this payload POSTs to the "
                                  "Ember board endpoint.",
                        "output_preview":
                            json.dumps(payload, indent=1)[:1200],
                        "captured_payload": payload})
                else:
                    steps.append({
                        "name": "action", "display": "Action",
                        "status": "error",
                        "detail": f"action {act!r} is not in the "
                                  "builder allowlist — nothing "
                                  "composed",
                        "output_preview": ""})
            if not actions:
                steps.append({
                    "name": "action", "display": "Action",
                    "status": "error",
                    "detail": "no actions in the registry entry — "
                              "nothing composed",
                    "output_preview": ""})
        return 200, {
            "ok": True, "mode": "dry-test",
            "flow": {"id": flow_id, "name": entry.get("name"),
                     "enabled": bool(entry.get("enabled")),
                     "trigger": trigger, "cron": cron or None,
                     "source": source, "action": action,
                     "actions": actions, "condition": condition},
            "steps": steps,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "note": "Test run — nothing was sent or filed.",
        }

    def _run_delta(self, before_calls: int, before_cost: float) -> dict:
        new = self.meter.ledger[before_calls:]
        return {
            "tokens_in": sum(e["prompt_tokens"] for e in new),
            "tokens_out": sum(e["completion_tokens"] for e in new),
            "calls": len(new),
            "cost_usd": round(self.meter.spent_usd - before_cost, 6),
        }

    def _dashboard_plan_error(self, plan: dict) -> str | None:
        """Deterministic spec gate (Dashboard Builder): validate
        every create_dashboard step against the live data profile.
        Returns the validator's teaching error or None. $0."""
        for step in plan.get("steps") or []:
            if step.get("name") != "create_dashboard":
                continue
            args = step.get("arguments") or {}
            eps = args.get("endpoints")
            if isinstance(eps, str):
                eps = [eps]
            try:
                if not eps:
                    validate_dashboard_params(args, {})
                profile = self.orch.tools.dashboard_profile(eps)
                validate_dashboard_params(args, profile)
            except ValueError as e:
                return str(e)
            except Exception as e:  # noqa: BLE001
                return f"data profile fetch failed: {e}"
        return None

    def _plc_plan_error(self, plan: dict) -> str | None:
        """Deterministic coverage gate (PLC Code Converter):
        validate every convert_plc_code step — coverage must
        account for every source instruction and the converted
        code must use target-platform forms. Returns the
        validator's teaching error or None. $0."""
        for step in plan.get("steps") or []:
            if step.get("name") != "convert_plc_code":
                continue
            try:
                validate_conversion(step.get("arguments") or {})
            except ValueError as e:
                return str(e)
        return None

    def propose(self, command: str, cookie: str,
                api_key: str = "") -> tuple[int, dict]:
        # Two auth paths, same propose body: a Cube session cookie
        # (Operator+, the UI) or the Cube services key (the MCP Bus,
        # Direction 2). The bus path is ledger-tagged "mcp-bus" and
        # can only propose — approval stays in the UI.
        via = "ui-live"
        if api_key:
            err = self._require_service(api_key)
            if err:
                return err
            via = "mcp-bus"
        else:
            user, err = self._require_live(cookie)
            if err:
                return err
        command = str(command or "").strip()
        if not command:
            return 400, {"error": "command is required"}
        # PLC conversions may paste a whole program into the
        # command; everything else stays at the 4000-char cap.
        cmd_limit = 20000 if is_plc_convert_command(command) else 4000
        if len(command) > cmd_limit:
            return 400, {"error": f"command too long "
                                  f"(max {cmd_limit} chars)"}
        # Capability questions are answered deterministically from
        # the effective tool set — no Ember call, no daily-cap
        # consumption.
        if is_capability_question(command):
            payload = capabilities_payload(self.orch.tool_schemas)
            payload["run"] = {"tokens_in": 0, "tokens_out": 0,
                              "calls": 0, "cost_usd": 0.0}
            payload["meter"] = self.status()["meter"]
            return 200, payload
        cap = load_cap_state(self.cap_path)
        if int(cap.get("count") or 0) >= self.daily_cap:
            return 429, {"error": f"daily live-run cap reached "
                                  f"({self.daily_cap}/day) — recorded "
                                  f"replays remain available"}
        # Agent-build commands (notably the bare "Build a new
        # automated agentic workflow." prefill) carry no
        # name/trigger/source — append the run context (same
        # assembly as the wizard design path) so Ember derives the
        # parameters from the last run instead of refusing.
        context = ""
        if is_agent_build_command(command):
            context = self._wizard_context()["text"]
            # Clarifying-question guard (Nov 26): only ask when a
            # required field genuinely cannot be derived — an
            # agent-build command with NO run context and NO
            # specifics (no source, time, or action words). $0.
            if ("tools used:" not in context
                    and not _agent_specifics(command)
                    and command.strip().lower().rstrip(".")
                    != "build a new automated agentic workflow"):
                return 200, {
                    "id": None, "steps": [], "status": "clarifying",
                    "executed": False, "via": via,
                    "refusal": False, "confidence": "low",
                    "clarifying_question": {
                        "question": "What should the automation "
                                    "check?",
                        "options": ["Watcher status", "Capacity",
                                    "FinOps costs"]},
                    "rationale": "I can build that — first, what "
                                 "should it check? Tap an answer "
                                 "or type more detail.",
                    "run": {"tokens_in": 0, "tokens_out": 0,
                            "calls": 0, "cost_usd": 0.0},
                    "meter": self.status()["meter"],
                }
        # Dashboard-build commands: Ember authors the spec against
        # the live data profile for the implied sources, so the
        # deterministic validator can check every field (Nov 27).
        dash_context = ""
        if is_dashboard_build_command(command):
            dash_context = _dashboard_context(self.orch, command)
            if not context:
                context = dash_context
        # PLC conversion commands: Ember converts with the vendor
        # manuals as long context (deterministic file reads) and
        # authors the converted code + coverage report at propose
        # time, so the human approves the actual code (Nov 27).
        plc = is_plc_convert_command(command)
        plc_context, plc_stats = "", None
        if plc:
            plc_context, plc_stats = _plc_context(command)
            if not context:
                context = plc_context
        with self.lock:
            before_calls = len(self.meter.ledger)
            before_cost = self.meter.spent_usd
            try:
                if plc:
                    # propose_content scrubs the long manuals body
                    # from the stored conversation so the approve
                    # compose turn does not re-send (and re-pay
                    # for) ~180K tokens of reference text.
                    plan = self.orch.propose_content(
                        command + "\n\n" + context, command,
                        purpose="integration", note_suffix=via,
                        max_tokens=8192)
                    plan["plc_manual_stats"] = plc_stats
                else:
                    plan = self.orch.propose(
                        command, purpose="integration",
                        note_suffix=via, context=context)
            except (BudgetHalt, BudgetStop) as e:
                return 429, {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                return 502, {"error": f"Ember call failed: {type(e).__name__}: {e}"}
            bump_cap(self.cap_path)
            # Deterministic spec gate: a create_dashboard step that
            # fails validation never reaches the approval screen.
            # One repair retry with the validator's teaching error.
            dash_err = self._dashboard_plan_error(plan)
            if dash_err:
                self.orch.reject(plan["id"])
                repair = ((dash_context + "\n\n") if dash_context
                          else "") + (
                    "Your previous dashboard spec was rejected by "
                    "the deterministic validator: " + dash_err +
                    " Fix exactly that and propose the corrected "
                    "create_dashboard call. Reference ONLY names "
                    "in the data profile.")
                try:
                    plan = self.orch.propose(
                        command, purpose="integration",
                        note_suffix=via, context=repair)
                except (BudgetHalt, BudgetStop) as e:
                    return 429, {"error": str(e)}
                except Exception as e:  # noqa: BLE001
                    return 502, {"error": f"Ember call failed: {type(e).__name__}: {e}"}
                dash_err = self._dashboard_plan_error(plan)
                if dash_err:
                    self.orch.reject(plan["id"])
                    return 422, {"error":
                                 "Ember's dashboard spec did not "
                                 "pass the deterministic validator: "
                                 + dash_err + " Nothing was "
                                 "created — try asking again with "
                                 "the data source named."}
            # Deterministic coverage gate: a convert_plc_code
            # step whose coverage report is incomplete (or whose
            # code still speaks the source language) never
            # reaches the approval screen. One repair retry.
            plc_err = self._plc_plan_error(plan)
            if plc_err:
                self.orch.reject(plan["id"])
                repair = ((plc_context + "\n\n") if plc_context
                          else "") + (
                    "Your previous PLC conversion was rejected by "
                    "the deterministic validator: " + plc_err +
                    " Fix exactly that and propose the corrected "
                    "convert_plc_code call with the full converted "
                    "code and a complete coverage report.")
                try:
                    plan = self.orch.propose_content(
                        command + "\n\n" + repair, command,
                        purpose="integration", note_suffix=via,
                        max_tokens=8192)
                    plan["plc_manual_stats"] = plc_stats
                except (BudgetHalt, BudgetStop) as e:
                    return 429, {"error": str(e)}
                except Exception as e:  # noqa: BLE001
                    return 502, {"error": f"Ember call failed: {type(e).__name__}: {e}"}
                plc_err = self._plc_plan_error(plan)
                if plc_err:
                    self.orch.reject(plan["id"])
                    return 422, {"error":
                                 "Ember's PLC conversion did not "
                                 "pass the deterministic coverage "
                                 "validator: " + plc_err +
                                 " Nothing was stored — try "
                                 "asking again."}
            delta = self._run_delta(before_calls, before_cost)
        return 200, {
            "id": plan["id"],
            "rationale": plan["rationale"],
            "steps": [{"name": s["name"], "arguments": s["arguments"]}
                      for s in plan["steps"]],
            "status": plan["status"],
            # A proposal executes nothing — approval happens only in
            # the Ember UI. Stated explicitly for MCP Bus consumers.
            "executed": False,
            "via": via,
            "refusal": len(plan["steps"]) == 0,
            "confidence": plan.get("confidence", "high"),
            "clarifying_question": plan.get("clarifying_question"),
            # PLC conversions: which manuals were loaded as long
            # context for this proposal (honest, $0 read).
            "plc_context": plan.get("plc_manual_stats"),
            "run": delta,
            "meter": self.status()["meter"],
        }

    def _goal_progress(self, goal: dict) -> dict:
        """Deterministic progress read for one goal: fetch its
        source and evaluate its metric (same evaluator as the
        flow condition gate). Never raises."""
        out = dict(goal)
        try:
            src = SOURCES[goal["source"]]
            body = self.source_fetch_fn(src["url"])
            actual, passed, err = evaluate_condition(
                body, goal["source"],
                {"field": goal["field"], "op": goal["op"],
                 "value": goal["value"]})
            out["current"] = actual
            out["on_track"] = bool(passed and not err)
            out["progress_error"] = err or ""
        except Exception as e:  # noqa: BLE001
            out["current"] = None
            out["on_track"] = None
            out["progress_error"] = f"{type(e).__name__}: {e}"
        return out

    def _open_goals_progress(self) -> list[dict]:
        if self.orch is None:
            return []
        return [self._goal_progress(g)
                for g in self.orch.tools.goals.list()
                if g.get("status") == "open"]

    def approve(self, plan_id: str, cookie: str) -> tuple[int, dict]:
        user, err = self._require_live(cookie)
        if err:
            return err
        plan_id = str(plan_id or "")
        with self.lock:
            plan = self.orch.plans.get(plan_id)
            if plan is None:
                return 404, {"error": f"unknown plan: {plan_id}"}
            if plan["status"] != "proposed":
                return 409, {"error": f"plan is {plan['status']}, not proposed"}
            if not plan["steps"]:
                return 400, {"error": "nothing to approve — Ember proposed "
                                      "no steps (refusal)"}
            before_calls = len(self.meter.ledger)
            before_cost = self.meter.spent_usd
            # PLC conversions: hand the propose-time manuals
            # stats to the tools layer so the stored record
            # carries them (cleared right after, under the lock).
            self.orch.tools._plc_context_stats = plan.get(
                "plc_manual_stats")
            try:
                done = self.orch.approve(plan_id, note_suffix="ui-live")
            except (BudgetHalt, BudgetStop) as e:
                return 429, {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                return 502, {"error": f"execution failed: {type(e).__name__}: {e}"}
            finally:
                self.orch.tools._plc_context_stats = None
            delta = self._run_delta(before_calls, before_cost)
        return 200, {
            "id": done["id"],
            # Medical/legal answers carry the deterministic
            # disclaimers (appended here, never left to the model).
            "final_text": _with_disclaimers(done),
            # Ember's next-step suggestions from the compose-final call
            # ([] when absent/malformed — page falls back to the
            # deterministic Automate prefill below + Done).
            "next_steps": done.get("next_steps", []),
            "automate_command": derive_automate_command(
                done.get("trace", [])),
            # Deterministic one-time follow-up for the "Take action"
            # primary when Ember proposed no action suggestion
            # (None when nothing sensible follows).
            "default_action": derive_default_action(
                done.get("trace", [])),
            "trace": [{"tool": t["tool"], "arguments": t["arguments"],
                       "result": t["result"]} for t in done.get("trace", [])],
            "verification": done.get("verification", []),
            "verification_summary": done.get("verification_summary", ""),
            "goal_progress": self._open_goals_progress(),
            "status": done["status"],
            "run": delta,
            "meter": self.status()["meter"],
        }

    def reject(self, plan_id: str, cookie: str) -> tuple[int, dict]:
        user, err = self._require_live(cookie)
        if err:
            return err
        plan_id = str(plan_id or "")
        with self.lock:
            if plan_id not in self.orch.plans:
                return 404, {"error": f"unknown plan: {plan_id}"}
            self.orch.reject(plan_id)
        return 200, {"ok": True, "status": "rejected"}

    # -- Ember-native Agent Builder Wizard ----------------------------
    # Ember designs (proposes params), the human customizes in the
    # form, and the FINAL edited params are re-validated against the
    # deterministic allowlist before the gated approval path mints a
    # single-use token and creates the DISABLED draft. No Axion: the
    # design call is Ember, assembly is agent_builder.py.
    #
    # Context-aware design (Nov 26, 2026): the design call receives
    # server-assembled context from the user's last run (tools used +
    # headline numbers; intake kind when the run was an intake) or
    # from the recording being replayed (by demo key), so Ember
    # proposes a PROCESS that builds on what just happened —
    # including a condition when alerting only on a threshold makes
    # sense. Context is tool names + numbers + kinds only; no
    # secrets, no raw payloads.
    def _wizard_context(self, demo_key: str = "") -> dict:
        chips: list[str] = []
        facts: list[str] = []
        trace: list[dict] = []
        origin = ""
        demo_key = str(demo_key or "").strip()
        if demo_key:
            for demo in self.demos().get("demos", []):
                if demo.get("key") == demo_key:
                    trace = demo.get("trace") or []
                    origin = f"the recorded '{demo_key}' run"
                    headline = str(demo.get("final_text") or "").strip()
                    if headline:
                        first = headline.split(". ")[0].strip()
                        facts.append("headline: " + first[:160])
                    break
        if not trace and self.orch is not None:
            trace = getattr(self.orch.tools, "last_run_trace", []) or []
            if trace:
                origin = "the user's last run"
        tools = []
        for entry in trace:
            name = entry.get("tool") or ""
            if name and name not in tools:
                tools.append(name)
        if tools:
            chips.append("Tools: " + " + ".join(tools[:5]))
            facts.append("tools used: " + ", ".join(tools[:6]))
        # Headline numbers: a few numeric scalars per tool result.
        for entry in trace[:4]:
            result = entry.get("result")
            if not isinstance(result, dict):
                continue
            nums = []

            def collect(obj, prefix, depth):
                if depth > 1 or not isinstance(obj, dict) \
                        or len(nums) >= 3:
                    return
                for k, v in obj.items():
                    if len(nums) >= 3:
                        return
                    if isinstance(v, bool):
                        continue
                    if isinstance(v, (int, float)):
                        nums.append(f"{prefix}{k}={v}")
                    elif isinstance(v, dict):
                        collect(v, prefix + k + ".", depth + 1)

            collect(result, "", 0)
            if nums:
                facts.append(f"{entry.get('tool')}: "
                             + ", ".join(nums))
                chips.extend(nums[:2])
        # Intake media type when the run (or the latest filing) was
        # an intake.
        intake_kind = ""
        for entry in trace:
            if entry.get("tool") == "file_intake":
                rec = (entry.get("arguments") or {}).get("record") or {}
                intake_kind = str(rec.get("kind") or "")
        if not intake_kind and self.orch is not None:
            try:
                records = self.orch.tools.intake.list()
                if records:
                    intake_kind = str(records[-1].get("kind") or "")
            except Exception:  # noqa: BLE001 — context is best-effort
                intake_kind = ""
        if intake_kind:
            chips.append(f"Intake: {intake_kind}")
            facts.append(f"latest intake kind: {intake_kind}")
        if not facts:
            return {"text": "", "chips": []}
        text = (f"Context from {origin}: " + "; ".join(facts)
                + ". Design a process that builds on this: derive "
                  "the name, trigger, source, and actions from "
                  "this context (source from the tools used; "
                  "default schedule daily 06:00 America/Chicago "
                  "unless stated) — do not refuse for missing "
                  "parameters when this context is available. "
                  "Prefer a condition when alerting only on a "
                  "threshold makes sense.")
        return {"text": text[:700], "chips": chips[:5]}

    def _scratch_live_values(self) -> dict:
        """Current readings for the scratch Q&A's suggested
        condition thresholds: {source: {field: actual}}. One cheap
        GET per allowlisted source (the same reads the suggestions
        endpoint makes), cached 60 s. Best-effort: a dead or
        non-numeric source yields no value and the chip keeps its
        static threshold."""
        now = time.time()
        ts, cached = self._scratch_live_cache
        if cached and now - ts < 60:
            return cached
        out: dict[str, dict] = {}
        for sid, sugs in SCRATCH_CONDITION_SUGGESTIONS.items():
            try:
                body = self.source_fetch_fn(SOURCES[sid]["url"])
            except Exception:  # noqa: BLE001 — skip dead sources
                continue
            vals = {}
            for sug in sugs:
                actual, _passed, err = evaluate_condition(
                    body, sid, {"field": sug["field"], "op": ">",
                                "value": -10 ** 18})
                if not err and actual is not None:
                    vals[sug["field"]] = actual
            if vals:
                out[sid] = vals
        self._scratch_live_cache = (now, out)
        return out

    def _scratch_options(self) -> dict:
        """The guided-Q&A structure for the wizard's "Start from
        scratch" path: questions, plain-words source chips with
        the Metatron tools that watch each source, action tools,
        and per-source condition suggestions enriched with live
        current values where the read succeeds."""
        live = self._scratch_live_values()
        return {
            "questions": SCRATCH_QUESTIONS,
            "sources": [{"id": s["id"], "plain": s["plain"],
                         "does": SOURCES[s["id"]].get("does", ""),
                         "tools": s["tools"]}
                        for s in SCRATCH_SOURCES],
            "action_tools": SCRATCH_ACTION_TOOLS,
            "builder_tool": SCRATCH_BUILDER_TOOL,
            "condition_suggestions": {
                sid: [{**sug,
                       "current": live.get(sid, {}).get(
                           sug["field"])}
                      for sug in sugs]
                for sid, sugs in
                SCRATCH_CONDITION_SUGGESTIONS.items()},
        }

    def wizard_options(self, cookie: str,
                       demo_key: str = "") -> tuple[int, dict]:
        user, err = self._require_live(cookie)
        if err:
            return err
        return 200, {
            "sources": [{"id": sid, "label": s["label"],
                         "does": s.get("does", ""),
                         "condition_fields": [
                             {"id": fid, "label": fspec["label"]}
                             for fid, fspec in
                             CONDITION_FIELDS.get(sid, {}).items()]}
                        for sid, s in SOURCES.items()],
            "actions": [{"id": a,
                         "label": WIZARD_ACTION_LABELS.get(a, a),
                         "does": WIZARD_ACTION_HINTS.get(a, "")}
                        for a in ACTIONS],
            "triggers": [{"id": t,
                          "label": WIZARD_TRIGGER_LABELS.get(t, t),
                          "does": WIZARD_TRIGGER_HINTS.get(t, "")}
                         for t in TRIGGERS],
            "presets": SCHEDULE_PRESETS,
            "condition_ops": list(CONDITION_OPS),
            "autonomy_levels": [{"id": a,
                                 "label": AUTONOMY_LABELS[a]}
                                for a in AUTONOMY_LEVELS],
            "preferences": (self.orch.tools.preferences.summary()
                            if self.orch is not None else None),
            # Pre-built templates for the "What do you need?" step:
            # deterministic pre-fills (no AI), served as clean
            # validated params so the page renders cards from data.
            "templates": [{"id": t["id"], "name": t["name"],
                           "does": t["does"],
                           "params": validate_agent_params(
                               dict(t["params"]))}
                          for t in TEMPLATES],
            # Guided Q&A ("Start from scratch", Rom Nov 27):
            # questions, tool suggestions, and live-enriched
            # condition suggestions — rendered from data.
            "scratch": self._scratch_options(),
            "context": self._wizard_context(demo_key),
        }

    def wizard_template(self, template_id: str,
                        cookie: str,
                        demo_key: str = "") -> tuple[int, dict]:
        """Step 1 (What) -> 3 (Review): a pre-built template becomes
        a proposed plan deterministically ($0 — no Ember call, no
        daily-cap consumption), in the same response shape as
        wizard_design so the Review form fills identically. The
        human still edits everything, and create re-validates
        before the gated approval path — templates change nothing
        about the gates."""
        user, err = self._require_live(cookie)
        if err:
            return err
        template = template_by_id(str(template_id or ""))
        if template is None:
            return 404, {"error": "unknown template — pick one of "
                                  "the cards, or describe it "
                                  "yourself"}
        clean = validate_agent_params(dict(template["params"]))
        with self.lock:
            plan = self.orch.propose_deterministic(
                [{"name": "create_agent_flow", "arguments": clean}],
                display_text=(f"Pre-built template: "
                              f"{template['name']}"),
                rationale=(
                    f"Pre-built template “{template['name']}” — "
                    "ready-made settings, no AI involved. Edit "
                    "anything in Review before approving; Ember "
                    "designs custom ones from your description."),
                purpose="integration")
        proposed = {k: clean.get(k) for k in
                    ("name", "trigger", "cron", "source", "action",
                     "actions", "condition", "steps", "autonomy")}
        return 200, {
            "plan_id": plan["id"],
            "rationale": plan["rationale"],
            "proposed_params": proposed,
            "validation": {"ok": True, "error": ""},
            "context": self._wizard_context(demo_key),
            "run": {"calls": 0, "cost_usd": 0.0},
            "meter": self.status()["meter"],
        }

    def wizard_scratch(self, answers: dict,
                       cookie: str,
                       demo_key: str = "") -> tuple[int, dict]:
        """Guided Q&A ("Start from scratch") -> Review: the chip
        answers assemble into agent params deterministically ($0
        — no Ember call, no daily-cap consumption) and become a
        proposed plan in the same response shape as
        wizard_template/wizard_design. The params pass the same
        allowlist validator, the human edits everything in
        Review, and create re-validates before the gated approval
        path — the Q&A changes nothing about the gates."""
        user, err = self._require_live(cookie)
        if err:
            return err
        try:
            clean = validate_agent_params(
                scratch_params_from_answers(answers or {}))
        except ValueError as e:
            return 400, {"error": str(e)}
        with self.lock:
            plan = self.orch.propose_deterministic(
                [{"name": "create_agent_flow", "arguments": clean}],
                display_text=(f"Start from scratch: "
                              f"{clean['name']}"),
                rationale=(
                    "Built from your answers — no AI involved. "
                    "Edit anything in Review, or ask Ember to "
                    "refine it from your answers."),
                purpose="integration")
        proposed = {k: clean.get(k) for k in
                    ("name", "trigger", "cron", "source", "action",
                     "actions", "condition", "steps", "autonomy")}
        return 200, {
            "plan_id": plan["id"],
            "rationale": plan["rationale"],
            "proposed_params": proposed,
            "validation": {"ok": True, "error": ""},
            "context": self._wizard_context(demo_key),
            "run": {"calls": 0, "cost_usd": 0.0},
            "meter": self.status()["meter"],
        }

    @staticmethod
    def _agent_step_index(plan: dict) -> int | None:
        """Index of the plan's single create_agent_flow step, or None
        when the plan is not a pure agent-build plan."""
        idx = [i for i, s in enumerate(plan["steps"])
               if s["name"] == "create_agent_flow"]
        if len(idx) != 1 or len(plan["steps"]) != 1:
            return None
        return idx[0]

    def wizard_design(self, description: str,
                      cookie: str,
                      demo_key: str = "") -> tuple[int, dict]:
        """Step 1 -> 2: Ember proposes create_agent_flow params from
        the plain-words description (metered, like propose), with
        server-assembled session context appended so the proposed
        PROCESS builds on the user's last run (or the recording
        being replayed). The proposed params come back with their
        deterministic validation result so the form can show
        Ember's design — and any flaw in it — before the human
        edits. Executes nothing."""
        user, err = self._require_live(cookie)
        if err:
            return err
        description = str(description or "").strip()
        if not description:
            return 400, {"error": "description is required"}
        if len(description) > 4000:
            return 400, {"error": "description too long (max 4000 chars)"}
        err = self._cap_check()
        if err:
            return err
        context = self._wizard_context(demo_key)
        prompt = description
        if context["text"]:
            prompt = description + "\n\n" + context["text"]
        # Learned preferences (Nov 26): pre-align Ember's design
        # with what the user usually edits proposals to.
        prefs_line = ""
        try:
            prefs_line = self.orch.tools.preferences.prompt_line()
        except Exception:  # noqa: BLE001
            prefs_line = ""
        if prefs_line:
            prompt = prompt + "\n\n" + prefs_line
        with self.lock:
            before_calls = len(self.meter.ledger)
            before_cost = self.meter.spent_usd
            try:
                plan = self.orch.propose(prompt,
                                         purpose="integration",
                                         note_suffix="ui-wizard")
            except (BudgetHalt, BudgetStop) as e:
                return 429, {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                return 502, {"error": f"Ember call failed: "
                                      f"{type(e).__name__}: {e}"}
            bump_cap(self.cap_path)
            delta = self._run_delta(before_calls, before_cost)
            idx = self._agent_step_index(plan)
            if idx is None:
                self.orch.reject(plan["id"])
                return 422, {"error": "Ember couldn't turn that "
                                      "into an automation — try "
                                      "saying what to check, when, "
                                      "and what to do with it "
                                      "(e.g. “Every morning at 6, "
                                      "check capacity and WhatsApp "
                                      "me only if disk is over "
                                      "80%”), then try again."}
            proposed = {k: plan["steps"][idx]["arguments"].get(k)
                        for k in ("name", "trigger", "cron", "source",
                                  "action", "actions", "condition")}
        validation = {"ok": True, "error": ""}
        try:
            clean = validate_agent_params(
                {k: v for k, v in proposed.items() if v is not None})
            # Normalize to the clean params so the form always sees
            # both `actions` and the legacy joined `action`.
            proposed = {k: clean.get(k) for k in
                        ("name", "trigger", "cron", "source", "action",
                         "actions", "condition", "steps", "autonomy")}
        except ValueError as e:
            validation = {"ok": False, "error": str(e)}
        return 200, {
            "plan_id": plan["id"],
            "rationale": plan["rationale"],
            "proposed_params": proposed,
            "validation": validation,
            "context": context,
            "confidence": plan.get("confidence", "high"),
            "clarifying_question": plan.get("clarifying_question"),
            "preferences_applied": (
                self.orch.tools.preferences.summary()
                if prefs_line else None),
            "run": delta,
            "meter": self.status()["meter"],
        }

    def wizard_validate(self, params: dict,
                        cookie: str) -> tuple[int, dict]:
        """Deterministic allowlist check for the Review form ($0 —
        no Ember call, no daily-cap consumption)."""
        user, err = self._require_live(cookie)
        if err:
            return err
        try:
            clean = validate_agent_params(params or {})
        except ValueError as e:
            return 200, {"ok": False, "error": str(e)}
        return 200, {"ok": True, "clean": clean}

    def wizard_create(self, plan_id: str, params: dict,
                      cookie: str) -> tuple[int, dict]:
        """Step 3: approve the design plan with the FINAL edited
        params. The params are re-validated deterministically before
        the gated approval path runs (400 on error — the plan stays
        proposed and nothing is created); the orchestrator validates
        again before minting the single-use token, so an edited
        design can never smuggle anything past the allowlist."""
        user, err = self._require_live(cookie)
        if err:
            return err
        plan_id = str(plan_id or "")
        try:
            clean = validate_agent_params(params or {})
        except ValueError as e:
            return 400, {"error": f"invalid agent parameters: {e}"}
        with self.lock:
            plan = self.orch.plans.get(plan_id)
            if plan is None:
                return 404, {"error": f"unknown plan: {plan_id}"}
            if plan["status"] != "proposed":
                return 409, {"error": f"plan is {plan['status']}, "
                                      "not proposed"}
            idx = self._agent_step_index(plan)
            if idx is None:
                return 400, {"error": "plan is not an agent-build "
                                      "plan — design a new one"}
            before_calls = len(self.meter.ledger)
            before_cost = self.meter.spent_usd
            try:
                done = self.orch.approve(
                    plan_id, note_suffix="ui-wizard",
                    param_overrides={idx: clean})
            except (BudgetHalt, BudgetStop) as e:
                return 429, {"error": str(e)}
            except ValueError as e:
                return 400, {"error": f"invalid agent parameters: {e}"}
            except Exception as e:  # noqa: BLE001
                return 502, {"error": f"execution failed: "
                                      f"{type(e).__name__}: {e}"}
            delta = self._run_delta(before_calls, before_cost)
            # Learn from edits (Nov 26): record how the final
            # params differ from Ember's proposal so the next
            # design can be pre-aligned. Deterministic, $0.
            proposed_args = plan["steps"][idx].get("arguments") or {}
            try:
                self.orch.tools.preferences.record_edit(
                    proposed_args, clean)
            except Exception:  # noqa: BLE001 — never block a create
                pass
        return 200, {
            "id": done["id"],
            "final_text": done.get("final_text", ""),
            "next_steps": done.get("next_steps", []),
            "automate_command": derive_automate_command(
                done.get("trace", [])),
            "default_action": derive_default_action(
                done.get("trace", [])),
            "trace": [{"tool": t["tool"], "arguments": t["arguments"],
                       **({"proposed_arguments": t["proposed_arguments"]}
                          if "proposed_arguments" in t else {}),
                       "result": t["result"]}
                      for t in done.get("trace", [])],
            "verification": done.get("verification", []),
            "verification_summary": done.get("verification_summary", ""),
            "goal_progress": self._open_goals_progress(),
            "status": done["status"],
            "run": delta,
            "meter": self.status()["meter"],
        }

    # -- Agentic extras (Nov 26, 2026): suggestions, goals,
    #    preferences, autonomy, undo — all deterministic ($0).
    def suggestions(self, cookie: str) -> tuple[int, dict]:
        """Proactive suggestions from live data: deterministic
        threshold rules over the allowlisted sources. Each maps to
        a pre-built template the wizard can open pre-filled.
        Nothing is created — suggestion -> wizard -> approval."""
        user, err = self._require_live(cookie)
        if err:
            return err
        rules = [
            ("capacity", "disk_pct", ">=", 65,
             "capacity-disk-alert",
             "Disk is at {v}% — want a daily alert at 80%?"),
            ("watcher_overview", "components_down", ">", 0,
             "morning-watcher-whatsapp",
             "{v} component(s) down — want the morning Watcher "
             "status on WhatsApp?"),
            ("finops", "budget_pct", ">=", 70,
             "finops-summary-board",
             "Projected spend is {v}% of budget — want a Monday "
             "FinOps summary on the board?"),
            ("predictions", "high_count", ">", 0,
             "predictions-digest-board",
             "{v} high-risk component(s) predicted — want a daily "
             "failure-predictions digest?"),
            ("secops", "critical_cves", ">", 0,
             "secops-cve-posture-board",
             "{v} critical CVEs in the current set — want a daily "
             "SecOps posture report?"),
        ]
        out = []
        for source, field, op, value, template_id, text in rules:
            try:
                body = self.source_fetch_fn(SOURCES[source]["url"])
                actual, passed, cond_err = evaluate_condition(
                    body, source,
                    {"field": field, "op": op, "value": value})
            except Exception:  # noqa: BLE001 — skip dead sources
                continue
            if passed and not cond_err and actual is not None:
                out.append({
                    "id": f"sug-{template_id}",
                    "text": text.format(v=actual),
                    "template_id": template_id,
                    "source": source, "field": field,
                    "current": actual})
        return 200, {"suggestions": out}

    def goals_list(self, cookie: str) -> tuple[int, dict]:
        user, err = self._require_live(cookie)
        if err:
            return err
        goals = [self._goal_progress(g)
                 for g in self.orch.tools.goals.list()]
        return 200, {"goals": goals}

    def goals_create(self, payload: dict,
                     cookie: str) -> tuple[int, dict]:
        user, err = self._require_live(cookie)
        if err:
            return err
        payload = payload or {}
        text = str(payload.get("text") or "").strip()[:200]
        source = str(payload.get("source") or "").strip().lower()
        field = str(payload.get("field") or "").strip()
        op = str(payload.get("op") or "").strip()
        value = payload.get("value")
        if not text:
            return 400, {"error": "goal text is required (e.g. "
                                  "'keep disk under 80% this week')"}
        if source not in SOURCES:
            return 400, {"error": "pick a source for the goal "
                                  "metric"}
        try:
            from orchestrator.agent_builder import _validate_condition
            cond = _validate_condition(
                {"field": field, "op": op, "value": value}, source)
        except ValueError as e:
            return 400, {"error": str(e)}
        import secrets as _secrets
        goal = {
            "id": "goal-" + _secrets.token_hex(4),
            "text": text, "source": source,
            "field": cond["field"], "op": cond["op"],
            "value": cond["value"], "status": "open",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime()),
            "closed_at": None}
        with self.lock:
            self.orch.tools.goals.add_goal(goal)
        return 200, {"goal": self._goal_progress(goal)}

    def goals_close(self, goal_id: str,
                    cookie: str) -> tuple[int, dict]:
        user, err = self._require_live(cookie)
        if err:
            return err
        with self.lock:
            closed = self.orch.tools.goals.close(str(goal_id or ""))
        if closed is None:
            return 404, {"error": f"unknown goal: {goal_id}"}
        return 200, {"goal": closed}

    def preferences_get(self, cookie: str) -> tuple[int, dict]:
        user, err = self._require_live(cookie)
        if err:
            return err
        return 200, {"preferences":
                     self.orch.tools.preferences.summary()}

    def preferences_reset(self, cookie: str) -> tuple[int, dict]:
        user, err = self._require_live(cookie)
        if err:
            return err
        with self.lock:
            self.orch.tools.preferences.reset()
        return 200, {"preferences":
                     self.orch.tools.preferences.summary()}

    def agent_flow_autonomy(self, flow_id: str, level: str,
                            cookie: str) -> tuple[int, dict]:
        """Set a Built Automation's trust level (registry only;
        the RPA flow is unchanged — the level governs reporting:
        auto-report expects a board action so each run files its
        report; auto-silent files nothing extra)."""
        user, err = self._require_live(cookie)
        if err:
            return err
        level = str(level or "").strip().lower()
        if level not in AUTONOMY_LEVELS:
            return 400, {"error": "level must be propose-only, "
                                  "auto-report, or auto-silent"}
        flow_id = str(flow_id or "").strip()
        with self.lock:
            entry = self.orch.tools.agent_flows.set_autonomy(
                flow_id, level)
        if entry is None:
            return 404, {"error": f"flow {flow_id!r} is not in the "
                                  "Agent Builder registry"}
        note = ""
        if level == "auto-report" and "board" not in (
                entry.get("actions") or []):
            note = ("Heads-up: this automation has no board "
                    "action, so runs will not file a report until "
                    "you add one (Edit flow). The level is stored "
                    "either way.")
        return 200, {"flow_id": flow_id, "autonomy": level,
                     "autonomy_label": AUTONOMY_LABELS[level],
                     "note": note}

    def undo(self, payload: dict, cookie: str) -> tuple[int, dict]:
        """One-tap undo (Nov 26): deterministic, Operator+,
        registry/board scoped, idempotent. Kinds: flow_delete,
        flow_disable, board_ticket, export. WhatsApp sends cannot
        be unsent — answered honestly, never faked."""
        user, err = self._require_live(cookie)
        if err:
            return err
        payload = payload or {}
        kind = str(payload.get("kind") or "").strip().lower()
        target = str(payload.get("id") or "").strip()
        if kind == "whatsapp":
            return 200, {"undone": False, "kind": kind,
                         "message": "WhatsApp messages can't be "
                                    "unsent — that send stands."}
        if kind not in ("flow_delete", "flow_disable",
                        "board_ticket", "export",
                        "dashboard_delete", "plc_delete") or not target:
            return 400, {"error": "kind must be flow_delete, "
                                  "flow_disable, board_ticket, "
                                  "export, dashboard_delete, or "
                                  "plc_delete, with an id"}
        tools = self.orch.tools
        with self.lock:
            try:
                if kind == "flow_delete":
                    out = tools.undo_flow_delete(target)
                elif kind == "flow_disable":
                    out = tools.undo_flow_disable(target)
                elif kind == "board_ticket":
                    out = tools.undo_board_ticket(target)
                elif kind == "dashboard_delete":
                    out = tools.undo_dashboard_delete(target)
                elif kind == "plc_delete":
                    out = tools.undo_plc_delete(target)
                else:
                    out = tools.undo_export(target)
            except Exception as e:  # noqa: BLE001
                return 502, {"error": f"undo failed: "
                                      f"{type(e).__name__}: {e}"}
        out["audit"] = (f"undo {kind} {target} by "
                        f"{user.get('email') or 'operator'} at "
                        + time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime()))
        return 200, out

    # -- Phase 4: multimodal intake + document intelligence -----------
    def _cap_check(self) -> tuple | None:
        cap = load_cap_state(self.cap_path)
        if int(cap.get("count") or 0) >= self.daily_cap:
            return 429, {"error": f"daily live-run cap reached "
                                  f"({self.daily_cap}/day) — recorded "
                                  f"replays remain available"}
        return None

    def _plan_response(self, plan, delta) -> dict:
        return {
            "id": plan["id"],
            "rationale": plan["rationale"],
            "steps": [{"name": s["name"], "arguments": s["arguments"]}
                      for s in plan["steps"]],
            "status": plan["status"],
            "refusal": len(plan["steps"]) == 0,
            "run": delta,
            "meter": self.status()["meter"],
        }

    def intake(self, image_b64: str, media_type: str, note: str,
               cookie: str) -> tuple[int, dict]:
        """Ember reads the image itself (multimodal) and proposes a
        file_intake call with the extracted fields. Executes nothing."""
        user, err = self._require_live(cookie)
        if err:
            return err
        media_type = str(media_type or "").strip().lower()
        if media_type not in INTAKE_MEDIA_TYPES:
            return 400, {"error": f"media_type must be one of "
                                  f"{sorted(INTAKE_MEDIA_TYPES)}"}
        try:
            raw = base64.b64decode(str(image_b64 or ""), validate=True)
        except (binascii.Error, ValueError):
            return 400, {"error": "image_base64 is not valid base64"}
        if not raw:
            return 400, {"error": "image_base64 is empty"}
        if len(raw) > INTAKE_MAX_BYTES:
            return 413, {"error": f"image too large "
                                  f"({len(raw)} bytes; max "
                                  f"{INTAKE_MAX_BYTES})"}
        err = self._cap_check()
        if err:
            return err
        instruction = (
            "Read the attached image yourself and extract its fields. "
            "Call file_intake once with kind and the fields you can "
            "see (for an invoice: vendor, invoice_number, date, "
            "po_number, bill_to, line_items, subtotal, total_due). "
            "Report only what is visible: include amount_paid and "
            "balance_due only when explicitly printed (omit blank or "
            "absent fields; never copy another field's value). Notes "
            "are for factual observations only — invoice totals are "
            "verified deterministically by the filing tool. A human "
            "approves before anything is filed.")
        if str(note or "").strip():
            instruction += " User note: " + str(note).strip()[:500]
        content = [
            {"type": "text", "text": instruction},
            {"type": "image_url",
             "image_url": {"url": f"data:{media_type};base64,"
                                  f"{image_b64}"}},
        ]
        display = "Image attached — read it and file the extracted fields."
        return self._intake_propose(
            content, display, "file_intake",
            "Ember could not extract fields from that image. Try a "
            "clearer, straight-on photo with the whole document in "
            "frame and good light.", "ui-intake")

    def _intake_propose(self, content, display, expected_tool, err_msg,
                        note_suffix):
        """Shared intake propose: metered Ember call (2048 tokens up
        front — multimodal reasoning is token-hungry; the orchestrator
        escalates further on truncation), then require the filing
        tool. If Ember produced no filing call (truncated reply or
        prose only), surface a clear, actionable error — never hand
        the page an empty plan whose rationale reads like a stuck run."""
        with self.lock:
            before_calls = len(self.meter.ledger)
            before_cost = self.meter.spent_usd
            try:
                plan = self.orch.propose_content(
                    content, display, purpose="integration",
                    note_suffix=note_suffix, max_tokens=2048)
            except (BudgetHalt, BudgetStop) as e:
                return 429, {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                return 502, {"error": f"Ember call failed: "
                                      f"{type(e).__name__}: {e}"}
            bump_cap(self.cap_path)
            delta = self._run_delta(before_calls, before_cost)
        if not any(s["name"] == expected_tool for s in plan["steps"]):
            return 502, {"error": err_msg}
        return 200, self._plan_response(plan, delta)

    def document(self, text: str, title: str,
                 cookie: str) -> tuple[int, dict]:
        """Ember parses the document (line-numbered) and proposes a
        file_document_report call. Executes nothing."""
        user, err = self._require_live(cookie)
        if err:
            return err
        text = str(text or "")
        if not text.strip():
            return 400, {"error": "document text is required"}
        if len(text) > DOCUMENT_MAX_CHARS:
            return 413, {"error": f"document too large ({len(text)} "
                                  f"chars; max {DOCUMENT_MAX_CHARS})"}
        err = self._cap_check()
        if err:
            return err
        numbered = "\n".join(f"{i}: {line}" for i, line in
                             enumerate(text.splitlines(), start=1))
        instruction = (
            "Analyze the attached document. Lines are numbered. Call "
            "file_document_report once with a title, a short summary, "
            "and findings for anomalies or problems you find (line = "
            "the number shown, severity, detail). Cite only lines "
            "that exist. A human approves before anything is filed.\n\n"
            + numbered)
        display = ("Document attached"
                   + (f" ({str(title).strip()[:80]})"
                      if str(title or "").strip() else "")
                   + " — analyze it and file a findings report.")
        return self._intake_propose(
            instruction, display, "file_document_report",
            "Ember could not produce a findings report for that "
            "document. Try a shorter excerpt or plain-text log.",
            "ui-document")

    def intake_file(self, filename: str, media_type: str, data_b64: str,
                    note: str, cookie: str) -> tuple[int, dict]:
        """Unified multimodal intake: one upload, routed by type to
        the Ember input format the API accepts (image_url / file /
        input_audio / video_url — all probed live Nov 26, 2026).
        Text files go through the line-numbered document path.
        Executes nothing; a human approves before filing."""
        user, err = self._require_live(cookie)
        if err:
            return err
        filename = str(filename or "").strip()[:120] or "attachment"
        media_type = str(media_type or "").strip().lower()
        if media_type not in INTAKE_FILE_TYPES:
            ext = os.path.splitext(filename)[1].lower()
            media_type = INTAKE_EXT_TYPES.get(ext, media_type)
        if media_type not in INTAKE_FILE_TYPES:
            return 400, {"error": f"unsupported file type "
                                  f"({media_type or 'unknown'}) — "
                                  f"{INTAKE_SUPPORTED_MSG}"}
        kind, cap = INTAKE_FILE_TYPES[media_type]
        try:
            raw = base64.b64decode(str(data_b64 or ""), validate=True)
        except (binascii.Error, ValueError):
            return 400, {"error": "data_base64 is not valid base64"}
        if not raw:
            return 400, {"error": "file is empty"}
        if cap is not None and len(raw) > cap:
            return 413, {"error": f"{kind} file too large ({len(raw)} "
                                  f"bytes; max {cap})"}
        err = self._cap_check()
        if err:
            return err
        note = str(note or "").strip()[:500]
        note_suffix = f"ui-intake-{kind}"
        if kind == "text":
            text = raw.decode("utf-8", errors="replace")
            if not text.strip():
                return 400, {"error": "text file is empty"}
            if len(text) > DOCUMENT_MAX_CHARS:
                return 413, {"error": f"text file too large "
                                      f"({len(text)} chars; max "
                                      f"{DOCUMENT_MAX_CHARS})"}
            numbered = "\n".join(
                f"{i}: {line}" for i, line in
                enumerate(text.splitlines(), start=1))
            instruction = (
                "Analyze the attached document. Lines are numbered. "
                "Call file_document_report once with a title, a short "
                "summary, and findings for anomalies or problems you "
                "find (line = the number shown, severity, detail). "
                "Cite only lines that exist. A human approves before "
                "anything is filed.\n\n" + numbered)
            if note:
                instruction += "\n\nUser note: " + note
            display = (f"Text file attached ({filename}) — analyze it "
                       "and file a findings report.")
            return self._intake_propose(
                instruction, display, "file_document_report",
                "Ember could not produce a findings report for that "
                "file. Try a shorter excerpt or plain-text log.",
                note_suffix)
        b64 = base64.b64encode(raw).decode()
        if kind == "image":
            instruction = (
                "Read the attached image yourself and extract its "
                "fields. Call file_intake once with kind and the "
                "fields you can see (for an invoice: vendor, "
                "invoice_number, date, po_number, bill_to, line_items, "
                "subtotal, total_due). Report only what is visible: "
                "include amount_paid and balance_due only when "
                "explicitly printed (omit blank or absent fields; "
                "never copy another field's value). Notes are for "
                "factual observations only — invoice totals are "
                "verified deterministically by the filing tool. A "
                "human approves before anything is filed.")
            part = {"type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{b64}"}}
            display = (f"Image attached ({filename}) — read it and "
                       "file the extracted fields.")
            err_msg = ("Ember could not extract fields from that "
                       "image. Try a clearer, straight-on photo with "
                       "the whole document in frame and good light.")
        elif kind == "pdf":
            instruction = (
                "Read the attached PDF yourself and extract its "
                "fields. Call file_intake once with kind and the "
                "fields you can see (for an invoice: vendor, "
                "invoice_number, date, po_number, bill_to, line_items, "
                "subtotal, total_due). Report only what is in the "
                "document: include amount_paid and balance_due only "
                "when explicitly printed (omit blank or absent "
                "fields; never copy another field's value). Notes "
                "are for factual observations only — invoice totals "
                "are verified deterministically by the filing tool. "
                "A human approves before anything is filed.")
            part = {"type": "file",
                    "file": {"filename": filename,
                             "file_data": f"data:application/pdf;"
                                          f"base64,{b64}"}}
            display = (f"PDF attached ({filename}) — read it and file "
                       "the extracted fields.")
            err_msg = ("Ember could not extract fields from that PDF. "
                       "Try a text-based PDF or a shorter document.")
        elif kind == "audio":
            fmt = "mp3" if media_type == "audio/mpeg" else "wav"
            instruction = (
                "Listen to the attached audio yourself. Call "
                "file_intake once with kind 'audio_note' and fields: "
                "transcript (the exact words spoken), plus structured "
                "items you hear (work_order, task, priority, due, "
                "action_items). Report only what is audible. A human "
                "approves before anything is filed.")
            part = {"type": "input_audio",
                    "input_audio": {"data": b64, "format": fmt}}
            display = (f"Audio attached ({filename}) — transcribe it "
                       "and file the extracted items.")
            err_msg = ("Ember could not transcribe that audio. Try a "
                       "clearer recording (MP3 or WAV).")
        else:  # video
            instruction = (
                "Watch the attached video yourself. Call file_intake "
                "once with kind 'video' and fields: on_screen_text "
                "(exact text shown), summary (what happens), and "
                "transcript (any speech, exact words). Report only "
                "what is visible or audible. A human approves before "
                "anything is filed.")
            part = {"type": "video_url",
                    "video_url": {"url": f"data:video/mp4;base64,{b64}"}}
            display = (f"Video attached ({filename}) — watch it and "
                       "file what it shows.")
            err_msg = ("Ember could not read that video. Try a short "
                       "MP4 clip.")
        if note:
            instruction += " User note: " + note
        content = [{"type": "text", "text": instruction}, part]
        return self._intake_propose(content, display, "file_intake",
                                    err_msg, note_suffix)

    # -- internal: board filing from Agent-Builder flows --------------
    def board_file(self, payload: dict, api_key: str) -> tuple[int, dict]:
        expected = _services_key()
        if not expected or not api_key or api_key != expected:
            return 401, {"error": "valid X-API-Key required"}
        if not isinstance(payload, dict):
            return 400, {"error": "JSON object required"}
        title = str(payload.get("title") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        if not title or not summary:
            return 400, {"error": "title and summary are required"}
        entry = {
            "title": title[:200],
            "summary": summary[:2000],
            "source": str(payload.get("source") or "")[:80],
            "findings": [],
            "filed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_tag": "agent-builder-flow",
            "status": "filed by agent flow — awaiting human review",
        }
        with self.lock:
            total = self.reports.append(entry)
        return 200, {"ok": True, "reports_total": total}


def build_live_service() -> DemoService:
    """Wire the real Ember client + Cube tools, or recorded-only mode."""
    spend_dir = os.path.join(ROOT, "spend")
    os.makedirs(spend_dir, exist_ok=True)
    rate_in = os.environ.get("EMBER_RATE_INPUT_PER_1K")
    rate_out = os.environ.get("EMBER_RATE_OUTPUT_PER_1K")
    meter = SpendMeter(
        state_path=os.path.join(spend_dir, "state.json"),
        rate_input_per_1k=float(rate_in) if rate_in else None,
        rate_output_per_1k=float(rate_out) if rate_out else None)
    orch = None
    model = "muse-ember-1.0"
    try:
        client = MetaEmberClient(timeout=120)
        model = client.model
        if meter.rate_input_per_1k is None:
            raise NotConfiguredError(
                "EMBER_RATE_INPUT_PER_1K / EMBER_RATE_OUTPUT_PER_1K not set "
                "— a live call must never meter at $0")
        tokens = ApprovalTokens()
        orch = Orchestrator(client, UiCubeTools(tokens), meter, tokens)
        print(f"ember demo service: LIVE (model {model})", flush=True)
    except NotConfiguredError as e:
        print(f"ember demo service: RECORDED-ONLY ({e})", flush=True)
    return DemoService(orch, meter, model=model)


SERVICE = None  # built in main(); tests inject their own


def make_handler(service: DemoService):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code, obj, set_cookies=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for c in set_cookies or []:
                self.send_header("Set-Cookie", c)
            self.end_headers()
            self.wfile.write(body)

        def _body(self, limit=1_000_000):
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise ValueError(f"body too large (max {limit} bytes)")
            return json.loads(self.rfile.read(n) or b"{}")

        def _cookie(self):
            return self.headers.get("Cookie") or ""

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                try:
                    with open(PAGE_PATH, "rb") as f:
                        body = f.read()
                except OSError:
                    self._json(500, {"error": "demo page missing"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/assets/"):
                name = os.path.basename(path)
                fpath = os.path.join(ASSETS_DIR, name)
                ctype = ASSET_TYPES.get(os.path.splitext(name)[1].lower())
                if not ctype or not os.path.isfile(fpath):
                    self._json(404, {"error": "not found"})
                    return
                with open(fpath, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/status":
                self._json(200, service.status())
            elif path == "/api/mcp/status":
                # MCP Bus service surface (services key, no session).
                code, obj = service.status_via_service(
                    self.headers.get("X-API-Key") or "")
                self._json(code, obj)
            elif path == "/api/mcp/automations":
                code, obj = service.automations_via_service(
                    self.headers.get("X-API-Key") or "")
                self._json(code, obj)
            elif path == "/api/demos":
                self._json(200, service.demos())
            elif path == "/api/whoami":
                self._json(200, service.whoami(self._cookie()))
            elif path == "/api/agent-flows":
                code, obj = service.agent_flows(self._cookie())
                self._json(code, obj)
            elif path == "/api/suggestions":
                code, obj = service.suggestions(self._cookie())
                self._json(code, obj)
            elif path == "/api/goals":
                code, obj = service.goals_list(self._cookie())
                self._json(code, obj)
            elif path == "/api/preferences":
                code, obj = service.preferences_get(self._cookie())
                self._json(code, obj)
            elif path == "/api/exports":
                code, obj = service.exports_list(self._cookie())
                self._json(code, obj)
            elif path.startswith("/api/plc-conversions/") \
                    and path.endswith("/download"):
                code, payload, headers = service.plc_download(
                    path[len("/api/plc-conversions/"):-len("/download")],
                    self._cookie())
                if code != 200:
                    self._json(code, payload)
                    return
                self.send_response(200)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif path.startswith("/api/exports/"):
                code, payload, headers = service.export_download(
                    path[len("/api/exports/"):], self._cookie())
                if code != 200:
                    self._json(code, payload)
                    return
                self.send_response(200)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif path == "/api/agent-wizard/options":
                demo_key = ""
                if "?" in self.path:
                    from urllib.parse import parse_qs
                    demo_key = (parse_qs(
                        self.path.split("?", 1)[1]).get("demo")
                        or [""])[0]
                code, obj = service.wizard_options(self._cookie(),
                                                   demo_key)
                self._json(code, obj)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            limit = (30_000_000 if path == "/api/intake-file"
                     else 8_000_000 if path == "/api/intake"
                     else 1_000_000)
            try:
                data = self._body(limit)
            except Exception as e:  # noqa: BLE001
                self._json(400, {"error": f"bad JSON body: {e}"})
                return
            if path == "/api/login":
                code, obj, cookies = service.login(
                    data.get("email", ""), data.get("password", ""))
                self._json(code, obj, cookies)
            elif path == "/api/logout":
                code, obj, cookies = service.logout(self._cookie())
                self._json(code, obj, cookies)
            elif path == "/api/propose":
                code, obj = service.propose(data.get("command", ""),
                                            self._cookie())
                self._json(code, obj)
            elif path == "/api/intake":
                code, obj = service.intake(data.get("image_base64", ""),
                                           data.get("media_type", ""),
                                           data.get("note", ""),
                                           self._cookie())
                self._json(code, obj)
            elif path == "/api/document":
                code, obj = service.document(data.get("text", ""),
                                             data.get("title", ""),
                                             self._cookie())
                self._json(code, obj)
            elif path == "/api/intake-file":
                code, obj = service.intake_file(
                    data.get("filename", ""), data.get("media_type", ""),
                    data.get("data_base64", ""), data.get("note", ""),
                    self._cookie())
                self._json(code, obj)
            elif path == "/api/board/file":
                code, obj = service.board_file(
                    data, self.headers.get("X-API-Key") or "")
                self._json(code, obj)
            elif path == "/api/mcp/propose":
                # MCP Bus propose (services key). Proposes only —
                # approval/execution stay in the Ember UI.
                code, obj = service.propose_via_service(
                    data.get("command", ""),
                    self.headers.get("X-API-Key") or "")
                self._json(code, obj)
            elif path == "/api/approve":
                code, obj = service.approve(data.get("plan_id", ""),
                                            self._cookie())
                self._json(code, obj)
            elif path == "/api/reject":
                code, obj = service.reject(data.get("plan_id", ""),
                                           self._cookie())
                self._json(code, obj)
            elif path == "/api/agent-flows/test":
                code, obj = service.agent_flow_test(
                    data.get("flow_id", ""), self._cookie())
                self._json(code, obj)
            elif path == "/api/plc-conversions/send-approval":
                code, obj = service.plc_send_approval(
                    data.get("conversion_id", ""), self._cookie())
                self._json(code, obj)
            elif path == "/api/agent-flows/autonomy":
                code, obj = service.agent_flow_autonomy(
                    data.get("flow_id", ""), data.get("level", ""),
                    self._cookie())
                self._json(code, obj)
            elif path == "/api/goals":
                code, obj = service.goals_create(data, self._cookie())
                self._json(code, obj)
            elif path == "/api/goals/close":
                code, obj = service.goals_close(
                    data.get("goal_id", ""), self._cookie())
                self._json(code, obj)
            elif path == "/api/preferences/reset":
                code, obj = service.preferences_reset(self._cookie())
                self._json(code, obj)
            elif path == "/api/undo":
                code, obj = service.undo(data, self._cookie())
                self._json(code, obj)
            elif path == "/api/agent-wizard/design":
                code, obj = service.wizard_design(
                    data.get("description", ""), self._cookie(),
                    data.get("demo_key", ""))
                self._json(code, obj)
            elif path == "/api/agent-wizard/template":
                code, obj = service.wizard_template(
                    data.get("template_id", ""), self._cookie(),
                    data.get("demo_key", ""))
                self._json(code, obj)
            elif path == "/api/agent-wizard/scratch":
                code, obj = service.wizard_scratch(
                    data.get("answers") or {}, self._cookie(),
                    data.get("demo_key", ""))
                self._json(code, obj)
            elif path == "/api/agent-wizard/validate":
                code, obj = service.wizard_validate(
                    data.get("params") or {}, self._cookie())
                self._json(code, obj)
            elif path == "/api/agent-wizard/create":
                code, obj = service.wizard_create(
                    data.get("plan_id", ""), data.get("params") or {},
                    self._cookie())
                self._json(code, obj)
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    return Handler


def main() -> int:
    global SERVICE
    SERVICE = build_live_service()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(SERVICE))
    print(f"ember demo service listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
