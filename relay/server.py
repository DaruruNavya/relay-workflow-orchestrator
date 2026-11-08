"""HTTP API (stdlib http.server) + console. Routes are declared in one table: easy to read, easy to port.

    python -m relay.server [--port 8080] [--with-worker]
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import ROOT, Settings
from .errors import RelayError
from .service import Relay

log = logging.getLogger("relay.api")
CONSOLE = ROOT / "console" / "index.html"
WORKFLOW_ID = r"(?P<wid>[^/]+)"

# (method, path regex, handler name, requires platform token)
ROUTES = [
    ("GET",  r"/health", "health", False),
    ("GET",  r"/workflows", "list_workflows", True),
    ("POST", r"/workflows", "create_workflow", True),
    ("GET",  rf"/workflows/{WORKFLOW_ID}", "get_workflow", True),
    ("PUT",  rf"/workflows/{WORKFLOW_ID}", "update_workflow", True),
    ("POST", rf"/workflows/{WORKFLOW_ID}/publish", "publish_workflow", True),
    ("POST", rf"/workflows/{WORKFLOW_ID}/trigger", "trigger_workflow", True),
    ("POST", rf"/hooks/{WORKFLOW_ID}", "webhook", False),          # protected by X-Relay-Secret instead
    ("GET",  r"/runs", "list_runs", True),
    ("GET",  r"/runs/(?P<rid>[^/]+)", "get_run", True),
    ("POST", r"/runs/(?P<rid>[^/]+)/cancel", "cancel_run", True),
    ("GET",  r"/approvals", "list_approvals", True),
    ("POST", r"/approvals/(?P<aid>[^/]+)/approve", "approve", True),
    ("POST", r"/approvals/(?P<aid>[^/]+)/reject", "reject", True),
]
COMPILED = [(m, re.compile(f"^{p}$"), h, auth) for m, p, h, auth in ROUTES]


class Api:
    """One method per route; each returns (status, json-able payload)."""

    def __init__(self, relay: Relay):
        self.relay = relay

    def health(self, req):                 return 200, {"status": "ok"}
    def list_workflows(self, req):         return 200, self.relay.list_workflows()
    def create_workflow(self, req):        return 201, self.relay.create_workflow(req.json())
    def get_workflow(self, req):           return 200, self.relay.get_workflow(req.args["wid"])
    def update_workflow(self, req):        return 200, self.relay.update_workflow(req.args["wid"], req.json())
    def publish_workflow(self, req):       return 200, self.relay.publish_workflow(req.args["wid"])
    def list_runs(self, req):              return 200, self.relay.list_runs()
    def get_run(self, req):                return 200, self.relay.get_run(req.args["rid"])
    def cancel_run(self, req):             return 200, self.relay.cancel_run(req.args["rid"])

    def trigger_workflow(self, req):
        body = req.json()
        if not isinstance(body, dict) or "input" not in body or not isinstance(body["input"], dict):
            raise RelayError(400, "invalid_request", 'Body must be {"input": {...}}')
        return 202, self.relay.trigger(req.args["wid"], body["input"], "manual")

    def webhook(self, req):
        return 202, self.relay.webhook(req.args["wid"], req.headers.get("X-Relay-Secret"), req.json())

    def list_approvals(self, req):
        status = req.query.get("status", [None])[0]
        return 200, self.relay.list_approvals(status)

    def approve(self, req):
        return 200, self.relay.decide(req.args["aid"], True, (req.json() or {}).get("decided_by"))

    def reject(self, req):
        return 200, self.relay.decide(req.args["aid"], False, (req.json() or {}).get("decided_by"))


class Request:
    def __init__(self, handler, args, query):
        self.headers, self.args, self.query, self._handler = handler.headers, args, query, handler

    def json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self._handler.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RelayError(400, "invalid_json", f"Request body is not valid JSON: {e}")


def make_handler(api: Api, settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, status: int, payload, content_type="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not settings.demo_token:
                return True
            header = self.headers.get("Authorization", "")
            return header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode(), settings.demo_token.encode())

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            if method == "GET" and parsed.path in ("/", "/console"):
                return self._send(200, CONSOLE.read_bytes(), "text/html; charset=utf-8")
            try:
                path_matched = False
                for m, rx, handler_name, needs_auth in COMPILED:
                    match = rx.match(parsed.path)
                    if not match:
                        continue
                    path_matched = True
                    if m != method:
                        continue
                    if needs_auth and not self._authorized():
                        raise RelayError(401, "unauthorized", "Missing or invalid bearer token")
                    req = Request(self, match.groupdict(), parse_qs(parsed.query))
                    status, payload = getattr(api, handler_name)(req)
                    return self._send(status, payload)
                if path_matched:
                    raise RelayError(405, "method_not_allowed", f"{method} not allowed on {parsed.path}")
                raise RelayError(404, "not_found", f"No route for {parsed.path}")
            except RelayError as e:
                self._send(e.status, e.body())
            except Exception as e:  # last-resort guard: never leak a stack trace over the wire
                log.exception("unhandled error")
                self._send(500, {"error": {"message": f"Internal error: {type(e).__name__}", "code": "internal_error"}})

        def do_GET(self):    self._dispatch("GET")
        def do_POST(self):   self._dispatch("POST")
        def do_PUT(self):    self._dispatch("PUT")

    return Handler


def create_server(settings: Settings, port: int, relay: Relay | None = None) -> ThreadingHTTPServer:
    relay = relay or Relay(settings)
    relay.seed()
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(Api(relay), settings))
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Relay API server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--with-worker", action="store_true", help="also run a worker thread in this process")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    server = create_server(settings, args.port)
    if args.with_worker:
        from .worker import build_worker
        threading.Thread(target=build_worker(settings).run_forever, daemon=True).start()
    log.info("Relay API on http://localhost:%d  (console at /console, token: %s)", args.port,
             settings.demo_token or "auth disabled")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
