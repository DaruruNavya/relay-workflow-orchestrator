"""JSON Schema enforcement for ai node output.

Uses the `jsonschema` package when installed; otherwise falls back to a built-in validator covering the
keywords the catalog uses (type, enum, required, properties, additionalProperties, items, min/max length,
min/max value). Either way the contract is the same: validate(instance, schema) -> list of error strings.
"""
from __future__ import annotations

try:  # optional dependency
    import jsonschema
except ImportError:  # pragma: no cover - exercised when the package is absent
    jsonschema = None

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate(instance, schema: dict) -> list[str]:
    if jsonschema is not None:
        validator_cls = jsonschema.validators.validator_for(schema)
        errors = sorted(validator_cls(schema).iter_errors(instance), key=lambda e: list(e.path))
        return [_fmt(list(e.path), e.message) for e in errors]
    return _mini(instance, schema, [])


def _fmt(path, message: str) -> str:
    return f"{'/'.join(str(p) for p in path) or '(root)'}: {message}"


def _mini(value, schema: dict, path: list) -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    if t is not None:
        allowed = t if isinstance(t, list) else [t]
        if not any(_TYPES[x](value) for x in allowed):
            return [_fmt(path, f"{value!r} is not of type {t!r}")]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(_fmt(path, f"{value!r} is not one of {schema['enum']!r}"))
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(_fmt(path, f"string longer than {schema['maxLength']} characters"))
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(_fmt(path, f"string shorter than {schema['minLength']} characters"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(_fmt(path, f"{value} is less than the minimum of {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(_fmt(path, f"{value} is greater than the maximum of {schema['maximum']}"))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errs.append(_fmt(path, f"'{name}' is a required property"))
        for name, sub in props.items():
            if name in value:
                errs.extend(_mini(value[name], sub, path + [name]))
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    errs.append(_fmt(path, f"additional property '{name}' is not allowed"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs.extend(_mini(item, schema["items"], path + [i]))
    return errs
