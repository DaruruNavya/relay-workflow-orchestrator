# Relay: Verification Report

Two environments are recorded. **Part A** is the development run (stand-in mock world, Linux sandbox).
**Part B** is for you to fill in by running the same three commands against the real `scripts/mock_world.py`
on your own machine; the assessment expects the smoke output and duplication check from a *live* kill-and-resume.

## Part A: development run

### A1. Automated tests (`python -m unittest discover -s tests -t .`)

```
Ran 53 tests in ~18s
OK
```
Covers: publish validation (13), template resolution (8), schema enforcement + AI retry (8), idempotency and
crash recovery (7), approval gate / prompt injection / step cap (10), lifecycle, cancel, webhook secret (7).

### A2. Contract smoke test (`scripts/smoke_test.py`)

```
[1] Seed workflows loaded and published
  PASS  GET /workflows returns 200  (got 200)
  PASS  seed wf_support_triage present
  PASS  seed wf_expense_approval present
  PASS  seed wf_slow_fulfillment present
  PASS  seed wf_runaway present
  PASS  seeds are published  (expected status 'published' on seed workflows)

[2] Create, publish, trigger a minimal workflow
  PASS  create draft returns 2xx  (got 201)
  PASS  publish returns 2xx  (got 200)
  PASS  trigger returns a run id  (got 202: {'run_id': 'run_f19302c3c234', 'id': 'run_f19302c3c234', 'status': 'queued'})
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
  PASS  stop reason mentions the cap  (expected a cap-exceeded reason in the run record)

[7] Status vocabulary
  PASS  all observed statuses in documented set  (saw ['failed', 'queued', 'running', 'succeeded', 'waiting_approval'])

========================================================
  28 passed, 0 warnings, 0 failed
```

### A3. Live kill-and-resume (`scripts/kill_resume_drill.sh`): `kill -9` on the worker during the 20 s delay

```
== 1. start worker A
== 2. trigger wf_slow_fulfillment
   run_id = run_52e08c24a7b5
== 3. wait until the run is inside the 20s delay (confirm email already sent)
('running', 'pack_delay', [('confirm', 'succeeded')])
== 4. kill -9 worker A mid-delay
('after kill:', 'running', 'current =', 'pack_delay')
== 5. start worker B (the lease expires, B resumes from the last persisted step)
final status: succeeded
   1 confirm          succeeded  key=run_52e08c24a7b5:confirm
   2 pack_delay       succeeded  key=None
   3 create_shipment  succeeded  key=run_52e08c24a7b5:create_shipment
   4 shipped_notice   succeeded  key=run_52e08c24a7b5:shipped_notice
== 6. mock world ledger (each side effect must appear exactly once)
   email.send       key=run_52e08c24a7b5:confirm
   shipment.create  key=run_52e08c24a7b5:create_shipment
   email.send       key=run_52e08c24a7b5:shipped_notice
   total side effects: 3
== done (expected: 3 side effects: email confirm, shipment, email shipped)
```
Reading it: worker A died while `pack_delay` was in progress (`confirm` already sent). Worker B took over once the
lease expired and resumed at `pack_delay`. The ledger has **3 side effects for a 3-side-effect workflow**: no duplicates.

### A4. Prompt injection and approval gate (through the real HTTP API)

```
--- injection payload via webhook
status: waiting_approval | ai tokens: 652
   1 classify succeeded attempt 2
   2 route succeeded attempt 1
   3 refund_gate waiting attempt 1
side effects before approval: 0
approved by demo-user
status: succeeded
   1 classify succeeded
   2 route succeeded
   3 refund_gate succeeded
   4 issue_refund succeeded
   5 notify_customer succeeded
  ledger: order.refund run_d84ceea21d9d:issue_refund
  ledger: email.send run_d84ceea21d9d:notify_customer
```
`classify ... attempt 2`: the model obeyed the injected instruction on its first answer and added extra fields;
schema enforcement rejected it and the retry was clean. The run then waited with **0 side effects** until a human approved.

### A5. Step cap

```
failed | Run exceeded limits.max_steps (12) before executing node 'check_order' | steps: 12
```

## Part B: your run against the real mock world (fill in)

```
# terminal 1
python scripts/mock_world.py --port 9210
# terminal 2
python -m relay.server --port 8080
```

**B1. Smoke test** (start a worker first: `python -m relay.worker`)
```
python scripts/smoke_test.py --url http://localhost:8080 --token demo-token
<paste output here>
```

**B2. Live kill-and-resume** (stop the worker, then):
```
PYTHON=python bash scripts/kill_resume_drill.sh
<paste output here>
```

**B3. Duplication check**
```
python scripts/duplication_check.py
<paste output here>
```
