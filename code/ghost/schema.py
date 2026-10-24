"""Tiny JSON-Schema-subset validator (stdlib only).

Supported keywords: type, enum, required, properties, additionalProperties (bool),
items, plus the annotations description and title. Any other keyword raises
ValueError, so a schema can never be silently under-enforced.
"""

TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")
KEYWORDS = {"type", "enum", "required", "properties", "additionalProperties", "items",
            "description", "title"}


class SchemaError(ValueError):
    """The instance does not satisfy the schema."""


def check_schema(schema, path="$"):
    """Raise ValueError if the schema uses anything this validator does not enforce."""
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: schema must be an object")
    for key in schema:
        if key not in KEYWORDS:
            raise ValueError(f"{path}: unsupported schema keyword {key!r}")
    types = schema.get("type")
    if types is not None:
        for t in [types] if isinstance(types, str) else types:
            if t not in TYPES:
                raise ValueError(f"{path}: unknown type {t!r}")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise ValueError(f"{path}: additionalProperties must be a boolean")
    for name, sub in schema.get("properties", {}).items():
        check_schema(sub, f"{path}.{name}")
    if "items" in schema:
        check_schema(schema["items"], f"{path}[]")


def _is(instance, t):
    if t == "object":
        return isinstance(instance, dict)
    if t == "array":
        return isinstance(instance, list)
    if t == "string":
        return isinstance(instance, str)
    if t == "boolean":
        return isinstance(instance, bool)
    if t == "null":
        return instance is None
    if t == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    return isinstance(instance, (int, float)) and not isinstance(instance, bool)


def validate(instance, schema, path="$"):
    """Raise SchemaError if instance violates schema. Returns None on success."""
    check_schema(schema, path)
    _validate(instance, schema, path)


def _validate(instance, schema, path):
    types = schema.get("type")
    if types is not None:
        allowed = [types] if isinstance(types, str) else list(types)
        if not any(_is(instance, t) for t in allowed):
            raise SchemaError(f"{path}: expected {'|'.join(allowed)}, got {type(instance).__name__}")
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path}: {instance!r} is not one of {schema['enum']}")
    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                raise SchemaError(f"{path}: missing required property {name!r}")
        props = schema.get("properties", {})
        for name, value in instance.items():
            if name in props:
                _validate(value, props[name], f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                raise SchemaError(f"{path}: unexpected property {name!r}")
    if isinstance(instance, list) and "items" in schema:
        for i, value in enumerate(instance):
            _validate(value, schema["items"], f"{path}[{i}]")
