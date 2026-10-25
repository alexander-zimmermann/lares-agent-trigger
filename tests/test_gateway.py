"""The rendered routes run as the gateway runs them: agentgateway, GitHub's server, a bridge
and Authentik.

The gateway starts as its chart starts it with the rendered values: every
variable they name set from its sealed key, every sealed key a value of its
own, and the routes and every credential laid into a volume the way kubelet
lays a Secret into a pod: a timestamped directory behind `..data`, swapped
whole on a change. The cluster's Authentik comes in as the overlay sets it.
Each upstream answers under the name the declaration gives it, on a network
of their own. The bridge is a stand-in, under the bridge's name and under
GitHub's, that lists a tool no route serves and answers with the key it was
called with and the caller the gateway named: what a route attached shows
there. Authentik is a stand-in serving one application's discovery and
signing keys; the test signs the tokens.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa

from lares_agent_trigger.generate import render
from lares_agent_trigger.persons import load_persons_file
from lares_agent_trigger.use_cases import ToolServer, load_use_case_file

from .test_generate import PERSONS, SETTINGS, USE_CASES

# What lares deploys; Renovate bumps it.
GATEWAY_IMAGE = "cr.agentgateway.dev/agentgateway:v1.6.0"
# The stand-in bridge's interpreter.
BRIDGE_IMAGE = "python:3.15-slim"

FAKE_BRIDGE = Path(__file__).parent / "fake_bridge.py"
FAKE_AUTHENTIK = Path(__file__).parent / "fake_authentik.py"
# The cluster's Authentik, as the gateway's overlay sets it.
AUTHENTIK_URL = "http://authentik:9000"
# The share of traces the gateway keeps, which lares sets beside the rendered values.
SAMPLING_RATIO = "0.5"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
# Where the rendered chart values mount the routes and the credentials.
MOUNT = "/etc/agentgateway"
TOKEN = "ghs_not-a-token"

# Writes files into a directory the way kubelet's AtomicWriter updates a Secret volume.
_ATOMIC_WRITE = """
import os, shutil, sys, time
root, files = sys.argv[1], dict(arg.split("=", 1) for arg in sys.argv[2:])
os.makedirs(root, exist_ok=True)
stamp = time.strftime("..%Y_%m_%d_%H_%M_%S.") + str(time.time_ns() % 10**9).zfill(9)
os.mkdir(os.path.join(root, stamp))
for name, text in files.items():
    with open(os.path.join(root, stamp, name), "w") as f:
        f.write(text)
data = os.path.join(root, "..data")
old = os.readlink(data) if os.path.islink(data) else None
os.symlink(stamp, os.path.join(root, "..data_tmp"))
os.rename(os.path.join(root, "..data_tmp"), data)
for name in files:
    if not os.path.islink(os.path.join(root, name)):
        os.symlink(os.path.join("..data", name), os.path.join(root, name))
if old:
    shutil.rmtree(os.path.join(root, old))
"""


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=300
    ).stdout.strip()


def _value(key: str) -> str:
    """What a sealed key holds in this test."""
    return f"value-of-{key}"


@dataclass
class Gateway:
    url: str
    container: str
    volume: str

    def write(self, directory: str, files: dict[str, str]) -> None:
        """Lay files into the gateway's volume as kubelet does, swapping the whole directory."""
        _docker(
            "run",
            "--rm",
            "-v",
            f"{self.volume}:{MOUNT}",
            BRIDGE_IMAGE,
            "python",
            "-I",
            "-c",
            _ATOMIC_WRITE,
            f"{MOUNT}/{directory}",
            *(f"{name}={text}" for name, text in files.items()),
        )

    def started_at(self) -> str:
        return _docker("inspect", "--format", "{{.State.StartedAt}}", self.container)


