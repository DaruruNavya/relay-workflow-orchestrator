"""Shared test fixtures: temp DB, in-process fake mock-world, engine + worker wired together."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from relay.catalog import Catalog
from relay.config import ROOT, Settings
from relay.db import Store
from relay.engine import Engine
from relay.providers import FakeProvider, HeuristicProvider
from relay.service import Relay
from relay.worker import Worker
from tests.fake_world import World, make_server


class RelayTestCase(unittest.TestCase):
    provider = None  # override per test: FakeProvider([...]) or HeuristicProvider()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.world = World()
        self.server = make_server(self.world, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.settings = Settings(
            db_path=str(Path(self.tmp.name) / "relay.db"), data_dir=ROOT / "data",
            world_url=f"http://127.0.0.1:{self.server.server_address[1]}",
            http_timeout=2.0, backoff_base=0.01, lease_seconds=0.6, poll_interval=0.05)
        self.relay = Relay(self.settings)
        self.build_engine(self.provider or FakeProvider([{}]))

    def build_engine(self, provider, fault_hook=None):
        self.provider_obj = provider
        self.engine = Engine(self.relay.store, self.settings, self.relay.catalog, provider, fault_hook=fault_hook)
        self.worker = Worker(self.relay.store, self.settings, self.engine, "test-worker")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    # -- helpers
    def drain(self):
        """Run the worker until the queue is empty."""
        while self.worker.run_once():
            pass

    def deploy(self, defn: dict) -> str:
        self.relay.create_workflow(defn)
        self.relay.publish_workflow(defn["id"])
        return defn["id"]

    def run_workflow(self, defn: dict, body=None) -> dict:
        wid = self.deploy(defn)
        run_id = self.relay.trigger(wid, body or {})["run_id"]
        self.drain()
        return self.relay.get_run(run_id)

    def ledger(self, action=None):
        return [e for e in self.world.ledger if action is None or e["action"] == action]


def workflow(wid, nodes, entry=None, trigger=None, max_steps=20):
    return {"id": wid, "name": wid, "trigger": trigger or {"type": "manual"}, "entry": entry or nodes[0]["id"],
            "limits": {"max_steps": max_steps, "timeout_seconds": 60, "max_ai_tokens": 0}, "nodes": nodes}


def notify(nid, nxt=None, message="hello", to="#t"):
    return {"id": nid, "type": "notify", "params": {"channel": "chat", "to": to, "message": message}, "next": nxt}


def ai_node(nid, nxt=None, schema=None):
    schema = schema or {"type": "object", "properties": {"category": {"type": "string", "enum": ["a", "b"]}},
                        "required": ["category"], "additionalProperties": False}
    return {"id": nid, "type": "ai", "params": {"prompt": "Classify: {{trigger.body.text}}", "output_schema": schema},
            "next": nxt}
