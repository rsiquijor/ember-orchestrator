"""Agent Builder — deterministic validation + RPA flow assembly (Phase 4).

Ember proposes HIGH-LEVEL parameters only (name, trigger, cron, source,
actions, optional condition). It never emits raw flow JSON. This module
validates those parameters against a strict allowlist and assembles the
Activepieces flow from the same proven step shapes the Metatron Agents
service uses (/opt/cube/agents/serve.py): POST /flows -> IMPORT_FLOW ->
never publish, so the result is a DISABLED draft a human must enable.

Built flows contain NO LLM steps: trigger -> HTTP GET (allowlisted
Cube read endpoint) -> CODE (deterministic compose) -> action step(s)
(WhatsApp via the proven watcher flow webhook, and/or board filing via
the Ember service's key-gated /api/board/file endpoint).

Optional condition step (Nov 26, 2026): no production flow uses an AP
branch/router step, so there is no proven branch shape to copy. The
condition is therefore implemented INSIDE the compose CODE step
(deterministic, same proven shape): the step evaluates a numeric
threshold against the fetched body and THROWS when it is not met, so
the run stops before any action step executes. When met, the composed
report carries a "condition met" suffix. The Python evaluator below
mirrors the JS exactly for the dry-run Test replay.

Refs use the bare form only ({{step_N['field']}} / whole-step
{{step_N}}): the {{step_N['output']['field']}} form resolves empty at
runtime in AP 0.93 (proven Nov 24, 2026).
"""
from __future__ import annotations

import json
import math
import re
import urllib.request

# Allowlisted deterministic Cube read endpoints (GET only, box-local).
SOURCES = {
    "watcher_overview": {
        "label": "Watcher overview (AIOps)",
        "does": "Live estate health — components up/down, open "
                "incidents, host vitals.",
        "url": "http://127.0.0.1:8008/api/overview",
    },
    "capacity": {
        "label": "Capacity report (AIOps)",
        "does": "Headroom and forecasts — disk, memory, GPU use and "
                "days until the disk fills.",
        "url": "http://127.0.0.1:8008/api/capacity",
    },
    "predictions": {
        "label": "Failure predictions (AIOps)",
        "does": "Per-component risk scores — what is likely to fail "
                "next, and why.",
        "url": "http://127.0.0.1:8008/api/predictions",
    },
    "finops": {
        "label": "FinOps summary",
        "does": "Cube running costs — today, month to date, "
                "projected, and budget use.",
        "url": "http://127.0.0.1:8019/api/summary",
    },
    "secops": {
        "label": "SecOps status",
        "does": "Security posture — critical/high CVE counts and "
                "known-exploited (KEV) exposure.",
        "url": "http://127.0.0.1:8014/api/status",
    },
    "dotkrc": {
        "label": "DOT-KRC bid board status",
        "does": "Bid board totals — how many DOT projects are "
                "tracked across the 14 states.",
        "url": "http://127.0.0.1:8011/api/status",
    },
}

ACTIONS = ("whatsapp", "board")
TRIGGERS = ("schedule", "webhook")

# Pre-built templates for the wizard's "What do you need?" step
# (Rom, Nov 26, 2026). Deterministic pre-fills ONLY — no AI involved;
# Ember designs custom ones from a description. Every template's
# params must pass validate_agent_params unchanged (asserted in
# tests), and the wizard serves them from the options endpoint so
# the page renders cards from data, never hard-coded HTML.
TEMPLATES = [
    {"id": "morning-watcher-whatsapp",
     "name": "Morning Watcher Status → WhatsApp",
     "does": "Every morning, the estate health report lands on "
             "your phone.",
     "params": {"name": "Morning Watcher Status",
                "trigger": "schedule", "cron": "0 6 * * *",
                "source": "watcher_overview",
                "actions": ["whatsapp"]}},
    {"id": "capacity-disk-alert",
     "name": "Capacity Disk Alert → WhatsApp",
     "does": "Alert-only: WhatsApp you only when disk use is "
             "over 80%.",
     "params": {"name": "Capacity Disk Alert",
                "trigger": "schedule", "cron": "0 6 * * *",
                "source": "capacity",
                "actions": ["whatsapp"],
                "condition": {"field": "disk_pct", "op": ">",
                              "value": 80}}},
    {"id": "daily-capacity-report",
     "name": "Daily Capacity Report → Board",
     "does": "A daily headroom report filed to the board for "
             "the record.",
     "params": {"name": "Daily Capacity Report",
                "trigger": "schedule", "cron": "0 6 * * *",
                "source": "capacity",
                "actions": ["board"]}},
    {"id": "finops-summary-board",
     "name": "FinOps Summary → Board",
     "does": "Monday cost summary — spend, projection, and "
             "budget use.",
     "params": {"name": "FinOps Weekly Summary",
                "trigger": "schedule", "cron": "0 6 * * 1",
                "source": "finops",
                "actions": ["board"]}},
    {"id": "predictions-digest-board",
     "name": "Failure Predictions Digest → Board",
     "does": "Daily digest of what is most likely to fail next.",
     "params": {"name": "Failure Predictions Digest",
                "trigger": "schedule", "cron": "0 6 * * *",
                "source": "predictions",
                "actions": ["board"]}},
    {"id": "secops-cve-posture-board",
     "name": "SecOps CVE Posture → Board",
     "does": "Daily security posture — critical/high CVEs and "
             "known-exploited exposure.",
     "params": {"name": "SecOps CVE Posture",
                "trigger": "schedule", "cron": "0 6 * * *",
                "source": "secops",
                "actions": ["board"]}},
    {"id": "dot-bid-digest-board",
     "name": "DOT Bid Digest → Board",
     "does": "Daily count of DOT projects tracked across the "
             "14 states.",
     "params": {"name": "DOT Bid Digest",
                "trigger": "schedule", "cron": "0 6 * * *",
                "source": "dotkrc",
                "actions": ["board"]}},
]


def template_by_id(template_id: str) -> dict | None:
    for t in TEMPLATES:
        if t["id"] == template_id:
            return t
    return None


