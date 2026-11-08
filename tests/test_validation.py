import copy
import json
import unittest

from relay.catalog import Catalog
from relay.config import ROOT
from relay.errors import RelayError
from relay.validation import validate_definition
from tests.helpers import notify, workflow

CATALOG = Catalog.load(ROOT / "data" / "node_catalog.json")


def code_of(defn):
    try:
        validate_definition(defn, CATALOG)
    except RelayError as e:
        return e.status, e.code
    return None


class PublishValidation(unittest.TestCase):
    def test_all_seed_workflows_are_valid(self):
        seeds = json.loads((ROOT / "data" / "seed_workflows.json").read_text())["workflows"]
        for wf in seeds:
            self.assertIsNone(code_of(wf), wf["id"])

    def test_unknown_node_type(self):
        wf = workflow("w", [{"id": "n1", "type": "teleport", "params": {}, "next": None}])
        self.assertEqual(code_of(wf), (422, "invalid_node_type"))

    def test_missing_required_param(self):
        wf = workflow("w", [{"id": "n1", "type": "notify", "params": {"channel": "chat"}, "next": None}])
        self.assertEqual(code_of(wf), (422, "missing_param"))

    def test_next_pointing_nowhere(self):
        self.assertEqual(code_of(workflow("w", [notify("n1", nxt="ghost")])), (422, "invalid_reference"))

    def test_condition_branch_pointing_nowhere(self):
        cond = {"id": "c", "type": "condition", "params": {"left": "1", "op": "equals", "right": "1"},
                "on_true": "ghost", "on_false": "c"}
        self.assertEqual(code_of(workflow("w", [cond])), (422, "invalid_reference"))

    def test_condition_needs_both_branches(self):
        cond = {"id": "c", "type": "condition", "params": {"left": "1", "op": "equals", "right": "1"}, "on_true": None}
        self.assertEqual(code_of(workflow("w", [cond])), (422, "missing_branch"))

    def test_entry_pointing_nowhere(self):
        self.assertEqual(code_of(workflow("w", [notify("n1")], entry="nope")), (422, "invalid_entry"))

    def test_bad_enum_value(self):
        n = {"id": "n1", "type": "notify", "params": {"channel": "pigeon", "to": "x", "message": "m"}, "next": None}
        self.assertEqual(code_of(workflow("w", [n])), (422, "invalid_param_value"))

    def test_template_reference_to_missing_node(self):
        wf = workflow("w", [notify("n1", message="{{nodes.ghost.output.x}}")])
        self.assertEqual(code_of(wf), (422, "invalid_template_reference"))

    def test_duplicate_node_ids(self):
        self.assertEqual(code_of(workflow("w", [notify("n1"), notify("n1")])), (422, "invalid_definition"))

    def test_unknown_trigger_and_missing_webhook_secret(self):
        wf = workflow("w", [notify("n1")], trigger={"type": "carrier_pigeon"})
        self.assertEqual(code_of(wf), (422, "invalid_trigger"))
        wf = workflow("w", [notify("n1")], trigger={"type": "webhook"})
        self.assertEqual(code_of(wf), (422, "invalid_trigger"))

    def test_backward_jump_loops_are_legal(self):
        a, b = notify("a", nxt="b"), notify("b", nxt="a")  # a -> b -> a ...
        self.assertIsNone(code_of(workflow("w", [a, b])))

    def test_error_messages_are_distinct_and_specific(self):
        msgs = set()
        for wf in (workflow("w", [{"id": "n1", "type": "teleport", "params": {}}]),
                   workflow("w", [notify("n1", nxt="ghost")]), workflow("w", [notify("n1")], entry="nope")):
            try:
                validate_definition(wf, CATALOG)
            except RelayError as e:
                msgs.add(e.message)
        self.assertEqual(len(msgs), 3)


if __name__ == "__main__":
    unittest.main()
