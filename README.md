# Ember Orchestrator for Metatron Cube

**Muse Ember as the intelligent orchestrator for the Metatron Cube** — a private
LLM + RPA appliance by [Metagen Group](https://metagengroup.com). Users describe
what they need in natural language; Ember plans the work over a small catalog of
**deterministic** workflows; a human approves; the Cube's RPA engine executes.

Built for the **Muse Ember Hackathon** (Cerebral Valley, Nov–Dec 2026).

## Architecture

```
User ──> Ember Orchestrator (only brain: Muse Ember, Meta Model API)
              │  proposes a plan — nothing runs yet
              ▼
         Human approval ── mints a single-use approval token
              │
              ▼
         Cube Tool API ── list_workflows · get_watcher_status
                         run_workflow · get_run_status
              │
              ▼
         Metatron Cube RPA (deterministic Activepieces flows only)
```

The pattern is deliberate: the model proposes, people decide, deterministic
systems execute. `run_workflow` cryptographically refuses to run without the
approval token, and every Ember call passes through a spend meter first.

## The $10 discipline

The hackathon gives each participant **$10 of Meta Model API credit — total**,
for experimentation, testing, and the submitted project. This repo is built
around that cap:

- **Stub-first development.** All building and debugging runs against a
  deterministic stub Ember client with recorded fixtures — $0. The live client
  is a single adapter (`orchestrator/ember_client.py`), wired in Phase 1 when
  credits land.
- **Spend meter + trace ledger** (`orchestrator/spend.py`, spec in
  `docs/spend-meter.md`): every call is authorized *before* it is made and
  recorded after — purpose tag, tokens, cost, running total. Gates: warn at
  **$7.00**, non-essential calls halt at **$9.00**, absolute stop at **$9.75**.
  The ledger exports JSON + CSV, which is also the submission's required
  prompt/token trace.
- **Step cap 8** per task; system prompt kept small; payloads trimmed
  deterministically Cube-side. Target ≤ **$0.35 per full demo run**.

## The three demos

1. **Watcher Status → WhatsApp** — Ember reads live Metatron Watcher status and
   fires the existing production flow that sends it to the operator's WhatsApp.
2. **Help-desk Triage Desk** — Ember classifies five fixture tickets
   (`fixtures/tickets.json`), drafts replies, proposes routing; a human
   approves before the deterministic routing flow files them.
3. **Shift Handover Brief** — Ember pulls Watcher status + open incidents and
   writes the outgoing shift's brief.

## Phase 0 — run it now ($0, offline)

```bash
python3 -m unittest discover -s tests -v   # smoke tests
python3 run_demo.py                        # two scripted demos in the terminal
python3 -m orchestrator.app                # UI shell at http://127.0.0.1:8320
```

No dependencies beyond the Python 3.11+ standard library. No API key needed —
the stub client and fixture Cube tools are the defaults.

## Layout

| Path | What |
|---|---|
| `orchestrator/ember_client.py` | Ember adapter: stub client + the Phase 1 Meta Model API wiring point |
| `orchestrator/orchestrator.py` | Propose → approve → execute loop, step cap, metering hooks |
| `orchestrator/cube_tools.py` | Cube Tool API: fixture backend (Phase 0) + real Cube HTTP backend |
| `orchestrator/spend.py` | Spend meter, budget gates, trace ledger + exports |
| `orchestrator/app.py` | Single-page UI shell (command box, plan approval, trace, pinned meter) |
| `tools/catalog.json` | The deterministic workflow catalog — the only things Ember may run |
| `fixtures/` | Reproducible demo data (tickets, handover sample) |
| `docs/spend-meter.md` | Spend meter spec and budget buckets |

## Status & timeline

- **Phase 0 (now):** skeleton, stub, catalog, meter, UI shell, fixtures. ✅
- **Phase 1 (on selection):** wire `MetaEmberClient` to the Meta Model API,
  set the meter rate from the published pricing, point `HttpCubeTools` at the
  production Cube.
- **Phase 2:** build the triage routing + handover flows; rehearse all three
  demos against the meter.
- **Phase 3 (Dec 1):** feature freeze, record the 3-minute demo video.
- **Phase 4 (Dec 2, by noon ET):** submit — this repo, the video, and the
  exported prompt/token trace.

Application status: submitted Nov 23, 2026 — pending organizer review.

## License

MIT — see `LICENSE`.
