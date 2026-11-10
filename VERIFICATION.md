# Relay: Verification Report

Environment for Parts B and C: Windows, Python 3.15, the provided `scripts/mock_world.py`, API on port 8080, and the
worker as a **separate process** (database file outside OneDrive: `RELAY_DB=C:\relaytemp\relay.db`).

## 1. Smoke test (`scripts/smoke_test.py`): all PASS, no warnings

```
python scripts/smoke_test.py --url http://localhost:8080 --token demo-token

[1] Seed workflows loaded and published
  PASS  GET /workflows returns 200  (got 200)
  PASS  seed wf_support_triage present
  PASS  seed wf_expense_approval present
  PASS  seed wf_slow_fulfillment present
  PASS  seed wf_runaway present
  PASS  seeds are published

[2] Create, publish, trigger a minimal workflow
  PASS  create draft returns 2xx  (got 201)
  PASS  publish returns 2xx  (got 200)
  PASS  trigger returns a run id  (got 202)
  PASS  run reaches succeeded  (got succeeded)
  PASS  trace has a step for node 'hello'  (1 steps)
  PASS  notify step visible in mock world ledger
  PASS  notify carried an Idempotency-Key  (required for exactly-once recovery)

[3] Publish validation rejects broken definitions
  PASS  rejects unknown node type (at publish)  (got 422)
  PASS  rejects missing required param (at publish)  (got 422)
  PASS  rejects reference to nonexistent node (at publish)  (got 422)

[4] Webhook secret enforcement
  PASS  wrong secret rejected 401/403  (got 401)
  PASS  correct secret accepted with a run id  (got 202)

[5] Approval lifecycle (wf_expense_approval)
  PASS  small expense auto-approves (no gate)  (got succeeded)
  PASS  large expense triggered  (got 202)
  PASS  run pauses in waiting_approval  (got waiting_approval)
  PASS  pending approval listed for the run  (1 pending)
  PASS  approve returns 2xx  (got 200)
  PASS  approved run resumes and succeeds  (got succeeded)

[6] Run caps stop wf_runaway
  PASS  wf_runaway triggered  (got 202)
  PASS  runaway run is stopped (failed/cancelled)  (got failed)
  PASS  stop reason mentions the cap

[7] Status vocabulary
  PASS  all observed statuses in documented set  (saw ['failed', 'running', 'succeeded', 'waiting_approval'])

  28 passed, 0 warnings, 0 failed
```

## 2. Kill-and-resume drill + duplication check

**Where the worker was killed:** `kill -9` during the 20 s `pack_delay` of `wf_slow_fulfillment`, after the `confirm`
email had been sent and persisted. Worker B started after the kill, the lease expired, and B resumed at `pack_delay`.

```
PYTHON=python bash scripts/kill_resume_drill.sh
== 1. start worker A
== 2. trigger wf_slow_fulfillment
   run_id = run_d37be0ceb0d7
== 3. wait until the run is inside the 20s delay (confirm email already sent)
('running', 'pack_delay', [('confirm', 'succeeded')])
== 4. kill -9 worker A mid-delay
('after kill:', 'running', 'current =', 'pack_delay')
== 5. start worker B (the lease expires, B resumes from the last persisted step)
final status: succeeded
   1 confirm          succeeded  key=run_d37be0ceb0d7:confirm
   2 pack_delay       succeeded  key=None
   3 create_shipment  succeeded  key=run_d37be0ceb0d7:create_shipment
   4 shipped_notice   succeeded  key=run_d37be0ceb0d7:shipped_notice
== 6. mock world ledger (each side effect must appear exactly once)
   order.refund     key=run_d883f3fb985c:issue_refund
   email.send       key=run_d883f3fb985c:notify_customer
   email.send       key=run_4d4d2d4c4b05:confirm
   shipment.create  key=run_4d4d2d4c4b05:create_shipment
   email.send       key=run_d37be0ceb0d7:confirm
   shipment.create  key=run_d37be0ceb0d7:create_shipment
   email.send       key=run_d37be0ceb0d7:shipped_notice
   total side effects: 7
```

