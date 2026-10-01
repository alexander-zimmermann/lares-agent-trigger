"""Generator tests: the use-case file and the settings in, the three rendered files out.

`golden/` holds one declaration that touches every surface and the files it
renders to. A deliberate change to the rendering is reviewed as a diff of
those files: `UPDATE_GOLDEN=1 pytest tests/test_generate.py` rewrites them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from lares_agent_trigger.generate import GENERATED_LINE, Rendering, main, render
from lares_agent_trigger.use_cases import load_use_case_file

GOLDEN = Path(__file__).parent / "golden"
USE_CASES = GOLDEN / "use-cases.yaml"
SETTINGS = GOLDEN / "settings.yaml"
EXPECTED = GOLDEN / "expected"


def _render(tmp_path: Path, use_cases: str | None = None, settings: str | None = None) -> Rendering:
    path = USE_CASES
    if use_cases is not None:
        path = tmp_path / "use-cases.yaml"
        path.write_text(use_cases, encoding="utf-8")
    text = settings if settings is not None else SETTINGS.read_text(encoding="utf-8")
    return render(load_use_case_file(path), text)


def _config(tmp_path: Path, use_cases: str | None = None) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_render(tmp_path, use_cases).hermes_config)
    return loaded


def _jobs(tmp_path: Path, use_cases: str | None = None) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = yaml.safe_load(_render(tmp_path, use_cases).cron_jobs)["jobs"]
    return loaded


def _allowlists(tmp_path: Path, use_cases: str | None = None) -> dict[str, list[str]]:
    line = _render(tmp_path, use_cases).client_tools.splitlines()[-1]
    name, _, value = line.partition("=")
    assert name == "MCP_AUTH_CLIENT_TOOLS"
    loaded: dict[str, list[str]] = json.loads(value)
    return loaded


def test_the_rendering_matches_the_golden_files(tmp_path: Path) -> None:
    rendered = _render(tmp_path)
    files = {
        "config.yaml": rendered.hermes_config,
        "cron-jobs.yaml": rendered.cron_jobs,
        "client-tools.env": rendered.client_tools,
    }
    if os.environ.get("UPDATE_GOLDEN"):
        for name, text in files.items():
            (EXPECTED / name).write_text(text, encoding="utf-8")

    for name, text in files.items():
        assert text == (EXPECTED / name).read_text(encoding="utf-8"), name


def test_each_surface_sees_only_the_servers_its_use_cases_may_hold(tmp_path: Path) -> None:
    toolsets = _config(tmp_path)["platform_toolsets"]

    # The chat keeps the harness's own Discord tools and gains the read and request servers.
    assert toolsets["discord"] == ["hermes-discord", "lares", "lares-control"]
    # An event run reads, nothing else.
    assert toolsets["api_server"] == ["lares"]
    # A cron job without a list of its own falls back to this: every read server in use.
    assert toolsets["cron"] == ["hermes-cron", "lares"]


def test_a_writing_server_lands_only_on_the_cron_job_that_names_it(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert all("lares-memory" not in names for names in config["platform_toolsets"].values())
    assert [job["enabled_toolsets"] for job in _jobs(tmp_path)] == [["lares", "lares-memory"]]
    # The harness must still know the server for the job to reach it.
    assert "lares-memory" in config["mcp_servers"]


def test_every_tool_server_entry_carries_its_key_and_include_list(tmp_path: Path) -> None:
    lares = _config(tmp_path)["mcp_servers"]["lares"]

    assert lares == {
        "url": "http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp",
        "headers": {"Authorization": "Bearer ${env:LARES_MCP_KEY}"},
        "timeout": 60,
        "tools": {
            "include": [
                "list_*",
                "get_current_knx",
                "get_episode",
                "query_*",
                "correlate_events",
                "search_wiki",
            ]
        },
    }


def test_the_hook_reports_every_finished_turn_to_the_trigger(tmp_path: Path) -> None:
    assert _config(tmp_path)["hooks"] == {
        "outbound": [
            {
                "name": "lares-agent-trigger",
                "url": "http://lares-agent-trigger.agents.svc.cluster.local:8080/hooks/hermes",
                "events": ["post_api_request", "on_session_end"],
                "secret_env": "LARES_AGENT_TRIGGER_HOOK_SECRET",
                "timeout": 10,
            }
        ]
    }


def test_a_schedule_use_case_becomes_one_managed_cron_job(tmp_path: Path) -> None:
    assert _jobs(tmp_path) == [
        {
            "name": "lares:propose-faults",
            "schedule": "0 3 * * 0",
            "skills": ["lares-propose"],
            "prompt": "Answer in English. Stay within 80 tool calls and 20 minutes.",
            # The trigger delivers; the harness keeps the output to itself.
            "deliver": "local",
            "enabled_toolsets": ["lares", "lares-memory"],
            "model": "gpt-6-sol",
        }
    ]


def test_each_client_of_a_server_in_use_gets_its_allowlist(tmp_path: Path) -> None:
    assert _allowlists(tmp_path) == {
        "lares-agent": [
            "list_*",
            "get_current_knx",
            "get_episode",
            "query_*",
            "correlate_events",
            "search_wiki",
        ],
        "lares-control": ["request_knx_write", "get_write_request"],
        "lares-memory": ["append_memory"],
    }


def test_a_dormant_use_case_renders_nothing(tmp_path: Path) -> None:
    """The dormant entry is the only one naming lares-wiki: no entry, no job, no allowlist."""
    text = USE_CASES.read_text(encoding="utf-8")
    without = text[: text.index("  - name: basalte-prose")]

    assert _render(tmp_path, without) == _render(tmp_path)
    assert "lares-wiki" not in _config(tmp_path)["mcp_servers"]
    assert "lares-wiki" not in _allowlists(tmp_path)


def test_a_surface_without_a_use_case_sees_no_tool_server(tmp_path: Path) -> None:
    """An explicit list without a server name would hand that surface every server."""
    text = USE_CASES.read_text(encoding="utf-8")
    only_chat = text[: text.index("  - name: explain-episode")]

    toolsets = _config(tmp_path, only_chat)["platform_toolsets"]
    assert toolsets["api_server"] == ["no_mcp"]
    # The chat's read server stays readable for a job made by hand; its request server does not.
    assert toolsets["cron"] == ["hermes-cron", "lares"]
    assert _jobs(tmp_path, only_chat) == []

    only_control = only_chat.replace(
        "    tools: [lares, lares-control]\n", "    tools: [lares-control]\n"
    )
    assert _config(tmp_path, only_control)["platform_toolsets"]["cron"] == ["hermes-cron", "no_mcp"]


def test_every_rendered_file_opens_with_the_generated_line(tmp_path: Path) -> None:
    rendered = _render(tmp_path)

    for text in (rendered.hermes_config, rendered.cron_jobs, rendered.client_tools):
        assert text.splitlines()[0] == GENERATED_LINE


def test_the_settings_are_kept_whole_beside_the_rendered_part(tmp_path: Path) -> None:
    settings = SETTINGS.read_text(encoding="utf-8")
    rendered = _render(tmp_path).hermes_config
    config = yaml.safe_load(rendered)

    # Comments and all: the settings are copied, never re-serialised.
    assert settings in rendered
    assert {key: config[key] for key in ("model", "agent")} == yaml.safe_load(settings)


@pytest.mark.parametrize("key", ["platform_toolsets", "hooks", "mcp_servers"])
def test_settings_carrying_a_rendered_key_are_refused(tmp_path: Path, key: str) -> None:
    settings = SETTINGS.read_text(encoding="utf-8") + f"\n{key}: {{}}\n"

    with pytest.raises(ValueError, match=f"`{key}` is rendered from the use-case file"):
        _render(tmp_path, settings=settings)


def test_settings_that_are_not_a_mapping_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="a mapping"):
        _render(tmp_path, settings="- model\n")


def _cli(tmp_path: Path, use_cases: Path = USE_CASES) -> int:
    return main(
        [
            "--use-cases",
            str(use_cases),
            "--settings",
            str(SETTINGS),
            "--hermes-config",
            str(tmp_path / "out" / "config.yaml"),
            "--cron-jobs",
            str(tmp_path / "out" / "cron-jobs.yaml"),
            "--client-tools",
            str(tmp_path / "out" / "client-tools.env"),
        ]
    )


def test_the_command_writes_the_three_files_whole(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "config.yaml").write_text("stale\n", encoding="utf-8")

    assert _cli(tmp_path) == 0

    for name in ("config.yaml", "cron-jobs.yaml", "client-tools.env"):
        written = (tmp_path / "out" / name).read_text(encoding="utf-8")
        assert written == (EXPECTED / name).read_text(encoding="utf-8"), name


def test_an_output_that_cannot_be_written_fails_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(tmp_path) == 1

    assert "cannot be written" in capsys.readouterr().err


def test_an_invalid_file_fails_with_the_field_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "use-cases.yaml"
    text = USE_CASES.read_text(encoding="utf-8")
    broken.write_text(text.replace("    language: en\n", "    languge: en\n", 1), encoding="utf-8")
    (tmp_path / "out").mkdir()

    assert _cli(tmp_path, broken) == 1

    error = capsys.readouterr().err
    assert "use-cases.yaml" in error
    assert "languge" in error
    assert not any((tmp_path / "out").iterdir())
