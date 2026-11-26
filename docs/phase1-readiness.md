# Phase 1 readiness — Ember Orchestrator

Prepared 2026-11-25 (America/New_York) after acceptance. Local prep only — nothing pushed, no live Ember call made, $0 spent.

## Status change

- Application: **ACCEPTED** — Cerebral Valley blast "You're in! Glad to have you at the Muse Ember Hackathon," forwarded by Rom 2026-11-25T23:42Z (UTC).
- Submission due: **Wed Dec 2, 2026** (build-plan target noon ET; event window to 9:00 PM ET). Rom is in Greenville through Dec 6, so Dec 1 feature freeze + Dec 2 submit happen mid-trip.
- README still says "pending organizer review" — update that line only on Rom's go-ahead, together with the Phase 1 wiring commit.

## The one gate before Phase 1

The blast says: claim $10 Meta Model API credits by **Wed Nov 25, 12:00 PM ET** — create Meta Model API account, add card for refundable hold, submit credits form (each participant claims their own). Credits land in batches ~10 AM / ~3 PM ET.

Claim status is **unconfirmed**. The main chat already asked Rom at 2026-11-25T23:42Z ("Have you already claimed them?") — do not ask again in a proactive message. Phase 1 wiring waits for Rom's answer + a real API key. If the claim was missed, the blast path is still: claim now anyway + ask in Discord; late claims may process. No key = no live wiring; stub work below still stands.

## Verified tonight ($0, stub only)

- `python3 -m unittest discover -s tests -v` → **8/8 pass**.
- `python3 run_demo.py` → Watcher→WhatsApp (simulated, nothing sent) + Shift Handover draft; **SPEND $0.00, 4 ledger entries, rate unset**. `spend/state.json` unchanged (still Nov 24 seed).
- Wiring point confirmed: `orchestrator/ember_client.py` → `MetaEmberClient.chat()` raises `NotImplementedError`; constructor requires `META_MODEL_API_KEY` (+ `EMBER_MODEL_ID`). That one method is the whole Phase 1 code change surface.

## Day-one checks (build plan §11) — need credited access

1. Ember model ID, endpoint, auth mechanics for the issued key.
2. Per-token pricing → set meter `rate_config` (currently null / "TBD at first credited access"); validate ≤$0.35/demo-run target.
3. Tool-calling schema per Ember cookbook/API docs → map onto `EmberResponse`/`ToolCall`.
4. Whether $10 shows as API balance the meter can cross-check.
5. Rate limits for rehearsal runs.

Budget gates already coded: warn $7.00 / halt non-essential $9.00 / stop $9.75. Buckets: experiment $3, test $2, rehearsal $1.50, final $1, reserve $2.50.

## Demo script (3:00, from build plan §9 — rehearse on stub first)

- 0:00 Problem: automation locked behind experts.
- 0:30 Architecture: Ember brain, Cube hands, human approves.
- 1:00 Demo 1 Watcher Status → WhatsApp (live flow; fixture says 12/12 — live estate is larger now, narration must use live counts at record time, never fixture numbers).
- 1:45 Demo 2 Triage Desk: 5 fixture tickets (`fixtures/tickets.json`), classify + draft + propose routing, approve, board updates.
- 2:30 Demo 3 Shift Handover brief; close-up on spend meter + total.
- 2:50 Close: data stayed in Cube; show final spend figure.

Eligibility line for narration: Ember is the only AI in the demo path — no Qwen/Axion/Cube-LLM/OCR steps.

## Remaining choice (Rom's)

Confirm credits claimed + key ready → wire `MetaEmberClient`, set rate, point `HttpCubeTools` at production Cube, smoke-test inside experiment bucket. Until then: stub rehearsals only.
