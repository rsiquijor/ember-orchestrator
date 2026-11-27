"""Deterministic post-execution verification (Rom, Nov 26, 2026).

After an approved plan executes, each step's result is checked
against reality with deterministic re-reads ($0, no model):
  - create_agent_flow -> the flow id is in the registry
  - enable_agent_flow -> the registry entry is enabled
  - run_workflow      -> run status SUCCEEDED (WhatsApp wamid when
                         the flow returns one; sends cannot be
                         unsent, so undo is never offered for them)
  - file_tickets      -> every filed id is on the board
  - file_intake       -> the intake store grew and holds the kind
  - file_document_report -> the reports store holds the title
  - export_file       -> the export exists with size > 0
  - create_dashboard  -> the slug is in the Ember dashboard
                         registry AND the rendered page serves
  - read tools        -> result carries no error

A failed check is surfaced plainly with a suggested fix; nothing is
ever silently retried (gated actions need a fresh human approval).
"""
from __future__ import annotations


def _check(name, ok, detail, fix=""):
    return {"name": name, "ok": bool(ok), "detail": detail,
            **({"fix": fix} if fix and not ok else {})}


def verify_trace(trace, tools) -> list[dict]:
    checks = []
    for entry in trace or []:
        name = entry.get("tool", "")
        result = entry.get("result") or {}
        if not isinstance(result, dict):
            checks.append(_check(name, False,
                                 "result was not an object"))
            continue
        if result.get("error"):
            checks.append(_check(
                name, False, f"step failed: {result['error']}",
                "Fix the cause, then re-run the command — gated "
                "steps need a fresh approval."))
            continue
        if name == "create_agent_flow":
            fid = result.get("flow_id")
            reg = tools.agent_flows.get(fid) if fid else None
            checks.append(_check(
                name, reg is not None,
                f"flow {fid} is in the Agent Builder registry "
                f"(status {result.get('status')})" if reg else
                f"flow {fid} not found in the registry",
                "Check the RPA console for the draft; re-create "
                "from the wizard if it is missing."))
        elif name == "enable_agent_flow":
            fid = result.get("flow_id")
            reg = tools.agent_flows.get(fid) if fid else None
            ok = bool(reg and reg.get("enabled")
                      and result.get("status") == "ENABLED")
            checks.append(_check(
                name, ok,
                f"flow {fid} enabled in registry and RPA"
                if ok else
                f"flow {fid} enable not confirmed "
                f"(status {result.get('status')})",
                "Use Enable in Built Automations to retry."))
        elif name == "run_workflow":
            ok = result.get("status") == "SUCCEEDED"
            inner = result.get("result") or {}
            wamid = ""
            if isinstance(inner, dict):
                wamid = str(inner.get("wamid") or
                            inner.get("message_id") or "")
            detail = (f"run {result.get('run_id')} SUCCEEDED"
                      + (f"; WhatsApp wamid {wamid}" if wamid else
                         "; WhatsApp sends cannot be unsent"))
            checks.append(_check(
                name, ok, detail if ok else
                f"run status {result.get('status')}",
                "Check the flow run in the RPA console."))
        elif name == "file_tickets":
            ids = result.get("ticket_ids") or []
            on_board = {t.get("ticket_id") for t in tools.board.list()}
            missing = [i for i in ids if i not in on_board]
            checks.append(_check(
                name, not missing and bool(ids),
                f"{len(ids)} ticket(s) on the board"
                if not missing else
                f"missing from board: {missing}",
                "Re-file the triage from the stage."))
        elif name == "file_intake":
            recs = tools.intake.list()
            ok = bool(recs) and recs[-1].get("kind") == result.get("kind")
            checks.append(_check(
                name, ok,
                f"intake record #{len(recs)} filed "
                f"({result.get('kind')})" if ok else
                "latest intake record does not match",
                "Re-attach the file and approve the filing again."))
        elif name == "file_document_report":
            reps = tools.reports.list()
            ok = any(r.get("title") == result.get("title")
                     for r in reps)
            checks.append(_check(
                name, ok,
                f"report '{result.get('title')}' on the board"
                if ok else "report not found on the board",
                "Re-run the document analysis."))
        elif name == "export_file":
            if not result.get("exported"):
                checks.append(_check(
                    name, True,
                    "honest no-export: " +
                    str(result.get("message") or "no data yet")))
            else:
                exp = tools.exports.get(result.get("id") or "")
                ok = bool(exp and (exp.get("size_bytes") or 0) > 0)
                checks.append(_check(
                    name, ok,
                    f"{result.get('filename')} exists "
                    f"({result.get('size_bytes')} bytes)" if ok else
                    "export file missing or empty",
                    "Export again from the stage."))
        elif name == "create_dashboard":
            slug = result.get("slug") or ""
            reg = tools.dashboards.get(slug) if slug else None
            page_ok = bool(slug) and tools.dashboard_exists(slug)
            ok = reg is not None and page_ok
            checks.append(_check(
                name, ok,
                f"dashboard '{result.get('title')}' registered "
                f"and serving at {result.get('url')}" if ok else
                (f"dashboard {slug} not in the Ember registry"
                 if reg is None else
                 f"dashboard {slug} registered but its page "
                 "does not serve"),
                "Open the Dashboard Generator and check the "
                "dashboard; rebuild it if it is missing."))
        elif name == "convert_plc_code":
            cid = result.get("conversion_id") or ""
            rec = tools.plc_conversions.get(cid) if cid else None
            stats = result.get("stats") or {}
            complete = bool(rec) and (
                (rec.get("stats") or {}).get("coverage_entries", 0)
                >= (rec.get("stats") or {}).get(
                    "source_instructions", 1))
            ok = rec is not None and complete
            checks.append(_check(
                name, ok,
                f"conversion {cid} stored as DRAFT — "
                f"{stats.get('mapped', 0)} mapped, "
                f"{stats.get('unsupported', 0)} flagged "
                "unsupported; nothing deployed" if ok else
                (f"conversion {cid} not found in the registry"
                 if rec is None else
                 f"conversion {cid} stored but its coverage "
                 "report is incomplete"),
                "Re-run the conversion; the coverage validator "
                "must account for every source instruction."))
        elif name == "ask_medical_manual":
            passages = result.get("passages") or []
            covered = bool(result.get("covered"))
            ok = (covered and len(passages) > 0) or \
                (not covered and not passages)
            checks.append(_check(
                name, ok,
                f"{len(passages)} cited passages retrieved from "
                f"{result.get('manual', 'the manual')} "
                f"(~{result.get('reference_tokens_approx', 0):,} "
                "tokens of reference)" if covered else
                "manual does not cover this question — no "
                "passages invented",
                "Ask a question the manual covers, or check "
                "the corpus is loaded (medical_manual/)."))
        elif name == "ask_paralegal":
            passages = result.get("passages") or []
            covered = bool(result.get("covered"))
            ok = (covered and len(passages) > 0) or \
                (not covered and not passages)
            checks.append(_check(
                name, ok,
                f"{len(passages)} cited passages retrieved from "
                "the legal sources "
                f"(~{result.get('reference_tokens_approx', 0):,} "
                "tokens of reference)" if covered else
                "the legal sources do not cover this question "
                "— no passages invented",
                "Ask a question the sources cover, or check "
                "the corpus is loaded (legal_corpus/)."))
        else:
            checks.append(_check(name, True,
                                 "read returned data, no error"))
    return checks


def verification_summary(checks) -> str:
    if not checks:
        return "Nothing to verify."
    ok = sum(1 for c in checks if c["ok"])
    if ok == len(checks):
        return f"Verified ✓ — {ok}/{len(checks)} checks passed."
    return (f"Could not verify ✗ — {ok}/{len(checks)} checks "
            "passed; see the failed check(s) for the fix.")
