#!/usr/bin/env bash
# Live kill-and-resume drill for wf_slow_fulfillment (Linux/macOS/Git Bash).
# Prereqs: API running (python -m relay.server), mock world running, NO worker running yet.
# Usage: bash scripts/kill_resume_drill.sh [api_url] [world_url]
set -u
API="${1:-http://localhost:8080}"
WORLD="${2:-http://localhost:9210}"
H="Authorization: Bearer ${RELAY_DEMO_TOKEN:-demo-token}"
PY="${PYTHON:-python3}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

field() { "$PY" -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

echo "== 1. start worker A"
"$PY" -m relay.worker > /tmp/relay_worker_a.log 2>&1 &
WORKER_A=$!
sleep 1.5

echo "== 2. trigger wf_slow_fulfillment"
RID=$(curl -s -X POST "$API/workflows/wf_slow_fulfillment/trigger" -H "$H" -H 'Content-Type: application/json' \
  -d '{"input":{"order_id":"ord_2003","customer_email":"lena@example.com"}}' | field "d['run_id']")
echo "   run_id = $RID"

echo "== 3. wait until the run is inside the 20s delay (confirm email already sent)"
for i in $(seq 1 30); do
  DONE=$(curl -s "$API/runs/$RID" -H "$H" | field "[s['node_id'] for s in d['steps'] if s['status']=='succeeded']")
  case "$DONE" in *confirm*) break;; esac
  sleep 0.5
done
sleep 2
curl -s "$API/runs/$RID" -H "$H" | field "(d['status'], d['current_node_id'], [(s['node_id'], s['status']) for s in d['steps']])"

echo "== 4. kill -9 worker A mid-delay"
kill -9 "$WORKER_A"
sleep 1
curl -s "$API/runs/$RID" -H "$H" | field "('after kill:', d['status'], 'current =', d['current_node_id'])"

echo "== 5. start worker B (the lease expires, B resumes from the last persisted step)"
"$PY" -m relay.worker > /tmp/relay_worker_b.log 2>&1 &
WORKER_B=$!
for i in $(seq 1 60); do
  S=$(curl -s "$API/runs/$RID" -H "$H" | field "d['status']")
  case "$S" in succeeded|failed|cancelled) break;; esac
  sleep 1
done
curl -s "$API/runs/$RID" -H "$H" | field "'final status: ' + d['status']"
curl -s "$API/runs/$RID" -H "$H" | field "'\n'.join('   %d %-16s %-10s key=%s' % (s['sequence'], s['node_id'], s['status'], s['idempotency_key']) for s in d['steps'])"

echo "== 6. mock world ledger (each side effect must appear exactly once)"
curl -s "$WORLD/admin/ledger" | field "'\n'.join('   %-16s key=%s' % (e['action'], e['idempotency_key']) for e in d['entries']) + '\n   total side effects: %d' % len(d['entries'])"

kill "$WORKER_B" 2>/dev/null
echo "== done (expected: 3 side effects: email confirm, shipment, email shipped)"