@dataclass(frozen=True)
class Rendered:
    # The routes file and the gateway's chart values.
    routes: str
    values: dict[str, Any]
    github: ToolServer

    def variables(self) -> dict[str, str]:
        """Each variable the values set, holding its sealed key."""
        return {
            entry["name"]: _value(entry["valueFrom"]["secretKeyRef"]["key"])
            for entry in self.values["extraEnv"]
        }

    def credentials(self) -> dict[str, dict[str, str]]:
        """Each mounted Secret's files: GitHub's a token, every other key a value of its own."""
        credentials: dict[str, dict[str, str]] = {}
        for volume in self.values["extraVolumes"]:
            if "secret" not in volume:
                continue
            secret = volume["secret"]["secretName"]
            keys = [item["key"] for item in volume["secret"]["items"]]
            token = secret == self.github.credential.secret
            credentials[secret] = {key: TOKEN if token else _value(key) for key in keys}
        return credentials


@pytest.fixture(scope="module")
def rendered() -> Rendered:
    declared = load_use_case_file(USE_CASES)
    rendering = render(
        declared, load_persons_file(PERSONS, declared), SETTINGS.read_text(encoding="utf-8")
    )
    github = declared.tool_servers["github"]
    return Rendered(
        routes=rendering.routes, values=yaml.safe_load(rendering.routes_values), github=github
    )


@pytest.fixture(scope="module")
def gateway(rendered: Rendered) -> Iterator[Gateway]:
    """The gateway as the chart starts it: its own settings naming the routes file."""
    github = rendered.github
    declared = load_use_case_file(USE_CASES)
    bridge_host = urlsplit(declared.tool_servers["lares"].url).hostname
    github_host = urlsplit(github.url).hostname
    assert bridge_host is not None and github_host is not None
    run = uuid.uuid4().hex[:8]
    network, volume = f"gateway-test-{run}", f"gateway-test-{run}"
    containers: list[str] = []
    _docker("network", "create", network)
    _docker("volume", "create", volume)
    try:
        gateway = Gateway(url="", container="", volume=volume)
        settings = {"config": {**rendered.values["config"]["config"], "adminAddr": "off"}}
        gateway.write("settings", {"config.yaml": yaml.safe_dump(settings)})
        gateway.write("routes", {"config.yaml": rendered.routes})
        for secret, files in rendered.credentials().items():
            gateway.write(f"credentials/{secret}", files)
        issuer = declared.connectors["claude-ai"].issuer.replace("${AUTHENTIK_URL}", AUTHENTIK_URL)
        jwk = {**jwt.algorithms.RSAAlgorithm.to_jwk(SIGNING_KEY.public_key(), as_dict=True)}
        jwks = json.dumps({"keys": [{**jwk, "kid": "authentik", "alg": "RS256", "use": "sig"}]})
        containers.append(
            _docker(
                "run",
                "-d",
                "--network",
                network,
                "--network-alias",
                "authentik",
                "-e",
                f"ISSUER={issuer}",
                "-e",
                f"JWKS={jwks}",
                "-v",
                f"{FAKE_AUTHENTIK}:/fake_authentik.py:ro",
                BRIDGE_IMAGE,
                "python",
                "-I",
                "/fake_authentik.py",
            )
        )
        containers.append(
            _docker(
                "run",
                "-d",
                "--network",
                network,
                "--network-alias",
                bridge_host,
                "-v",
                f"{FAKE_BRIDGE}:/fake_bridge.py:ro",
                BRIDGE_IMAGE,
                "python",
                "-I",
                "/fake_bridge.py",
            )
        )
        containers.append(
            _docker(
                "run",
                "-d",
                "--network",
                network,
                "--network-alias",
                github_host,
                "-e",
                f"PORT={urlsplit(github.url).port}",
                "-v",
                f"{FAKE_BRIDGE}:/fake_bridge.py:ro",
                BRIDGE_IMAGE,
                "python",
                "-I",
                "/fake_bridge.py",
            )
        )
        port = urlsplit(declared.gateway.url).port
        container = _docker(
            "run",
            "-d",
            "--network",
            network,
            "-p",
            f"127.0.0.1::{port}",
            *(
                option
                for name, value in rendered.variables().items()
                for option in ("-e", f"{name}={value}")
            ),
            "-e",
            f"AUTHENTIK_URL={AUTHENTIK_URL}",
            "-e",
            f"TRACING_SAMPLING_RATIO={SAMPLING_RATIO}",
            "--mount",
            f"type=volume,src={volume},dst={MOUNT},readonly",
            GATEWAY_IMAGE,
            "-f",
            f"{MOUNT}/settings/config.yaml",
        )
        containers.append(container)
        published = _docker("port", container, str(port)).splitlines()[0].rsplit(":", 1)[1]
        gateway.url, gateway.container = f"http://127.0.0.1:{published}", container
        _wait_until_serving(gateway)
        yield gateway
    finally:
        for container in containers:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


