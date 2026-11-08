"""Publish-time validation of a workflow definition against the node catalog.

Each failure mode has its own error code and message so a builder always knows what to fix:
invalid_definition, invalid_trigger, invalid_node_type, missing_param, invalid_param_value,
missing_branch, invalid_reference, invalid_entry, invalid_template_reference.
Backward jumps (loops) are legal; run caps bound them at execution time.
"""
from __future__ import annotations

import re

from .catalog import Catalog
from .errors import RelayError
from .templates import references

ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
NODE_REF_RE = re.compile(r"^nodes\.([^.\s]+)")


def _bad(code: str, message: str) -> RelayError:
    return RelayError(422, code, message)


def check_shape(defn) -> None:
    """Minimal structural check, applied when a draft is saved (so incomplete drafts can still be stored)."""
    if not isinstance(defn, dict):
        raise _bad("invalid_definition", "Definition must be a JSON object")
    if not isinstance(defn.get("id"), str) or not ID_RE.match(defn["id"]):
        raise _bad("invalid_definition", "'id' is required and may only contain letters, digits, '_' and '-'")
    if not isinstance(defn.get("name"), str) or not defn["name"].strip():
        raise _bad("invalid_definition", "'name' is required")
    nodes = defn.get("nodes")
    if not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes):
        raise _bad("invalid_definition", "'nodes' must be a list of node objects")


def validate_definition(defn: dict, catalog: Catalog) -> None:
    check_shape(defn)
    nodes = defn["nodes"]
    if not nodes:
        raise _bad("invalid_definition", "A workflow needs at least one node")

    trigger = defn.get("trigger")
    if not isinstance(trigger, dict) or trigger.get("type") not in catalog.triggers:
        got = trigger.get("type") if isinstance(trigger, dict) else trigger
        raise _bad("invalid_trigger", f"Unknown or missing trigger type {got!r}; allowed: {sorted(catalog.triggers)}")
    for key, spec in catalog.triggers[trigger["type"]].get("config", {}).items():
        if spec.get("required") and not trigger.get(key):
            raise _bad("invalid_trigger", f"Trigger '{trigger['type']}' is missing required config '{key}'")

    limits = defn.get("limits", {})
    if not isinstance(limits, dict):
        raise _bad("invalid_definition", "'limits' must be an object")
    max_steps = limits.get("max_steps")
    if max_steps is not None and (not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1):
        raise _bad("invalid_definition", "limits.max_steps must be a positive integer")

    ids: set[str] = set()
    for node in nodes:
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            raise _bad("invalid_definition", "Every node needs a string 'id'")
        if nid in ids:
            raise _bad("invalid_definition", f"Duplicate node id '{nid}'")
        ids.add(nid)

    for node in nodes:
        _validate_node(node, ids, catalog)

    entry = defn.get("entry")
    if entry not in ids:
        raise _bad("invalid_entry", f"entry '{entry}' does not match any node id")

    for node in nodes:  # template references to other nodes must exist
        for path in references(node.get("params", {})):
            m = NODE_REF_RE.match(path)
            if m and m.group(1) not in ids:
                raise _bad("invalid_template_reference",
                           f"Node '{node['id']}' references '{{{{{path}}}}}' but node '{m.group(1)}' does not exist")


def _validate_node(node: dict, ids: set[str], catalog: Catalog) -> None:
    nid, ntype = node["id"], node.get("type")
    spec = catalog.nodes.get(ntype)
    if spec is None:
        raise _bad("invalid_node_type", f"Node '{nid}' has unknown type '{ntype}'")
    params = node.get("params", {})
    if not isinstance(params, dict):
        raise _bad("invalid_definition", f"Node '{nid}': 'params' must be an object")
    for pname, pspec in spec["params"].items():
        if pspec.get("required") and params.get(pname) is None:
            raise _bad("missing_param", f"Node '{nid}' ({ntype}) is missing required param '{pname}'")
        if pname in params:
            _check_param(nid, ntype, pname, pspec, params[pname])

    refs = {"next": node.get("next"), "on_true": node.get("on_true"), "on_false": node.get("on_false")}
    if ntype == "condition":
        for branch in ("on_true", "on_false"):
            if refs[branch] is None:
                raise _bad("missing_branch", f"Condition node '{nid}' must define '{branch}'")
    for key, target in refs.items():
        if target is not None and target not in ids:
            raise _bad("invalid_reference", f"Node '{nid}' has {key} -> '{target}', which does not exist")


def _check_param(nid, ntype, pname, pspec, value) -> None:
    if "enum" in pspec and value not in pspec["enum"] and "{{" not in str(value):
        raise _bad("invalid_param_value", f"Node '{nid}' ({ntype}): '{pname}' must be one of {pspec['enum']}, got {value!r}")
    expected = pspec.get("type")
    ok = {
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "object": isinstance(value, dict),
    }.get(expected, True)
    if not ok:
        raise _bad("invalid_param_value", f"Node '{nid}' ({ntype}): '{pname}' must be of type {expected}")
