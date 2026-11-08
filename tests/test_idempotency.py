import time
import unittest

from relay.engine import SimulatedCrash
from relay.providers import FakeProvider
from tests.helpers import RelayTestCase, notify, workflow


class IdempotencyAndRecovery(RelayTestCase):
    def test_side_effect_carries_the_canonical_stable_key(self):
        run = self.run_workflow(workflow("w", [notify("n1")]))
        key = f"{run['run_id']}:n1"
        self.assertEqual(self.ledger()[0]["idempotency_key"], key)
        self.assertEqual(run["steps"][0]["idempotency_key"], key)

    def test_retry_after_transient_failure_reuses_the_same_key(self):
        self.world.fail_next = 2  # two 503s, then success: 3 attempts, one effect
        run = self.run_workflow(workflow("w", [notify("n1")]))
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["steps"][0]["attempt"], 3)
        self.assertEqual(len(self.ledger()), 1)

    def test_timeout_after_the_effect_landed_is_absorbed_on_retry(self):
        # The world performs the action, then stalls past our timeout: the client cannot know it landed.
        self.settings.http_timeout = 0.3
        self.world.slow_next, self.world.slow_seconds = 1, 1.0
        run = self.run_workflow(workflow("w", [notify("n1")]))
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["steps"][0]["attempt"], 2)
        self.assertEqual(len(self.ledger()), 1, "exactly one side effect")
        self.assertEqual(self.world.replays, 1, "the retry was recognised as a replay")

    def test_exhausted_retries_fail_the_run_without_hanging(self):
        self.world.fail_next = 99
        run = self.run_workflow(workflow("w", [notify("n1")]))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "dependency_unavailable")
        self.assertEqual(run["steps"][0]["attempt"], self.settings.max_attempts)

    def test_crash_between_effect_and_persist_still_yields_exactly_one_effect(self):
        """The dangerous window: side effect sent, worker dies before the step is saved."""
        crashed = []

        def crash_once(point, run_id, node_id):
            if node_id == "n2" and not crashed:
                crashed.append(1)
                raise SimulatedCrash()  # behaves like kill -9: no cleanup, no failure recording

        self.build_engine(FakeProvider([{}]), fault_hook=crash_once)
        wid = self.deploy(workflow("w", [notify("n1", nxt="n2"), notify("n2", nxt="n3"), notify("n3")]))
        run_id = self.relay.trigger(wid, {})["run_id"]

        with self.assertRaises(SimulatedCrash):
            self.worker.run_once()
        mid = self.relay.get_run(run_id)
        self.assertEqual(mid["status"], "running")
        self.assertEqual([s["node_id"] for s in mid["steps"]], ["n1"], "n2 was sent but never persisted")
        self.assertEqual(len(self.ledger()), 2)

        time.sleep(self.settings.lease_seconds + 0.2)       # the dead worker's lease expires
        self.drain()                                          # a (restarted) worker resumes
        final = self.relay.get_run(run_id)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual([s["node_id"] for s in final["steps"]], ["n1", "n2", "n3"])
        self.assertEqual(len(self.ledger()), 3, "n1, n2, n3 each exactly once")
        self.assertEqual(self.world.replays, 1, "n2's re-send was absorbed by its idempotency key")

    def test_resume_never_re_executes_completed_steps(self):
        crashed = []

        def crash_on_n3(point, run_id, node_id):
            if node_id == "n3" and not crashed:
                crashed.append(1)
                raise SimulatedCrash()

        self.build_engine(FakeProvider([{}]), fault_hook=crash_on_n3)
        wid = self.deploy(workflow("w", [notify("n1", nxt="n2"), notify("n2", nxt="n3"), notify("n3")]))
        run_id = self.relay.trigger(wid, {})["run_id"]
        with self.assertRaises(SimulatedCrash):
            self.worker.run_once()
        time.sleep(self.settings.lease_seconds + 0.2)
        self.drain()
        self.assertEqual(self.world.replays, 1, "only the in-flight step was re-sent; n1 and n2 were not")

    def test_revisiting_a_node_in_a_loop_gets_a_distinct_key(self):
        cond = {"id": "c", "type": "condition", "params": {"left": "{{trigger.body.x}}", "op": "equals", "right": "1"},
                "on_true": "done", "on_false": "again"}
        again = notify("again", nxt="c")
        wf = workflow("w", [notify("start", nxt="c"), cond, again, notify("done")], max_steps=7)
        self.run_workflow(wf, {"x": "0"})  # loops until the step cap, notifying each time
        keys = [e["idempotency_key"] for e in self.ledger()]
        self.assertEqual(len(keys), len(set(keys)), "each loop visit is its own effect, not a replay")


if __name__ == "__main__":
    unittest.main()