# "Start from scratch" guided Q&A (Rom, Nov 27, 2026): the wizard's
# third entry path — Ember asks a few questions, the user answers
# with chips (or free text), and the page shows which Metatron
# tools fit the answers as they are picked. The Q&A structure lives
# here (single source of truth, served by the options endpoint);
# answers assemble into agent params deterministically via
# scratch_params_from_answers — no AI call on the chip path. The
# free-text path and the optional "Ask Ember to refine" go through
# the normal metered design path instead.
SCRATCH_QUESTIONS = [
    {"id": "source", "text": "What do you want to keep an eye on?",
     "hint": "Pick one — Ember suggests the Metatron tool that "
             "watches it. Or type your own and Ember will design "
             "it from your words."},
    {"id": "schedule", "text": "How often should it run?",
     "hint": "Times are America/Chicago. You can fine-tune the "
             "exact schedule in Review."},
    {"id": "actions", "text": "What should happen with the report?",
     "hint": "WhatsApp sends it to your phone; the board files "
             "it on this page for the record."},
    {"id": "condition", "text": "Only when…?",
     "hint": "Optional: make it alert-only, so it acts only when "
             "a number crosses a threshold. “Always” sends every "
             "run's report."},
    {"id": "notes", "text": "Anything else Ember should know?",
     "hint": "Optional — e.g. “weekdays only” or “name it "
             "Morning Watch”. Ember uses it if you ask it to "
             "refine; the settings stay editable in Review."},
]

# Plain-words Q1 chips + the Metatron tools that watch each source.
# Tool names/one-liners mirror tools/catalog.json (the RPA process
# itself fetches the SOURCES endpoint; these are the Ember-side
# tools that read the same data, shown so the user sees what
# Metatron has for the job).
SCRATCH_SOURCES = [
    {"id": "watcher_overview", "plain": "Estate health (Watcher)",
     "short": "Estate Health",
     "tools": [
         {"name": "get_watcher_status",
          "does": "Live Watcher status from AIOps — components, "
                  "incidents, host vitals."},
         {"name": "cube_status",
          "does": "Live estate status via the MCP Bus — "
                  "components up/down, host vitals, open "
                  "incidents."}]},
    {"id": "capacity", "plain": "Capacity & headroom",
     "short": "Capacity",
     "tools": [
         {"name": "capacity_report",
          "does": "Capacity report — host saturation and the "
                  "disk-days-to-90 forecast."}]},
    {"id": "finops", "plain": "Costs (FinOps)", "short": "FinOps",
     "tools": [
         {"name": "finops_summary",
          "does": "FinOps summary — Cube run costs today, month "
                  "to date, projected, and budget use."}]},
    {"id": "secops", "plain": "Security posture", "short": "SecOps",
     "tools": [
         {"name": "secops_status",
          "does": "SecOps posture — CVE/KEV counts and the top "
                  "priority CVEs."}]},
    {"id": "predictions", "plain": "Failure predictions",
     "short": "Failure Predictions",
     "tools": [
         {"name": "predictions",
          "does": "Failure predictions — per-component risk "
                  "scores, bands, and reasons."}]},
    {"id": "dotkrc", "plain": "DOT bids", "short": "DOT Bids",
     "tools": [
         {"name": "DOT-KRC bid board status",
          "does": "Bid board totals across the 14 states — the "
                  "RPA process fetches it directly."}]},
]

SCRATCH_ACTION_TOOLS = {
    "whatsapp": [
        {"name": "run_workflow",
         "does": "Fires the proven Watcher Status → WhatsApp "
                 "flow, so the report lands on your phone."}],
    "board": [
        {"name": "Ember board filing",
         "does": "Files the composed report to the triage board "
                 "on this page."}],
}
SCRATCH_BUILDER_TOOL = {
    "name": "create_agent_flow",
    "does": "Builds this as a Metatron RPA process — created "
            "DISABLED until you enable it.",
}

# Suggested Q4 conditions per source: sensible static thresholds;
# the options endpoint enriches each with the live current value
# when the source read succeeds ("now 69.9"). Every suggestion
# must pass validate_agent_params (asserted in tests).
SCRATCH_CONDITION_SUGGESTIONS = {
    "watcher_overview": [
        {"field": "components_down", "op": ">", "value": 0,
         "label": "only when a component is down"},
        {"field": "open_incidents", "op": ">", "value": 0,
         "label": "only when incidents are open"},
        {"field": "disk_pct", "op": ">", "value": 80,
         "label": "only when disk is over 80%"},
    ],
    "capacity": [
        {"field": "disk_pct", "op": ">", "value": 80,
         "label": "only when disk is over 80%"},
        {"field": "mem_pct", "op": ">", "value": 85,
         "label": "only when memory is over 85%"},
        {"field": "disk_days_to_90", "op": "<", "value": 30,
         "label": "only when the disk fills within 30 days"},
    ],
    "predictions": [
        {"field": "high_count", "op": ">", "value": 0,
         "label": "only when high-risk components appear"},
        {"field": "risk_max", "op": ">", "value": 60,
         "label": "only when the top risk score passes 60"},
    ],
    "finops": [
        {"field": "budget_pct", "op": ">", "value": 80,
         "label": "only when projected spend passes 80% of "
                  "budget"},
        {"field": "cost_today_usd", "op": ">", "value": 15,
         "label": "only when today's cost passes $15"},
    ],
    "secops": [
        {"field": "critical_cves", "op": ">", "value": 0,
         "label": "only when critical CVEs are present"},
        {"field": "kev_in_set", "op": ">", "value": 0,
         "label": "only when known-exploited CVEs are in the "
                  "set"},
    ],
    "dotkrc": [
        {"field": "projects", "op": ">", "value": 0,
         "label": "only when projects are on the board"},
    ],
}


def _scratch_cadence(cron: str) -> str:
    """Plain cadence word for a cron, for the derived name."""
    parts = str(cron or "").strip().split()
    if len(parts) != 5:
        return "Scheduled"
    _mi, hh, _dom, _mon, dow = parts
    if hh == "*":
        return "Hourly"
    if dow == "*":
        return "Daily"
    if dow == "1-5":
        return "Weekday"
    if dow == "1":
        return "Monday"
    days = ["Sunday", "Monday", "Tuesday", "Wednesday",
            "Thursday", "Friday", "Saturday"]
    if dow.isdigit() and 0 <= int(dow) <= 6:
        return days[int(dow)]
    return "Scheduled"


