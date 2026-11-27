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

from .cube_tools import GATED_TOOLS, ApprovalTokens, CubeTools
from .ember_client import EmberClient
from .spend import SpendMeter

STEP_CAP = 8

# Appended to the compose-final turn: Ember ends its summary with one
# machine-readable line of next-step suggestions (parsed + stripped by
# parse_next_steps below). One call does both jobs — the summary and
# the recommendations — so next steps cost no extra Ember call.
NEXT_STEPS_INSTRUCTION = (
    "Write your final answer now, grounded only in the tool results "
    "above. If a tool result contains a totals_check verdict, report "
    "it exactly as given (mismatch -> state its detail; ok -> totals "
    "verified) and never add your own arithmetic analysis or claim "
    "a discrepancy the verdict does not report. Then, on its own "
    "final line, append next-step suggestions "
    "in exactly this format:\n"
    'NEXT_STEPS: [{"label": "short button label", "kind": "action", '
    '"command": "a follow-up command the catalog tools can do"}, '
    '{"label": "short button label", "kind": "automate", "command": '
    '"Build a new automated agentic workflow."}]\n'
    "Rules for NEXT_STEPS: 2 to 4 items total, at most one \"automate\" "
    "item; every suggestion must follow logically from the results "
    "above (name the specific finding, component, or number); action "
    "commands must be one-time actions answerable by the catalog "
    "tools (read checks, \"Send the Watcher status to my WhatsApp.\", "
    "filing to the board, exporting results to a file — e.g. \"Save "
    "the last extraction as Excel.\", \"List the dashboards.\" after "
    "building one — there is no generic email or "
    "arbitrary-message tool, so never suggest one); the automate "
    "command must be exactly \"Build a new automated agentic "
    "workflow.\" — no tail: the Agent Builder Wizard designs the "
    "specifics (schedule, source, condition, actions) from the "
    "run context, so put the specific idea in the label instead "
    "(e.g. \"Automate a daily capacity check\"). If nothing sensible "
    "follows, append NEXT_STEPS: [] instead.")

NEXT_STEPS_KINDS = ("action", "automate")
NEXT_STEPS_CAP = 4


def _validate_next_steps(payload: str) -> list[dict]:
    """Parse + validate the JSON array after NEXT_STEPS:. Anything
    malformed yields [] — the page falls back to Automate + Done."""
    try:
        data = json.loads(payload)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data[:NEXT_STEPS_CAP]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        command = str(item.get("command") or "").strip()
        if not label or not command or kind not in NEXT_STEPS_KINDS:
            continue
        out.append({"label": label[:80], "kind": kind,
                    "command": command[:400]})
    return out


def parse_next_steps(text: str) -> tuple[str, list[dict]]:
    """Split a trailing NEXT_STEPS: [...] line off Ember's summary.

    Returns (clean_text, steps). The marker line is stripped whether
    or not its JSON parses — it is machine noise, never user-facing.
    Action commands still go through the normal propose path (Ember
    re-plans them, a human approves), so a bad suggestion can do no
    harm; validation here is about shape, not safety."""
    if not text:
        return text or "", []
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if line.startswith("NEXT_STEPS:"):
            steps = _validate_next_steps(line[len("NEXT_STEPS:"):].strip())
            clean = "\n".join(lines[:i] + lines[i + 1:]).strip()
            return clean, steps
    return text, []

