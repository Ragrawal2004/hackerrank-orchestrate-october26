import unittest

from ghost import schema


def ok(instance, sch):
    schema.validate(instance, sch)


def bad(test, instance, sch):
    with test.assertRaises(schema.SchemaError):
        schema.validate(instance, sch)


class SchemaTests(unittest.TestCase):
    def test_types(self):
        ok("a", {"type": "string"})
        ok(3, {"type": "integer"})
        ok(3, {"type": "number"})
        ok(3.5, {"type": "number"})
        ok(True, {"type": "boolean"})
        ok(None, {"type": "null"})
        ok([], {"type": "array"})
        ok({}, {"type": "object"})
        ok(None, {"type": ["string", "null"]})

    def test_type_mismatches(self):
        bad(self, 1, {"type": "string"})
        bad(self, "1", {"type": "integer"})
        bad(self, 3.5, {"type": "integer"})
        bad(self, True, {"type": "integer"})  # bool is not an integer here
        bad(self, True, {"type": "number"})
        bad(self, {}, {"type": "array"})
        bad(self, 1, {"type": ["string", "null"]})

    def test_enum(self):
        ok("A", {"enum": ["A", "B"]})
        bad(self, "C", {"enum": ["A", "B"]})

    def test_required_and_properties(self):
        sch = {"type": "object", "required": ["intent"],
               "properties": {"intent": {"type": "string"}, "n": {"type": "integer"}}}
        ok({"intent": "x"}, sch)
        ok({"intent": "x", "n": 2}, sch)
        bad(self, {}, sch)
        bad(self, {"intent": 1}, sch)
        bad(self, {"intent": "x", "n": "no"}, sch)

    def test_additional_properties_false(self):
        sch = {"type": "object", "properties": {"a": {"type": "string"}},
               "additionalProperties": False}
        ok({"a": "x"}, sch)
        bad(self, {"a": "x", "b": 1}, sch)

    def test_additional_properties_default_allows_extras(self):
        ok({"a": "x", "b": 1}, {"type": "object", "properties": {"a": {"type": "string"}}})

    def test_items(self):
        sch = {"type": "array", "items": {"type": "integer"}}
        ok([1, 2], sch)
        bad(self, [1, "x"], sch)

    def test_nested_error_names_the_path(self):
        sch = {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "string"}}}}
        with self.assertRaises(schema.SchemaError) as cm:
            schema.validate({"a": ["x", 2]}, sch)
        self.assertIn("a", str(cm.exception))
        self.assertIn("1", str(cm.exception))

    def test_unknown_keyword_raises_instead_of_being_ignored(self):
        with self.assertRaises(ValueError):
            schema.validate("x", {"type": "string", "pattern": "^a"})
        with self.assertRaises(ValueError):
            schema.check_schema({"type": "object", "properties": {"a": {"minimum": 1}}})

    def test_unknown_type_raises(self):
        with self.assertRaises(ValueError):
            schema.check_schema({"type": "strng"})

    def test_description_and_title_are_annotations(self):
        ok("x", {"type": "string", "description": "d", "title": "t"})


if __name__ == "__main__":
    unittest.main()
