# MAC (Metatron Agentic Commander) — README DRAFT (Nov 27, 2026)

> ⚠️ **Superseded Nov 27, 2026** — this draft became the repo [README.md](../README.md) (with [ARCHITECTURE.md](ARCHITECTURE.md) and [DEMO-GUIDE.md](DEMO-GUIDE.md) as companions). Kept for history; do not edit.

> DRAFT for Rom's approval. Not yet the repo README; nothing pushed.
> Replaces the Nov 24 README (Phase 0, three demos, "pending organizer
> review") and the Nov 26 draft (29 tools, $0.1474, 265 tests).
> Updated Nov 27, 2026 after the overnight MAC build: 31 tools,
> 391/391 tests verified locally, meter $0.37 of $10 (reported
> Nov 27 08:28 UTC; final figure lands at the Dec 1 freeze).
> Refreshed again Nov 27 after the Medical Manual and Paralegal
> builds: 33 tools, 19 demos, 456/456 tests, meter $0.50 of $10
> (reported Nov 27 14:46 UTC; re-verify at the Dec 1 freeze).
> Updated Nov 27 after the 4-group demo-page reorganization
> (AIOps / Process Automation / Business Use Cases / Grounding,
> everything collapsed on load): 470/470 tests at HEAD afdf34d;
> re-verify meter and tests at the Dec 1 freeze before push.

**MAC — Metatron Agentic Commander**
Powered by: Meta's Muse Ember
*You prompt. MAC checks. You approve. Metatron Cube executes.*

Muse Ember as the intelligent orchestrator for the **Metatron Cube** — a
private LLM + RPA appliance by [Metagen Group](https://metagengroup.com).
Users describe what they need in natural language; Ember plans the work
over a catalog of **deterministic** Cube tools; a human approves; the
Cube executes — and Ember verifies the result.

Built for the **Muse Ember Hackathon** (Cerebral Valley, Nov–Dec 2026).
**Accepted.** Live demo: https://ember.metatroncube.online (apex door:
https://metatroncube.online/ember).

## Architecture

```
User ──> MAC / Ember Orchestrator (only brain: Muse Ember 1.0, Meta Model API)
              │  proposes a plan — nothing runs yet
              ▼
         Human approval (in-screen; single-use approval token)
              │
              ▼
         Cube Tool API — 33 deterministic tools, discovered at runtime
              │           from the Metatron MCP Bus (static catalog fallback)
              ▼
         Metatron Cube (Watcher, ITSM, SecOps, FinOps, RPA flows…)
              │
              ▼
         Ember verification beat — re-checks each step, reports verified/failed
```

The pattern is deliberate: the model proposes, people decide, deterministic
systems execute, and the model verifies. Write tools refuse to run without
the approval token, and every Ember call passes through a spend meter first.
Ember is also exposed back on the MCP Bus (`ember_status`,
`ember_propose`, `ember_list_automations`) — deliberately with no
approve/execute tool on the bus, so human approval stays in the MAC screen.

## What's live (Nov 27, 2026)

- **33 deterministic tools** across IT Operations, Help Desk, Finance,
  Security, Risk & Impact, Capacity Planning, Build & Automate, and
  Document Intake — ask "What can you do?" for the full table. Read tools
  are discovered from the Metatron MCP Bus at runtime (`tools/list`,
  schemas generated from the bus); eligibility-excluded tools (other-model
  paths) never enter the catalog.
- **19 demos**, including Watcher → WhatsApp, Help-desk Triage Desk,
  Shift Handover Brief, FinOps summary, blast radius, CVE exposure,
  capacity outlook, Document Intelligence, dashboard building, the
  PLC Code Converter, Medical Manual Q&A, and the Paralegal Assistant.
- **Universal PLC Code Converter (AB ⇄ Siemens)** — Ember's long-context
  and coding demo: ~180K tokens of vendor manual (Rockwell 1756-RM003,
  Siemens S7-1200 System Manual; 2,053 pages ingested box-side, never
  committed to this repo) are fed as context, Ember converts the program,
  and a deterministic coverage validator accounts for **every** source
  instruction — unsupported instructions are flagged with redesign notes,
  never silently replaced. Converted code is stored as **DRAFT** and
  routed through a Metatron RPA change-approval flow (ITSM change record
  + approval waitpoint); a qualified engineer signs off before anything
  real. Demo-grade, labelled as such.
- **Dashboard Builder** — Ember designs a dashboard spec against the live
  data profile, a deterministic validator checks it, the human approves,
  and Metatron's renderer builds it. No other model in the path.
- **Medical Manual Q&A (practitioner assistant)** — the Merck Manual of
  Diagnosis & Therapy, 19th Edition (4,114 pages, 17,651 sections) as a
  deterministic retrieval corpus: ask about a symptom and Ember answers
  only from the manual, one cited summary block per relevant chapter
  (presentation, differentials, workup, management) plus a synthesis,
  with a "not medical advice" disclaimer on every answer. Out-of-scope
  questions get an honest "not covered."
- **Paralegal Assistant** — the same pattern over US public-domain
  primary law: the U.S. Code (53 titles, 88,234 sections), the Federal
  Rules (Civil, Criminal, Evidence, Bankruptcy, Appellate), the U.S.
  Constitution, and the CFR (49 of 50 titles, 324,148 sections) —
  413,116 sections total. Answers cite per section (e.g. 28 U.S.C.
  § 1331, Fed. R. Civ. P. 26(b)(1)) with a "legal information, not
  legal advice" disclaimer; state law honestly declines.
- **Multimodal intake ("Paste Anything")**: image, PDF, audio, video, or
  text — Ember reads them itself; extraction is filed only after human
  approval. Invoice totals are checked by a deterministic verifier
  (items → subtotal → total, balance arithmetic) — Ember reports the
  verifier's verdict, never its own arithmetic.
- **Deterministic file export**: Excel/CSV/JSON for tabular results,
  Word/TXT for transcripts — Ember proposes, human approves, Cube
  generates, download card appears.
- **Agent Builder Wizard, three ways in**: 7 pre-built templates,
  "Describe it myself" (guided, with examples), and a five-question
  "Start from scratch" Q&A that suggests the Metatron tools that fit as
  you answer. All paths land on Review in plain words ("Every weekday at
  6:00 AM, Metatron will…") and create **DISABLED** drafts on Metatron
  RPA; enablement approval stays in the MAC screen. Every built
  automation has an Edit-flow link and a side-effect-free Test dry-run.
- **Agentic layer**: verification loop, multi-step chained automations
  (fetch → condition → action → action), proactive suggestions from live
  data, goal tracking, confidence chips + clarifying questions, learning
  from your wizard edits, per-automation autonomy levels, and one-tap
  undo (WhatsApp honestly says "can't be unsent").
- **Honest refusal**: out-of-scope requests get "NO TOOL FITS" — no
  invented capabilities.

## The $10 discipline

Every call is authorized *before* it is made and recorded after (purpose,
tokens, cost, running total) in the trace ledger — which is also the
submission's required prompt/token trace. Gates: warn at **$7.00**,
non-essential calls halt at **$9.00**, absolute stop at **$9.75**.
Pricing: $0.10 / 1M input, $0.50 / 1M output.
As of Nov 27, 2026: **$0.50 of $10 spent, 470/470 tests passing**
(after the 4-group demo-page reorganization; re-verify at freeze)
(`python3 -m unittest discover -s tests`).

## Eligibility note

Muse Ember is the only AI model in the submitted path. All Cube-side work
is deterministic (RPA flows, validators, renderers, file generation, data
reads); tools that would invoke other models are excluded from the
catalog by design.

## Status

- Phase 0–4 complete, plus the Nov 26–27 MAC build: MCP Bus discovery
  both ways, templates-first + Q&A wizard, dashboard builder, PLC Code
  Converter, invoice totals verifier, eight agentic upgrades, Medical
  Manual practitioner Q&A, and the Paralegal Assistant.
- **Dec 1:** feature freeze. **Dec 2, noon ET:** submission target
  (public repo + 3-minute demo video + prompt/token trace).
