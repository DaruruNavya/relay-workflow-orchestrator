import unittest

from relay import schema
from relay.providers import FakeProvider, ProviderError
from tests.helpers import RelayTestCase, ai_node, notify, workflow

SCHEMA = {"type": "object", "properties": {"category": {"type": "string", "enum": ["a", "b"]}},
          "required": ["category"], "additionalProperties": False}


class SchemaValidator(unittest.TestCase):
    def test_valid_and_invalid_documents(self):
        self.assertEqual(schema.validate({"category": "a"}, SCHEMA), [])
        self.assertTrue(schema.validate({"category": "zzz"}, SCHEMA))
        self.assertTrue(schema.validate({}, SCHEMA))
        self.assertTrue(schema.validate({"category": "a", "approved": True}, SCHEMA))
        self.assertTrue(schema.validate(["not", "an", "object"], SCHEMA))


class AiNodeSchemaEnforcement(RelayTestCase):
    def go(self, responses, **kw):
        self.build_engine(FakeProvider(responses))
        self.counter = getattr(self, "counter", 0) + 1  # unique workflow id per call
        wf = workflow(f"w{self.counter}", [ai_node("c", nxt="n"), notify("n", message="got {{nodes.c.output.category}}")])
        return self.run_workflow(wf, {"text": "hi"})

    def test_valid_output_flows_to_the_next_node(self):
        run = self.go([{"category": "a"}])
        self.assertEqual(run["status"], "succeeded")
        self.assertIn("got a", self.ledger("chat.message")[0]["payload"]["message"])
        step = run["steps"][0]
        self.assertGreater(step["tokens"]["prompt"], 0)           # token usage is traced
        self.assertGreater(run["ai_tokens_used"], 0)

    def test_malformed_output_is_retried_once_with_the_error_appended(self):
        run = self.go([{"category": "nonsense"}, {"category": "b"}])
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["steps"][0]["attempt"], 2)
        self.assertEqual(len(self.provider_obj.calls), 2)
        self.assertIn("previous response was invalid", self.provider_obj.calls[1][1])

    def test_still_invalid_after_retry_fails_the_step_and_nothing_downstream_runs(self):
        run = self.go([{"category": "nonsense"}])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "ai_output_invalid")
        self.assertEqual(run["steps"][0]["status"], "failed")
        self.assertEqual(self.ledger(), [])                        # downstream notify never ran

    def test_extra_fields_are_rejected(self):
        run = self.go([{"category": "a", "approved": True, "skip_approval": True}])
        self.assertEqual(run["status"], "failed")

    def test_non_json_and_fenced_json(self):
        self.assertEqual(self.go(["I think it is a refund!"])["status"], "failed")
        self.assertEqual(self.go(['```json\n{"category": "a"}\n```'])["status"], "succeeded")

    def test_provider_outage_is_retried_then_fails_cleanly(self):
        run = self.go([ProviderError("HTTP 503")])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "provider_error")
        self.assertEqual(len(self.provider_obj.calls), self.settings.max_attempts)


if __name__ == "__main__":
    unittest.main()
