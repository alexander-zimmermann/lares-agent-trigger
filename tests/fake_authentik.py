"""A stand-in Authentik for the gateway test: one application's discovery and signing keys.

Run inside a container on the gateway's network, standard library only:
`python -I fake_authentik.py`. `ISSUER` is the application's issuer, as the
connector declares it; `JWKS` the key set its tokens are signed with.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override
from urllib.parse import urlsplit

ISSUER = os.environ["ISSUER"]
_BASE = urlsplit(ISSUER).path

DOCUMENTS: dict[str, Any] = {
    f"{_BASE}jwks/": json.loads(os.environ["JWKS"]),
    f"{_BASE}.well-known/openid-configuration": {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}authorize/",
        "token_endpoint": f"{ISSUER}token/",
        "jwks_uri": f"{ISSUER}jwks/",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
    },
}


class _Handler(BaseHTTPRequestHandler):
    @override
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        document = DOCUMENTS.get(self.path)
        data = json.dumps(document).encode()
        self.send_response(200 if document is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), _Handler).serve_forever()
