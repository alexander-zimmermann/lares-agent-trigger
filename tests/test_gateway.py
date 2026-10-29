"""The rendered routes run as the gateway runs them: agentgateway, GitHub's server, a bridge,
Authentik and OpenAI.

The gateway starts as its chart starts it with the rendered values: every
variable they name set from its sealed key, every sealed key a value of its
own, and the chart's config file and every credential laid into a volume the
way kubelet lays a ConfigMap or Secret into a pod: a timestamped directory
behind `..data`, swapped whole on a change. The cluster's Authentik comes in as the overlay sets it.
Each upstream answers under the name the declaration gives it, on a network
of their own. The bridge is a stand-in, under the bridge's name and under
GitHub's, that lists a tool no route serves and answers with the key it was
called with and the caller the gateway named: what a route attached shows
there. Authentik is a stand-in serving one application's discovery and
signing keys; the test signs the tokens. OpenAI is a stand-in over plain
HTTP, where the declaration names OpenAI's own address, that echoes the key
and the upload it got.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
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
GATEWAY_IMAGE = "cr.agentgateway.dev/agentgateway:v1.7.0"
# The stand-in bridge's interpreter.
BRIDGE_IMAGE = "python:3.15-slim"

FAKE_BRIDGE = Path(__file__).parent / "fake_bridge.py"
FAKE_AUTHENTIK = Path(__file__).parent / "fake_authentik.py"
FAKE_OPENAI = Path(__file__).parent / "fake_openai.py"
FAKE_COLLECTOR = Path(__file__).parent / "fake_collector.py"
# Where the stand-in OpenAI answers, in place of the declared https://api.openai.com.
OPENAI_URL = "http://openai:8090"
# The cluster's Authentik, as the gateway's overlay sets it.
AUTHENTIK_URL = "http://authentik:9000"
# The share of traces the gateway keeps, which lares sets beside the rendered values: every
# one here, so a span the test looks for is never sampled out.
SAMPLING_RATIO = "1.0"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
# Where the test lays the chart's config file, and the rendered chart values mount the
# credentials.
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
    # Where the test reads what the stand-in OTLP receiver got.
    collector: str

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
    # The gateway's chart values.
    values: dict[str, Any]
    github: ToolServer

    def config(self) -> dict[str, Any]:
        """The chart's config file: the routes, and of the gateway's own settings the admin
        port off."""
        return {**self.values["config"], "config": {"adminAddr": "off"}}

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
def rendered(tmp_path_factory: pytest.TempPathFactory) -> Rendered:
    """The declaration rendered with speech going to the stand-in OpenAI."""
    use_cases = tmp_path_factory.mktemp("rendering") / "use-cases.yaml"
    text = USE_CASES.read_text(encoding="utf-8")
    use_cases.write_text(text.replace("https://api.openai.com", OPENAI_URL), encoding="utf-8")
    declared = load_use_case_file(use_cases)
    rendering = render(
        declared, load_persons_file(PERSONS, declared), SETTINGS.read_text(encoding="utf-8")
    )
    github = declared.tool_servers["github"]
    return Rendered(values=yaml.safe_load(rendering.routes_values), github=github)


@pytest.fixture(scope="module")
def gateway(rendered: Rendered) -> Iterator[Gateway]:
    """The gateway as the chart starts it: one config file, the routes in it."""
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
        gateway = Gateway(url="", container="", volume=volume, collector="")
        gateway.write("config", {"config.yaml": yaml.safe_dump(rendered.config())})
        for secret, files in rendered.credentials().items():
            gateway.write(f"credentials/{secret}", files)
        issuer = declared.connectors["claude-ai"].issuer.replace("${AUTHENTIK_URL}", AUTHENTIK_URL)
        jwk = {**jwt.algorithms.RSAAlgorithm.to_jwk(SIGNING_KEY.public_key(), as_dict=True)}
        jwks = json.dumps({"keys": [{**jwk, "kid": "authentik", "alg": "RS256", "use": "sig"}]})
        containers.append(_stand_in(network, "authentik", FAKE_AUTHENTIK, ISSUER=issuer, JWKS=jwks))
        containers.append(_stand_in(network, bridge_host, FAKE_BRIDGE))
        containers.append(
            _stand_in(network, github_host, FAKE_BRIDGE, PORT=str(urlsplit(github.url).port))
        )
        openai = urlsplit(OPENAI_URL)
        containers.append(
            _stand_in(network, str(openai.hostname), FAKE_OPENAI, PORT=str(openai.port))
        )
        otlp = urlsplit(declared.gateway.otlp_endpoint)
        collector = _stand_in(
            network, str(otlp.hostname), FAKE_COLLECTOR, publish=otlp.port, PORT=str(otlp.port)
        )
        containers.append(collector)
        gateway.collector = f"http://127.0.0.1:{_published(collector, otlp.port)}"
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
            f"{MOUNT}/config/config.yaml",
        )
        containers.append(container)
        gateway.url = f"http://127.0.0.1:{_published(container, port)}"
        gateway.container = container
        _wait_until_serving(gateway)
        yield gateway
    finally:
        for container in containers:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


def _stand_in(
    network: str, alias: str, script: Path, publish: int | None = None, **env: str
) -> str:
    """A stand-in on the gateway's network under the name it answers to: one script of the
    standard library, with its settings as variables; `publish` opens its port to the test."""
    return _docker(
        "run",
        "-d",
        "--network",
        network,
        "--network-alias",
        alias,
        *(["-p", f"127.0.0.1::{publish}"] if publish is not None else []),
        *(option for name, value in env.items() for option in ("-e", f"{name}={value}")),
        "-v",
        f"{script}:/{script.name}:ro",
        BRIDGE_IMAGE,
        "python",
        "-I",
        f"/{script.name}",
    )


def _published(container: str, port: int | None) -> str:
    """The host port Docker opened for the container's port."""
    return _docker("port", container, str(port)).splitlines()[0].rsplit(":", 1)[1]


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

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        return self.request("tools/call", name=tool, arguments=arguments)

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
    # The stand-in bridge answers every call, so no result is the gateway's refusal.
    assert "result" not in lares.call("set_verdict")
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


