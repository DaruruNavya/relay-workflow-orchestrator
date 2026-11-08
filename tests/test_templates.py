import unittest

from relay.templates import TemplateError, references, render

CTX = {"trigger": {"body": {"name": "Ada", "amount": 250, "ok": True, "tags": ["x", "y"], "obj": {"k": 1}}},
       "nodes": {"classify": {"output": {"category": "refund_request"}}}}


class TemplateResolution(unittest.TestCase):
    def test_trigger_and_node_output_paths(self):
        self.assertEqual(render("Hi {{trigger.body.name}}: {{nodes.classify.output.category}}", CTX),
                         "Hi Ada: refund_request")

    def test_whitespace_inside_braces(self):
        self.assertEqual(render("{{ trigger.body.name }}", CTX), "Ada")

    def test_numbers_and_booleans_are_stringified(self):
        self.assertEqual(render("{{trigger.body.amount}}/{{trigger.body.ok}}", CTX), "250/true")

    def test_list_index_and_object_values(self):
        self.assertEqual(render("{{trigger.body.tags.1}}", CTX), "y")
        self.assertEqual(render("{{trigger.body.obj}}", CTX), '{"k": 1}')

    def test_recursive_in_dicts_and_lists(self):
        out = render({"a": ["{{trigger.body.name}}", {"b": "{{trigger.body.amount}}"}], "n": 5}, CTX)
        self.assertEqual(out, {"a": ["Ada", {"b": "250"}], "n": 5})

    def test_missing_path_fails_loudly(self):
        with self.assertRaises(TemplateError):
            render("{{trigger.body.nope}}", CTX)
        with self.assertRaises(TemplateError):
            render("{{nodes.ghost.output.x}}", CTX)

    def test_text_without_placeholders_is_untouched(self):
        self.assertEqual(render("plain $100 {not a template}", CTX), "plain $100 {not a template}")

    def test_references_lists_every_path(self):
        self.assertEqual(references({"m": "{{a.b}} and {{ c.d }}", "x": ["{{e}}"]}), {"a.b", "c.d", "e"})


if __name__ == "__main__":
    unittest.main()
