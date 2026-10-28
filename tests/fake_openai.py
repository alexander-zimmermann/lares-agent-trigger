"""A stand-in for OpenAI's transcriptions in the gateway test: echoes what reached it.

Run inside a container on the gateway's network, standard library only:
`python -I fake_openai.py`, on `PORT` (8090 by default). It answers a
transcription the way OpenAI does, with the text in JSON, and beside it what
names the call: the path, the `Authorization` header and the content type it
arrived with, and the SHA-256 of the body, so the test sees whether the upload
came through as it was sent.
"""

from __future__ import annotations

import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override


class _Handler(BaseHTTPRequestHandler):
    @override
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        answer = {
            "text": "Hallo aus der Küche",
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8090"))), _Handler).serve_forever()
