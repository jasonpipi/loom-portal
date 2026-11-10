"""LOOM portal over HTTP (standard library only).

    loom-portal --port 8787 --seed-credit cambridge-clinic=85

Endpoints (JSON in, JSON out):

    GET  /health
    POST /v1/<namespace>/requests   new access request
    POST /v1/<namespace>/fetch      fresh data under a grant
    GET  /v1/<namespace>/credit     publisher -> credit
    GET  /v1/<namespace>/log        every decision, in order

A namespace is one isolated portal state (one person, one test run);
it is created on first use, with the configured seed credits.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .policy import POLICY_VERSION, Portal
from .record import synthetic_record

_NS = re.compile(r"^/v1/([A-Za-z0-9_.-]{1,80})/(requests|fetch|credit|log)$")


class PortalStore:
    def __init__(self, seed_credit: dict[str, int] | None = None):
        self.seed_credit = dict(seed_credit or {})
        self.portals: dict[str, Portal] = {}
        self.lock = threading.Lock()

    def get(self, ns: str) -> Portal:
        if ns not in self.portals:
            self.portals[ns] = Portal(record=synthetic_record(),
                                      credit=dict(self.seed_credit))
        return self.portals[ns]


def make_handler(store: PortalStore):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True, "version": __version__,
                                        "policy": POLICY_VERSION})
            m = _NS.match(self.path)
            if not m or m.group(2) not in ("credit", "log"):
                return self._send(404, {"error": "not found"})
            with store.lock:
                p = store.get(m.group(1))
                out = p.credit if m.group(2) == "credit" else p.log
                return self._send(200, out)

        def do_POST(self):
            m = _NS.match(self.path)
            if not m or m.group(2) not in ("requests", "fetch"):
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict) or "request_id" not in req:
                    raise ValueError("body must be an object with request_id")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"error": str(exc)})
            with store.lock:
                p = store.get(m.group(1))
                try:
                    out = (p.request(req) if m.group(2) == "requests"
                           else p.fetch(req))
                except (KeyError, TypeError, ValueError) as exc:
                    return self._send(400, {"error": f"bad request: {exc}"})
            return self._send(409 if out["decision"] == "conflict" else 200,
                              out)
    return Handler


def serve(host: str = "127.0.0.1", port: int = 8787,
          seed_credit: dict[str, int] | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port),
                                 make_handler(PortalStore(seed_credit)))
    return server


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="loom-portal")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--seed-credit", action="append", default=[],
                    metavar="PUBLISHER=SCORE")
    args = ap.parse_args(argv)
    seeds = {}
    for item in args.seed_credit:
        pub, _, score = item.partition("=")
        seeds[pub] = int(score)
    server = serve(args.host, args.port, seeds)
    print(f"loom-portal {__version__} ({POLICY_VERSION}) on"
          f" http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
