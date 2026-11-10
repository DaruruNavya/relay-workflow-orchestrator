"""{{ path.to.value }} resolution over the run context: {"trigger": {"body": ...}, "nodes": {id: {"output": ...}}}.

A path that does not resolve raises TemplateError, which fails the step with a clear message instead of
silently sending an empty string to a customer.
"""
from __future__ import annotations

import json
import re

PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


class TemplateError(Exception):
    pass


def lookup(path: str, ctx: dict):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise TemplateError(f"cannot resolve '{{{{{path}}}}}': '{part}' not found")
    return cur


def stringify(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def render(value, ctx: dict):
    """Recursively resolve placeholders in strings, dicts and lists."""
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda m: stringify(lookup(m.group(1), ctx)), value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


def references(value) -> set[str]:
    """All placeholder paths used inside a value (for publish-time linting)."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(m.group(1) for m in PLACEHOLDER.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= references(v)
    elif isinstance(value, list):
        for v in value:
            found |= references(v)
    return found
