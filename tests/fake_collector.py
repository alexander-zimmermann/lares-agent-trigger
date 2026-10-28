"""A stand-in OTLP/HTTP receiver for the gateway test: keeps every export, hands them out.

Run inside a container on the gateway's network, standard library only:
`python -I fake_collector.py`, on `PORT` (4318 by default). It answers every
POST as a receiver does and keeps its body; `GET /exports` returns
them all, hex-encoded in a JSON list, for the test to search.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override

_EXPORTS: list[bytes] = []
_LOCK = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    @override
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with _LOCK:
            _EXPORTS.append(body)
        self._send(b"", "application/x-protobuf")

    def do_GET(self) -> None:
        with _LOCK:
            exports = [export.hex() for export in _EXPORTS]
        self._send(json.dumps(exports).encode(), "application/json")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "4318"))), _Handler).serve_forever()
