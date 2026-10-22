"""A stand-in bridge for the gateway test: lists a tool no route serves, echoes the caller.

Run inside a container on the gateway's network, standard library only:
`python -I fake_bridge.py`, on `PORT` (8080 by default). It speaks MCP over
streamable HTTP without sessions, the way the gateway calls a route's server,
and answers every tool call with what names the call: the `Authorization`
header it arrived with, and the client and the person the gateway set.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override

# One tool of each server the rendering declares, and one no tool server serves.
TOOLS = (
    "list_episodes",
    "get_episode",
    "query_timeseries",
    "request_knx_write",
    "get_memory",
    "set_verdict",
)


class _Handler(BaseHTTPRequestHandler):
    @override
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: dict[str, Any] | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self) -> None:
        self._send(200)

    def do_POST(self) -> None:
        message: dict[str, Any] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in message:
            self._send(202)
            return
        method = message.get("method")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-bridge", "version": "0"},
            }
        elif method == "tools/list":
            schema = {"type": "object", "properties": {}}
            result = {"tools": [{"name": n, "inputSchema": schema} for n in TOOLS]}
        elif method == "tools/call":
            caller = {
                "authorization": self.headers.get("Authorization"),
                "client": self.headers.get("X-Client-Id"),
                "enduser": self.headers.get("X-Enduser-Id"),
            }
            result = {"content": [{"type": "text", "text": json.dumps(caller)}]}
        else:
            error = {"code": -32601, "message": f"no {method}"}
            self._send(200, {"jsonrpc": "2.0", "id": message["id"], "error": error})
            return
        self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), _Handler).serve_forever()
