# Ember submission readiness — Nov 27, 2026 (updated)

> ⚠️ **Superseded Nov 27, 2026** — current state, the v3 video script, and the live checklist now live in [DEMO-GUIDE.md](DEMO-GUIDE.md); the repo README is [README.md](../README.md). Kept for history; do not edit.

Submission = public GitHub repo + 3-minute demo video + prompt/token trace,
via cerebralvalley.ai/e/muse-ember-hackathon/hackathon/submit.
Freeze Dec 1; submit Dec 2 by noon ET (9 PM is buffer, not target).

## Where it stands (verified Nov 27, 2026 ~09:53 ET)

- Live build (MAC — Metatron Agentic Commander): **33 tools, 19 demos**,
  MCP Bus discovery both ways, templates-first + Q&A Agent Builder
  Wizard, Dashboard Builder, Universal PLC Code Converter (AB ⇄ Siemens),
  deterministic invoice totals verifier, eight agentic upgrades
  (verification, chaining, suggestions, goals, confidence, learning,
  autonomy, undo), Medical Manual practitioner Q&A (Merck Manual 19th
  Ed., per-chapter cited summaries), and the Paralegal Assistant (USC +
  Federal Rules + Constitution + CFR, 413,116 sections, per-section
  citations).
- **456/456 tests passing** — reported Nov 27 14:46 UTC after the
  Paralegal build (supersedes 391/391 from Nov 27 morning and 423/423
  after the Medical practitioner rework). Re-run
  `python3 -m unittest discover -s tests` at the final rehearsal
  before quoting.
- Meter: **$0.50 of $10** (reported Nov 27 14:46 UTC; supersedes $0.37
  from Nov 27 08:28 UTC and $0.1474 / 265 tests from Nov 26 evening).
  Final figure lands at the Dec 1 freeze after rehearsals.
- Local HEAD: **e968bd9** ("Paralegal Assistant: ask_paralegal tool
  (33 tools)…"), after 76877da (Medical per-chapter summaries) and
  7fdb389 (Medical Manual Q&A, 32 tools). Working tree clean except
  untracked `tmp/`. The two docs files are **tracked** in git; numbers
  in both docs refreshed to the 33-tool / 456-test / $0.50 state on
  Nov 27 (this refresh).
- GitHub: last verified remote head **a723894** (Phase 3, Nov 26 12:34
  UTC) on rsiquijor/ember-orchestrator — everything since (wizard,
  export, MCP, dashboard, PLC, MAC rename) is local-only and still
  needs the push. Note: the local repo has **no git remote configured**
  (.git/config has no [remote] section) — pushes have gone through the
  GitHub connector/snapshot sync with Rom present for the approval
  card, not a plain `git push`. Delivery: re-check the remote head live
  before pushing; do not assume a723894 is still current.
- Trace ledger: the copy in this repo (`spend/trace.json` / `trace.csv`,
  125 lines) is a **Nov 26 snapshot and is stale** against the live
  $0.50 meter. The submission trace must be exported fresh from the
  live box after the last rehearsal run.
- README on disk is still the Nov 24 Phase-0 version ("pending organizer
  review", three demos). Updated draft: docs/README-draft-nov26.md
  (content refreshed to the Nov 27 MAC state incl. Medical + Paralegal)
  — needs Rom's go-ahead to become README.md. Approval was requested
  Nov 27 06:55 ET; no reply yet — do not re-ask today; next escalation
  Nov 29 if still silent.
- No demo video recorded yet.
- Open approvals in RPA (Rom's call, not submission blockers):
  PLC change records **RFC-0007** and **RFC-0008** sit at their approval
  waitpoints; Built Automations also shows "Capacity Disk Alert"
  (DISABLED) and "Daily Ticket Report" (ENABLED), both left untouched.

## 3-minute video script (v2 — PLC converter as centerpiece)

- 0:00 Problem: nontechnical users, disconnected tools, automation locked
  behind experts; industrial sites can't send data to the cloud.
- 0:20 Architecture in one breath: MAC — Ember is the brain, the Cube is
  the hands, a human approves, Ember verifies. Show the stage rail
  (Human → Ember → Approve → Metatron → Ember → Next).
- 0:40 Demo 1 — Watcher status → WhatsApp, live approval + verified beat.
- 1:05 Demo 2 — Paste Anything invoice: Ember extracts, the deterministic
  verifier flags the planted $7.50 discrepancy (and passes a correct
  invoice clean), approve → export to Excel.
- 1:35 Demo 3 — Universal PLC Code Converter: paste an Allen-Bradley
  program → Ember converts to Siemens using ~180K tokens of vendor
  manual as long context → coverage report (6 mapped, 2 flagged, never
  silently replaced) → approve → DRAFT stored → RPA change-approval
  record filed. This one demo shows Ember's 1M-token context and coding
  strengths together.
- 2:25 Demo 4 (fast) — Agent Builder Wizard: "What do you need?" →
  template or start-from-scratch Q&A → plain-words summary → DISABLED
  draft → Test dry-run. (Or swap in Dashboard Builder if time allows.)
- 2:45 Close: spend meter ($0.50 of $10) and trace ledger; data stayed
  in the Cube. Final spend figure on screen.
- Optional swap (Rom's call at approval time): the Medical practitioner
  Q&A and Paralegal Assistant are second/third long-context proofs —
  either can replace Demo 4 or get a 15-second mention in the close if
  Rom wants them in the video and README headline.

## Remaining before Dec 2 noon ET

1. Rom approves README draft → replace README.md, commit, push to
   GitHub (rsiquijor/ember-orchestrator) via the connector with Rom
   present for the approval card, verify remote read-back.
2. Record the 3-minute video (script above); Rom reviews.
3. Final trace export from the live box after the last rehearsal run
   (the repo's spend/ copy is a stale Nov 26 snapshot).
4. Submit via the Cerebral Valley portal; complete organizer survey.
5. Optional if Rom wants them before freeze: demo gaps #3–#7 from
   Nov 27 (volume batch, one continuous story, visible speed/cost,
   error recovery, multimodal fusion) — build only on his word;
   freeze discipline otherwise.