def scratch_params_from_answers(answers: dict) -> dict:
    """Assemble agent params from guided-Q&A answers (deterministic,
    $0). Answers: {source, cron, actions, condition|null} — the
    chip path. The name is derived from the answers (editable in
    Review). Raises ValueError with a teaching message on bad
    answers; the caller still runs validate_agent_params as the
    final gate, so nothing here can smuggle past the allowlist."""
    if not isinstance(answers, dict):
        raise ValueError("answer the questions first — pick what "
                         "to watch, how often, and what to do "
                         "with the report")
    source = str(answers.get("source") or "").strip().lower()
    if source not in SOURCES:
        friendly = ", ".join(s["plain"] for s in SCRATCH_SOURCES)
        raise ValueError(
            f"pick what to keep an eye on — one of: {friendly}; "
            f"got {source!r}")
    cron = validate_cron(answers.get("cron") or "")
    raw_actions = answers.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError("pick what happens with the report — "
                         "WhatsApp, the board, or both")
    actions = []
    for a in raw_actions:
        a = str(a or "").strip().lower()
        if a not in ACTIONS:
            raise ValueError(
                "the report can go to WhatsApp and/or the "
                f"board; got {a!r}")
        if a not in actions:
            actions.append(a)
    condition = answers.get("condition")
    if condition is not None:
        condition = _validate_condition(condition, source)
    short = next(s["short"] for s in SCRATCH_SOURCES
                 if s["id"] == source)
    kind = "Alert" if condition else "Report"
    name = f"{short} {_scratch_cadence(cron)} {kind}"
    return {"name": name, "trigger": "schedule", "cron": cron,
            "source": source, "actions": actions,
            "condition": condition}
ALLOWED_KEYS = {"name", "trigger", "cron", "source", "action",
                "actions", "condition", "steps", "autonomy"}

# Autonomy (trust) levels per automation (Rom, Nov 26, 2026).
# The RPA flow itself is identical at every level — it runs on its
# schedule once enabled. The level governs REPORTING: auto-report
# expects a board action in the process (each run files its report
# to the board); auto-silent files nothing extra. propose-only is
# the default and the pre-existing behavior.
AUTONOMY_LEVELS = ("propose-only", "auto-report", "auto-silent")
AUTONOMY_LABELS = {
    "propose-only": "Propose-only (default)",
    "auto-report": "Auto-run + report to board",
    "auto-silent": "Auto-run silently",
}
CHAIN_MAX_STEPS = 4
CHAIN_KINDS = ("fetch", "condition", "action")

CONDITION_OPS = (">", ">=", "<", "<=", "==", "!=")

# Numeric fields a condition may test, per source — derived from the
# live payloads of the allowlisted endpoints (probed Nov 26, 2026).
# "path" resolves a dot-path into the unwrapped response body;
# "derived" computes path_a - path_b (e.g. components down).
CONDITION_FIELDS = {
    "watcher_overview": {
        "components_down": {
            "label": "Components down",
            "derived": ("totals.components", "totals.up")},
        "open_incidents": {"label": "Open incidents",
                           "path": "totals.open_incidents"},
        "components_up": {"label": "Components up",
                          "path": "totals.up"},
        "disk_pct": {"label": "Disk %", "path": "host.disk_pct"},
        "mem_pct": {"label": "Memory %", "path": "host.mem_pct"},
        "cpu_pct": {"label": "CPU %", "path": "host.cpu_pct"},
        "chat_pct": {"label": "Chat quality %",
                     "path": "quality.chat_pct"},
    },
    "capacity": {
        "disk_pct": {"label": "Disk %", "path": "host_disk_pct"},
        "mem_pct": {"label": "Memory %", "path": "host_mem_pct"},
        "cpu_pct": {"label": "CPU %", "path": "host_cpu_pct"},
        "gpu_mem_pct": {"label": "GPU memory %",
                        "path": "host_gpu_mem_pct"},
        "disk_days_to_90": {"label": "Days until disk 90%",
                            "path": "disk_days_to_90"},
        "components_down": {
            "label": "Components down",
            "derived": ("components_total", "components_up")},
    },
    "predictions": {
        "risk_max": {"label": "Max risk score", "path": "risk_max"},
        "high_count": {"label": "High-risk components",
                       "path": "high_count"},
        "watch_count": {"label": "Watch-band components",
                        "path": "watch_count"},
        "disk_pct": {"label": "Disk %", "path": "host_risk.disk_pct"},
        "disk_days_to_90": {"label": "Days until disk 90%",
                            "path": "host_risk.disk_days_to_90"},
    },
    "finops": {
        "budget_pct": {"label": "Budget % (projected)",
                       "path": "budget_pct"},
        "cost_today_usd": {"label": "Cost today (USD)",
                           "path": "cost_today_usd"},
        "cost_month_projected_usd": {
            "label": "Projected month cost (USD)",
            "path": "cost_month_projected_usd"},
        "daily_burn_usd": {"label": "Daily burn (USD)",
                           "path": "daily_burn_usd"},
    },
    "secops": {
        "critical_cves": {"label": "Critical CVEs",
                          "path": "kpis.critical"},
        "high_cves": {"label": "High CVEs", "path": "kpis.high"},
        "kev_in_set": {"label": "KEV in set", "path": "kpis.kev_in_set"},
        "kev_recent": {"label": "Recent KEV", "path": "kpis.kev_recent"},
        "components_down": {
            "label": "Components down",
            "derived": ("estate.components", "estate.up")},
        "open_incidents": {"label": "Open incidents",
                           "path": "estate.open_incidents"},
    },
    "dotkrc": {
        "projects": {"label": "Projects on board", "path": "projects"},
    },
}

