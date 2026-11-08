"""A small stand-in for scripts/mock_world.py, used only by the automated tests.

Same idea as the real one: every side effect is appended to a ledger, and a repeated Idempotency-Key is
absorbed (replayed) instead of executing twice. Test knobs: POST /admin/fail {"next": N} answers the next N
requests with 503; POST /admin/slow {"seconds": S, "next": N} performs the action, THEN stalls before
replying (the "effect landed but the client timed out" case).
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class World:
    def __init__(self):
        self.lock = threading.Lock()
        self.ledger: list[dict] = []
        self.keys: dict[str, dict] = {}
        self.replays = 0
        self.fail_next = 0
        self.slow_next = 0
        self.slow_seconds = 0.0

    def apply(self, action: str, payload: dict, key: str | None, reply_extra: dict) -> dict:
        with self.lock:
            if key and key in self.keys:
                self.replays += 1
                return self.keys[key]
            ref = f"{action.split('.')[0][:3]}_{len(self.ledger) + 1:04d}"
            self.ledger.append({"action": action, "payload": payload, "idempotency_key": key, "at": time.time()})
            reply = {"id": ref, "notification_id": ref, "reference_id": ref, "status": "ok", **reply_extra}
            if key:
                self.keys[key] = reply
            return reply


def make_server(world: World, port: int = 0) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/admin/ledger":
                return self._reply(200, {"entries": world.ledger, "replays": world.replays})
            m = re.match(r"^/orders/([\w-]+)$", self.path)
            if m:
                return self._reply(200, {"order_id": m.group(1), "status": "processing", "total_usd": 120})
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            body = self._body()
            if self.path == "/admin/fail":
                world.fail_next = body.get("next", 1)
                return self._reply(200, {"ok": True})
            if self.path == "/admin/slow":
                world.slow_next, world.slow_seconds = body.get("next", 1), body.get("seconds", 2)
                return self._reply(200, {"ok": True})
            if self.path == "/admin/reset":
                world.ledger.clear(); world.keys.clear(); world.replays = 0
                return self._reply(200, {"ok": True})
            if world.fail_next > 0:
                world.fail_next -= 1
                return self._reply(503, {"error": "injected failure"})
            key = self.headers.get("Idempotency-Key")
            routes = {"/email/send": "email.send", "/chat/message": "chat.message", "/shipments": "shipment.create"}
            action, extra = routes.get(self.path), {}
            m = re.match(r"^/orders/([\w-]+)/(refund|replacement)$", self.path)
            if m:
                action = f"order.{m.group(2)}"
                body = {**body, "order_id": m.group(1)}
            if self.path == "/shipments":
                extra = {"shipment_id": f"shp_{len(world.ledger) + 1:04d}"}
            if action is None:
                return self._reply(404, {"error": "unknown path"})
            reply = world.apply(action, body, key, extra)
            if world.slow_next > 0:
                world.slow_next -= 1
                time.sleep(world.slow_seconds)
            self._reply(200, reply)

    class Quiet(ThreadingHTTPServer):
        def handle_error(self, request, client_address):  # clients that time out on purpose are expected
            pass

    srv = Quiet(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":  # python tests/fake_world.py 9210
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9210
    make_server(World(), port).serve_forever()