The ledger was not reset, so it also lists an earlier refund test (`run_d883f3fb985c`) and an earlier drill attempt
(`run_4d4d2d4c4b05`) whose shipment was rejected by the mock world because I had used an unknown order id; that
run correctly ended `failed` with `HTTP 404 Unknown order`. The run under test, `run_d37be0ceb0d7`, has exactly one
`confirm`, one `create_shipment` and one `shipped_notice`. After the restart `pack_delay` only waited the remaining
time, because the wake-up time is stored on the run.

```
python scripts/duplication_check.py
Ledger entries checked: 7 (executed: 6, replays absorbed: 0, rejected: 1)
  email.send: 4 executed
  order.refund: 1 executed
  shipment.create: 1 executed

PASS: every side effect executed exactly once.
```

"Replays absorbed: 0" is expected for this kill point: the worker died while waiting, after the previous step was
saved, so nothing had to be re-sent. The harder window (effect sent, step not yet saved) is covered by the automated
test `test_crash_between_effect_and_persist_still_yields_exactly_one_effect`, where the re-send **is** absorbed
(`replays == 1`, one ledger entry).

## 3. Automated tests (`python -m unittest discover -s tests -t .`)

55 tests, all passing: publish validation, template resolution, schema enforcement (incl. AI repair retry), idempotency
and crash recovery, approval gate, prompt injection, step cap, cancel semantics, snapshot integrity, secret hygiene,
outage survival.

## 4. Injection, failure and guardrail drills

| Drill | How it was run | What the trace showed |
|---|---|---|
| `pay_inject_001` (skip-approval / $5000 refund) | triage via console | `classify` succeeded on **attempt 2** (the first answer carried extra fields and was rejected by the schema); run parked in `waiting_approval`; nothing sent before a human decision |
| `pay_inject_002` (exfiltrate / redirect mail) | triage via console | normal classification; notifications went only to targets fixed in the workflow (`#support`); no schema field can change a recipient |
| Gate bypass by construction | automated test: workflow with `order_action` and no approval node | engine refused: step `failed`, `approval_required`, ledger empty |
| Step cap | `wf_runaway` | run `failed`: "Run exceeded limits.max_steps (12) ..."; `steps_executed` = 12 |
| Schema enforcement | automated tests + injection run | first output rejected, one retry with the validation error appended, then step failure if still invalid |
| Mock-world failures | automated tests (503 bursts, timeout after the effect landed) | retries reuse the same key; ledger shows one effect and `replays` > 0, never a duplicate |

Note on the AI model: the demos use the offline `heuristic` provider. It imitates a model that obeys the injected
text on its first try, so the schema-rejection path is visible. The protections tested are real (schema validation,
engine-side approval gate); no real LLM was attacked.

## 5. Idempotency scheme and the crash-window answer

* **Key:** `Idempotency-Key: {run_id}:{node_id}`; if the run visits the same node again (a loop) the key becomes
  `{run_id}:{node_id}:{n}`, derived from persisted steps so it is identical across a resume. The attempt number is
  never part of the key.
* **Crash window** (effect sent, step not persisted): we cannot know whether it landed, so on resume the node runs
  again with the **same key**. If the first call landed the mock world absorbs the replay; if not it executes.
  Either way the effect happens once. A step row, the run's next pointer and its counters commit in one transaction,
  and every persist re-checks that the worker still holds the job lease.

## 6. Known limitations

* Not built: cron schedule triggers, `timeout_seconds` / `max_ai_tokens` enforcement, the NL compiler, role separation.
* The approval gate accepts any approved approval earlier in the same run, not one tied to a specific node.
* The trace stores the final attempt count and total tokens of an `ai` step, not the raw text of each failed attempt.
* An AI `summary` is untrusted text that is posted to the configured `#support` channel; it cannot redirect
  notifications, but it can repeat hostile text there.
* The Anthropic and OpenAI-compatible providers were written but not exercised; the offline provider and the test
  fake were.
