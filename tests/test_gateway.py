"""The rendered routes run as the gateway runs them: agentgateway, GitHub's server and a bridge.

The routes file is filled the way Sealed Secrets fills it, every sealed key a
value of its own, and laid into a volume the way kubelet lays a Secret into
a pod: a timestamped directory behind `..data`, swapped whole on a change.
Each upstream answers under the name the declaration gives it, on a network
of their own. GitHub's server is the real one, started as lares starts it;
it refuses a call without a token, so a route that lists its tools attached
one. The bridge is a stand-in that lists a tool beyond every allowlist and
answers with the key it was called with.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

from lares_agent_trigger.generate import render
from lares_agent_trigger.persons import load_persons_file
from lares_agent_trigger.use_cases import HttpServer, load_use_case_file

from .test_generate import PERSONS, SETTINGS, USE_CASES

# What lares deploys; Renovate bumps them.
GATEWAY_IMAGE = "cr.agentgateway.dev/agentgateway:v1.6.0"
GITHUB_SERVER_IMAGE = "ghcr.io/github/github-mcp-server:v2.0.3"
# The stand-in bridge's interpreter.
BRIDGE_IMAGE = "python:3.15-slim"

FAKE_BRIDGE = Path(__file__).parent / "fake_bridge.py"
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


def _routes_file(template: str, renewed: dict[str, str] | None = None) -> str:
    """The template filled as the controller fills it, with a renewed value where given."""
    renewed = renewed or {}
    return re.sub(
        r'{{ index \. "([^"]+)" \| toJson }}',
        lambda match: json.dumps(renewed.get(match[1], _value(match[1]))),
        template,
    )


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
    # The routes file as a template of the sealed keys, and the gateway's chart values.
    template: str
    values: dict[str, Any]
    github: HttpServer


@pytest.fixture(scope="module")
def rendered() -> Rendered:
    declared = load_use_case_file(USE_CASES)
    rendering = render(
        declared, load_persons_file(PERSONS, declared), SETTINGS.read_text(encoding="utf-8")
    )
    patch = yaml.safe_load(rendering.routes_template)
    github = declared.tool_servers["github"]
    assert isinstance(github, HttpServer)
    return Rendered(
        template=patch["spec"]["template"]["data"]["config.yaml"],
        values=yaml.safe_load(rendering.routes_values),
        github=github,
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
        gateway.write("routes", {"config.yaml": _routes_file(rendered.template)})
        credential = github.credential
        gateway.write(f"credentials/{credential.secret}", {credential.key: TOKEN})
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
                GITHUB_SERVER_IMAGE,
                "http",
                "--read-only",
                "--toolsets=repos,issues,pull_requests",
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
            if _initialize(gateway, "github", _value("kim-github-gateway-key")) == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise AssertionError(_docker("logs", gateway.container))


class _Session:
    """One client on one route, as the harness holds it: one handshake, then its requests.

    It keeps the session id the gateway hands out, if it hands one out.
    """

    def __init__(self, gateway: Gateway, route: str, key: str | None) -> None:
        self._url = f"{gateway.url}/mcp/{route}"
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
            **({"Authorization": f"Bearer {key}"} if key is not None else {}),
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


def _initialize(gateway: Gateway, route: str, key: str | None) -> int:
    return _Session(gateway, route, key).initialize()


def _open(gateway: Gateway, route: str, key: str) -> _Session:
    session = _Session(gateway, route, key)
    assert session.initialize() == 200
    return session


def test_a_key_is_refused_on_every_route_but_its_own(gateway: Gateway) -> None:
    """The machines' key for lares opens lares only; kim's opens kim's own route only."""
    machines, kim = _value("lares-gateway-key"), _value("kim-lares-gateway-key")

    assert _initialize(gateway, "lares", machines) == 200
    assert _initialize(gateway, "lares-kim", kim) == 200
    assert _initialize(gateway, "lares", kim) == 401
    assert _initialize(gateway, "lares-kim", machines) == 401
    assert _initialize(gateway, "lares-memory", machines) == 401
    assert _initialize(gateway, "github", machines) == 401
    assert _initialize(gateway, "lares", None) == 401


def test_a_tool_outside_a_servers_tools_is_neither_listed_nor_callable(gateway: Gateway) -> None:
    key = _value("lares-gateway-key")

    # The read server's allowlist: no verdict, no request through the gate, no memory.
    lares = _open(gateway, "lares", key)
    assert lares.tools() == ["list_episodes", "get_episode", "query_timeseries"]
    refused = lares.call("set_verdict")
    assert "result" not in refused
    assert "set_verdict" in refused["error"]["message"]
    memory = _open(gateway, "lares-memory", _value("lares-memory-gateway-key"))
    assert memory.tools() == ["get_memory"]


def test_each_bridge_route_signs_in_as_its_own_client(gateway: Gateway) -> None:
    """The caller's key never reaches the bridge; the route's client key does."""
    calls = {
        "lares": ("lares-gateway-key", "lares-agent"),
        "lares-kim": ("kim-lares-gateway-key", "kim"),
        "lares-control": ("kim-lares-control-gateway-key", "lares-control"),
    }
    for route, (key, client) in calls.items():
        tool = "request_knx_write" if route == "lares-control" else "get_episode"
        answer = _open(gateway, route, _value(key)).call(tool)
        assert answer["result"]["content"][0]["text"] == f"Bearer {_value(client)}", route


def test_github_answers_through_its_route_with_the_attached_token(
    gateway: Gateway, rendered: Rendered
) -> None:
    """Without a token the server answers nothing, so listed tools mean the gateway attached one."""
    tools = _open(gateway, "github", _value("kim-github-gateway-key")).tools()

    assert {"get_file_contents", "list_issues", "issue_read", "pull_request_read"} <= set(tools)
    assert all(any(fnmatchcase(name, p) for p in rendered.github.tools) for name in tools)
    assert not any(name.startswith(("create_", "update_", "merge_")) for name in tools)


def test_a_resealed_key_reaches_the_bridge_without_a_restart(
    gateway: Gateway, rendered: Rendered
) -> None:
    """kubelet swaps the routes file in; the harness's open session carries the new key next.

    The same holds for the token the github route reads from its file.
    """
    started = gateway.started_at()
    session = _open(gateway, "lares", _value("lares-gateway-key"))
    assert session.call("get_episode")["result"]["content"][0]["text"] == (
        f"Bearer {_value('lares-agent')}"
    )

    gateway.write(
        "routes", {"config.yaml": _routes_file(rendered.template, {"lares-agent": "new"})}
    )

    deadline = time.monotonic() + 30
    seen = ""
    while time.monotonic() < deadline and seen != "Bearer new":
        time.sleep(0.5)
        seen = session.call("get_episode")["result"]["content"][0]["text"]
    assert seen == "Bearer new"
    assert gateway.started_at() == started