def assess_confidence(steps: list[dict]) -> tuple[str, dict | None]:
    """Deterministic confidence for a proposed plan (Nov 26).

    high   — steps present and every required field is filled.
    medium — steps present but a default was derived (e.g. a
             schedule with no cron yet).
    low    — no steps, or a create_agent_flow step is missing a
             required field that cannot be derived. Only the low
             agent-build case carries ONE clarifying question with
             tappable answers; the derivation rule (fb5e00c) means
             we never nag when context can fill the gap."""
    if not steps:
        return "low", None
    for s in steps:
        if s.get("name") == "create_dashboard":
            args = s.get("arguments") or {}
            eps = args.get("endpoints")
            if not eps or (isinstance(eps, list) and not eps):
                return "low", {
                    "question": "What should the dashboard show?",
                    "options": ["Capacity", "FinOps costs",
                                "Estate health"]}
            if not str(args.get("title") or "").strip():
                return "medium", None
        if s.get("name") == "create_agent_flow":
            args = s.get("arguments") or {}
            if not str(args.get("source") or "").strip():
                return "low", {
                    "question": "What should the automation check?",
                    "options": ["Watcher status", "Capacity",
                                "FinOps costs"]}
            if not str(args.get("trigger") or "").strip():
                return "low", {
                    "question": "When should it run?",
                    "options": ["Every day at 6 AM",
                                "Weekdays at 6 AM", "Every hour"]}
            if (str(args.get("trigger")).lower() == "schedule"
                    and not str(args.get("cron") or "").strip()):
                return "medium", None
    return "high", None


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
    "- For Cube cost questions, call finops_summary.\n"
    "- For 'what breaks if X goes down' / blast-radius questions, call "
    "graph_dependents with the component slug (the brain is "
    "'model-endpoint').\n"
    "- For CVE / security-posture questions, call secops_status. It is "
    "read-only data — never claim an investigation ran.\n"
    "- For capacity / headroom / forecast questions, call "
    "capacity_report and predictions.\n"
    "- For 'full picture' / overall health-and-risk requests, chain "
    "several read tools in one plan (cube_status, graph_dependents "
    "for the brain 'model-endpoint', secops_status, predictions, "
    "finops_summary) and write one consolidated brief.\n"
    "- To build an automated agentic workflow / automation "
    "('build a new automated agentic workflow'), call "
    "create_agent_flow ONCE with "
    "high-level params only: name, trigger ('schedule' with a "
    "5-field cron, or 'webhook'), source (one of: "
    "watcher_overview, capacity, predictions, finops, secops, "
    "dotkrc), actions (a list with 'whatsapp' to notify via the "
    "proven Watcher WhatsApp flow and/or 'board' to file the "
    "report — both allowed, run in order), and an optional "
    "condition {field, op, value} so the action(s) run only when "
    "the fetched data crosses a threshold. Condition fields per "
    "source — watcher_overview: components_down, open_incidents, "
    "components_up, disk_pct, mem_pct, cpu_pct, chat_pct; capacity: "
    "disk_pct, mem_pct, cpu_pct, gpu_mem_pct, disk_days_to_90, "
    "components_down; predictions: risk_max, high_count, "
    "watch_count, disk_pct, disk_days_to_90; finops: budget_pct, "
    "cost_today_usd, cost_month_projected_usd, daily_burn_usd; "
    "secops: critical_cves, high_cves, kev_in_set, kev_recent, "
    "components_down, open_incidents; dotkrc: projects. Ops: >, "
    ">=, <, <=, ==, !=. Propose a condition when the request or "
    "context implies 'only if / when / alert me if' (e.g. "
    "components_down > 0, disk_pct > 80, budget_pct > 80). Never "
    "emit flow JSON — the Cube assembles it deterministically as "
    "a Metatron RPA process. The result is a DISABLED draft a "
    "human must enable. When the user asks to build an automated "
    "agentic workflow, agent, or automation WITHOUT spelling out "
    "every parameter, DERIVE the missing parameters from the run "
    "context provided with the request (or the request itself) "
    "instead of asking or refusing: source from the tools just "
    "used (capacity_report -> capacity; get_watcher_status or "
    "cube_status -> watcher_overview; predictions -> predictions; "
    "finops_summary -> finops; secops_status -> secops); trigger "
    "default 'schedule' with cron '0 6 * * *' (daily 06:00 "
    "America/Chicago) unless the user stated otherwise; a short "
    "name derived from the context (e.g. 'Daily Capacity Report', "
    "'Watcher Status Alert'); actions from the context "
    "('whatsapp' for Watcher status, 'board' otherwise). "
    "Proposing derived defaults is expected and safe: a human "
    "reviews and edits every parameter before approval, and the "
    "result is only a DISABLED draft. Refusing because "
    "name/trigger/source were not spelled out is wrong whenever "
    "run context is available — only ask the user when there is "
    "genuinely no context to derive from.\n"
    "- To enable a built automation ('enable the automation flow "
    "<id>'), call enable_agent_flow ONCE with the exact flow_id. "
    "Only Agent-Builder-created drafts can be enabled; a human "
    "approves before it publishes.\n"
    "- When an image or PDF is attached, read it yourself and call "
    "file_intake with the fields you extracted (kind + fields object; "
    "for invoices: vendor, invoice_number, date, po_number, "
    "line_items, subtotal, total_due). Report only what is visible: "
    "include amount_paid and balance_due ONLY when the invoice "
    "explicitly prints them — if a field is blank or absent, omit "
    "it; never copy another field's value into it. Put only factual "
    "observations in notes (e.g. illegible text); do NOT put "
    "arithmetic verdicts in notes — the filing tool verifies "
    "invoice totals deterministically and returns a totals_check "
    "verdict, and your summary must report that verdict exactly, "
    "never your own discrepancy claim.\n"
    "- When an audio clip is attached, listen yourself and call "
    "file_intake with kind 'audio_note' and fields: transcript (the "
    "exact words spoken), plus any structured items you hear (work "
    "order numbers, tasks, priorities, dates, action items). Report "
    "only what is audible.\n"
    "- When a video is attached, watch it yourself and call "
    "file_intake with kind 'video' and fields: on_screen_text (exact "
    "text shown), summary (what happens), and transcript (any speech, "
    "exact words). Report only what is visible or audible.\n"
    "- When a document is attached for analysis, parse it and call "
    "file_document_report with a title, a short summary, and findings "
    "(line number as numbered in the text, severity, detail). Cite "
    "only lines that exist.\n"
    "- To save or export results as a file ('save that as Excel', "
    "'export as CSV', 'give me a Word file', 'download as JSON'), "
    "call export_file ONCE with format ('csv', 'xlsx', 'json', 'txt', "
    "or 'docx') and source: 'intake' for the most recent filed "
    "extraction (invoices, photos, audio/video transcripts) — this "
    "is the default whenever the user says 'save that/it as ...' "
    "after an intake filing; 'board' for the triage board tickets; "
    "'document' for the most recent document-analysis report; "
    "'last_run' ONLY when the user asks to export a run's or "
    "report's results (e.g. 'export those results', 'save that "
    "report as a spreadsheet'); 'plc' for the most recent PLC "
    "code conversion (converted code + coverage report — use "
    "this when the user says 'save the converted code as ...' "
    "after a conversion); 'medical' for the most recent "
    "medical-manual answer (question + cited passages — use "
    "this when the user says 'save the medical answer as ...' "
    "after a medical question); 'legal' for the most recent "
    "paralegal answer (question + cited passages — use this "
    "when the user says 'save the legal answer as ...' after "
    "a legal question). File generation is "
    "deterministic "
    "($0); a human approves before the file is created. If a "
    "document is attached AND a file format is requested in the "
    "same request, call file_intake first and export_file second "
    "in the same plan.\n"
    "- To build a dashboard ('build a dashboard showing ...', "
    "'create a dashboard for ...'), call create_dashboard ONCE. "
    "You design the dashboard: a title, 1-3 endpoints (data "
    "sources: watcher-overview, capacity, predictions, finops, "
    "leases, sentiment, finclose, dotkrc-status, agents-status), "
    "KPI cards {label, field, format} and charts {type, title, "
    "from, label_field, value_field} chosen for what the user "
    "asked to see. The DATA PROFILE for the chosen sources is "
    "provided with the request (fact names with sample values, "
    "list names with columns) — reference ONLY those names "
    "exactly; with 2+ endpoints every name is prefixed with its "
    "source (capacity_host_disk_pct, finops_costs_today_usd, "
    "list capacity_components). A deterministic validator checks "
    "the spec against the live profile before approval is "
    "offered; Metatron then renders it — you never write HTML. "
    "Add refresh_frequency only when the user asks for scheduled "
    "refreshes (the refresh flow is created DISABLED). Derive "
    "the endpoints and title from the request or run context "
    "instead of refusing (capacity/disk -> capacity; costs/"
    "budget -> finops; estate health/status -> "
    "watcher-overview; failures/risk -> predictions; tickets/"
    "sentiment -> sentiment; leases -> leases; month-end -> "
    "finclose; bids -> dotkrc-status).\n"
    "- To convert PLC code ('convert this Allen-Bradley program "
    "to Siemens', 'convert PLC code', 'translate this SCL to "
    "Studio 5000'), call convert_plc_code ONCE. You write the "
    "conversion yourself: direction ('ab_to_siemens' or "
    "'siemens_to_ab'), program_name, source_code (the full "
    "source program — from the request or the sample program "
    "provided with it), converted_code (the full program in "
    "the target platform's language), coverage (one entry per "
    "source instruction: {instruction, status 'mapped' with "
    "its target equivalent, or 'unsupported' with a note "
    "explaining the redesign needed}), and notes (assumptions "
    "an engineer must check). The vendor instruction-reference "
    "manuals are provided with the request as long context — "
    "convert using them. A deterministic validator checks the "
    "coverage report accounts for EVERY source instruction and "
    "that the converted code uses target-platform instructions "
    "only; unsupported instructions must be flagged in coverage "
    "and left out of the converted code with an explanatory "
    "comment — never silently replaced or invented. The result "
    "is a DRAFT for qualified-engineer review: never claim it "
    "is deployed, safety-certified, or production-ready. "
    "Derive the direction from the request or context instead "
    "of refusing (Allen-Bradley/Studio 5000/Logix -> Siemens/"
    "SCL/TIA = ab_to_siemens; the reverse = siemens_to_ab).\n"
    "- For medical questions ('what does the manual say "
    "about...', symptoms, diagnosis, treatment, causes of a "
    "condition), call ask_medical_manual ONCE with the user's "
    "question. The tool retrieves passages from The Merck "
    "Manual of Diagnosis & Therapy (19th Ed.) by deterministic "
    "keyword search. Answer ONLY from the passages it returns "
    "— never from general medical knowledge, and never "
    "supplement them. Paraphrase in your own words; do not "
    "quote long passages verbatim. Cite the chapter, section, "
    "and pages for every claim (e.g. '(Chapter 28. Hepatitis "
    "— Diagnosis, pp. 324-325)'). Organize the answer as one "
    "block per relevant chapter — a '## Chapter N. Title' "
    "heading, then a concise clinical summary of that "
    "chapter's relevant content (presentation, differential "
    "considerations, workup, management, as the passages "
    "present them) — and end with a brief synthesis when "
    "several chapters apply; this per-chapter structure "
    "takes precedence over brevity for medical answers. If "
    "the tool returns "
    "covered=false, say plainly that the manual does not "
    "cover that question and stop there. Describe what the "
    "manual says in general terms only — never give "
    "personalized diagnosis or treatment recommendations for "
    "the user or anyone else. This is a demo reference, not "
    "medical advice.\n"
    "- For legal questions (statutes, the U.S. Code, federal "
    "court rules, the Constitution, regulations, jurisdiction, "
    "discovery, evidence, deadlines, procedure), call "
    "ask_paralegal ONCE with the user's question. The tool "
    "retrieves passages from US public-domain legal sources "
    "(U.S. Code, Federal Rules, US Constitution, CFR) by "
    "deterministic keyword search. Answer ONLY from the "
    "passages it returns — never from general legal "
    "knowledge, and never supplement them. Paraphrase in "
    "your own words; do not quote long passages verbatim. "
    "Cite the section or rule for every claim (e.g. '(28 "
    "U.S.C. § 1331)', '(Fed. R. Civ. P. 26(b)(1))', '(U.S. "
    "Const. amend. XIV, § 1)', '(29 C.F.R. § 1910.95)'). "
    "Organize the answer as one block per relevant "
    "source/title — a '## Title 28 — Judiciary and Judicial "
    "Procedure' style heading, then a concise summary of "
    "that source's relevant content (the provision, its "
    "scope, requirements, exceptions, deadlines, as the "
    "passages present them) — and end with a brief "
    "synthesis when several sources apply; this per-source "
    "structure takes precedence over brevity for legal "
    "answers. If the tool returns covered=false, say "
    "plainly that these sources do not cover that question "
    "and stop there. Describe what the law says in general "
    "terms only — never give legal advice for a specific "
    "case or predict how a court would rule. This is legal "
    "information, not legal advice.\n"
    "- Other read-only reports are available (CMDB assets/changes, "
    "tickets, RCA records, dashboards, RPA flows, brain status, "
    "connectors, graph search, leases, sentiment, FinClose demo KPIs) — "
    "call the matching tool.\n"
    "- If a request matches no tool or workflow, say so plainly, name what "
    "you can do (Watcher status, WhatsApp status send, ticket triage, "
    "handover brief, Cube costs, blast radius, CVE posture, capacity "
    "outlook, full-picture briefs, building RPA agents, image/PDF/audio/"
    "video intake, document analysis, exporting results to CSV/Excel/"
    "Word/TXT/JSON files, building dashboards from Cube data, "
    "converting PLC code between Allen-Bradley and Siemens "
    "(DRAFT, engineer-gated), answering medical questions "
    "from the Merck Manual corpus with citations (demo "
    "reference, not medical advice), answering legal "
    "questions from US public-domain sources (U.S. Code, "
    "Federal Rules, Constitution, CFR) with citations "
    "(legal information, not legal advice), other "
    "read-only Cube reports), "
    "and do not invent "
    "capabilities, data, or results. (Deriving create_agent_flow "
    "parameters from run context, as instructed above, is not "
    "inventing — it is the expected behavior.)\n"
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
        self.system = SYSTEM_PROMPT

    @property
    def tool_schemas(self) -> list[dict]:
        """Effective Ember schemas from the tools backend: the static
        catalog for fixtures; bus-discovered (eligibility-filtered,
        merged over the catalog) for the live HttpCubeTools. Read
        per call so discovery TTL refreshes propagate."""
        return self.tools.tool_schemas()

    # ------------------------------------------------------------- propose
    def propose(self, user_text: str, purpose: str = "experiment",
                note_suffix: str = "",
                max_tokens: int | None = None,
                context: str = "") -> dict:
        """Ask Ember for a plan. Executes NOTHING.

        `context` (optional, server-assembled run context) is
        appended to the user content Ember sees — e.g. so a bare
        "Build a new automated agentic workflow." can derive its
        parameters from the last run — while the plan's recorded
        display text stays the user's original command."""
        content = user_text
        if context:
            content = user_text + "\n\n" + context
        return self._propose(content, user_text, purpose, note_suffix,
                             max_tokens=max_tokens)

    def propose_deterministic(self, steps: list[dict],
                              display_text: str, rationale: str,
                              purpose: str = "integration") -> dict:
        """Build a proposed plan WITHOUT an Ember call ($0).

        Used by the wizard's pre-built templates: the steps are
        deterministic pre-fills the human reviews and edits, so no
        AI is needed to propose them. The plan carries the same
        shape as an Ember-proposed one (system + user messages), so
        approve() runs the identical gated path — token minting,
        allowlist re-validation, execution, and the compose turn.
        Eligibility note: the template params are data, not AI
        output; Ember remains the only AI in the path."""
        plan_id = "plan-" + secrets.token_hex(4)
        plan = {
            "id": plan_id,
            "user_text": display_text,
            "purpose": purpose,
            "rationale": rationale,
            "steps": [{"name": s["name"],
                       "arguments": dict(s["arguments"]),
                       "id": s.get("id") or f"call_{i}_{s['name']}"}
                      for i, s in enumerate(steps)],
            "status": "proposed",
            "messages": [{"role": "system", "content": self.system},
                         {"role": "user", "content": display_text}],
            "confidence": "high",
            "clarifying_question": None,
        }
        self.plans[plan_id] = plan
        return plan

    def propose_content(self, content, display_text: str,
                        purpose: str = "experiment",
                        note_suffix: str = "",
                        max_tokens: int | None = None) -> dict:
        """Propose from multimodal content parts (image/PDF/audio/video
        intake) or a long document body. `display_text` is the
        human-readable command recorded with the plan. Binary payloads
        are scrubbed from the stored conversation after the call so
        the compose turn does not re-send (and re-pay for) the bytes."""
        plan = self._propose(content, display_text, purpose, note_suffix,
                             max_tokens=max_tokens)
        binary_labels = {"image_url": "image", "file": "file",
                         "input_audio": "audio", "video_url": "video",
                         "input_video": "video", "input_file": "file",
                         "input_image": "image"}
        for msg in plan["messages"]:
            if isinstance(msg.get("content"), list):
                msg["content"] = [
                    ({"type": "text",
                      "text": f"[{binary_labels[p.get('type')]} attached "
                              "— read by Ember]"}
                     if isinstance(p, dict)
                     and p.get("type") in binary_labels
                     else p)
                    for p in msg["content"]]
            elif (isinstance(msg.get("content"), str)
                  and len(msg["content"]) > 4000):
                # Long document: the compose turn only needs the tool
                # call + result, not the body again.
                msg["content"] = (display_text
                                  + " [document body omitted from replay]")
        return plan

    def _propose(self, user_content, display_text: str,
                 purpose: str = "experiment",
                 note_suffix: str = "",
                 max_tokens: int | None = None) -> dict:
        self.meter.authorize(purpose)
        messages = [{"role": "system", "content": self.system},
                    {"role": "user", "content": user_content}]
        # tool_choice "required" is not supported by the API (only "auto"),
        # so if Ember narrates a plan instead of calling tools, nudge once.
        resp = self.ember.chat(messages, self.tool_schemas,
                               max_tokens=max_tokens)
        self.meter.record(purpose, resp.prompt_tokens, resp.completion_tokens,
                          note="propose" + (f":{note_suffix}" if note_suffix else ""))
        truncated = (resp.finish_reason == "length"
                     or any(c.args_truncated for c in resp.tool_calls))
        # Retry only when there is nothing usable: no calls at all, or a
        # call whose arguments were cut off. Calls that parsed cleanly
        # stand even if trailing prose hit the cap.
        unusable = (not resp.tool_calls
                    or any(c.args_truncated for c in resp.tool_calls))
        if unusable:
            # Two failure shapes land here: (a) Ember narrated instead of
            # calling tools — nudge it; (b) the reply was TRUNCATED at
            # max_tokens (finish_reason "length" / unparseable call
            # arguments) — Ember is a reasoning model and a messy photo
            # can burn the whole budget reasoning before any tool call
            # (Rom's intake failure, Nov 26: two calls at exactly the
            # 512 cap, no call emitted). For (b) the retry also gets a
            # much larger token budget.
            retry_tokens = max_tokens
            if truncated:
                retry_tokens = min(4096, max(2048, (max_tokens or 512) * 4))
            self.meter.authorize(purpose)
            retry = messages + [
                {"role": "assistant", "content": resp.text},
                {"role": "user", "content":
                 "Express that plan as tool calls now. Do not describe the "
                 "calls — make them. If no tool fits the request, reply "
                 "with plain text saying so and make no calls."}]
            resp = self.ember.chat(retry, self.tool_schemas,
                                   max_tokens=retry_tokens)
            self.meter.record(purpose, resp.prompt_tokens,
                              resp.completion_tokens,
                              note="propose-retry"
                              + (f":{note_suffix}" if note_suffix else ""))
        plan_id = "plan-" + secrets.token_hex(4)
        plan = {
            "id": plan_id,
            "user_text": display_text,
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
        plan["confidence"], plan["clarifying_question"] = \
            assess_confidence(plan["steps"])
        self.plans[plan_id] = plan
        return plan

    # ------------------------------------------------------------- approve
    def approve(self, plan_id: str, note_suffix: str = "",
                param_overrides: dict | None = None) -> dict:
        """Human approval: mint a fresh single-use token per gated step,
        execute the plan's tool calls in order, then let Ember compose the
        final answer from the results.

        param_overrides ({step_index: arguments}) lets a human edit a
        step's arguments at approval time — the Agent Builder Wizard's
        Review step. Overrides for create_agent_flow are re-validated
        against the deterministic allowlist BEFORE any token is minted
        or step executed (ValueError -> nothing runs, plan untouched),
        and the trace records both the proposed and the final
        arguments, so what was approved is auditable."""
        plan = self.plans[plan_id]
        if plan["status"] != "proposed":
            raise ValueError(f"plan {plan_id} is {plan['status']}, not proposed")
        overrides = {int(k): v for k, v in (param_overrides or {}).items()}
        for idx, new_args in overrides.items():
            if not (0 <= idx < len(plan["steps"])):
                raise ValueError(f"param override for unknown step {idx}")
            if not isinstance(new_args, dict):
                raise ValueError("param override must be an object")
            if plan["steps"][idx]["name"] == "create_agent_flow":
                from .agent_builder import validate_agent_params
                validate_agent_params(new_args)
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
            args = dict(overrides.get(i, step["arguments"]))
            if step["name"] in GATED_TOOLS:
                args["approval_token"] = self.tokens.issue()
            if step["name"] in ("file_tickets", "file_intake",
                                "file_document_report",
                                "create_agent_flow",
                                "enable_agent_flow") and note_suffix:
                args["run_tag"] = note_suffix
            try:
                result = self.tools.call(step["name"], args)
            except Exception as e:  # noqa: BLE001 — feed back, don't crash
                result = {"error": f"{type(e).__name__}: {e}"}
            entry = {"tool": step["name"],
                     "arguments": {k: v for k, v in args.items()
                                   if k not in ("approval_token",
                                                "run_tag")},
                     "result": result}
            if i in overrides:
                entry["proposed_arguments"] = step["arguments"]
            trace.append(entry)
            messages.append({"role": "tool", "name": step["name"],
                             "tool_call_id": step.get("id")
                             or f"call_{i}_{step['name']}",
                             "content": json.dumps(result)})
        # One compose call writes the summary AND the NEXT_STEPS
        # suggestions (parsed + stripped below; absent/malformed ->
        # [] and the page falls back to Automate + Done).
        messages.append({"role": "user",
                         "content": NEXT_STEPS_INSTRUCTION})
        # Medical answers carry one summary block per relevant
        # chapter (up to 5) — they need a larger compose
        # budget than the default 1024 tokens. Legal answers
        # carry one summary block per relevant source/title
        # (up to 5) — same larger budget.
        compose_tokens = 1024
        if any(t["tool"] in ("ask_medical_manual", "ask_paralegal")
               and (t["result"] or {}).get("covered")
               for t in trace):
            compose_tokens = 4096
        self.meter.authorize(plan["purpose"])
        resp = self.ember.chat(messages, self.tool_schemas,
                               max_tokens=compose_tokens)
        self.meter.record(plan["purpose"], resp.prompt_tokens,
                          resp.completion_tokens,
                          note="compose-final"
                          + (f":{note_suffix}" if note_suffix else ""))
        plan["status"] = "executed"
        plan["trace"] = trace
        # Verification loop (Nov 26): deterministic re-reads of what
        # the execution claims to have done ($0, no extra Ember
        # call). Failures surface with a fix; nothing auto-retries.
        from .verifier import verify_trace, verification_summary
        plan["verification"] = verify_trace(trace, self.tools)
        plan["verification_summary"] = verification_summary(
            plan["verification"])
        # The export_file 'last_run' source reads the previous
        # completed run's trace from the tools object.
        self.tools.last_run_trace = trace
        plan["final_text"], plan["next_steps"] = parse_next_steps(resp.text)
        return plan

    def reject(self, plan_id: str) -> dict:
        plan = self.plans[plan_id]
        plan["status"] = "rejected"
        return plan