def _wait_until_serving(gateway: Gateway) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if _initialize(gateway, "github", _value("kim-gateway-key")) == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise AssertionError(_docker("logs", gateway.container))


class _Session:
    """One client on one route, as the harness holds it: one handshake, then its requests.

    It keeps the session id the gateway hands out, if it hands one out. `path`
    is a connector's own; a tool server's route is `/mcp/<route>`.
    """

    def __init__(
        self,
        gateway: Gateway,
        route: str,
        key: str | None,
        headers: dict[str, str] | None = None,
        path: str | None = None,
    ) -> None:
        self._url = f"{gateway.url}{path or f'/mcp/{route}'}"
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
            **({"Authorization": f"Bearer {key}"} if key is not None else {}),
            **(headers or {}),
        }
        self._ids = iter(range(1, 1000))

    def _post(self, message: dict[str, Any]) -> httpx.Response:
        response = httpx.post(
            self._url, json={"jsonrpc": "2.0", **message}, headers=self._headers, timeout=30
        )
        if "mcp-session-id" in response.headers:
            self._headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        return response

    def initialize(self) -> int:
        params = {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }
        response = self._post({"id": next(self._ids), "method": "initialize", "params": params})
        if response.status_code == 200:
            self._post({"method": "notifications/initialized"})
        return response.status_code

    def request(self, method: str, **params: Any) -> dict[str, Any]:
        response = self._post({"id": next(self._ids), "method": method, "params": params})
        text = response.text
        # The answer comes as JSON or as one server-sent event.
        if "text/event-stream" in response.headers.get("content-type", ""):
            text = [line[5:] for line in text.splitlines() if line.startswith("data:")][-1]
        message: dict[str, Any] = json.loads(text)
        return message

    def tools(self) -> list[str]:
        return [tool["name"] for tool in self.request("tools/list")["result"]["tools"]]

    def call(self, tool: str) -> dict[str, Any]:
        return self.request("tools/call", name=tool, arguments={})

    def caller(self, tool: str) -> dict[str, str | None]:
        """What the bridge was told about the call: its key, the client and the person."""
        answer: dict[str, str | None] = json.loads(self.call(tool)["result"]["content"][0]["text"])
        return answer


def _initialize(gateway: Gateway, route: str, key: str | None) -> int:
    return _Session(gateway, route, key).initialize()


def _open(
    gateway: Gateway, route: str, key: str, headers: dict[str, str] | None = None
) -> _Session:
    session = _Session(gateway, route, key, headers)
    assert session.initialize() == 200
    return session


def _token(username: str, **claims: Any) -> str:
    """An access token of the connector's Authentik application for this username."""
    issuer = f"{AUTHENTIK_URL}/application/o/claude-ai/"
    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": "claude-ai",
        "sub": f"sub-{username}",
        "preferred_username": username,
        "iat": now,
        "exp": now + 600,
        **claims,
    }
    return jwt.encode(payload, SIGNING_KEY, algorithm="RS256", headers={"kid": "authentik"})


def _connector(gateway: Gateway, token: str | None) -> _Session:
    return _Session(gateway, "claude-ai", token, path="/mcp")


BRIDGE_KEY = f"Bearer {_value('lares-mcp-bridge')}"