WATCHER_FLOW_ID = "rBAcAYAXvIL2hNyrMQG1s"
BOARD_FILE_URL = "http://127.0.0.1:8023/api/board/file"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-—]{2,79}$")
_CRON_FIELD_RE = re.compile(r"^(\*|\d+(-\d+)?)(/\d+)?(,\d+(-\d+)?(/\d+)?)*$")
_CRON_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def validate_cron(cron: str) -> str:
    """Strict 5-field cron validation. Returns the cron or raises."""
    parts = str(cron or "").strip().split()
    if len(parts) != 5:
        raise ValueError(
            "that schedule isn't valid — pick a preset above, or use "
            "a 5-part cron like '0 6 * * *' (minute hour day month "
            f"weekday); got {cron!r}")
    for part, (lo, hi) in zip(parts, _CRON_RANGES):
        if not _CRON_FIELD_RE.match(part):
            raise ValueError(
                f"that schedule isn't valid — {part!r} is not a "
                "cron value; pick a preset above, or use a 5-part "
                "cron like '0 6 * * *'")
        for token in re.findall(r"\d+", part):
            if not (lo <= int(token) <= hi):
                raise ValueError(
                    f"that schedule isn't valid — {token} is out of "
                    f"range ({lo}–{hi}) in {part!r}; pick a preset "
                    "above, or use a 5-part cron like '0 6 * * *'")
    return " ".join(parts)


def _cron_field_values(field: str, lo: int, hi: int) -> set:
    """Expand one cron field (``*``, ``a``, ``a-b``, ``*/n``, ``a-b/n``,
    comma lists) to its value set. Input is pre-validated by
    validate_cron, so the shapes are the strict ones it accepts."""
    values = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


def next_cron_fire(cron: str, now=None, tz_name: str = "America/Chicago"):
    """Next fire time (aware datetime) for a validated 5-field cron.

    Standard cron semantics: when BOTH day-of-month and day-of-week
    are restricted, a day matches if EITHER matches; day-of-week 0
    and 7 are both Sunday. Computed in the flow's own timezone
    (America/Chicago, as assembled into the schedule trigger).
    Raises ValueError on an invalid cron or no fire within 2 years.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    cron = validate_cron(cron)
    tz = ZoneInfo(tz_name)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    f = cron.split()
    minutes = sorted(_cron_field_values(f[0], 0, 59))
    hours = sorted(_cron_field_values(f[1], 0, 23))
    doms = _cron_field_values(f[2], 1, 31)
    months = _cron_field_values(f[3], 1, 12)
    dows = {0 if d == 7 else d for d in _cron_field_values(f[4], 0, 7)}
    dom_restricted = f[2] != "*"
    dow_restricted = f[4] != "*"
    # First candidate minute strictly after `now`.
    start = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for day_off in range(0, 366 * 2):
        day = (start + timedelta(days=day_off)).date()
        if day.month not in months:
            continue
        dom_match = day.day in doms
        cron_dow = (day.weekday() + 1) % 7  # Mon=0..Sun=6 -> Sun=0..Sat=6
        dow_match = cron_dow in dows
        if dom_restricted and dow_restricted:
            day_ok = dom_match or dow_match
        elif dom_restricted:
            day_ok = dom_match
        elif dow_restricted:
            day_ok = dow_match
        else:
            day_ok = True
        if not day_ok:
            continue
        for h in hours:
            for m in minutes:
                cand = datetime(day.year, day.month, day.day, h, m,
                                tzinfo=tz)
                if cand >= start:
                    return cand
    raise ValueError(f"no fire time within 2 years for cron {cron!r}")


def _js_str(v) -> str:
    """Mirror JS String(v) for the scalars COMPOSE_CODE stringifies."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _unwrap_body(body):
    """Mirror the JS unwrap in COMPOSE_CODE: .output then .body,
    JSON-parse string bodies, {} for null."""
    o = body
    if isinstance(o, dict) and "output" in o:
        o = o["output"]
    b = o["body"] if isinstance(o, dict) and "body" in o else o
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except ValueError:
            b = {"raw": b}
    return {} if b is None else b


