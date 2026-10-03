"""The rendered GitHub entry run as the harness runs it: the real server, the App's key in place.

The harness starts a stdio server from `command`, `args` and `env` alone, with
its own environment cut down to PATH, HOME and the like. Here the same entry
starts in a container standing in for the harness's: the server binary copied
out of its image, as the pod's init container does, and a key file where the
secret is mounted. The server mints its token on the first call, not at
start, so it answers MCP over stdio with the network off.
"""

from __future__ import annotations

import json
import select
import subprocess
import time
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest
import yaml

from lares_agent_trigger.generate import render
from lares_agent_trigger.use_cases import load_use_case_file

from .test_generate import SETTINGS, USE_CASES

# The server lares deploys; Renovate bumps it.
GITHUB_SERVER_IMAGE = "ghcr.io/github/github-mcp-server:v1.14.0"

# Paths of the rendering's declaration, inside the stand-in container.
SERVER_DIR = "/opt/github-mcp"
KEY_DIR = "/etc/github-reader"


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=300
    ).stdout.strip()


@pytest.fixture(scope="module")
def server_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The server binary, copied out of its image the way the pod's init container copies it."""
    target = tmp_path_factory.mktemp("github-mcp")
    container = _docker("create", GITHUB_SERVER_IMAGE)
    try:
        _docker("cp", f"{container}:/server/github-mcp-server", str(target))
    finally:
        _docker("rm", container)
    yield target


@pytest.fixture
def entry() -> dict[str, Any]:
    rendered = render(load_use_case_file(USE_CASES), SETTINGS.read_text(encoding="utf-8"))
    config: dict[str, Any] = yaml.safe_load(rendered.hermes_config)
    github: dict[str, Any] = config["mcp_servers"]["github"]
    return github


def _start(entry: dict[str, Any], server_dir: Path, key_dir: Path) -> subprocess.Popen[bytes]:
    environment = [
        option for name, value in entry["env"].items() for option in ("-e", f"{name}={value}")
    ]
    return subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "-v",
            f"{server_dir}:{SERVER_DIR}:ro",
            "-v",
            f"{key_dir}:{KEY_DIR}:ro",
            *environment,
            "--entrypoint",
            entry["command"],
            GITHUB_SERVER_IMAGE,
            *entry["args"],
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _send(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
    process.stdin.flush()


def _answer(process: subprocess.Popen[bytes], request_id: int) -> dict[str, Any]:
    """The response to one request; notifications and other responses are skipped."""
    assert process.stdout is not None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], deadline - time.monotonic())
        if not ready:
            break
        line = process.stdout.readline()
        if not line:
            break
        message: dict[str, Any] = json.loads(line)
        if message.get("id") == request_id:
            return message
    raise AssertionError(f"no answer to request {request_id}")


def _tools(process: subprocess.Popen[bytes]) -> list[dict[str, Any]]:
    _send(
        process,
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    _answer(process, 1)
    _send(process, {"method": "notifications/initialized"})
    _send(process, {"id": 2, "method": "tools/list"})
    tools: list[dict[str, Any]] = _answer(process, 2)["result"]["tools"]
    return tools


def _stop(process: subprocess.Popen[bytes]) -> str:
    assert process.stdin is not None
    process.stdin.close()
    _, stderr = process.communicate(timeout=30)
    return stderr.decode()


def test_the_server_starts_as_the_app_and_offers_reads_only(
    entry: dict[str, Any], server_dir: Path, tmp_path: Path
) -> None:
    key_dir = tmp_path / "key"
    key_dir.mkdir()
    # A key GitHub never sees: the server only parses it before the first call.
    subprocess.run(
        ["openssl", "genrsa", "-out", str(key_dir / "private-key"), "2048"],
        check=True,
        capture_output=True,
    )

    process = _start(entry, server_dir, key_dir)
    try:
        tools = _tools(process)
    finally:
        stderr = _stop(process)

    # Signed in as the App, it never falls back to the interactive login nobody would answer.
    assert "OAuth" not in stderr
    assert tools
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools), [
        tool["name"] for tool in tools if not tool["annotations"].get("readOnlyHint")
    ]
    # What the chat sees: the server's tools through the harness's include list.
    include = entry["tools"]["include"]
    seen = {tool["name"] for tool in tools if any(fnmatchcase(tool["name"], p) for p in include)}
    assert {"list_issues", "search_issues", "issue_read", "get_file_contents"} <= seen


def test_without_its_key_the_server_never_starts(
    entry: dict[str, Any], server_dir: Path, tmp_path: Path
) -> None:
    key_dir = tmp_path / "key"
    key_dir.mkdir()

    process = _start(entry, server_dir, key_dir)
    stderr = _stop(process)

    assert process.returncode != 0
    assert "private key" in stderr
    assert "starting server" not in stderr
