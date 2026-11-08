"""Loads data/node_catalog.json: the registry of allowed trigger and node types."""
from __future__ import annotations

import json
from pathlib import Path


class Catalog:
    def __init__(self, raw: dict):
        self.nodes = {n["type"]: n for n in raw["nodes"]}
        self.triggers = {t["type"]: t for t in raw["triggers"]}

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def requires_approval(self, node_type: str) -> bool:
        return bool(self.nodes.get(node_type, {}).get("requires_approval"))

    def is_side_effect(self, node_type: str) -> bool:
        return bool(self.nodes.get(node_type, {}).get("side_effect"))
