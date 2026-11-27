# MAC Demo Guide — judging map, video script, submission checklist

Companion to the [README](../README.md) and [ARCHITECTURE.md](ARCHITECTURE.md). This guide maps the 19 demos to the Muse Ember Hackathon judging criteria, gives the refreshed 3-minute video script for the current build, and tracks the submission checklist.

**Contents**

- [How to run the demos](#how-to-run-the-demos)
- [The 19 demos mapped to judging criteria](#the-19-demos-mapped-to-judging-criteria)
- [3-minute video script (v3 — current build)](#3-minute-video-script-v3--current-build)
- [Submission checklist](#submission-checklist)

---

## How to run the demos

Open **[ember.metatroncube.online](https://ember.metatroncube.online)**:

- **▶ Recorded** — replays a real run (recorded Nov 27, 2026) beat by beat: prompt → plan → approval → execution → verification → summary → next steps. Free, no login. This is the fastest way for a judge to see everything.
- **⚡ Live** — runs Ember now against the live Cube (Cube login, Operator+). Live runs are metered; each costs a fraction of a cent.
- **Chips** under USE CASES load a prompt into the composer — editable, never auto-run. Press **CHECK** to propose.
- **"What can you do?"** returns the full 33-tool capabilities table instantly ($0).

Every recorded card shows its run's real token counts and cost — the budget story is visible on the page itself.

## The 19 demos mapped to judging criteria

Judging criteria: **Technicality** (depth of engineering), **Creativity** (novelty of the idea/use), **Demo** (clarity and polish of the presentation), **Potential** (real-world impact).

### 1 · AIOps — operate

| Demo | Primary criterion | What it demonstrates |
|---|---|---|
| Watcher Status → WhatsApp | Demo | The complete loop in one tap: live read → approval → real RPA send → verified wamid. The "it actually works" opener. |
| Shift Handover Brief | Potential | A daily operational artifact written from facts — the kind of drudgery LLMs should absorb. |
| Full Picture Brief | Technicality | Five chained tool calls in one plan, consolidated — multi-tool orchestration, not a single lookup. |
| Help-desk Triage Desk | Potential | Batch classification + draft replies with a human gate before filing — AI drafts, humans send. |
| FinOps Summary | Potential | "What is this costing us?" answered from the platform's own cost service. |
| CVE Exposure Check | Potential | CISO-grade posture from live NVD/KEV/EPSS data, read-only by construction. |
| Blast Radius | Technicality | Knowledge-graph traversal (transitive dependents) behind one plain question. |
| Capacity Outlook | Technicality | Deterministic forecasts + per-component failure-risk scoring, narrated by Ember. |

### 2 · Process Automation — automate

| Demo | Primary criterion | What it demonstrates |
|---|---|---|
| Agent Builder | Creativity | Natural language → validated RPA process (templates / 5-question Q&A / Ember design), DISABLED drafts, dry-run Test, separate enable gate. Turns one-off asks into governed automations. |
| Dashboard Builder | Creativity | Ember authors a spec against the live data profile; deterministic validation + rendering; zero Cube-side AI. Generative UI with a safety net. |

### 3 · Business Use Cases — long-context value

| Demo | Primary criterion | What it demonstrates |
|---|---|---|
| PLC Code Converter | Technicality | ~180K tokens of vendor manuals as context + Ember-authored code + deterministic coverage proof + RPA change approval. Ember's 1M-context and coding strengths in one demo. |
| Multimodal Intake | Technicality | Ember reads the photo itself (no OCR engine); deterministic totals verifier catches a planted $7.50 overstatement and passes correct invoices clean. |
| PDF Intake | Demo | Same path for PDFs — text + page images, filed after approval. |
| Audio Intake | Technicality | Ember transcribes speech directly (no STT engine); structured items filed. |
| Video Intake | Creativity | Ember watches frames + listens; on-screen text and summary filed. |
| Document Intelligence | Technicality | Long-document parsing with line-referenced findings. |
| Medical Manual Q&A | Creativity | 4,114-page manual as cited long-context reference; per-chapter practitioner summaries; honest "not covered"; deterministic disclaimer. |
| Paralegal Assistant | Creativity | ~187M tokens of US public-domain law; per-source cited summaries; legal information, never advice. |

### 4 · Grounding — trust

| Demo | Primary criterion | What it demonstrates |
|---|---|---|
| The Honest "No" | Demo | Out-of-catalog refusal with a capability summary. The trust closer: MAC knows its edges. |

**Cross-cutting technicality** (visible in every demo): single-use approval tokens, deterministic verification with ✓/✗ beats, MCP Bus tool discovery with eligibility filtering, the spend meter with hard gates, 479 tests, stdlib-only Python.

## 3-minute video script (v3 — current build)

Supersedes the v2 script in `submission-readiness-nov26.md` (which predates the Medical/Paralegal builds and the 4-group page). Target: 3:00. Record at desktop width; keep the stage rail and MODEL strip (spend meter) in frame throughout.

| Time | Beat | On screen |
|---|---|---|
| 0:00–0:15 | **Problem** | "Industrial sites run powerful tools behind expert interfaces — and their data can't leave the building." Show the MAC hero. |
| 0:15–0:30 | **The idea in one breath** | "MAC — Metatron Agentic Commander. Ember is the brain, the Cube is the hands, a human approves, Ember verifies." Point at the 6-stage rail: Human → Ember → Approve → Metatron → Ember → Next. |
| 0:30–0:55 | **Demo 1 — Watcher → WhatsApp (live)** | Tap the chip, CHECK, show the plain-words plan, Approve, real WhatsApp send, **VERIFIED ✓** beat. "Nothing ran until I approved it." |
| 0:55–1:20 | **Demo 2 — Invoice photo (Paste Anything)** | Paste the invoice photo → Ember extracts fields → deterministic verifier flags the planted $7.50 overstatement → approve filing → "Export as Excel" → download card. "The totals check is code, not the model — a correct invoice passes clean." |
| 1:20–1:55 | **Demo 3 — PLC Code Converter (centerpiece)** | "Convert the sample Allen-Bradley conveyor program to Siemens SCL." Show ~180K tokens of vendor manuals injected as context, the coverage report (6 mapped, 2 flagged unsupported — never silently replaced), approve → DRAFT stored → RPA change-approval record filed. "Ember's million-token context and coding, with a deterministic proof every instruction is accounted for." |
| 1:55–2:20 | **Demo 4 — Paralegal Assistant (long-context proof #2)** | "What does the law say about federal-question jurisdiction?" Per-source cited summary (28 U.S.C. § 1331…), disclaimer visible. Fast cut: Medical Manual chip + the tire-pressure question → "the manual does not cover that." "187 million tokens of law, 4,114 pages of medicine — cited, or honestly declined." |
| 2:20–2:45 | **Demo 5 — Agent Builder Wizard** | NEXT STEPS → Automate → "What do you need?" → template (Capacity Disk Alert) → plain-words summary → Approve & create (DISABLED draft) → Test dry-run shows the exact message, **not sent**. "One-off ask becomes a governed automation — nothing runs until a human enables it." |
| 2:45–3:00 | **Close — budget + trust** | Zoom the MODEL strip: spend meter ≈ $0.50 of $10, 479 tests, trace ledger. "Ember is the only AI. The Cube executes deterministically. A human approves everything. You prompt. MAC checks. You approve. Metatron Cube executes." |

Fallback if time runs long: drop Demo 5's Test step (keep the DISABLED-draft beat). If a live run misbehaves on camera, every demo has a **▶ Recorded** replay of a real run — indistinguishable in flow, $0.

## Submission checklist

Submission = public GitHub repo + 3-minute demo video + prompt/token trace, via cerebralvalley.ai/e/muse-ember-hackathon/hackathon/submit. **Feature freeze Dec 1; submit Dec 2 by noon ET** (9 PM ET is buffer, not target).

- [x] **Build complete** — 33 tools, 19 demos, 479/479 tests green (verified Nov 27, 2026)
- [x] **Live deployment** — https://ember.metatroncube.online (recorded replays public; live Operator+)
- [x] **README.md** — replaced Nov 27, 2026 (this documentation set: README + ARCHITECTURE + DEMO-GUIDE, with real screenshots in `docs/img/`)
- [ ] **Push to GitHub** — local commits are ahead of the remote (last verified remote head a723894, Nov 26 — re-check live before pushing); push goes through the GitHub connector with Rom present for the approval card; verify remote read-back after
- [ ] **Record the 3-minute video** — script above (v3); Rom reviews before upload
- [ ] **Final trace export** — export fresh from the live deployment after the last rehearsal (the repo's `spend/` copy is a Nov 26 snapshot, 124 entries)
- [ ] **Final meter figure** — read at the Dec 1 freeze; update README/compliance numbers if moved
- [ ] **Submit** via the Cerebral Valley portal by Dec 2, noon ET; complete the organizer survey when it arrives
- [ ] **Discord join** — unconfirmed (Rom's action)

Non-blockers noted for Rom: PLC change records RFC-0007/RFC-0008 await accept/reject at their RPA waitpoints; two automations in Built Automations ("Capacity Disk Alert" DISABLED, "Daily Ticket Report" ENABLED) were left untouched.

---

← Back to the [README](../README.md) · [ARCHITECTURE.md](ARCHITECTURE.md)