def test_a_profiles_key_opens_the_routes_it_holds_and_no_other(gateway: Gateway) -> None:
    """One key per profile; which routes it opens is the route's list of profiles."""
    machines, kim, lou = _value("gateway-key"), _value("kim-gateway-key"), _value("lou-gateway-key")

    # The machines hold the event run's and the schedule's servers.
    assert _initialize(gateway, "lares", machines) == 200
    assert _initialize(gateway, "lares-memory", machines) == 200
    assert _initialize(gateway, "lares-control", machines) == 401
    assert _initialize(gateway, "github", machines) == 401
    # kim holds the chat's servers, lou the read server only.
    assert _initialize(gateway, "lares", kim) == 200
    assert _initialize(gateway, "lares-control", kim) == 200
    assert _initialize(gateway, "github", kim) == 200
    assert _initialize(gateway, "lares-memory", kim) == 401
    assert _initialize(gateway, "lares", lou) == 200
    assert _initialize(gateway, "lares-control", lou) == 401
    assert _initialize(gateway, "lares", None) == 401


def test_a_tool_outside_a_servers_tools_is_neither_listed_nor_callable(gateway: Gateway) -> None:
    key = _value("gateway-key")

    # The read server's tools: no verdict, no request through the gate, no memory.
    lares = _open(gateway, "lares", key)
    assert lares.tools() == ["list_episodes", "get_episode", "query_timeseries"]
    refused = lares.call("set_verdict")
    assert "result" not in refused
    assert "set_verdict" in refused["error"]["message"]
    memory = _open(gateway, "lares-memory", key)
    assert memory.tools() == ["get_memory"]


def test_each_route_names_its_client_and_the_keys_person_to_the_bridge(gateway: Gateway) -> None:
    """The caller's key never reaches the bridge; the gateway's key does, and who called."""
    calls = {
        ("lares", "gateway-key", "get_episode"): None,
        ("lares", "kim-gateway-key", "get_episode"): "kim",
        ("lares-control", "kim-gateway-key", "request_knx_write"): "kim",
        ("lares-memory", "gateway-key", "get_memory"): None,
    }
    for (route, key, tool), enduser in calls.items():
        caller = _open(gateway, route, _value(key)).caller(tool)
        assert caller == {"authorization": BRIDGE_KEY, "client": route, "enduser": enduser}, key


def test_a_caller_never_names_itself_to_the_bridge(gateway: Gateway) -> None:
    """The headers a caller sends are overwritten, or dropped where the key names nobody."""
    forged = {"X-Client-Id": "github", "X-Enduser-Id": "lou"}

    machines = _open(gateway, "lares", _value("gateway-key"), forged)
    kim = _open(gateway, "lares", _value("kim-gateway-key"), forged)

    assert machines.caller("get_episode") == {
        "authorization": BRIDGE_KEY,
        "client": "lares",
        "enduser": None,
    }
    assert kim.caller("get_episode")["enduser"] == "kim"


def test_a_route_attaches_its_servers_credential_from_the_mounted_file(gateway: Gateway) -> None:
    """GitHub's route carries the token laid into its Secret's directory, never the caller's key."""
    caller = _open(gateway, "github", _value("kim-gateway-key")).caller("get_episode")

    assert caller == {"authorization": f"Bearer {TOKEN}", "client": "github", "enduser": "kim"}


def test_a_renewed_credential_reaches_an_open_session_without_a_restart(
    gateway: Gateway, rendered: Rendered
) -> None:
    """kubelet swaps a credential's Secret in; the harness's open session carries it next.

    The bridge's key is renewed here, the way the GitHub token is every 30 minutes.
    """
    started = gateway.started_at()
    session = _open(gateway, "lares", _value("gateway-key"))
    assert session.caller("get_episode")["authorization"] == BRIDGE_KEY

    keys = rendered.credentials()["agentgateway-keys"]
    gateway.write("credentials/agentgateway-keys", {**keys, "lares-mcp-bridge": "new"})

    assert _seen(session, "Bearer new") == "Bearer new"
    assert gateway.started_at() == started
    # The other tests read the key the volume held.
    gateway.write("credentials/agentgateway-keys", keys)
    assert _seen(session, BRIDGE_KEY) == BRIDGE_KEY


def _seen(session: _Session, key: str) -> str | None:
    """The key the bridge is called with, once it is `key` or 30 seconds have passed."""
    deadline = time.monotonic() + 30
    seen = session.caller("get_episode")["authorization"]
    while time.monotonic() < deadline and seen != key:
        time.sleep(0.5)
        seen = session.caller("get_episode")["authorization"]
    return seen


