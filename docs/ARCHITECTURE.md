# MAC Architecture — technical companion

Deep-dive companion to the [README](../README.md). The README explains *what* MAC does and *why*; this document explains *how* the orchestrator works inside: the propose/approve/execute/verify pipeline, the approval-token lifecycle, the layered validators, MCP discovery merge rules, deterministic corpora retrieval, the audit ledger schema, and the failure modes and honest limits.

**Contents**

- [System overview](#system-overview)
- [The propose → approve → execute → verify pipeline](#the-propose--approve--execute--verify-pipeline)
- [Approval-token lifecycle](#approval-token-lifecycle)
- [Parameter validation layers](#parameter-validation-layers)
- [MCP discovery merge rules](#mcp-discovery-merge-rules)
- [Deterministic corpora retrieval](#deterministic-corpora-retrieval)
- [Audit ledger schema](#audit-ledger-schema)
- [The Ember client](#the-ember-client)
- [Failure modes and honest limits](#failure-modes-and-honest-limits)

---

## System overview

MAC is two layers:

- **`serve.py`** — the HTTP service (port 8023). Hosts the demo page, enforces the Cube session auth gate (live routes require an Operator+ `cube_session` cookie, validated by forwarding it to the Cube interface's `/api/auth/me`), and implements the UI-facing endpoints: propose/approve/reject, multimodal intake, the Agent Builder Wizard (options/design/template/scratch/validate/create), agent-flow test/enable/autonomy, goals, suggestions, preferences, undo, exports, PLC change-approval, and the service-key `/api/mcp/*` routes.
- **`orchestrator/`** — the core loop and its deterministic machinery. `Orchestrator` (propose/approve/execute/verify) is UI-agnostic: `run_demo.py`, `run_phase2.py`, and the test suite drive it directly with a stub client and fixture tools.

Backends behind the tool API (`orchestrator/cube_tools.py`):

- `FixtureCubeTools` — demo data from `fixtures/`, zero side effects (tests, offline runs).
- `HttpCubeTools` — the real Cube: AIOps `/api/overview` + Activepieces sync webhooks + MCP Bus `tools/call` for reads.
- `UiCubeTools` (in `serve.py`) — `HttpCubeTools` plus the UI's board/intake/reports/exports/dashboards/plc-conversion stores that the verifier re-reads.

## The propose → approve → execute → verify pipeline

### Propose (`Orchestrator._propose`)

1. `meter.authorize(purpose)` — the budget gate runs **before** any API call.
2. Messages = system prompt + user content (command, plus server-assembled run context when the command needs derivation, plus multimodal parts for intake).
3. One Ember call with the effective tool schemas. The Meta Model API supports `tool_choice: "auto"` only (not `"required"`), so if Ember narrates instead of calling tools, the orchestrator nudges **once** ("Express that plan as tool calls now…"). If the reply was **truncated** (`finish_reason == "length"` or unparseable call arguments — Ember is a reasoning model and can burn the budget reasoning), the retry also escalates the token budget (×4, capped at 4,096). Calls that parsed cleanly stand even if trailing prose hit the cap.
4. The plan is stored as `proposed` with its steps (trimmed to the 8-step cap), Ember's rationale text, and a deterministic **confidence** assessment (`assess_confidence`): high / medium (a default was derived) / low (a required field can't be derived → exactly one clarifying question with tappable answers).
5. Binary payloads are **scrubbed** from the stored conversation (`propose_content`): image/PDF/audio/video parts become `[image attached — read by Ember]` placeholders, and long document bodies are omitted from replay — so the compose turn never re-sends (or re-pays for) the bytes.

Deterministic proposals (`propose_deterministic`) skip Ember entirely ($0): the wizard's pre-built templates build a plan in the identical shape, so `approve()` runs the same gated path. The rationale honestly states "no AI involved."

### Approve (`Orchestrator.approve`)

1. Plan must be `proposed` (else `ValueError` — double-approval is impossible).
2. **Human param overrides** (the wizard Review step) are applied per step index; `create_agent_flow` overrides are re-validated via `validate_agent_params` **before any token is minted** — a validation failure raises, nothing runs, the plan stays `proposed`.
3. The assistant turn is replayed **with its `tool_calls`** (the API rejects tool results without a matching assistant `tool_calls` entry — learned live, Nov 26).
4. Steps execute in order. For each gated step, a fresh token is minted and injected as `approval_token`; the token (and internal `run_tag`) is stripped from the recorded trace arguments. Overrides record both `arguments` (final) and `proposed_arguments` (Ember's original).
5. Tool exceptions become `{"error": ...}` results in the trace — fed back to Ember as data, never crashing the run.
6. One compose call (1,024 tokens; 4,096 for medical/legal answers with per-chapter blocks) writes the summary **and** the `NEXT_STEPS:` line, parsed and stripped by `parse_next_steps` (malformed → `[]`, page falls back to Automate + Done).
7. `verify_trace` runs deterministic re-reads (see [README](../README.md#the-verification-loop)); the plan is marked `executed` with `trace`, `verification`, `verification_summary`, `final_text`, `next_steps`.

### Reject

`reject()` marks the plan `rejected`. Nothing runs; no tokens were minted.

## Approval-token lifecycle

`ApprovalTokens` (`orchestrator/cube_tools.py`) is deliberately tiny:

```python
class ApprovalTokens:
    """Single-use tokens, minted only by the human approval step."""
    def issue(self) -> str:      # secrets.token_hex(16), added to live set
    def consume(self, token) -> bool:  # True once, then discarded forever
```

Properties:

- **Minted only in `approve()`**, one per gated step, immediately before that step executes.
- **Never shown to Ember** — injected into tool arguments after the plan is fixed; stripped from the trace and from anything Ember sees.
- **Single-use**: `consume()` removes the token from the live set; a replay returns `False` and the tool refuses.
- **In-memory only**: tokens do not survive a restart, and there is no API to list or forge them. A plan approved across a restart simply cannot execute its gated steps.
- **Scope**: the token authorizes *that one tool call in that one approved plan* — it is not a session credential.

## Parameter validation layers

Defense in depth — every gated write passes up to four deterministic layers before it can execute:

| Layer | Where | What it checks |
|---|---|---|
| 1. Schema | Ember tool schemas (catalog / discovered) | Shape of arguments Ember may emit |
| 2. Domain validator | `validate_agent_params`, `validate_dashboard_params`, `validate_conversion` | Allowlist semantics: known sources/actions/triggers, cron syntax, condition fields+ops per source, spec caps (8 KPIs / 4 charts / 8 thresholds), PLC coverage completeness + mnemonic hygiene |
| 3. Approval-time re-validation | `Orchestrator.approve` | Human edits re-run layer 2 before token minting |
| 4. Execution guard | Tool implementations | Token consumption, registry scoping (`enable_agent_flow` accepts only registry ids), source allowlists re-checked |

Teaching errors: validators return specific, actionable messages ("at most 8 KPI cards", "pick what to keep an eye on — one of: …"). The Dashboard Builder uses them for its one repair retry; the wizard surfaces them as "Fix this to continue:".

## MCP discovery merge rules

`orchestrator/mcp_discovery.py`:

1. **Fetch**: JSON-RPC `tools/list` to the bus with the Cube services key (`X-API-Key`), 5 s timeout in the live service (10 s function default).
   Only known reads (`READ_TOOLS`) and tools the bus annotates `readOnlyHint: true` are admitted, because discovered tools run without an approval token; everything else is quarantined and listed as `quarantined_tools` in `/api/status`.
2. **Filter**: drop `EXCLUDED_BUS_TOOLS` (`create_ticket`, `knowledge_search`, `ask_cube` — non-Ember models) and anything starting with `ember_` (self-calls). Malformed entries are skipped; duplicate names deduped.
3. **Convert**: bus `inputSchema` → Ember function schema; descriptions capped at 240 chars so a verbose bus tool can't blow up the prompt.
4. **Merge** (`merge_schemas`): Ember-local tools (`LOCAL_ONLY_TOOLS` — gated writes + local reads) always come from the static catalog; for bus-backed tools the discovered schema wins, except `required` is **unioned** with the catalog's hand-tuned required fields. Order: catalog order first, then discovered-only tools in bus order.
5. **Cache**: `HttpCubeTools` caches discovery for `MCP_DISCOVERY_TTL_S` (default 300 s) and refreshes lazily; `Orchestrator.tool_schemas` reads per call so refreshes propagate.
6. **Fallback**: any failure (transport, RPC error, empty result) logs to stderr and returns `None` → the static catalog is used. The demo never dies because the bus is down.

Direction 2 (MAC on the bus): `serve.py` exposes `/api/mcp/status`, `/api/mcp/automations`, `/api/mcp/propose` behind the Cube services key (Operator-equivalent, ledger-tagged `mcp-bus`); the bus wraps them as `ember_status`, `ember_propose`, `ember_list_automations`. **No approve/execute route exists** — by design.

## Deterministic corpora retrieval

Both reference tools follow the same pattern: chunk → keyword-rank → group → budget → cite. No embeddings, no vector DB, no model in retrieval.

### Medical (`orchestrator/medical_manual.py`)

- Corpus: Merck Manual 19th Ed., chunked by chapter/section with printed-page citations (17,651 chunks, box-only).
- Ranking: BM25 with title/section boosts; neighbour expansion (±2 chunks around anchors).
- Selection: chapters ranked by top-anchor score; top ≤5 chapters (≥10% of top score) selected; anchors taken **round-robin** across chapters so no chapter crowds out the others (~13–20 passages each).
- Budget: ~160K chars (≈40K tokens) of reference per answer.
- Coverage honesty: ≥2 matched terms or a title hit required, plus an out-of-vocabulary rule (added after a live over-match); otherwise `covered=false` with zero passages.

### Legal (`orchestrator/legal_reference.py`)

- Corpus: 413,116 chunks in SQLite FTS5 (porter stemming) — U.S. Code 88,234 sections, CFR 324,148 sections, Federal Rules, Constitution (box-only, ~1.4 GB index).
- Ranking: FTS5 BM25; passages grouped by source/title (≤5 groups, round-robin anchors), same ~160K-char budget.
- Coverage bar tuned live: ≥3 matched terms on 4+-term queries (after a "Texas speed limit" false positive on a wine regulation).

### PLC manuals (`orchestrator/plc_converter.py`)

Not retrieval — **long-context injection**: `load_manual_context()` assembles ~720K chars (≈180K tokens) of vendor manual text per direction (45% source-platform / 55% target-platform split) by deterministic file reads. The validator (`validate_conversion`) then proves: every parsed source instruction appears in the coverage report; no source-only mnemonics survive in the output; distinctive target instructions named in coverage are actually used.

## Audit ledger schema

`orchestrator/spend.py` — one JSON object per Ember call, persisted atomically (`state.json.tmp` → `os.replace`):

```json
{
  "ts": "2026-11-27T14:46:03Z",
  "purpose": "integration",
  "prompt_tokens": 25853,
  "completion_tokens": 984,
  "cost_usd": 0.003077,
  "total_after_usd": 0.497096,
  "note": "compose-final"
}
```

- `purpose` ∈ {experiment, integration, test, rehearsal, final}; buckets tracked separately; `final` is the only "essential" purpose at the $9 gate.
- Export (`export_json` / `export_csv`) carries the rate config, totals, gates, and the full ledger — the submission trace.
- Rates are config (`EMBER_RATE_INPUT_PER_1K` / `EMBER_RATE_OUTPUT_PER_1K`); live mode **refuses to start** without them ("a live call must never meter at $0").

## The Ember client

`orchestrator/ember_client.py`:

- `MetaEmberClient` — OpenAI-compatible Chat Completions against `https://api.meta.ai/v1`, model `muse-ember-1.0`, `reasoning_effort: "minimal"` (the API rejects `"none"` — Ember always reasons), 120 s timeout in the live service. Multimodal content parts probed live Nov 26: PDF = `file` part, audio = `input_audio`, video = `video_url` object.
- `StubEmberClient` — deterministic scripted responses for tests/offline runs; the entire 479-test suite runs against it at $0.
- Truncation surfacing: responses carry `finish_reason` and per-call `args_truncated`, which drives the orchestrator's budget-escalating retry.

## Failure modes and honest limits

| Failure | Behavior |
|---|---|
| Ember narrates instead of calling tools | One nudge retry, then the text stands as the plan rationale with zero steps (refusal path) |
| Ember reply truncated at token cap | Retry with escalated budget (×4, ≤4,096); cleanly-parsed calls are kept |
| Ember proposes an invalid spec/params | Deterministic validator rejects with a teaching error; dashboard gets one repair retry; agent params get 422 in the wizard design path — invalid plans never reach approval |
| Tool execution error | Captured as `{"error": ...}` in the trace; Ember reports it; verifier marks the step ✗ with a fix; no silent retry |
| MCP Bus down | Discovery falls back to the static catalog; reads route per catalog |
| Budget gate closed | `BudgetHalt` ($9, non-essential) / `BudgetStop` ($9.75) raised before the call; recorded replays and $0 paths keep working |
| No API key | Recorded-only mode: page, replays, capabilities, templates all work; live calls disabled with a clear message |
| Unsupported intake format (m4a/mov/webm) | Clear error naming supported formats; no transcoding |
| Question outside a corpus | `covered=false`, zero passages, Ember says so — never answers from general knowledge |
| PLC instruction with no equivalent | Flagged `unsupported` with a redesign note; left out of converted code with an explanatory comment |

Known limits (stated plainly):

- **Plans are in-memory** in the orchestrator process; a service restart drops un-approved plans (tokens too — by design).
- **The approval gate is per-plan, not per-step**: a human approves the whole plan (with per-step edits), not each step individually mid-execution.
- **Retrieval is keyword-based**: BM25/FTS5 miss purely semantic matches an embedding model would catch — the deliberate trade for Ember-only-AI eligibility and determinism.
- **The meter trusts API-reported token counts**; rates are config, set from Meta's published pricing.
- **Phone-width layout** is verified by CSS fit math + tests; the cloud browser viewport is fixed at desktop width (see [DEMO-GUIDE.md](DEMO-GUIDE.md)).

---

← Back to the [README](../README.md) · [DEMO-GUIDE.md](DEMO-GUIDE.md)