def test_a_call_longer_than_the_gateways_own_ten_seconds_returns(gateway: Gateway) -> None:
    """The route waits its server's `timeout_seconds` for a reply that comes only once done."""
    session = _open(gateway, "lares", _value("gateway-key"))

    answer = session.call("get_episode", seconds=12)

    assert json.loads(answer["result"]["content"][0]["text"])["client"] == "lares"


def test_a_changed_route_reaches_the_gateway_without_a_restart(
    gateway: Gateway, rendered: Rendered
) -> None:
    """kubelet swaps the chart's ConfigMap in; a route taken out is gone, and back once it
    returns, without a restart."""
    started = gateway.started_at()
    config = rendered.config()
    bind = config["binds"][0]
    listener = bind["listeners"][0]
    routes = [route for route in listener["routes"] if route["name"] != "github"]
    without = {**config, "binds": [{**bind, "listeners": [{**listener, "routes": routes}]}]}
    kim = _value("kim-gateway-key")

    gateway.write("config", {"config.yaml": yaml.safe_dump(without)})
    assert _answered(lambda: _initialize(gateway, "github", kim), 404) == 404
    assert gateway.started_at() == started
    # The other tests call GitHub's route.
    gateway.write("config", {"config.yaml": yaml.safe_dump(config)})
    assert _answered(lambda: _initialize(gateway, "github", kim), 200) == 200


def _answered(ask: Callable[[], int], status: int) -> int:
    """The status `ask` answers with, once it is `status` or 30 seconds have passed."""
    deadline = time.monotonic() + 30
    answered = ask()
    while time.monotonic() < deadline and answered != status:
        time.sleep(0.5)
        answered = ask()
    return answered


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
    gateway, before the call to the bridge, which would answer a handshake."""
    assert _connector(gateway, _token("stranger")).initialize() == 500


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


# The first bytes of an Ogg file, as a voice message from Discord or Open WebUI starts.
AUDIO = b"OggS\x00\x02" + bytes(range(256)) * 8


def _transcribe(gateway: Gateway, key: str | None) -> tuple[bytes, httpx.Response]:
    """A transcription as Hermes and Open WebUI send it, a form with the model and the file:
    the body sent, and the answer."""
    request = httpx.Request(
        "POST",
        f"{gateway.url}/v1/audio/transcriptions",
        data={"model": "whisper-1", "language": "de"},
        files={"file": ("voice.ogg", AUDIO, "audio/ogg")},
        headers={"Authorization": f"Bearer {key}"} if key is not None else {},
    )
    with httpx.Client(timeout=30) as client:
        return request.read(), client.send(request)


def test_speech_passes_the_upload_on_under_openais_key(gateway: Gateway) -> None:
    """The form reaches OpenAI byte for byte, with the mounted key in place of the caller's."""
    sent, response = _transcribe(gateway, _value("kim-gateway-key"))

    assert response.status_code == 200
    assert response.json() == {
        "text": "Hallo aus der Küche",
        "path": "/v1/audio/transcriptions",
        "authorization": f"Bearer {_value('openai-api-key')}",
        "content_type": response.request.headers["content-type"],
        "body_sha256": hashlib.sha256(sent).hexdigest(),
    }


def test_speech_takes_every_profiles_key_and_each_clients_and_no_other(gateway: Gateway) -> None:
    for key in ("gateway-key", "kim-gateway-key", "lou-gateway-key", "open-webui-gateway-key"):
        assert _transcribe(gateway, _value(key))[1].status_code == 200, key

    assert _transcribe(gateway, None)[1].status_code == 401
    assert _transcribe(gateway, _value("openai-api-key"))[1].status_code == 401


def _enduser(value: str) -> bytes:
    """The span attribute `enduser.id` holding this value, as OTLP's protobuf encodes it."""
    key, text = b"enduser.id", value.encode()
    any_value = b"\x0a" + bytes([len(text)]) + text
    return b"\x0a" + bytes([len(key)]) + key + b"\x12" + bytes([len(any_value)]) + any_value


def _exported(gateway: Gateway, attribute: bytes) -> bool:
    """True once an export carrying the attribute reached the receiver, within 30 seconds."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        exports = httpx.get(f"{gateway.collector}/exports", timeout=30).json()
        if any(attribute in bytes.fromhex(export) for export in exports):
            return True
        time.sleep(0.5)
    return False


def test_every_span_names_whoever_called(gateway: Gateway) -> None:
    """The person or client behind a key, else the person the connector signed in.

    The stranger is refused only after the sign-in, so their span names them.
    """
    _transcribe(gateway, _value("lou-gateway-key"))
    _transcribe(gateway, _value("open-webui-gateway-key"))
    _connector(gateway, _token("stranger")).initialize()

    assert _exported(gateway, _enduser("lou"))
    assert _exported(gateway, _enduser("open-webui"))
    assert _exported(gateway, _enduser("stranger"))
