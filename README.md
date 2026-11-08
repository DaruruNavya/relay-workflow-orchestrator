# Relay: AI Workflow Orchestrator

Relay runs workflows made of typed nodes (`http_request`, `condition`, `delay`, `notify`, `ai`, `approval`,
`order_action`). A run starts from a webhook or the manual API, executes **durably** on a background worker,
pauses for **human approval**, validates **AI output against a JSON schema**, survives a **killed worker**
without duplicating side effects, and records a **full trace** of every step.

Pure Python 3.9+ standard library: **nothing to `pip install`** (an optional `jsonschema` package is used if present).

## Quick start

Open three terminals in the repo root (`relay-capstone/`).

```bash
# 1) the mock world (provided in this repo)
python scripts/mock_world.py --port 9210        # see `--help` if your copy uses different flags

# 2) the API + console  (seeds the 4 demo workflows as Published)
python -m relay.server --port 8080

# 3) the worker (separate process on purpose: it is the thing you kill in the demo)
python -m relay.worker
```

Open the console at **http://localhost:8080/console** (token `demo-token`).
Shortcut for development: `python -m relay.server --with-worker` runs a worker thread inside the API process.

| Check | Command |
|---|---|
| Unit + integration tests (53) | `python -m unittest discover -s tests -t .` |
| Contract smoke test | `python scripts/smoke_test.py --url http://localhost:8080 --token demo-token` |
| Kill-and-resume drill | start API + mock world, **no worker**, then `PYTHON=python bash scripts/kill_resume_drill.sh` (Git Bash on Windows) |
| Duplication check | `python scripts/duplication_check.py` (provided in the repo; reads the mock world's ledger) |

Configuration (environment variables): `RELAY_DB` (default `relay.db`), `MOCK_WORLD_URL` (default
`http://localhost:9210`), `RELAY_DEMO_TOKEN` (default `demo-token`, empty disables auth),
`RELAY_LEASE_SECONDS` (default 10), `RELAY_HTTP_TIMEOUT`, `RELAY_AI_TIMEOUT`,
`RELAY_PROVIDER` = `heuristic` (default, offline) | `anthropic` (`ANTHROPIC_API_KEY`) | `openai-compatible`
(`openai`, `OPENAI_API_KEY`, optional `RELAY_OPENAI_BASE_URL`), and `RELAY_MODEL`.

## Architecture

```
 external caller --POST /hooks/{id} (X-Relay-Secret)--+
 builder/operator/approver --REST + console-----------+--> API process (relay/server.py -> service.py)
                                                      |            |  one transaction: run row + snapshot + queue job
                                                      v            v
                                              SQLite (WAL): workflows, runs (+definition_snapshot), steps,
                                              approvals, queue_jobs          <-- the only shared state
                                                      ^
                       worker process(es) (relay/worker.py) claim jobs with a lease + heartbeat
                                                      |
                                       engine.py: loop { cap check -> gate check -> execute node -> persist step }
                                                      |
                                nodes.py ---> mock world (HTTP, timeout, retries, Idempotency-Key)
                                      \-----> AI provider adapter (providers.py, timeout, mockable)
```

| Module | Responsibility |
|---|---|
| `service.py` | create/publish/trigger/cancel/approve; every multi-row change is one transaction |
| `validation.py` | publish-time checks against `data/node_catalog.json` (distinct error code per failure) |
| `jobqueue.py` / `worker.py` | durable queue on SQLite; claim = lease; heartbeat thread renews; expired lease = job is claimable again |
| `engine.py` | the state machine, step cap, approval gate, persist-then-advance, resume |
| `nodes.py` | node executors, retries with backoff, idempotency keys, AI retry-with-error |
| `providers.py` | AI adapter: `FakeProvider` (tests), `HeuristicProvider` (offline demo), Anthropic, OpenAI-compatible |
| `templates.py`, `schema.py` | `{{ }}` resolution; JSON Schema enforcement |

## Run state machine

```
                       cancel                                  
   queued ------------------------------------------+
     |  worker claims job                            v
     v                                           cancelled <--- reject approval
   running --- last node done --------------------> succeeded           ^
     |  \--- node failed / step cap / gate refused -> failed            |
     |   \--- cancel requested (checked between steps)--> cancelled     |
     \--- approval node --> waiting_approval --- approve --> queued ----+(re-enqueued, atomically)
```

Statuses: `queued`, `running`, `waiting_approval`, `succeeded`, `failed`, `cancelled`.
A paused run holds no worker and no queue job; approving re-enqueues it in the same transaction as the decision.

## Exactly-once recovery (the core design)

1. **Persist, then advance.** A finished step (row + `runs.current_node_id` + counters) is committed in **one
   transaction**. After a crash, `runs.current_node_id` says what runs next and the `steps` rows rebuild the
   template context. Nothing lives only in memory.
2. **Leases.** A worker claims a job with `leased_until = now + 10s` and renews it from a heartbeat thread.
   `kill -9` stops the heartbeat; after the lease expires any worker re-claims the job. Every persist checks the
   worker still holds the lease, so a zombie worker cannot write after losing it.
3. **The crash window.** If the process dies after a side effect was sent but before its step was saved, we
   cannot know whether it landed. So the node simply runs again and sends the **same `Idempotency-Key`**
   (`{run_id}:{node_id}`; a repeat visit to the same node in a loop gets `...:{n}`, derived from persisted steps).
   If the first call landed, the mock world absorbs the replay; if not, it executes. Either way: once.
   The attempt number is deliberately **not** in the key, so timeout retries and crash resumes share it.
4. **Delays survive restarts.** `delay` stores its wake-up time on the run, so a restarted worker waits only the
   remaining time.
5. **Poison runs.** A job delivered more than 5 times without finishing is failed (`crash loop`), not retried forever.

Proven by `tests/test_idempotency.py` (simulated crash inside the window, timeout-after-effect) and the live
`kill -9` drill in `VERIFICATION.md`.

## Guardrails (enforced in the engine, not the prompt)

* **Approval gate:** before executing any node whose catalog entry has `requires_approval: true`, the engine
  queries the `approvals` table for an `approved` row on this run. Only the approve API writes such rows; model
  output cannot. No approval → the step fails with `approval_required` and nothing is sent.
* **Step cap:** before **every** node execution, `steps_executed >= limits.max_steps` fails the run with
  `max_steps_exceeded` ("Run exceeded limits.max_steps (12) ..."). Loops count each visit.
* **AI output:** validated against the node's `output_schema` (`additionalProperties: false` in the seeds). On
  failure: ONE retry with the validation error appended, then the step fails. A model that obeys an injected
  instruction and adds fields like `approved: true` is rejected by the schema.
* **Prompt injection:** the system prompt tells the model the customer text is data; routing uses only
  schema-validated enum values; the refund still needs a human. `order_id` is checked against `[A-Za-z0-9_-]+`
  so untrusted text cannot rewrite a URL path.
* **Timeouts:** every mock-world and model call has a timeout; timeouts/5xx/429 are retried (3 attempts,
  exponential backoff), 4xx fail fast. A hung dependency fails the step, never the engine.

## API

All platform routes need `Authorization: Bearer demo-token`. Errors look like
`{"error": {"message": "...", "code": "invalid_node_type"}}`.

| Method & path | Purpose | Notes |
|---|---|---|
| `GET /workflows` | list (`id`, `name`, `status`, `trigger_type`) | |
| `POST /workflows` | create a **draft** from a definition | 201; 409 duplicate id; 422 malformed |
| `GET /workflows/{id}` | one workflow incl. `definition` | |
| `PUT /workflows/{id}` | update a draft | 409 once published (frozen) |
| `POST /workflows/{id}/publish` | validate + publish | 422 with a distinct `code` per failure |
| `POST /workflows/{id}/trigger` | body `{"input": {...}}` | 202 `{"run_id": ...}`; 409 if draft |
| `POST /hooks/{id}` | webhook; `X-Relay-Secret` header | 401 wrong/missing secret; body becomes `{{trigger.body}}` |
| `GET /runs`, `GET /runs/{id}` | run + full trace (`steps[]`) | resolved input, output, attempt, duration, tokens, idempotency key |
| `POST /runs/{id}/cancel` | cancel | queued/waiting: immediate; running: between steps; terminal: 409 |
| `GET /approvals?status=pending` | list approvals | |
| `POST /approvals/{id}/approve` · `/reject` | decide (optional `{"decided_by": ...}`) | 409 if already decided; reject ends the run `cancelled` |
| `GET /console` | web console | workflows, runs + traces, approvals |

Publish validation error codes: `invalid_definition`, `invalid_trigger`, `invalid_node_type`, `missing_param`,
`invalid_param_value`, `missing_branch`, `invalid_reference`, `invalid_entry`, `invalid_template_reference`.
Backward jumps (loops) are legal; the step cap bounds them at run time.

## Design decisions

* **SQLite is the database and the queue.** One file, no broker to install, transactions give atomic
  "run + job" creation and atomic "decision + re-enqueue". `BEGIN IMMEDIATE` serializes writers across processes.
* **Standard library only.** Nothing to install, so setup cannot fail; `server.py` is a thin adapter over
  `service.py`, so moving to FastAPI is mechanical.
* **Approval as data.** The gate is a query over rows only the approval API can write.
* **Snapshots, not versions.** A run stores a copy of the definition it started with; editing or republishing
  never changes in-flight or historical runs (`tests/test_lifecycle.py`).
* **Trace stores the resolved AI prompt.** Useful for debugging; it includes customer text, so in production
  you would add a retention policy or redact it. Token counts and validated output are always stored.

## Known limitations

* Not implemented (Good To Have): cron schedule triggers, `timeout_seconds` and `max_ai_tokens` enforcement,
  the natural-language compiler, role separation (one demo token for all personas).
* `PUT` edits drafts only; to change a published workflow, publish a new id.
* The approval gate accepts *any* approved approval earlier in the same run, not one tied to a specific node.
* HTTP timeouts are per socket operation (urllib), not a hard wall-clock cap on a slow trickling response.
* The Anthropic/OpenAI providers are written against the documented APIs but were not exercised in the
  development sandbox (no network there); the offline `heuristic` provider and `FakeProvider` were.
* The request bodies sent to the mock world (`/email/send`, `/chat/message`, `/orders/{id}/refund`,
  `/shipments`) follow the node catalog; if your mock world expects different field names, adjust
  `run_notify` / `run_order_action` in `relay/nodes.py` (one function each).

## Demo script (for the explainer video)

1. Console -> Workflows: run `wf_expense_approval` with amount 250 -> run pauses `waiting_approval` -> Approvals tab -> Approve -> succeeds. Run it with amount 40 -> auto-approves.
2. Webhook with `curl` and a wrong secret -> 401; with the right secret -> run id.
3. `wf_support_triage` via webhook with an injection message (`data/sample_payloads.jsonl`) -> AI step shows `attempt 2` (schema rejected the tainted answer), run waits for approval, **zero** side effects until Approve.
4. `bash scripts/kill_resume_drill.sh` -> `kill -9` mid-delay -> resume -> ledger shows each effect once.
5. `wf_runaway` -> fails at `max_steps (12)`.