def test_a_connector_without_a_token_points_the_client_at_its_sign_in(gateway: Gateway) -> None:
    """A 401 naming the metadata, which names the gateway as the authorization server."""
    response = httpx.post(f"{gateway.url}/mcp", json={}, timeout=30)

    assert response.status_code == 401
    metadata = f"{gateway.url}/.well-known/oauth-protected-resource/mcp"
    assert response.headers["www-authenticate"] == f'Bearer resource_metadata="{metadata}"'
    resource = httpx.get(metadata, timeout=30).json()
    assert resource["resource"] == f"{gateway.url}/mcp"
    assert resource["authorization_servers"] == [f"{gateway.url}/mcp"]
    assert "offline_access" in resource["scopes_supported"]


def test_a_connector_behind_tls_names_its_public_address(gateway: Gateway) -> None:
    """Behind the ingress, the scheme comes from `X-Forwarded-Proto` and the host from `Host`."""
    public = {"Host": "mcp.example.org", "X-Forwarded-Proto": "https"}

    response = httpx.post(f"{gateway.url}/mcp", json={}, headers=public, timeout=30)
    metadata = "https://mcp.example.org/.well-known/oauth-protected-resource/mcp"
    assert response.headers["www-authenticate"] == f'Bearer resource_metadata="{metadata}"'
    resource = httpx.get(
        f"{gateway.url}/.well-known/oauth-protected-resource/mcp", headers=public, timeout=30
    ).json()
    assert resource["resource"] == "https://mcp.example.org/mcp"
    assert resource["authorization_servers"] == ["https://mcp.example.org/mcp"]


def test_a_connector_hands_out_authentiks_endpoints_and_its_one_client(gateway: Gateway) -> None:
    """Authentik registers no clients: the gateway answers with the pre-registered one."""
    server = httpx.get(
        f"{gateway.url}/.well-known/oauth-authorization-server/mcp", timeout=30
    ).json()
    assert server["authorization_endpoint"] == (
        f"{AUTHENTIK_URL}/application/o/claude-ai/authorize/"
    )
    assert server["registration_endpoint"].endswith("/mcp/client-registration")

    registered = httpx.post(
        f"{gateway.url}/.well-known/oauth-authorization-server/mcp/client-registration",
        json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
        timeout=30,
    )
    assert registered.json()["client_id"] == "claude-ai"


def test_a_connector_serves_a_declared_person_every_tool(gateway: Gateway) -> None:
    session = _connector(gateway, _token("kim"))

    assert session.initialize() == 200
    assert len(session.tools()) == 6
    assert session.caller("set_verdict") == {
        "authorization": BRIDGE_KEY,
        "client": "claude-ai",
        "enduser": "kim",
    }


def test_a_connector_refuses_anyone_else(gateway: Gateway) -> None:
    """Authentik may sign in someone the declaration does not name: the handshake fails at the
    gateway, before the call to the bridge."""
    session = _connector(gateway, _token("stranger"))

    assert session.initialize() == 500
    refusal = session.request("initialize", protocolVersion="2025-11-25", capabilities={})
    assert refusal["error"]["message"].endswith("authorization failed")


def test_a_connector_caller_never_names_itself_to_the_bridge(gateway: Gateway) -> None:
    """The person comes from the token, whatever headers the client sends."""
    forged = {"X-Client-Id": "lares", "X-Enduser-Id": "lou"}
    session = _Session(gateway, "claude-ai", _token("kim"), forged, path="/mcp")

    assert session.initialize() == 200
    assert session.caller("get_episode") == {
        "authorization": BRIDGE_KEY,
        "client": "claude-ai",
        "enduser": "kim",
    }


@pytest.mark.parametrize(
    "claims",
    [{"aud": "another-application"}, {"iss": "http://authentik:9000/application/o/other/"}],
)
def test_a_connector_refuses_a_token_of_another_application(
    gateway: Gateway, claims: dict[str, str]
) -> None:
    assert _connector(gateway, _token("kim", **claims)).initialize() == 401