def _get_path(obj, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def condition_field_label(source: str, condition: dict) -> str:
    """Bare field label, e.g. 'Components down' — the exact string
    the flow's compose step uses in its messages and report suffix."""
    spec = CONDITION_FIELDS.get(source, {}).get(
        condition.get("field"), {})
    return spec.get("label", str(condition.get("field")))


def condition_label(source: str, condition: dict) -> str:
    """Human label for a condition, e.g. 'Components down > 0'."""
    return (f"{condition_field_label(source, condition)} "
            f"{condition.get('op')} {_js_str(condition.get('value'))}")


def evaluate_condition(body, source: str, condition: dict):
    """Python mirror of the JS gate in COMPOSE_CODE.

    Returns (actual, passed, error): actual is the numeric field
    value (None when unresolvable), passed is True/False, error is
    a string when the field is missing or non-numeric (the flow's
    code step throws in that case, so the action never runs)."""
    spec = CONDITION_FIELDS.get(source, {}).get(condition.get("field"))
    if spec is None:
        return None, False, (f"condition field "
                             f"{condition.get('field')!r} is not "
                             f"allowlisted for source {source!r}")
    b = _unwrap_body(body)
    if "derived" in spec:
        pa, pb = spec["derived"]
        va, vb = _get_path(b, pa), _get_path(b, pb)
        if (isinstance(va, bool) or isinstance(vb, bool)
                or not isinstance(va, (int, float))
                or not isinstance(vb, (int, float))):
            return None, False, (
                f"condition field {spec['label']} could not be "
                "computed from the source response — action skipped")
        actual = va - vb
    else:
        actual = _get_path(b, spec["path"])
        if isinstance(actual, bool) or not isinstance(actual,
                                                       (int, float)):
            return None, False, (
                f"condition field {spec['label']} is missing or "
                "not numeric in the source response — action skipped")
    value = condition.get("value")
    op = condition.get("op")
    if op == ">":
        passed = actual > value
    elif op == ">=":
        passed = actual >= value
    elif op == "<":
        passed = actual < value
    elif op == "<=":
        passed = actual <= value
    elif op == "==":
        passed = actual == value
    elif op == "!=":
        passed = actual != value
    else:
        return actual, False, f"unknown condition operator {op!r}"
    return actual, passed, ""


def compose_report_text(source_label: str, body,
                        source: str | None = None,
                        condition: dict | None = None) -> str:
    """Python port of COMPOSE_CODE — the exact text a built flow's
    compose step produces from a source response. Used by the
    dry-run Test so the captured payload matches what a real run
    would send/file. Unwraps .output then .body like the JS does;
    flattens scalars up to one nesting level; arrays become
    "N item(s)"; first 12 parts; 900-char cap. With a condition,
    the JS appends " · condition met: <label> = <actual>" (the gate
    has already passed — a failed gate throws before any text is
    returned); mirrored here when the condition evaluates true."""
    b = _unwrap_body(body)
    parts = []

    def walk(obj, prefix, depth):
        if depth > 1 or not isinstance(obj, dict):
            return
        for k, v in obj.items():
            if v is None or not isinstance(v, (dict, list)):
                parts.append(f"{prefix}{k}: {_js_str(v)}")
            elif isinstance(v, list):
                parts.append(f"{prefix}{k}: {len(v)} item(s)")
            else:
                walk(v, prefix + k + ".", depth + 1)

    walk(b, "", 0)
    text = (f"Cube report ({source_label}) — "
            + "; ".join(parts[:12]))
    if condition and source:
        actual, passed, _err = evaluate_condition(body, source,
                                                  condition)
        if passed:
            text += (f" · condition met: "
                     f"{condition_field_label(source, condition)} = "
                     f"{_js_str(actual)}")
    return text[:900]


def _validate_condition(raw, source: str) -> dict | None:
    """Validate an optional condition {field, op, value} against the
    per-source numeric field allowlist. None/absent -> None."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("the condition needs a field, a comparison "
                         "and a number — or untick it to act "
                         "unconditionally")
    unknown = set(raw) - {"field", "op", "value"}
    if unknown:
        raise ValueError(
            f"unknown condition setting(s): {sorted(unknown)} — a "
            "condition is just field, comparison (op) and value")
    fields = CONDITION_FIELDS.get(source, {})
    field = str(raw.get("field") or "").strip()
    if field not in fields:
        friendly = ", ".join(
            fspec["label"] for fspec in fields.values()) or "none"
        raise ValueError(
            f"that condition field can't be tested for "
            f"{SOURCES[source]['label']} — pick one of: {friendly}")
    op = str(raw.get("op") or "").strip()
    if op not in CONDITION_OPS:
        raise ValueError(
            "pick a condition comparison: > (greater than), >= "
            "(at least), < (less than), <= (at most), == (equals) "
            "or != (not equals)")
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"the condition value must be a number (e.g. 80), "
            f"got {value!r}")
    if not math.isfinite(value):
        raise ValueError("the condition value must be a finite "
                         "number (e.g. 80)")
    return {"field": field, "op": op, "value": value}


def _validate_steps(raw) -> list[dict]:
    """Validate an optional multi-step chain (Nov 26).

    A chain is a list of at most 4 steps:
      {"kind": "fetch", "source": <allowlisted source>}
      {"kind": "condition", "field":..., "op":..., "value":...}
          (gates on the most recent fetch; at most one per fetch)
      {"kind": "action", "action": "whatsapp"|"board"}
    Must start with a fetch, contain at least one fetch and one
    action, and every condition/action follows a fetch. Returns
    the clean chain."""
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            "a process chain needs at least a fetch step and an "
            "action step")
    if len(raw) > CHAIN_MAX_STEPS:
        raise ValueError(
            f"a process chain is capped at {CHAIN_MAX_STEPS} steps "
            f"(fetch / condition / action); got {len(raw)}")
    clean = []
    current_source = None
    fetches = actions = 0
    seen_condition_for_fetch = False
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("each process step must be an object "
                             "with a kind")
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in CHAIN_KINDS:
            raise ValueError(
                f"process step {i + 1}: kind must be fetch, "
                f"condition or action; got {kind!r}")
        if kind == "fetch":
            unknown = set(item) - {"kind", "source"}
            if unknown:
                raise ValueError(
                    f"fetch step has unknown setting(s): "
                    f"{sorted(unknown)}")
            source = str(item.get("source") or "").strip().lower()
            if source not in SOURCES:
                friendly = ", ".join(s["label"]
                                     for s in SOURCES.values())
                raise ValueError(
                    f"fetch step {i + 1}: pick one of: {friendly}; "
                    f"got {source!r}")
            current_source = source
            seen_condition_for_fetch = False
            fetches += 1
            clean.append({"kind": "fetch", "source": source})
        elif kind == "condition":
            if current_source is None:
                raise ValueError(
                    "a condition step must follow a fetch step")
            if seen_condition_for_fetch:
                raise ValueError(
                    "one condition per fetch — combine thresholds "
                    "or add another fetch")
            unknown = set(item) - {"kind", "field", "op", "value"}
            if unknown:
                raise ValueError(
                    f"condition step has unknown setting(s): "
                    f"{sorted(unknown)}")
            cond = _validate_condition(
                {k: item.get(k) for k in ("field", "op", "value")},
                current_source)
            seen_condition_for_fetch = True
            clean.append({"kind": "condition", **cond})
        else:  # action
            if current_source is None:
                raise ValueError(
                    "an action step must follow a fetch step")
            unknown = set(item) - {"kind", "action"}
            if unknown:
                raise ValueError(
                    f"action step has unknown setting(s): "
                    f"{sorted(unknown)}")
            action = str(item.get("action") or "").strip().lower()
            if action not in ACTIONS:
                raise ValueError(
                    "action steps must be 'WhatsApp status' or "
                    f"'File to board'; got {action!r}")
            actions += 1
            clean.append({"kind": "action", "action": action})
    if clean[0]["kind"] != "fetch" or fetches == 0 or actions == 0:
        raise ValueError(
            "a process chain starts with a fetch and needs at "
            "least one action step")
    return clean


def chain_summary(chain: list[dict]) -> list[str]:
    """Human-readable chain lines for results + the wizard."""
    out = []
    source = None
    for st in chain or []:
        if st["kind"] == "fetch":
            source = st["source"]
            out.append("fetch " + SOURCES[source]["label"])
        elif st["kind"] == "condition":
            out.append("only if " + condition_label(
                source, {k: st[k] for k in ("field", "op", "value")}))
        else:
            out.append("whatsapp notify" if st["action"] == "whatsapp"
                       else "file to board")
    return out


def validate_agent_params(params: dict) -> dict:
    """Deterministic allowlist validation. Returns clean params.

    Rejects anything outside the allowlist: unknown keys, unknown
    source/action/trigger, malformed cron, unsafe names, unknown
    condition fields/operators, non-numeric thresholds. Ember's
    proposal is untrusted input until this passes.

    Actions: `actions` (list of 1-2 distinct) or the legacy single
    `action` string; clean params carry `actions` plus `action`
    ("+".join) for older consumers. Condition: optional
    {field, op, value} — the flow's compose step gates on it.
    """
    if not isinstance(params, dict):
        raise ValueError("the automation settings are missing — "
                         "describe it again and let Ember design it")
    unknown = set(params) - ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f"unknown setting(s): {sorted(unknown)} — an automation "
            "only takes name, trigger, cron, source, actions and "
            "condition")
    name = str(params.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            "give the automation a name — 3 to 80 characters of "
            "letters, numbers, spaces, '-' or '_' (e.g. 'Daily "
            f"Capacity Report'); got {name!r}")
    trigger = str(params.get("trigger") or "").strip().lower()
    if trigger not in TRIGGERS:
        raise ValueError(
            "pick when it runs: 'schedule' (on a timer — the usual "
            "choice) or 'webhook' (when something calls its URL)")
    cron = ""
    if trigger == "schedule":
        cron = validate_cron(params.get("cron") or "")
    elif params.get("cron"):
        raise ValueError(
            "a schedule only applies to the 'schedule' trigger — "
            "switch the trigger to Schedule, or clear the cron")
    autonomy = str(params.get("autonomy") or "propose-only") \
        .strip().lower()
    if autonomy not in AUTONOMY_LEVELS:
        raise ValueError(
            "pick an autonomy level: propose-only (default), "
            "auto-report, or auto-silent")
    if params.get("steps") is not None:
        chain = _validate_steps(params.get("steps"))
        first_source = chain[0]["source"]
        given = str(params.get("source") or "").strip().lower()
        if given and given != first_source:
            raise ValueError(
                "the source setting disagrees with the first "
                "fetch step of the chain")
        chain_actions = [s["action"] for s in chain
                         if s["kind"] == "action"]
        first_cond = next(
            ({k: s[k] for k in ("field", "op", "value")}
             for s in chain if s["kind"] == "condition"), None)
        return {"name": name, "trigger": trigger, "cron": cron,
                "source": first_source, "actions": chain_actions,
                "action": "+".join(chain_actions),
                "condition": first_cond, "steps": chain,
                "autonomy": autonomy}
    source = str(params.get("source") or "").strip().lower()
    if source not in SOURCES:
        friendly = ", ".join(s["label"] for s in SOURCES.values())
        raise ValueError(
            f"pick what the automation fetches — one of: {friendly}; "
            f"got {source!r}")
    # Actions: list form wins; legacy single `action` still accepted.
    raw_actions = params.get("actions")
    if raw_actions is None and params.get("action") is not None:
        raw_actions = [params.get("action")]
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError(
            "pick at least one action — 'WhatsApp status', 'File to "
            "board', or both")
    actions = []
    for a in raw_actions:
        a = str(a or "").strip().lower()
        if a not in ACTIONS:
            raise ValueError(
                "actions must be 'WhatsApp status' and/or 'File to "
                f"board'; got {a!r}")
        if a in actions:
            raise ValueError(
                "each action can be picked only once — untick the "
                "duplicate")
        actions.append(a)
    if params.get("action") is not None and params.get("actions"):
        legacy = str(params.get("action") or "").strip().lower()
        # Accept the clean-params joined form ("whatsapp+board") so
        # already-validated params survive re-validation.
        if legacy not in actions and legacy != "+".join(actions):
            raise ValueError(
                "the action settings disagree — pick the actions "
                "with the checkboxes")
    condition = _validate_condition(params.get("condition"), source)
    return {"name": name, "trigger": trigger, "cron": cron,
            "source": source, "actions": actions,
            "action": "+".join(actions), "condition": condition,
            "steps": None, "autonomy": autonomy}


# ── Flow assembly (proven Agents-service shapes; NO LLM steps) ───────

ERR = {"retryOnFailure": {"value": False},
       "continueOnFailure": {"value": False}}
MANUAL_HTTP = {k: {"type": "MANUAL"} for k in
               ("url", "method", "headers", "body_type", "queryParams",
                "timeout")}

COMPOSE_CODE = """export const code = async (inputs) => {
  let o = inputs.response;
  if (o && o.output !== undefined) o = o.output;
  let b = o && o.body !== undefined ? o.body : o;
  if (typeof b === "string") { try { b = JSON.parse(b); } catch { b = { raw: b }; } }
  b = b || {};
  const parts = [];
  const walk = (obj, prefix, depth) => {
    if (depth > 1 || obj === null || typeof obj !== "object") return;
    for (const [k, v] of Object.entries(obj)) {
      if (v === null || typeof v !== "object") parts.push(prefix + k + ": " + v);
      else if (Array.isArray(v)) parts.push(prefix + k + ": " + v.length + " item(s)");
      else walk(v, prefix + k + ".", depth + 1);
    }
  };
  walk(b, "", 0);
  let text = "Cube report (" + inputs.source + ") — " + parts.slice(0, 12).join("; ");
  // Optional condition gate (inputs.cond_op set only when the process
  // has a condition): evaluate a numeric threshold against the
  // fetched body; throw when not met so the run stops BEFORE any
  // action step executes. Mirrors evaluate_condition in Python.
  if (inputs.cond_op) {
    const getPath = (obj, p) => p.split(".").reduce(
      (a, k) => (a !== null && typeof a === "object") ? a[k] : undefined, obj);
    let actual;
    if (inputs.cond_a) {
      const va = getPath(b, inputs.cond_a), vb = getPath(b, inputs.cond_b);
      actual = (typeof va === "number" && typeof vb === "number") ? va - vb : undefined;
    } else {
      actual = getPath(b, inputs.cond_path);
    }
    if (typeof actual !== "number") {
      throw new Error("Condition field " + inputs.cond_label +
        " is missing or not numeric in the source response — action skipped.");
    }
    const v = Number(inputs.cond_value), op = inputs.cond_op;
    const pass = op === ">" ? actual > v : op === ">=" ? actual >= v :
      op === "<" ? actual < v : op === "<=" ? actual <= v :
      op === "==" ? actual === v : actual !== v;
    if (!pass) {
      throw new Error("Condition not met: " + inputs.cond_label + " = " + actual +
        " (need " + op + " " + v + ") — action skipped by design.");
    }
    text += " · condition met: " + inputs.cond_label + " = " + actual;
    return { text: text.slice(0, 900), fields: parts.length,
             condition_passed: true, condition_value: actual };
  }
  return { text: text.slice(0, 900), fields: parts.length };
};"""


def _http_step(name, display, url, method="GET", body=None, headers=None,
               timeout=60):
    return {
        "name": name, "skip": False, "type": "PIECE", "valid": True,
        "displayName": display,
        "settings": {
            "pieceName": "@activepieces/piece-http",
            "actionName": "send_request", "pieceVersion": "0.13.3",
            "input": {"url": url, "method": method,
                      "headers": headers or {},
                      "body_type": "json" if body is not None else "none",
                      "body": {"data": body} if body is not None else {},
                      "queryParams": {}, "timeout": timeout},
            "propertySettings": MANUAL_HTTP, "errorHandlingOptions": ERR,
        },
        "nextAction": None,
    }


def _code_step(name, display, inputs, code):
    return {
        "name": name, "skip": False, "type": "CODE", "valid": True,
        "displayName": display,
        "settings": {"input": inputs, "useDeno": True,
                     "sourceCode": {"code": code, "packageJson": "{}"},
                     "errorHandlingOptions": {}},
        "nextAction": None,
    }


def _return_step(name, display, text_ref):
    return {
        "name": name, "skip": False, "type": "PIECE", "valid": True,
        "displayName": display,
        "settings": {
            "pieceName": "@activepieces/piece-webhook",
            "actionName": "return_response", "pieceVersion": "0.1.43",
            "input": {"fields": {"body": {"text": text_ref}, "status": 200,
                                 "headers": {}},
                      "respond": "stop", "responseType": "json"},
            "propertySettings": {
                "fields": {"type": "MANUAL", "schema": {
                    "body": {"type": "JSON", "required": True,
                             "displayName": "JSON Body"},
                    "status": {"type": "NUMBER", "required": False,
                               "displayName": "Status", "defaultValue": 200},
                    "headers": {"type": "OBJECT", "required": False,
                                "displayName": "Headers"}}},
                "respond": {"type": "MANUAL"},
                "responseType": {"type": "MANUAL"}},
            "errorHandlingOptions": ERR,
        },
        "nextAction": None,
    }


def _compose_inputs_for(source: str, condition, fetch_ref: str,
                        src_label: str) -> dict:
    inputs = {"response": fetch_ref, "source": src_label}
    if condition:
        spec = CONDITION_FIELDS[source][condition["field"]]
        inputs["cond_label"] = spec["label"]
        inputs["cond_op"] = condition["op"]
        inputs["cond_value"] = _js_str(condition["value"])
        if "derived" in spec:
            inputs["cond_a"], inputs["cond_b"] = spec["derived"]
        else:
            inputs["cond_path"] = spec["path"]
    return inputs


def _compose_inputs(params: dict, src_label: str) -> dict:
    """Inputs for the compose CODE step. With a condition, the gate
    inputs ride along (cond_op/cond_value/cond_label + the field
    path(s) from the allowlist — never free-form)."""
    return _compose_inputs_for(params["source"],
                               params.get("condition"),
                               "{{step_1}}", src_label)


def _action_step(n: int, action: str, name: str, source: str,
                 compose_ref: str, board_key: str):
    if action == "whatsapp":
        return _http_step(
            f"step_{n}", "Notify via Watcher WhatsApp flow",
            "http://127.0.0.1:8085/api/v1/webhooks/"
            + WATCHER_FLOW_ID + "/sync",
            method="POST",
            body={"agent": name, "source": source,
                  "summary": compose_ref},
            headers={"Content-Type": "application/json"},
            timeout=120)
    return _http_step(
        f"step_{n}", "File report to Ember board",
        BOARD_FILE_URL, method="POST",
        body={"title": name, "source": source,
              "summary": compose_ref},
        headers={"Content-Type": "application/json",
                 "X-API-Key": board_key}, timeout=60)


def _build_chain_steps(params: dict, board_key: str) -> list:
    """AP steps for a validated chain: per fetch a fetch+compose
    pair (condition gate inside compose, proven shape), actions
    reference the most recent compose step's text."""
    chain = params["steps"]
    steps = []
    compose_name = None
    compose_source = None
    for i, st in enumerate(chain):
        if st["kind"] == "fetch":
            src = SOURCES[st["source"]]
            n = len(steps) + 1
            steps.append(_http_step(f"step_{n}",
                                    "Fetch " + src["label"],
                                    src["url"]))
            cond = None
            if (i + 1 < len(chain)
                    and chain[i + 1]["kind"] == "condition"):
                c = chain[i + 1]
                cond = {k: c[k] for k in ("field", "op", "value")}
            compose_name = f"step_{n + 1}"
            compose_source = st["source"]
            steps.append(_code_step(
                compose_name,
                "Compose report + condition gate" if cond
                else "Compose report",
                _compose_inputs_for(st["source"], cond,
                                    "{{step_%d}}" % n, src["label"]),
                COMPOSE_CODE))
        elif st["kind"] == "action":
            steps.append(_action_step(
                len(steps) + 1, st["action"], params["name"],
                compose_source, "{{%s['text']}}" % compose_name,
                board_key))
        # condition steps are folded into their fetch's compose
    return steps


def build_flow_trigger(params: dict, board_key: str = "") -> dict:
    """Assemble the full trigger tree for validated params.

    Process shape: trigger -> fetch -> compose (with the optional
    condition gate inside) -> one HTTP step per action (whatsapp
    and/or board, in the given order) -> webhook return step."""
    if params.get("steps"):
        steps = _build_chain_steps(params, board_key)
        last_compose = None
        for s in steps:
            if s["type"] == "CODE":
                last_compose = s["name"]
        if params["trigger"] == "webhook":
            steps.append(_return_step(
                f"step_{len(steps) + 1}", "Return result",
                "{{%s['text']}}" % last_compose))
    else:
        src = SOURCES[params["source"]]
        cond = params.get("condition")
        steps = [
            _http_step("step_1", "Fetch " + src["label"], src["url"]),
            _code_step("step_2",
                       "Compose report + condition gate" if cond
                       else "Compose report",
                       _compose_inputs(params, src["label"]),
                       COMPOSE_CODE),
        ]
        for action in params["actions"]:
            steps.append(_action_step(
                len(steps) + 1, action, params["name"],
                params["source"], "{{step_2['text']}}", board_key))
        if params["trigger"] == "webhook":
            steps.append(_return_step(f"step_{len(steps) + 1}",
                                      "Return result",
                                      "{{step_2['text']}}"))
    for i in range(len(steps) - 1):
        steps[i]["nextAction"] = steps[i + 1]
    first = steps[0]
    if params["trigger"] == "schedule":
        return {
            "name": "trigger", "type": "PIECE_TRIGGER", "valid": True,
            "displayName": "Cron Expression",
            "settings": {
                "pieceName": "@activepieces/piece-schedule",
                "pieceVersion": "0.1.22", "triggerName": "cron_expression",
                "input": {"timezone": "America/Chicago",
                          "cronExpression": params["cron"]},
                "propertySettings": {},
            },
            "nextAction": first,
        }
    return {
        "name": "trigger", "valid": True, "displayName": "Catch Webhook",
        "type": "PIECE_TRIGGER",
        "settings": {
            "pieceName": "@activepieces/piece-webhook",
            "pieceVersion": "0.1.43", "triggerName": "catch_webhook",
            "input": {"authType": "none", "authFields": {}},
            "propertySettings": {
                "authType": {"type": "MANUAL"},
                "authFields": {"type": "MANUAL", "schema": {}},
                "liveMarkdown": {"type": "MANUAL"},
                "syncMarkdown": {"type": "MANUAL"},
                "testMarkdown": {"type": "MANUAL"},
            },
        },
        "nextAction": first,
    }


def _ap_call(base: str, token: str, method: str, path: str,
             body: dict | None = None) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json",
                 "User-Agent": "ember-orchestrator/0.4"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def create_agent_flow(params: dict, ap_base: str, ap_token: str,
                      project: str, board_key: str = "") -> dict:
    """Create the flow as a DISABLED draft. Never publishes."""
    clean = validate_agent_params(params)
    trigger = build_flow_trigger(clean, board_key=board_key)
    created = _ap_call(ap_base, ap_token, "POST", "/flows",
                       {"projectId": project, "displayName": clean["name"]})
    flow_id = created["id"]
    _ap_call(ap_base, ap_token, "POST", "/flows/" + flow_id,
             {"type": "IMPORT_FLOW",
              "request": {"displayName": clean["name"],
                          "trigger": trigger}})
    flow = _ap_call(ap_base, ap_token, "GET", "/flows/" + flow_id)
    if clean.get("steps"):
        steps = chain_summary(clean["steps"])
    else:
        steps = ["fetch " + clean["source"], "compose report"]
        if clean["condition"]:
            steps.append("condition: " + condition_label(
                clean["source"], clean["condition"]))
        steps += ["whatsapp notify" if a == "whatsapp"
                  else "file to board" for a in clean["actions"]]
    return {
        "flow_id": flow_id,
        "name": clean["name"],
        "status": flow.get("status", "DISABLED"),
        "trigger": clean["trigger"],
        "cron": clean["cron"] or None,
        "source": clean["source"],
        "source_label": SOURCES[clean["source"]]["label"],
        "action": clean["action"],
        "actions": clean["actions"],
        "condition": clean["condition"],
        "chain": clean.get("steps"),
        "autonomy": clean.get("autonomy", "propose-only"),
        "steps": steps,
        "note": ("Created as a DISABLED draft in Metatron RPA — it "
                 "will not run until a human enables it with the "
                 "Enable button on the Ember page."),
    }


def enable_agent_flow(flow_id: str, ap_base: str, ap_token: str) -> dict:
    """Publish + enable a builder-created draft. AP 0.93 (probed
    Nov 26, 2026): LOCK_AND_PUBLISH flips a fresh draft to ENABLED;
    CHANGE_STATUS ENABLED covers an already-published-but-disabled
    flow. Idempotent — safe on an already-enabled flow."""
    flow = _ap_call(ap_base, ap_token, "GET", "/flows/" + flow_id)
    if not flow.get("publishedVersionId"):
        _ap_call(ap_base, ap_token, "POST", "/flows/" + flow_id,
                 {"type": "LOCK_AND_PUBLISH", "request": {}})
        flow = _ap_call(ap_base, ap_token, "GET", "/flows/" + flow_id)
    if flow.get("status") != "ENABLED":
        _ap_call(ap_base, ap_token, "POST", "/flows/" + flow_id,
                 {"type": "CHANGE_STATUS",
                  "request": {"status": "ENABLED"}})
        flow = _ap_call(ap_base, ap_token, "GET", "/flows/" + flow_id)
    return {
        "flow_id": flow_id,
        "name": flow.get("displayName")
        or (flow.get("version") or {}).get("displayName") or "",
        "status": flow.get("status", "UNKNOWN"),
        "published_version_id": flow.get("publishedVersionId"),
    }


def disable_agent_flow(flow_id: str, ap_base: str, ap_token: str) -> dict:
    """Disable a builder-created flow (undo of enablement).
    Idempotent — safe on an already-disabled flow."""
    flow = _ap_call(ap_base, ap_token, "GET", "/flows/" + flow_id)
    if flow.get("status") == "ENABLED":
        _ap_call(ap_base, ap_token, "POST", "/flows/" + flow_id,
                 {"type": "CHANGE_STATUS",
                  "request": {"status": "DISABLED"}})
        flow = _ap_call(ap_base, ap_token, "GET", "/flows/" + flow_id)
    return {"flow_id": flow_id, "status": flow.get("status", "UNKNOWN")}


def delete_agent_flow(flow_id: str, ap_base: str, ap_token: str) -> dict:
    """Delete a builder-created flow (undo of creation). Disables
    first when enabled. AP 0.93 DELETE needs a {} body (204)."""
    try:
        disable_agent_flow(flow_id, ap_base, ap_token)
    except Exception:  # noqa: BLE001 — still try the delete
        pass
    _ap_call(ap_base, ap_token, "DELETE", "/flows/" + flow_id, {})
    return {"flow_id": flow_id, "deleted": True}
